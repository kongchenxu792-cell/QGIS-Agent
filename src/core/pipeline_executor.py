
"""PipelineExecutor — 声明式模板引擎执行器 v3。

将 coverage_analysis.json 模板定义的 step chain 按序执行，
支持 8 种引擎 + fallback 链 + $ref 跨步数据传递。
"""

from __future__ import annotations

import json
import logging
import traceback
import os
import re
import time
import traceback

_log = logging.getLogger("PipelineExecutor")
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Union

from .guards import GuardChecker, GuardCheck, GuardDef

# ─── dataclasses (与 coverage_analysis.json 严格对齐) ──────────


@dataclass
class EngineSpec:
    """单条引擎声明。"""
    name: str                 # qgis_processing | qgis_spatial_index | shapely | shapely_wkt | geojson_file | qgis_memory_layer
    algorithm: Optional[str] = None
    method: Optional[str] = None
    validate: Optional[str] = None
    params: Dict[str, Any] = field(default_factory=dict)
    path_template: Optional[str] = None  # geojson_file 路径模板


@dataclass
class StepDef:
    """单个步骤声明。"""
    id: str
    input_source: Optional[str] = None        # $boundary_layer / $buffer.output ...
    input_boundary: Optional[str] = None      # $boundary_buffer.output ...
    params: Dict[str, Any] = field(default_factory=dict)
    engine_chain: List[EngineSpec] = field(default_factory=list)  # primary + fallback 扁平化
    output_keys: Optional[List[str]] = None
    output_layer: Optional[Dict[str, Any]] = None
    log: str = ""
    comment: str = ""

    # 以下由模板扩展字段赋值（不在 JSON 标准字段中，通过 params 或计算生成）
    propagate_boundary: bool = False   # 是否将本步输出作为后续 boundary


@dataclass
class ParamDef:
    """模板入参定义。"""
    name: str
    type: str = "string"
    role: str = ""
    default: Any = None
    required: bool = False
    comment: str = ""


@dataclass
class TemplateDef:
    """模板完整定义。"""
    template_id: str
    zh_triggers: List[str] = field(default_factory=list)
    ja_triggers: List[str] = field(default_factory=list)
    en_triggers: List[str] = field(default_factory=list)
    params: List[ParamDef] = field(default_factory=list)
    guards: Optional[GuardCheck] = None
    steps: List[StepDef] = field(default_factory=list)
    output_base: str = "user_data/exports/shapefiles/"
    # P2-2 输出步结构期望声明：{"empty": "ok"|"degraded"} —— 空语义逐模板声明，不一刀切
    output_check: Dict[str, Any] = field(default_factory=dict)


@dataclass
class StepResult:
    """单步执行结果。"""
    step_id: str
    output_type: str = ""
    feature_count: int = 0
    elapsed: float = 0.0
    qgis_layer: Any = None
    shapely_geom: Any = None
    file_path: str = ""
    stats: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    engine_used: str = ""
    status: str = "ok"  # P2-1 显式状态机: ok|degraded|failed（degraded 留给 P2-2 出口体检）


# ─── 变量映射表 ─────────────────────────────────────────────────

_VALIDATE_VAR_MAP: Dict[str, Callable[[StepResult], Any]] = {
    "fc":            lambda r: r.feature_count,
    "t":             lambda r: r.elapsed,
    "area":          lambda r: r.shapely_geom.area if r.shapely_geom else 0,
    "is_empty":      lambda r: r.shapely_geom.is_empty if r.shapely_geom else True,
    "covered_area":  lambda r: (r.stats or {}).get("covered_area", 0),
    "total_area":    lambda r: (r.stats or {}).get("total_area", 0),
    "coverage_rate": lambda r: (r.stats or {}).get("coverage_rate", 0),
}


# ─── 正则表达式解析器 ──────────────────────────────────────────

_VALIDATE_PATTERN = re.compile(
    r"^(not\s+)?(\w+)\s*(>|<|>=|<=|==|!=)\s*([\d.]+)(s)?$"
)


def _parse_and_eval(expr: str, result: StepResult) -> bool:
    """解析校验表达式并求值。支持 && 复合表达式。"""
    if not expr or not expr.strip():
        return True
    expr = expr.strip()

    if "&&" in expr:
        return all(_parse_and_eval(sub.strip(), result) for sub in expr.split("&&"))

    # "not is_empty"
    not_match = re.match(r"^not\s+(\w+)$", expr)
    if not_match:
        var = not_match.group(1)
        val = _VALIDATE_VAR_MAP.get(var, lambda r: True)(result)
        return not val

    # 标准比较 "fc > 0"
    m = _VALIDATE_PATTERN.match(expr)
    if not m:
        return True

    negate = m.group(1) is not None
    var = m.group(2)
    op = m.group(3)
    raw_val = m.group(4)

    fn = _VALIDATE_VAR_MAP.get(var)
    if fn is None:
        return True

    actual = fn(result)
    expected: Union[int, float] = float(raw_val) if "." in raw_val else int(raw_val)

    if op == ">":
        cmp_r = actual > expected
    elif op == "<":
        cmp_r = actual < expected
    elif op == ">=":
        cmp_r = actual >= expected
    elif op == "<=":
        cmp_r = actual <= expected
    elif op == "==":
        cmp_r = actual == expected
    elif op == "!=":
        cmp_r = actual != expected
    else:
        cmp_r = False

    return not cmp_r if negate else cmp_r


# ─── PipelineExecutor ───────────────────────────────────────────

class PipelineExecutor:
    """声明式 Pipeline 执行引擎。"""

    def __init__(self):
        self.params_map: Dict[str, Any] = {}
        self.step_results: Dict[str, StepResult] = {}
        self.template: Optional[TemplateDef] = None
        self.project: Any = None
        self.canvas: Any = None
        self._find_layer_fn: Optional[Callable] = None
        self._is_main_thread: bool = True

    # ── 入口 ─────────────────────────────────────────────────────

    def execute(
        self,
        template_path: str,
        source_layer_name: str = "",
        boundary_layer_name: str = "",
        radius_m: float = 500.0,
        population_layer_name: str = "",
        population_field: str = "",
        intensity_layer_name: str = "",
        intensity_field: str = "",
        project: Any = None,
        canvas: Any = None,
        _find_layer_fn: Optional[Callable] = None,
    ) -> Dict[str, Any]:
        t0 = time.time()

        self.template = self._parse_template(template_path)

        self._find_layer_fn = _find_layer_fn

        # 建筑风险模式：intensity_layer_name 优先于 source_layer_name
        if intensity_layer_name:
            source_layer_name = intensity_layer_name

        source_layer = self._find_layer(source_layer_name) if source_layer_name else None
        boundary_layer = self._find_layer(boundary_layer_name) if boundary_layer_name else None

        if intensity_layer_name and source_layer is None:
            return {"success": False, "status": "failed", "message": f"图层 '{intensity_layer_name}' 未找到"}
        if source_layer_name and not intensity_layer_name and source_layer is None:
            return {"success": False, "status": "failed", "message": f"图层 '{source_layer_name}' 未找到"}
        if boundary_layer_name and boundary_layer is None:
            return {"success": False, "status": "failed", "message": f"图层 '{boundary_layer_name}' 未找到"}

        self.project = project
        self.canvas = canvas

        # 检测是否在主线程：QGIS 画布操作必须在主线程
        try:
            from PyQt5.QtCore import QThread
            from PyQt5.QtWidgets import QApplication
            app = QApplication.instance()
            if app is not None:
                self._is_main_thread = (QThread.currentThread() == app.thread())
        except ImportError:
            pass
        source_layer_crs = ""
        if source_layer:
            source_layer_crs = source_layer.crs().authid() if source_layer.crs().isValid() else "EPSG:4326"

        self.params_map = {
            "source_layer": source_layer,
            "boundary_layer": boundary_layer,
            "radius_m": radius_m,
            "source_name": self._sanitize_name(source_layer.name()) if source_layer else "building_risk",
            "crs": source_layer_crs,
        }

        # 建筑风险模式：注册震度图层和震度字段
        if intensity_layer_name:
            self.params_map["intensity_layer_name"] = intensity_layer_name
            self.params_map["intensity_layer"] = source_layer
            if intensity_field:
                self.params_map["intensity_field"] = intensity_field

        # 人口覆盖率模板：解析人口图层
        if population_layer_name:
            population_layer = self._find_layer(population_layer_name)
            if population_layer is None:
                return {"success": False, "message": f"图层 '{population_layer_name}' 未找到"}
            self.params_map["population_layer"] = population_layer
            if population_field:
                self.params_map["population_field"] = population_field

        guard_ok = self._check_guards()
        if not guard_ok:
            # fail-closed：_check_guards 已按 effective on_fail（模板级 + 守卫级覆盖）
            # 判定是否阻断；此处补充失败明细便于端到端诊断
            detail = ""
            report = getattr(self, "_last_guard_report", None)
            if report is not None and report.blocking_failed:
                detail = " | ".join(
                    f"{r.condition}: {r.message}" for r in report.blocking_failed
                )
            message = f"Guard check failed: {detail}" if detail else "Guard check failed"
            # B 兜底：几何角色 Guard 失败时，追加「应为什么几何图层 + 当前候选」，
            # 使用户/后续澄清可直接修正（只增强报错文案，不改 Guard 判定）
            role_hint = self._build_guard_role_hint(report)
            if role_hint:
                message = f"{message} | {role_hint}"
            return {"success": False, "status": "failed", "message": message}

        # step_outputs: {step_id → resolved output, "$source_layer" → layer, ...}
        step_outputs: Dict[str, Any] = {
            "$source_layer": source_layer,
            "$boundary_layer": boundary_layer,
            "source_layer": source_layer,
            "boundary_layer": boundary_layer,
        }
        # 建筑风险模式：注册震度图层引用
        if "intensity_layer" in self.params_map:
            step_outputs["$intensity_layer"] = source_layer
            step_outputs["intensity_layer"] = source_layer

        # 人口覆盖率模板：注册人口图层引用
        if "population_layer" in self.params_map:
            pop_layer = self.params_map["population_layer"]
            step_outputs["$population_layer"] = pop_layer
            step_outputs["population_layer"] = pop_layer

        current_boundary = boundary_layer  # 跟踪当前边界

        try:
            for step_idx, step in enumerate(self.template.steps, start=1):
                t_step = time.time()

                # 解析 input_source
                input_data = self._resolve_step_input(step, step_outputs)

                # 解析 input_boundary
                step_boundary = current_boundary
                if step.input_boundary:
                    resolved = self._resolve_ref(step.input_boundary, step_outputs)
                    if resolved is not None:
                        step_boundary = resolved

                # 执行 engine chain
                result = self._execute_step(step, input_data, step_boundary)
                result.elapsed = time.time() - t_step

                self.step_results[step.id] = result

                # 注入 step_outputs
                step_outputs["$" + step.id] = result
                step_outputs["$" + step.id + ".output"] = self._extract_output_value(result)
                step_outputs[step.id] = result
                step_outputs[step.id + ".output"] = self._extract_output_value(result)

                # P2-1 fail-fast：步骤失败立即中止后续步骤，不再照跑
                if result.error is not None or result.status == "failed":
                    _log.error("[execute] 第 %d 步 '%s' 失败，中止后续步骤: %s",
                               step_idx, step.id, result.error or "status=failed")
                    return {
                        "success": False,
                        "status": "failed",
                        "message": f"第 {step_idx} 步 '{step.id}' 失败: {result.error or 'unknown error'}",
                        "step_results": self.step_results,
                    }

                # 声明式边界传播
                if hasattr(step, 'propagate_boundary') and step.propagate_boundary:
                    if result.qgis_layer is not None:
                        current_boundary = result.qgis_layer
                # 自动推断：dissolve 或 buffer 类型且以 boundary_ 开头 → 更新边界
                elif step.id.startswith("boundary_") and result.qgis_layer is not None:
                    current_boundary = result.qgis_layer

        except Exception as e:
            traceback.print_exc()
            _log.error("[execute] 执行异常中止: %s", e)
            return {"success": False, "status": "failed", "message": str(e), "step_results": self.step_results}

        output_result = self.step_results.get("output_layer", self.step_results.get("output"))
        stats_result = self.step_results.get("stats")

        _log.info("[execute] output_result=%s error=%s qgis_layer=%s",
                  output_result is not None,
                  output_result.error if output_result else "N/A",
                  output_result.qgis_layer is not None if output_result else "N/A")

        # ── P2-1 终态诚实化：输出步缺失/error/无有效图层 → 真失败（不再无条件 success:True）──
        stats_snapshot = stats_result.stats if stats_result else {}
        if output_result is None:
            _log.error("[execute] 终态失败：未找到输出步骤结果（output_layer/output）")
            return {
                "success": False,
                "status": "failed",
                "message": "输出步骤缺失：未找到 output_layer/output 步骤结果",
                "elapsed": time.time() - t0,
                "step_count": len(self.step_results),
                "step_results": self.step_results,
                "output_layer": None,
                "feature_count": 0,
                "stats": stats_snapshot,
            }
        if output_result.error is not None:
            _log.error("[execute] 终态失败：输出步骤 '%s' error=%s",
                       output_result.step_id, output_result.error)
            return {
                "success": False,
                "status": "failed",
                "message": f"输出步骤 '{output_result.step_id}' 失败: {output_result.error}",
                "elapsed": time.time() - t0,
                "step_count": len(self.step_results),
                "step_results": self.step_results,
                "output_layer": None,
                "feature_count": 0,
                "stats": stats_snapshot,
            }
        if output_result.qgis_layer is None:
            _log.error("[execute] 终态失败：输出步骤 '%s' 未产生有效图层",
                       output_result.step_id)
            return {
                "success": False,
                "status": "failed",
                "message": f"输出步骤 '{output_result.step_id}' 未产生有效图层",
                "elapsed": time.time() - t0,
                "step_count": len(self.step_results),
                "step_results": self.step_results,
                "output_layer": None,
                "feature_count": 0,
                "stats": stats_snapshot,
            }

        # ── P2-2 出口体检：输出步结构检查（复用 _VALIDATE_VAR_MAP 表达式机制）──
        # 只查结构（fc>0、几何非空）；严禁把业务值当失败（0% 覆盖率合法、gap 全覆盖空图层合法）。
        output_check = self.template.output_check if self.template else {}
        empty_semantics = (output_check or {}).get("empty", "degraded")
        if empty_semantics not in ("ok", "degraded"):
            _log.warning("[execute] 模板 '%s' output_check.empty=%r 非法，使用缺省 'degraded'",
                         self.template.template_id if self.template else "?",
                         empty_semantics)
            empty_semantics = "degraded"

        structure_issues = []
        if not _parse_and_eval("fc > 0", output_result):
            structure_issues.append(f"fc={output_result.feature_count}（fc>0 不满足）")
        if not _parse_and_eval("not is_empty", output_result):
            structure_issues.append("shapely_geom 缺失或为空")

        status = "ok"
        if structure_issues:
            _log.warning("[execute] 输出步骤 '%s' 结构检查不过: %s；output_check.empty=%s",
                         output_result.step_id, "; ".join(structure_issues), empty_semantics)
            if empty_semantics == "degraded":
                status = "degraded"
        # 其它结构期望（字段存在 / CRS 一致 / 几何有效）后续切片按需扩展

        # 构建详细输出信息
        output_name = ""
        feature_count = 0
        if output_result and output_result.qgis_layer:
            output_name = output_result.qgis_layer.name()
            feature_count = output_result.feature_count
            _log.info("[execute] output_name=%s fc=%d", output_name, feature_count)
        else:
            _log.warning("[execute] output_layer FAILED: result=%s error=%s",
                         output_result is not None,
                         output_result.error if output_result else "no_result")

        stats = stats_result.stats if stats_result else {}
        coverage_pct = None
        if stats and stats.get("coverage_rate") is not None:
            coverage_pct = stats["coverage_rate"]  # 已是百分比，如 32.7
        radius = self.params_map.get("radius_m", 500)

        action = self.template.template_id if self.template else "coverage_analysis"

        if action == "gap_analysis":
            title = "盲区分析完成"
        elif action == "population_coverage":
            title = "人口覆盖率分析完成"
        elif action == "building_risk_analysis":
            title = "建筑倒塌风险分析完成"
        else:
            title = "覆盖率分析完成"

        message_parts = [f"{title}，新增图层：{output_name}"]
        detail_parts = []
        if feature_count:
            detail_parts.append(f"{feature_count} 个要素")

        # 建筑风险模式用震度统计，其他模式用缓冲区半径
        if action == "building_risk_analysis":
            total_exp = stats.get("total_exposed_population") if stats else None
            high_risk = stats.get("high_risk_population") if stats else None
            risk_idx = stats.get("building_risk_index") if stats else None
            if total_exp is not None:
                detail_parts.append(f"总受灾人口 {total_exp:.0f}")
            if high_risk is not None:
                detail_parts.append(f"高风险人口 {high_risk:.0f}")
            if risk_idx is not None:
                detail_parts.append(f"建筑风险指数 {risk_idx:.1f}")
        else:
            detail_parts.append(f"{radius}m 缓冲区")
        if action == "gap_analysis":
            gap_pct = stats.get("gap_rate") if stats else None
            gap_area = stats.get("gap_area") if stats else None
            if gap_pct is not None:
                detail_parts.append(f"盲区率 {gap_pct:.1f}%")
            if gap_area is not None:
                detail_parts.append(f"盲区面积 {gap_area:.0f} m²")
        elif action == "population_coverage":
            if coverage_pct is not None:
                detail_parts.append(f"面积覆盖率 {coverage_pct:.1f}%")
            pop_rate = stats.get("pop_coverage_rate") if stats else None
            total_pop = stats.get("total_population") if stats else None
            if pop_rate is not None:
                detail_parts.append(f"人口覆盖率 {pop_rate:.1f}%")
            if total_pop is not None:
                detail_parts.append(f"总人口 {total_pop:.0f}")
        else:
            if coverage_pct is not None:
                detail_parts.append(f"覆盖率 {coverage_pct:.1f}%")
        message_parts.append(f"（{'，'.join(detail_parts)}）")

        message = "".join(message_parts)
        if status == "degraded":
            # P2-2：输出空存疑——警告但不判死（业务值仍以 stats 为准）
            message += "⚠️ 结果为空，请检查数据/参数"

        return {
            "success": True,
            "status": status,  # P2-1 状态机 + P2-2 出口体检: ok | degraded
            "message": message,
            "elapsed": time.time() - t0,
            "step_count": len(self.step_results),
            "output_file": self._ensure_output_file(output_result),
            "output_layer": output_result.qgis_layer if output_result else None,
            "feature_count": feature_count,
            "stats": stats,
        }

    def _ensure_output_file(self, output_result) -> str:
        """非主线程时，若产出为内存图层，强制写入 GeoJSON 文件以便调用方加载。"""
        file_path = output_result.file_path if output_result else ""
        if file_path:
            return file_path
        if not self._is_main_thread and output_result and output_result.qgis_layer is not None:
            from qgis.core import QgsVectorFileWriter
            layer = output_result.qgis_layer
            output_name = output_result.qgis_layer.name()
            output_dir = self.template.output_base if self.template else "user_data/exports/shapefiles"
            file_path = os.path.abspath(os.path.join(output_dir, f"{output_name}.geojson"))
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            error = QgsVectorFileWriter.writeAsVectorFormat(
                layer, file_path, "UTF-8", layer.crs(), "GeoJSON"
            )
            if error[0] == QgsVectorFileWriter.NoError:
                _log.info("_ensure_output_file: saved memory layer → %s", file_path)
                return file_path
            else:
                _log.warning("_ensure_output_file: write failed %s", error)
        return file_path

    # ── 模板解析 (与 coverage_analysis.json 严格对齐) ─────────

    def _parse_template(self, path: str) -> TemplateDef:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)

        # params
        params = [ParamDef(**{k: v for k, v in p.items() if k in ParamDef.__dataclass_fields__})
                   for p in raw.get("params", [])]

        # guards: 顶层列表 [{condition, params, on_fail?}, ...]
        # 顶层 guards_on_fail: "error" | "warn"（P2-0 fail-closed，缺省 "error"）
        guards = None
        if "guards" in raw and raw["guards"]:
            guard_list = []
            for g in raw["guards"]:
                gd = GuardDef(
                    condition=g.get("condition", ""),
                    params=g.get("params", {}),
                    on_fail=g.get("on_fail", ""),
                )
                guard_list.append(gd)
            template_on_fail = raw.get("guards_on_fail", "error")
            if template_on_fail not in ("error", "warn"):
                _log.warning(
                    "_parse_template: 非法 guards_on_fail=%r，使用缺省 'error'",
                    template_on_fail,
                )
                template_on_fail = "error"
            guards = GuardCheck(guards=guard_list, on_fail=template_on_fail)

        # steps
        steps = []
        for s in raw.get("steps", []):
            # 扁平化 engine: {primary, fallback[]} → engine_chain[]
            engine_chain = []
            engine_block = s.get("engine", {})
            primary = engine_block.get("primary")
            fallbacks = engine_block.get("fallback", [])

            if primary:
                engine_chain.append(EngineSpec(
                    name=primary.get("name", ""),
                    algorithm=primary.get("algorithm"),
                    method=primary.get("method"),
                    validate=primary.get("validate"),
                    params=primary.get("params", {}),
                    path_template=primary.get("path_template"),
                ))
            for fb in fallbacks:
                engine_chain.append(EngineSpec(
                    name=fb.get("name", ""),
                    algorithm=fb.get("algorithm"),
                    method=fb.get("method"),
                    validate=fb.get("validate"),
                    params=fb.get("params", {}),
                    path_template=fb.get("path_template"),
                ))

            step = StepDef(
                id=s["id"],
                input_source=s.get("input_source"),
                input_boundary=s.get("input_boundary"),
                params=s.get("params", {}),
                engine_chain=engine_chain,
                output_keys=s.get("output_keys"),
                output_layer=s.get("output_layer"),
                log=s.get("log", ""),
                comment=s.get("comment", ""),
            )
            # 从 params 自动填充 output_layer (name_template/add_to_project/zoom_to_layer)
            if step.output_layer is None:
                p = step.params
                if any(k in p for k in ("add_to_project", "zoom_to_layer", "name_template")):
                    step.output_layer = {
                        "name_template": p.get("name_template", ""),
                        "add_to_project": p.get("add_to_project", False),
                        "zoom_to_layer": p.get("zoom_to_layer", False),
                    }
            # 声明式边界传播：boundary_dissolve / boundary_buffer 自动标记
            if step.id.startswith("boundary_") and any(
                eng.name in ("qgis_processing", "shapely") for eng in engine_chain
            ):
                step.propagate_boundary = True

            steps.append(step)

        return TemplateDef(
            template_id=raw.get("action", raw.get("template_id", "unknown")),
            zh_triggers=raw.get("zh_triggers", []),
            ja_triggers=raw.get("ja_triggers", []),
            en_triggers=raw.get("en_triggers", []),
            params=params,
            guards=guards,
            steps=steps,
            output_base=raw.get("output_base", "user_data/exports/shapefiles/"),
            output_check=raw.get("output_check") or {},
        )

    # ── Guards ───────────────────────────────────────────────────

    def _check_guards(self) -> bool:
        if not self.template or not self.template.guards:
            return True
        checker = GuardChecker(self.params_map)
        report = checker.check(self.template.guards)
        # P2-0：守卫失败报告写日志（逐条件 passed/failed + message）
        self._last_guard_report = report
        for r in report.results:
            if r.auto_skipped:
                _log.info("[guards] condition=%s auto_skipped: %s", r.condition, r.message)
            elif r.passed:
                _log.info("[guards] condition=%s passed", r.condition)
            else:
                _log.log(
                    logging.ERROR if r.blocking else logging.WARNING,
                    "[guards] condition=%s %s: %s",
                    r.condition,
                    "FAILED (blocking)" if r.blocking else "FAILED (warn)",
                    r.message,
                )
        # fail-closed：仅 blocking 的失败中止执行（warn 级不阻断）
        if report.blocking_failed:
            return False
        return True

    def _build_guard_role_hint(self, report) -> str:
        """B 兜底：几何角色 Guard 失败时，列出「应为什么几何图层 + 当前可用候选」。

        只增强 Guard 失败报错文案，便于用户/后续澄清直接修正；
        不改 Guard 判定（Guard 仍为最后防线，纠偏在参数修正层）。
        """
        if report is None or not report.blocking_failed:
            return ""
        try:
            from core.clarification import role_candidates, ROLE_LABELS
        except ImportError:
            return ""
        geom_guards = {
            "source_is_point": ("source_layer", "Point"),
            "boundary_is_polygon": ("boundary_layer", "Polygon"),
            "population_is_polygon": ("population_layer", "Polygon"),
            "intensity_is_polygon": ("intensity_layer", "Polygon"),
        }
        parts = []
        for r in report.blocking_failed:
            if r.condition not in geom_guards:
                continue
            param_key, expected = geom_guards[r.condition]
            label = ROLE_LABELS.get(param_key, {}).get("zh", param_key)
            cands = role_candidates(self.project, param_key)
            if cands:
                parts.append(f"{label}应为{expected}图层（当前候选：{'、'.join(cands)}）")
            else:
                parts.append(f"{label}应为{expected}图层（当前无可用候选）")
        return " | ".join(parts)

    # ── Step 执行 ────────────────────────────────────────────────

    def _execute_step(self, step: StepDef, input_data: Any, boundary_data: Any) -> StepResult:
        """按 engine_chain 执行，成功且 validate 通过即止。"""
        last_error = None
        last_result = None  # P2-2：记录最后运行成功但 validate 未过的引擎结果（仅输出步）
        eng_start = time.time()

        for eng in step.engine_chain:
            try:
                # 合并 step.params + eng.params（eng.params 优先）
                merged_params = dict(step.params)
                merged_params.update(eng.params)
                eng.params = merged_params

                # 诊断日志：pop_intersect 步骤时 dump params_map
                if step.id == "pop_intersect" or eng.method == "pop_intersect":
                    _log.info("[step %s] DIAG params_map keys=%s", step.id, sorted(self.params_map.keys()))
                    _log.info("[step %s] DIAG merged_params=%s", step.id, merged_params)

                result = self._run_engine(eng, step, input_data, boundary_data)
                result.elapsed = time.time() - eng_start

                if result.error is not None:
                    _log.warning("[step %s] engine=%s ERROR: %s", step.id, eng.name, result.error)
                    last_error = result.error
                    continue

                # 引擎级 validate（唯一校验点）
                if not self._validate(eng.validate, result):
                    _log.warning("[step %s] engine=%s VALIDATE FAILED: %s", step.id, eng.name, eng.validate)
                    last_error = f"Validate failed [{eng.name}]: {eng.validate}"
                    # P2-2：输出步 validate 失败不判死——空图层语义由模板 output_check
                    # 逐模板声明（ok 合法 / degraded 存疑），保留引擎结果交由 execute()
                    # 终态结构检查裁决；中间步骤仍走 P2-1 fail-fast（error 语义）
                    if step.id in ("output_layer", "output"):
                        last_result = result
                    continue

                _log.info("[step %s] engine=%s OK", step.id, eng.name)
                result.status = "ok"  # P2-1 显式成功态
                return result

            except Exception as e:
                _log.error("[step %s] engine=%s CRASHED: %s", step.id, eng.name, e)
                traceback.print_exc()
                last_error = f"{eng.name} crashed: {e}"
                continue

        # 全部引擎耗尽
        if (step.id in ("output_layer", "output")
                and last_result is not None
                and last_error is not None
                and last_error.startswith("Validate failed")):
            # P2-2：输出步引擎均运行成功但 validate 未过（结构不健康）→ 不判死，
            # 保留最后一个引擎结果交由 execute() 终态结构检查（ok / degraded）
            _log.warning("[step %s] 输出步引擎全部 validate 未过: %s；交由终态结构检查裁决",
                         step.id, last_error)
            return last_result
        # P2-1：全部引擎耗尽 → failed（显式状态 + 错误日志）
        _log.error("[step %s] FAILED (all engines exhausted): %s", step.id, last_error)
        return StepResult(step_id=step.id, error=last_error, status="failed")

    # ── 引擎调度 ─────────────────────────────────────────────────

    def _run_engine(self, eng: EngineSpec, step: StepDef,
                    input_data: Any, boundary_data: Any) -> StepResult:
        name = eng.name
        if name == "qgis_processing":
            return self._engine_qgis_processing(eng, step, input_data, boundary_data)
        elif name == "qgis_spatial_index":
            return self._engine_spatial_index(eng, step, input_data, boundary_data)
        elif name == "shapely":
            return self._engine_shapely(eng, step, input_data, boundary_data)
        elif name == "shapely_wkt":
            return self._engine_shapely_wkt(eng, step, input_data, boundary_data)
        elif name == "geojson_file":
            return self._engine_geojson_file(eng, step, input_data, boundary_data)
        elif name == "qgis_memory_layer":
            return self._engine_memory_layer(eng, step, input_data, boundary_data)
        else:
            return StepResult(step_id=step.id, error=f"Unknown engine: {name}")

    # ── engine: qgis_processing ──────────────────────────────────

    def _engine_qgis_processing(self, eng: EngineSpec, step: StepDef,
                                 input_data: Any, boundary_data: Any) -> StepResult:
        try:
            import processing
            from qgis.core import QgsVectorLayer
        except ImportError as e:
            return StepResult(step_id=step.id, error=f"Import error: {e}")

        algo = eng.algorithm
        if not algo:
            return StepResult(step_id=step.id, error="Missing algorithm")

        # 从 eng.params 读取所有参数（step.params 已在 _execute_step 合并）
        params = dict(eng.params)

        input_layer = self._extract_qgis_layer(input_data)
        if input_layer is None:
            return StepResult(step_id=step.id, error="No valid input layer")

        params["INPUT"] = input_layer

        # 处理 $radius_m 占位符
        for k, v in list(params.items()):
            if v == "$radius_m":
                params[k] = self.params_map.get("radius_m", 500.0)

        if "OUTPUT" not in params:
            params["OUTPUT"] = "TEMPORARY_OUTPUT"

        # clip 需要 OVERLAY
        if algo in ("native:clip", "native:intersection") and "OVERLAY" not in params:
            overlay = self._extract_qgis_layer(boundary_data)
            if overlay is None:
                return StepResult(step_id=step.id, error="No boundary layer for clip/intersection")
            # 跨 CRS 时，将 overlay 重投影到 input CRS，避免零命中
            try:
                from qgis.core import QgsCoordinateTransform, QgsProject, QgsWkbTypes
                in_crs = input_layer.crs()
                ov_crs = overlay.crs()
                if in_crs.isValid() and ov_crs.isValid() and in_crs.authid() != ov_crs.authid():
                    xform = QgsCoordinateTransform(ov_crs, in_crs, QgsProject.instance())
                    mem_layer = QgsVectorLayer(
                        f"{QgsWkbTypes.displayString(overlay.wkbType())}?crs={in_crs.authid()}",
                        f"{overlay.name() or 'overlay'}_reprojected", "memory"
                    )
                    mem_layer.dataProvider().addAttributes(overlay.fields())
                    mem_layer.updateFields()
                    feats = []
                    for feat in overlay.getFeatures():
                        geom = feat.geometry()
                        if geom is None or geom.isEmpty():
                            continue
                        geom.transform(xform)
                        feat.setGeometry(geom)
                        feats.append(feat)
                    if feats:
                        mem_layer.dataProvider().addFeatures(feats)
                        mem_layer.updateExtents()
                        overlay = mem_layer
            except Exception:
                pass
            params["OVERLAY"] = overlay

        try:
            result = processing.run(algo, params)
            output_layer = result.get("OUTPUT")
            if output_layer is None:
                return StepResult(step_id=step.id, error=f"QGIS {algo} returned None OUTPUT")

            if isinstance(output_layer, str):
                output_layer = QgsVectorLayer(output_layer, f"{step.id}_qgis", "ogr")

            return StepResult(
                step_id=step.id,
                output_type="layer",
                feature_count=output_layer.featureCount() if output_layer.isValid() else 0,
                qgis_layer=output_layer,
                engine_used=f"qgis_processing::{algo}",
            )
        except Exception as e:
            return StepResult(step_id=step.id, error=f"QGIS {algo}: {e}")

    # ── engine: qgis_spatial_index ──────────────────────────────

    def _engine_spatial_index(self, eng: EngineSpec, step: StepDef,
                               input_data: Any, boundary_data: Any) -> StepResult:
        try:
            from qgis.core import (
                QgsVectorLayer, QgsFeatureRequest, QgsSpatialIndex, QgsFeature,
                QgsWkbTypes, QgsCoordinateTransform, QgsProject,
            )
        except ImportError as e:
            return StepResult(step_id=step.id, error=f"Import error: {e}")

        source_layer = self._extract_qgis_layer(input_data)
        boundary_layer = self._extract_qgis_layer(boundary_data)

        if source_layer is None:
            return StepResult(step_id=step.id, error="No source layer")
        if boundary_layer is None:
            return StepResult(step_id=step.id, error="No boundary layer")

        try:
            source_crs = source_layer.crs()
            boundary_crs = boundary_layer.crs()

            # CRS 一致性校验：任一 CRS 无效时显式报错，禁止静默降级
            if not source_crs.isValid() or not boundary_crs.isValid():
                return StepResult(
                    step_id=step.id,
                    error=(
                        "Spatial index: invalid CRS "
                        f"source={source_crs.authid() or 'unknown'} "
                        f"boundary={boundary_crs.authid() or 'unknown'}"
                    ),
                )

            # 跨 CRS 时，将 boundary 几何与 extent 转换到 source CRS，避免零命中
            crs_mismatch = source_crs.authid() != boundary_crs.authid()
            # 显式拷贝 QgsFeature：getFeatures() 迭代复用同一对象，list() 会导致多要素退化
            boundary_features = [QgsFeature(f) for f in boundary_layer.getFeatures()]
            if crs_mismatch:
                try:
                    xform = QgsCoordinateTransform(
                        boundary_crs, source_crs, QgsProject.instance())
                    transformed = []
                    for feat in boundary_features:
                        geom = feat.geometry()
                        if geom is None or geom.isEmpty():
                            continue
                        geom.transform(xform)
                        transformed.append(feat)
                    boundary_features = transformed
                except Exception as e:
                    return StepResult(
                        step_id=step.id,
                        error=f"Spatial index: reproject boundary to {source_crs.authid()} failed: {e}",
                    )

            index = QgsSpatialIndex()
            index.addFeatures(boundary_features)
            rect = boundary_layer.extent()
            if crs_mismatch:
                try:
                    rect = QgsCoordinateTransform(
                        boundary_crs, source_crs, QgsProject.instance()
                    ).transformBoundingBox(rect)
                except Exception as e:
                    return StepResult(
                        step_id=step.id,
                        error=f"Spatial index: reproject boundary extent failed: {e}",
                    )

            req = QgsFeatureRequest().setFilterRect(rect)
            candidate_ids = []
            for feat in source_layer.getFeatures(req):
                geom = feat.geometry()
                if geom is None or geom.isEmpty():
                    continue
                if index.intersects(geom.boundingBox()):
                    candidate_ids.append(feat.id())

            mem_layer = QgsVectorLayer(
                f"{QgsWkbTypes.displayString(source_layer.wkbType())}?crs={source_layer.crs().authid()}",
                f"{step.id}_filtered", "memory"
            )
            mem_layer.dataProvider().addAttributes(source_layer.fields())
            mem_layer.updateFields()

            features = [source_layer.getFeature(fid) for fid in candidate_ids]
            mem_layer.dataProvider().addFeatures([f for f in features if f.isValid()])
            mem_layer.updateExtents()

            return StepResult(
                step_id=step.id,
                output_type="layer",
                feature_count=mem_layer.featureCount(),
                qgis_layer=mem_layer,
                engine_used="qgis_spatial_index",
            )
        except Exception as e:
            return StepResult(step_id=step.id, error=f"Spatial index: {e}")

    # ── engine: shapely ──────────────────────────────────────────

    def _engine_shapely(self, eng: EngineSpec, step: StepDef,
                         input_data: Any, boundary_data: Any) -> StepResult:
        try:
            from shapely.ops import unary_union
        except ImportError as e:
            return StepResult(step_id=step.id, error=f"Import error: {e}")

        method = eng.method or "buffer"
        input_geom = self._extract_shapely_geom(input_data)
        if input_geom is None:
            return StepResult(step_id=step.id, error="No shapely geometry from input")

        if method == "buffer":
            radius = float(eng.params.get("DISTANCE", eng.params.get("distance",
                            self.params_map.get("radius_m", 500))))
            buffered = input_geom.buffer(radius)
            return StepResult(
                step_id=step.id, output_type="shapely_geom",
                shapely_geom=buffered, engine_used="shapely::buffer",
            )

        elif method in ("dissolve", "union"):
            dissolved = unary_union(input_geom)
            return StepResult(
                step_id=step.id, output_type="shapely_geom",
                shapely_geom=dissolved, engine_used=f"shapely::{method}",
            )

        elif method == "intersection":
            boundary_geom = self._extract_shapely_geom(boundary_data)
            if boundary_geom is None:
                return StepResult(step_id=step.id, error="No boundary geometry")
            result = input_geom.intersection(boundary_geom)
            return StepResult(
                step_id=step.id, output_type="shapely_geom",
                shapely_geom=result, engine_used="shapely::intersection",
            )

        elif method == "area":
            boundary_geom = self._extract_shapely_geom(boundary_data)
            if boundary_geom is None:
                return StepResult(step_id=step.id, error="No boundary geometry for area calc")

            clip_geom = input_geom
            total = boundary_geom.area
            covered = clip_geom.area if clip_geom else 0
            rate = (covered / total * 100) if total > 0 else 0

            stats = {}
            for key in (step.output_keys or []):
                if key == "source_count":
                    sf = self.step_results.get("spatial_filter")
                    stats[key] = sf.feature_count if sf else 0
                elif key == "radius_m":
                    stats[key] = self.params_map.get("radius_m", 500.0)
                elif key == "total_area":
                    stats[key] = total
                elif key == "covered_area":
                    stats[key] = covered
                elif key == "coverage_rate":
                    stats[key] = rate
                elif key == "gap_area":
                    stats[key] = max(0.0, total - covered)
                elif key == "gap_rate":
                    stats[key] = max(0.0, 100.0 - rate) if total > 0 else 0.0
                elif key == "total_population":
                    pop_step = self.step_results.get("pop_intersect")
                    if pop_step and pop_step.stats:
                        stats[key] = pop_step.stats.get("total_population", 0.0)
                    else:
                        stats[key] = 0.0
                elif key == "covered_population":
                    pop_step = self.step_results.get("pop_intersect")
                    if pop_step and pop_step.file_path and os.path.exists(pop_step.file_path):
                        try:
                            import json as _json
                            from shapely.geometry import shape
                            with open(pop_step.file_path, "r", encoding="utf-8") as f:
                                gj = _json.load(f)
                            covered_pop = 0.0
                            pop_field = self.params_map.get("population_field", "population")
                            # 回退校验：params_map 中的 population_field 可能被 LLM 幻觉污染
                            # （如 "人口字段名"/"pop"），必须与 GeoJSON 实际字段匹配
                            if gj.get("features"):
                                actual_keys = set(gj["features"][0]["properties"].keys())
                                if pop_field not in actual_keys:
                                    if "population" in actual_keys:
                                        pop_field = "population"
                                    elif "JINKO" in actual_keys:
                                        pop_field = "JINKO"
                            for feat in gj.get("features", []):
                                props = feat.get("properties", {})
                                pop_val = float(props.get(pop_field, 0))
                                zone_area = props.get("zone_area", 0)
                                intersect_geom = shape(feat["geometry"])
                                intersect_area = intersect_geom.area
                                if zone_area > 0:
                                    covered_pop += pop_val * (intersect_area / zone_area)
                            stats[key] = covered_pop
                        except Exception:
                            stats[key] = 0.0
                    else:
                        stats[key] = 0.0
                elif key == "pop_coverage_rate":
                    tp = stats.get("total_population", 0.0)
                    cp = stats.get("covered_population", 0.0)
                    stats[key] = (cp / tp * 100) if tp > 0 else 0.0
                elif key == "total_exposed_population":
                    pop_step = self.step_results.get("pop_intersect")
                    if pop_step and pop_step.file_path and os.path.exists(pop_step.file_path):
                        try:
                            import json as _json
                            from shapely.geometry import shape
                            with open(pop_step.file_path, "r", encoding="utf-8") as f:
                                gj = _json.load(f)
                            exposed_pop = 0.0
                            pf = self.params_map.get("population_field", "population")
                            if gj.get("features"):
                                actual_keys = set(gj["features"][0]["properties"].keys())
                                if pf not in actual_keys:
                                    if "population" in actual_keys:
                                        pf = "population"
                                    elif "JINKO" in actual_keys:
                                        pf = "JINKO"
                            for feat in gj.get("features", []):
                                props = feat.get("properties", {})
                                pop_val = float(props.get(pf, 0))
                                zone_area = float(props.get("zone_area", 0))
                                int_prob = float(props.get("intensity_probability", 0))
                                intersect_geom = shape(feat["geometry"])
                                intersect_area = intersect_geom.area
                                if zone_area > 0 and int_prob > 0:
                                    exposed_pop += pop_val * (intersect_area / zone_area) * int_prob
                            stats[key] = exposed_pop
                        except Exception:
                            stats[key] = 0.0
                    else:
                        stats[key] = 0.0
                elif key == "high_risk_population":
                    pop_step = self.step_results.get("pop_intersect")
                    if pop_step and pop_step.file_path and os.path.exists(pop_step.file_path):
                        try:
                            import json as _json
                            from shapely.geometry import shape
                            with open(pop_step.file_path, "r", encoding="utf-8") as f:
                                gj = _json.load(f)
                            high_risk = 0.0
                            pf = self.params_map.get("population_field", "population")
                            if gj.get("features"):
                                actual_keys = set(gj["features"][0]["properties"].keys())
                                if pf not in actual_keys:
                                    if "population" in actual_keys:
                                        pf = "population"
                                    elif "JINKO" in actual_keys:
                                        pf = "JINKO"
                            for feat in gj.get("features", []):
                                props = feat.get("properties", {})
                                int_prob = float(props.get("intensity_probability", 0))
                                if int_prob <= 0.9:
                                    continue
                                pop_val = float(props.get(pf, 0))
                                zone_area = float(props.get("zone_area", 0))
                                intersect_geom = shape(feat["geometry"])
                                intersect_area = intersect_geom.area
                                if zone_area > 0:
                                    high_risk += pop_val * (intersect_area / zone_area)
                            stats[key] = high_risk
                        except Exception:
                            stats[key] = 0.0
                    else:
                        stats[key] = 0.0
                elif key == "building_risk_index":
                    tp = stats.get("total_population", 0.0)
                    ep = stats.get("total_exposed_population", 0.0)
                    stats[key] = round((ep / tp * 100), 2) if tp > 0 else 0.0

            return StepResult(
                step_id=step.id, output_type="stats_dict",
                stats=stats, engine_used="shapely::area",
            )

        else:
            return StepResult(step_id=step.id, error=f"Unknown shapely method: {method}")

    # ── engine: shapely_wkt ──────────────────────────────────────

    def _engine_shapely_wkt(self, eng: EngineSpec, step: StepDef,
                              input_data: Any, boundary_data: Any) -> StepResult:
        try:
            from shapely import wkt
            from shapely.ops import unary_union
        except ImportError as e:
            return StepResult(step_id=step.id, error=f"Import error: {e}")

        method = eng.method or "intersection"

        # pop_intersect handles shapely geometry input directly — bypass early QGIS layer check
        if method == "pop_intersect":
            return self._engine_shapely_wkt_pop_intersect(eng, step, input_data, boundary_data, wkt)

        input_layer = self._extract_qgis_layer(input_data)
        if input_layer is None:
            return StepResult(step_id=step.id, error="No QGIS layer for shapely_wkt")

        try:
            wkts = [f.geometry().asWkt() for f in input_layer.getFeatures()
                    if f.geometry() and not f.geometry().isEmpty()]
            if not wkts:
                return StepResult(step_id=step.id, error="No valid geometries")
            geom = unary_union([wkt.loads(w) for w in wkts])

            if method == "buffer":
                radius = float(eng.params.get("DISTANCE", self.params_map.get("radius_m", 500)))
                result = geom.buffer(radius)
            elif method in ("dissolve", "union"):
                result = unary_union([wkt.loads(w) for w in wkts])
            elif method == "intersection":
                b_geom = self._extract_shapely_or_layer(boundary_data)
                if b_geom is None:
                    return StepResult(step_id=step.id, error="No valid boundary geometry for intersection")
                result = geom.intersection(b_geom)
            elif method == "difference":
                b_geom = self._extract_shapely_or_layer(boundary_data)
                if b_geom is None:
                    return StepResult(step_id=step.id, error="No valid boundary geometry for difference")
                result = geom.difference(b_geom)
            else:
                return StepResult(step_id=step.id, error=f"Unknown shapely_wkt method: {method}")

            return StepResult(
                step_id=step.id, output_type="shapely_geom",
                shapely_geom=result, engine_used=f"shapely_wkt::{method}",
            )
        except Exception as e:
            return StepResult(step_id=step.id, error=f"shapely_wkt: {e}")

    def _engine_shapely_wkt_pop_intersect(self, eng: EngineSpec, step: StepDef,
                                            input_data: Any, boundary_data: Any,
                                            wkt_module) -> StepResult:
        """pop_intersect：覆盖区域/震度图层与人口矢量面图层求交集，输出 GeoJSON 含人口属性。

        两种模式：
        - 覆盖模式（无 intensity_field）：单覆盖几何 × 人口面
        - 建筑风险模式（有 intensity_field）：震度图层 × 人口面，每 fragment 记录震度概率
        """
        try:
            from shapely.geometry import mapping
            import json as _json
            import re as _re
        except ImportError as e:
            return StepResult(step_id=step.id, error=f"Import error: {e}")

        try:
            population_layer = self._extract_qgis_layer(boundary_data)
            if population_layer is None:
                return StepResult(step_id=step.id, error="No population layer")

            pop_field = self.params_map.get("population_field", "population")
            layer_fields = [f.name() for f in population_layer.fields()]
            if pop_field not in layer_fields:
                _log.warning("[step %s] population_field '%s' not found in layer fields (%s), falling back to 'population'",
                             step.id, pop_field, layer_fields[:10])
                if "population" in layer_fields:
                    pop_field = "population"
                elif "JINKO" in layer_fields:
                    pop_field = "JINKO"
                else:
                    pop_field = layer_fields[0] if layer_fields else "population"

            intensity_field = self.params_map.get("intensity_field")

            # 模板承诺留空自动检测震度字段（T\d+_I\d+），缺失时进入覆盖模式会导致
            # building_risk 统计全 0。这里按注释实现自动检测。
            if not intensity_field:
                try:
                    from core.seismic_situation_map import JMA_INTENSITY_FIELD_PATTERN
                except ImportError:
                    from src.core.seismic_situation_map import JMA_INTENSITY_FIELD_PATTERN
                try:
                    int_layer_tmp = self._extract_qgis_layer(input_data)
                    if int_layer_tmp is not None:
                        for fname in [f.name() for f in int_layer_tmp.fields()]:
                            if JMA_INTENSITY_FIELD_PATTERN.match(fname):
                                intensity_field = fname
                                _log.info("[step %s] auto-detected intensity field: %s",
                                          step.id, intensity_field)
                                break
                except ImportError:
                    pass

            # ── 建筑风险模式：震度图层 × 人口图层 ─────────────────
            if intensity_field:
                intensity_layer = self._extract_qgis_layer(input_data)
                if intensity_layer is None:
                    return StepResult(step_id=step.id, error="No intensity layer for building_risk pop_intersect")

                # 自动检测震度字段（正则回退）
                int_layer_fields = [f.name() for f in intensity_layer.fields()]
                if intensity_field not in int_layer_fields:
                    try:
                        from core.seismic_situation_map import JMA_INTENSITY_FIELD_PATTERN
                    except ImportError:
                        from src.core.seismic_situation_map import JMA_INTENSITY_FIELD_PATTERN
                    for fname in int_layer_fields:
                        if JMA_INTENSITY_FIELD_PATTERN.match(fname):
                            intensity_field = fname
                            _log.info("[step %s] auto-detected intensity field: %s", step.id, intensity_field)
                            break

                if intensity_field not in int_layer_fields:
                    return StepResult(step_id=step.id,
                                      error=f"intensity_field '{intensity_field}' not found in intensity layer fields ({int_layer_fields[:10]}…)")

                # 预加载震度多边形列表
                intensity_polygons = []
                for int_feat in intensity_layer.getFeatures():
                    geo = int_feat.geometry()
                    if not geo or geo.isEmpty():
                        continue
                    try:
                        int_geom = wkt_module.loads(geo.asWkt())
                    except Exception:
                        continue
                    try:
                        int_prob = float(int_feat.attribute(intensity_field) or 0)
                    except (ValueError, TypeError):
                        int_prob = 0.0
                    intensity_polygons.append((int_geom, int_prob))

                if not intensity_polygons:
                    return StepResult(step_id=step.id, error="No valid intensity polygons")

                # 预加载人口 zones
                pop_zones = []
                for feat in population_layer.getFeatures():
                    geo = feat.geometry()
                    if not geo or geo.isEmpty():
                        continue
                    try:
                        pop_geom = wkt_module.loads(geo.asWkt())
                    except Exception:
                        continue
                    try:
                        pop_val = float(feat.attribute(pop_field) or 0)
                    except (ValueError, TypeError):
                        pop_val = 0.0
                    pop_zones.append((pop_geom, pop_val))

                if not pop_zones:
                    return StepResult(step_id=step.id, error="No valid population zones")

                # 使用 STRtree 空间索引加速：对人口 zones 建索引，
                # 每个 intensity 多边形只查可能相交的候选，避免 O(n*m) 暴力双重循环
                import shapely
                from shapely import STRtree as _STRtree
                pop_geoms = [g for g, _ in pop_zones]
                tree = _STRtree(pop_geoms)

                # 收集与任一 intensity 多边形相交的人口 zone 索引
                total_population = 0.0
                intersecting_pop_indices = set()

                # 计算所有震度 × 人口的交集
                features = []
                for int_geom, int_prob in intensity_polygons:
                    candidate_indices = tree.query(int_geom)
                    if candidate_indices.size == 0:
                        continue
                    for idx in candidate_indices.tolist():
                        pop_geom, pop_val = pop_zones[idx]
                        try:
                            if not int_geom.intersects(pop_geom):
                                continue
                        except Exception:
                            continue
                        intersecting_pop_indices.add(idx)
                        try:
                            intersection = pop_geom.intersection(int_geom)
                            if intersection.is_empty:
                                continue
                            zone_area = pop_geom.area
                            features.append({
                                "type": "Feature",
                                "properties": {
                                    pop_field: pop_val,
                                    "zone_area": zone_area,
                                    "intensity_probability": int_prob,
                                },
                                "geometry": mapping(intersection),
                            })
                        except Exception:
                            continue

                total_population = sum(pop_zones[i][1] for i in intersecting_pop_indices)

                if not features:
                    return StepResult(step_id=step.id, error="No population features intersect with intensity area")

                output_path = os.path.abspath(self._resolve_path(
                    "{output_dir}/{source_name}_pop_intersect_building_risk.geojson"
                ))
                os.makedirs(os.path.dirname(output_path), exist_ok=True)
                geojson = {"type": "FeatureCollection", "features": features}
                with open(output_path, "w", encoding="utf-8") as f:
                    _json.dump(geojson, f, ensure_ascii=False)

                return StepResult(
                    step_id=step.id, output_type="file_path",
                    feature_count=len(features), file_path=output_path,
                    engine_used="shapely_wkt::pop_intersect",
                    stats={"total_population": total_population},
                )

            # ── 覆盖模式（原有逻辑） ─────────────────────────────
            input_geom = self._extract_shapely_geom(input_data)
            if input_geom is None:
                return StepResult(step_id=step.id, error="No clip geometry for pop_intersect")

            boundary_layer_name = self.params_map.get("boundary_layer")

            # 提取边界几何用于判断人口 zone 是否落在边界内
            boundary_geom = None
            if boundary_layer_name:
                boundary_geom = self._extract_shapely_geom(boundary_layer_name)

            features = []
            total_population = 0.0

            for feat in population_layer.getFeatures():
                geo = feat.geometry()
                if not geo or geo.isEmpty():
                    continue
                pop_geom = wkt_module.loads(geo.asWkt())
                pop_val = feat.attribute(pop_field)
                if pop_val is None:
                    pop_val = 0
                else:
                    try:
                        pop_val = float(pop_val)
                    except (ValueError, TypeError):
                        pop_val = 0.0

                # 判断该人口 zone 是否落在边界内（用于 total_population）
                if boundary_geom and boundary_geom.intersects(pop_geom):
                    total_population += pop_val

                # 与覆盖区域求交集
                intersection = pop_geom.intersection(input_geom)
                if intersection.is_empty:
                    continue

                zone_area = pop_geom.area
                features.append({
                    "type": "Feature",
                    "properties": {
                        pop_field: pop_val,
                        "zone_area": zone_area,
                    },
                    "geometry": mapping(intersection),
                })

            if not features:
                return StepResult(step_id=step.id, error="No population features intersect with covered area")

            # 写入 GeoJSON
            output_path = os.path.abspath(self._resolve_path(
                "{output_dir}/{source_name}_pop_intersect_{radius_m}m.geojson"
            ))
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            geojson = {"type": "FeatureCollection", "features": features}
            with open(output_path, "w", encoding="utf-8") as f:
                _json.dump(geojson, f, ensure_ascii=False)

            result = StepResult(
                step_id=step.id, output_type="file_path",
                feature_count=len(features), file_path=output_path,
                shapely_geom=input_geom, engine_used="shapely_wkt::pop_intersect",
                stats={"total_population": total_population},
            )
            return result
        except Exception as _tb_e:
            _log.error("[step %s] pop_intersect TRACEBACK:\n%s", step.id, traceback.format_exc())
            raise

    # ── engine: geojson_file ─────────────────────────────────────

    def _engine_geojson_file(self, eng: EngineSpec, step: StepDef,
                               input_data: Any, boundary_data: Any) -> StepResult:
        try:
            import json as _json
            from shapely.geometry import mapping
            from qgis.core import QgsVectorLayer
        except ImportError as e:
            return StepResult(step_id=step.id, error=f"Import error: {e}")

        input_geom = self._extract_shapely_geom(input_data)

        # pop_intersect 场景：输入已是含属性的 GeoJSON，直接复制以保留人口字段
        src_geojson = None
        if isinstance(input_data, StepResult) and input_data.file_path:
            if input_data.file_path.endswith(".geojson") and os.path.exists(input_data.file_path):
                src_geojson = input_data.file_path
                if input_geom is None:
                    input_geom = self._extract_shapely_geom(input_data)

        if input_geom is None:
            input_layer = self._extract_qgis_layer(input_data)
            if input_layer:
                input_geom = self._extract_shapely_geom(input_layer)
        if input_geom is None:
            return StepResult(step_id=step.id, error="No geometry to write")

        path_template = eng.path_template or eng.params.get(
            "path_template", "{output_dir}/{source_name}_coverage_{radius_m}m.geojson"
        )
        file_path = os.path.abspath(self._resolve_path(path_template))

        try:
            if src_geojson and src_geojson != file_path:
                # 直接从 pop_intersect 复制含属性的 GeoJSON
                import shutil
                shutil.copy2(src_geojson, file_path)
            else:
                geojson = {
                    "type": "FeatureCollection",
                    "features": [{
                        "type": "Feature",
                        "properties": {},
                        "geometry": mapping(input_geom),
                    }],
                }
                with open(file_path, "w", encoding="utf-8") as f:
                    _json.dump(geojson, f, ensure_ascii=False)

            name_template = eng.params.get("name_template") or step.params.get("name_template")
            if name_template:
                layer_name = self._resolve_string(name_template)
            else:
                layer_name = f"{step.id}_geojson"
            layer = QgsVectorLayer(file_path, layer_name, "ogr")
            if not layer.isValid():
                return StepResult(step_id=step.id, error=f"Failed to load GeoJSON: {file_path}")

            if step.output_layer and step.output_layer.get("zoom_to_layer") and self.canvas and self._is_main_thread:
                self.canvas.setExtent(layer.extent())
                self.canvas.refresh()

            if step.output_layer and step.output_layer.get("add_to_project") and self._is_main_thread:
                from qgis.core import QgsProject
                QgsProject.instance().addMapLayer(layer)

            return StepResult(
                step_id=step.id, output_type="file_path",
                feature_count=layer.featureCount(), file_path=file_path,
                qgis_layer=layer, shapely_geom=input_geom, engine_used="geojson_file",
            )
        except Exception as e:
            return StepResult(step_id=step.id, error=f"GeoJSON write: {e}")

    # ── engine: qgis_memory_layer ───────────────────────────────

    def _engine_memory_layer(self, eng: EngineSpec, step: StepDef,
                               input_data: Any, boundary_data: Any) -> StepResult:
        try:
            from qgis.core import QgsVectorLayer
        except ImportError as e:
            return StepResult(step_id=step.id, error=f"Import error: {e}")

        try:
            input_layer = self._extract_qgis_layer(input_data)
            shapely_geom = None

            # 从 input_data 自动检测几何类型（修复 MultiPolygon 硬编码问题）
            if input_layer and input_layer.isValid():
                shapely_geom = self._extract_shapely_geom(input_layer)
                _log.info("[mem_layer] input=%s fc=%d", type(input_layer).__name__, input_layer.featureCount())
            else:
                shapely_geom = self._extract_shapely_geom(input_data)
                _log.info("[mem_layer] input=%s shapely=%s", type(input_data).__name__ if input_data is not None else "None",
                          type(shapely_geom).__name__ if shapely_geom is not None else "None")

            if shapely_geom is not None:
                from shapely.geometry import MultiPolygon, Polygon
                if isinstance(shapely_geom, MultiPolygon):
                    memory_type = "MultiPolygon"
                elif isinstance(shapely_geom, Polygon):
                    memory_type = "Polygon"
                else:
                    memory_type = shapely_geom.geom_type
                _log.info("[mem_layer] detected geom_type=%s is_empty=%s area=%.0f", memory_type, shapely_geom.is_empty, shapely_geom.area)
            else:
                memory_type = eng.params.get("geometry_type", "Polygon")
                _log.warning("[mem_layer] shapely_geom is None, using default type=%s", memory_type)

            crs = self.params_map.get("crs", "EPSG:4326")

            name_template = eng.params.get("name_template",
                                            step.params.get("name_template", f"{step.id}_memory"))
            layer_name = self._resolve_string(name_template)

            uri = f"{memory_type}?crs={crs}"
            _log.info("[mem_layer] creating layer uri=%s name=%s", uri, layer_name)
            layer = QgsVectorLayer(uri, layer_name, "memory")
            _log.info("[mem_layer] layer valid=%s featureCount=%s", layer.isValid(), layer.featureCount())

            if input_layer and input_layer.isValid():
                layer.dataProvider().addAttributes(input_layer.fields())
                layer.updateFields()
                features = [f for f in input_layer.getFeatures() if f.isValid()]
                layer.dataProvider().addFeatures(features)
                _log.info("[mem_layer] copied %d features from input layer", len(features))
            else:
                if shapely_geom is not None and not shapely_geom.is_empty:
                    from qgis.core import QgsFeature, QgsGeometry
                    feat = QgsFeature()
                    wkt_str = shapely_geom.wkt
                    _log.info("[mem_layer] creating feat from wkt len=%d", len(wkt_str))
                    feat.setGeometry(QgsGeometry.fromWkt(wkt_str))
                    add_ok = layer.dataProvider().addFeatures([feat])
                    _log.info("[mem_layer] addFeatures result=%s fc=%d", add_ok, layer.featureCount())
                else:
                    _log.warning("[mem_layer] shapely_geom is None or empty, no features added")

            layer.updateExtents()

            if step.output_layer and step.output_layer.get("add_to_project") and self._is_main_thread:
                from qgis.core import QgsProject
                QgsProject.instance().addMapLayer(layer)
                _log.info("[mem_layer] added to project")

            if step.output_layer and step.output_layer.get("zoom_to_layer") and self.canvas and self._is_main_thread:
                self.canvas.setExtent(layer.extent())
                self.canvas.refresh()

            result = StepResult(
                step_id=step.id, output_type="layer",
                feature_count=layer.featureCount(), qgis_layer=layer,
                shapely_geom=shapely_geom, engine_used="qgis_memory_layer",
            )
            _log.info("[mem_layer] SUCCESS fc=%d qgis_layer=%s", result.feature_count, result.qgis_layer is not None)
            return result

        except Exception as e:
            _log.error("[mem_layer] EXCEPTION: %s", e)
            traceback.print_exc()
            return StepResult(step_id=step.id, error=f"qgis_memory_layer: {e}")

    # ── Helpers ──────────────────────────────────────────────────

    def _extract_shapely_geom(self, data: Any) -> Any:
        """从 QgsVectorLayer / Shapely / file_path 提取 Shapely 几何。"""
        if data is None:
            return None
        try:
            from shapely.geometry.base import BaseGeometry
            if isinstance(data, BaseGeometry):
                return data
        except ImportError:
            pass
        try:
            from qgis.core import QgsVectorLayer
            if isinstance(data, QgsVectorLayer):
                from shapely import wkt as _wkt
                from shapely.ops import unary_union
                geoms = [_wkt.loads(f.geometry().asWkt())
                         for f in data.getFeatures()
                         if f.geometry() and not f.geometry().isEmpty()]
                return unary_union(geoms) if geoms else None
        except ImportError:
            pass
        if isinstance(data, StepResult):
            if data.shapely_geom:
                return data.shapely_geom
            if data.qgis_layer:
                return self._extract_shapely_geom(data.qgis_layer)
            if data.file_path:
                return self._extract_shapely_geom(data.file_path)
        if isinstance(data, str) and data.endswith(".geojson"):
            try:
                import json as _json
                from shapely.geometry import shape
                from shapely.ops import unary_union
                with open(data, "r", encoding="utf-8") as f:
                    gj = _json.load(f)
                if gj.get("type") == "FeatureCollection":
                    geoms = [shape(feat["geometry"]) for feat in gj.get("features", [])]
                    return unary_union(geoms) if geoms else None
            except Exception:
                pass
        return None

    def _extract_shapely_or_layer(self, data: Any) -> Any:
        """优先 Shapely，fallback QgsVectorLayer → Shapely。兼容跨步数据传递中
        input/input_boundary 可能是 shapely 几何、StepResult 或 QgsVectorLayer 的情况。"""
        geom = self._extract_shapely_geom(data)
        if geom is not None:
            return geom
        layer = self._extract_qgis_layer(data)
        if layer is not None:
            return self._extract_shapely_geom(layer)
        return None

    def _extract_qgis_layer(self, data: Any) -> Any:
        """提取 QgsVectorLayer。"""
        if data is None:
            return None
        try:
            from qgis.core import QgsVectorLayer
            if isinstance(data, QgsVectorLayer):
                return data
        except ImportError:
            pass
        if isinstance(data, StepResult):
            if data.qgis_layer:
                return data.qgis_layer
            if data.file_path:
                try:
                    from qgis.core import QgsVectorLayer
                    lyr = QgsVectorLayer(data.file_path, "loaded", "ogr")
                    if lyr.isValid():
                        return lyr
                except ImportError:
                    pass
        if isinstance(data, str) and os.path.exists(data):
            try:
                from qgis.core import QgsVectorLayer
                lyr = QgsVectorLayer(data, "loaded", "ogr")
                if lyr.isValid():
                    return lyr
            except ImportError:
                pass
        return None

    def _extract_output_value(self, result: StepResult) -> Any:
        if result.output_type == "layer":
            return result.qgis_layer
        elif result.output_type == "shapely_geom":
            return result.shapely_geom
        elif result.output_type == "stats_dict":
            return result.stats
        elif result.output_type == "file_path":
            return result.file_path
        return result

    def _resolve_step_input(self, step: StepDef, step_outputs: Dict[str, Any]) -> Any:
        if step.input_source is None:
            return self.params_map.get("source_layer")
        return self._resolve_ref(step.input_source, step_outputs)

    def _resolve_ref(self, ref: str, step_outputs: Dict[str, Any]) -> Any:
        """解析 $source_layer / $buffer.output 等引用。"""
        raw_key = ref.lstrip("$")
        # 处理 .output / .geom / .stats / .path 后缀
        suffix = None
        for sfx in (".output", ".geom", ".stats", ".path"):
            if raw_key.endswith(sfx):
                suffix = sfx
                key = raw_key[: -len(sfx)]
                break
        else:
            key = raw_key

        def _lookup(k: str):
            if k in self.params_map:
                return self.params_map[k]
            if k in step_outputs:
                return step_outputs[k]
            if k in self.step_results:
                return self.step_results[k]
            return None

        val = _lookup(key)
        if val is None:
            # fallback: try with $ prefix
            val = _lookup(f"${key}")
        if val is None:
            return None

        if isinstance(val, StepResult):
            if suffix == ".geom":
                return self._extract_shapely_geom(val)
            elif suffix == ".stats":
                return val.stats
            elif suffix == ".path":
                return val.file_path
            # .output or no suffix: default extract
            return self._extract_output_value(val)
        return val

    def _resolve_path(self, template: str) -> str:
        t = template
        t = t.replace("{output_dir}", self.template.output_base if self.template else ".")
        t = t.replace("{source_name}", self.params_map.get("source_name", "output"))
        t = t.replace("{radius_m}", str(int(self.params_map.get("radius_m", 500))))
        parent = os.path.dirname(t)
        if parent:
            os.makedirs(parent, exist_ok=True)
        return t

    def _resolve_string(self, template: str) -> str:
        t = template
        for key, val in self.params_map.items():
            t = t.replace(f"{{{key}}}", str(val))
        return t

    def _validate(self, expr: Optional[str], result: StepResult) -> bool:
        if result.error:
            return False
        if not expr:
            return True
        return _parse_and_eval(expr, result)

    def _sanitize_name(self, name: str) -> str:
        return re.sub(r'[<>:"/\\|?*\s]', '_', name).strip('_')

    def _find_layer(self, name: str) -> Any:
        if self._find_layer_fn:
            return self._find_layer_fn(name)
        try:
            from qgis.core import QgsProject
            layers = QgsProject.instance().mapLayersByName(name)
            if layers:
                return layers[0]
            for lyr in QgsProject.instance().mapLayers().values():
                if hasattr(lyr, 'source') and name in lyr.source():
                    return lyr
        except Exception:
            pass
        return None
