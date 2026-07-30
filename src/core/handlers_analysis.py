"""handlers_analysis — 空间分析处理器 Mixin。

从 instruction_mapper.py 抽离的空间关联和覆盖率分析 handler 方法，
通过 Mixin 继承注入到 InstructionMapper 中。
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict

_log = logging.getLogger("instruction_mapper")


class HandlersAnalysisMixin:
    """空间分析处理器（5 handlers）。

    以 Mixin 形式被 InstructionMapper 继承。
    - _handle_spatial_join：空间关联分析（native:joinattributesbylocation）
    - _handle_coverage_analysis：覆盖率分析（Pipeline 引擎）
    - _handle_gap_analysis：盲区分析（Pipeline 引擎）
    - _handle_population_coverage：人口覆盖率分析（Pipeline 引擎）
    - _handle_building_risk_analysis：建筑倒塌风险分析（Pipeline 引擎）
    """

    def _handle_spatial_join(self, canvas=None, project=None,
                              target_layer: str = "", join_layer: str = "",
                              predicate: str = "intersects",
                              join_fields: Any = None,
                              summary_mode: str = "first",
                              **kwargs) -> Dict[str, Any]:
        """空间关联分析：将 join_layer 的属性按空间关系赋予 target_layer。

        Parameters
        ----------
        target_layer : str
            目标图层名称（点/面），属性将写入此图层。
        join_layer : str
            关联图层名称（面/线/点），提供属性。
        predicate : str
            空间谓词：intersects / within / contains。
        join_fields : list[str] or None
            要关联的字段列表，空/None 表示全部字段。
        summary_mode : str
            汇总方式：first（取第一个匹配）/ max（取最大重叠）。
        """
        import processing
        from qgis.core import QgsVectorLayer

        if not target_layer or not join_layer:
            return {"success": False, "message": "spatial_join 需要 target_layer 和 join_layer 两个参数"}

        tl = self._find_layer(project, target_layer)
        if tl is None:
            return {"success": False, "message": f"未找到目标图层：{target_layer}"}
        err = self._check_vector(tl)
        if err:
            return err

        jl = self._find_layer(project, join_layer)
        if jl is None:
            return {"success": False, "message": f"未找到关联图层：{join_layer}"}
        err = self._check_vector(jl)
        if err:
            return err

        # ── 防回归守护线：sidecar 配套文件完整性校验 ──
        jl_source = jl.source()
        if jl_source and jl_source.lower().endswith(".shp"):
            complete, missing = self._validate_shapefile_sidecars(jl_source)
            if not complete:
                return {
                    "success": False,
                    "message": (
                        f"关联图层配套文件不完整：{jl.name()}。"
                        f"缺失：{', '.join(os.path.basename(p) for p in missing[:3])}"
                        f"{' 等' if len(missing) > 3 else ''}。"
                        f"请确保 .shp / .shx / .dbf 三件套齐备。"
                    ),
                }

        # 重新从磁盘创建图层，彻底绕过 QgsVectorLayer 内存缓存的字段定义。
        jl_name = jl.name()
        jl = QgsVectorLayer(jl_source, jl_name, "ogr")
        if not jl.isValid():
            return {"success": False, "message": f"无法从数据源重新加载关联图层：{jl_source}"}

        # 空间谓词映射（QGIS native:joinattributesbylocation 的 PREDICATE 索引）
        predicate_map = {
            "intersects": 0, "contains": 1, "equals": 2,
            "touches": 3, "overlaps": 4, "within": 5, "crosses": 6,
        }
        pred_int = predicate_map.get(predicate)
        if pred_int is None:
            return {"success": False,
                    "message": f"不支持的空间谓词：{predicate}，可选：{list(predicate_map.keys())}"}

        # QGIS METHOD 映射
        if summary_mode in ("max", "largest_overlap"):
            method = 2
        elif summary_mode in ("one_to_many", "separate"):
            method = 0
        else:
            method = 1

        target_count = tl.featureCount()
        join_layer_fields = [f.name() for f in jl.fields()]

        jf = join_fields if join_fields else None
        if jf is not None and not isinstance(jf, list):
            jf = None

        # ── 验证 join_fields ──
        if jf is not None:
            jl_field_names = [f.name() for f in jl.fields()]
            valid_jf = [f for f in jf if f in jl_field_names]
            if not valid_jf:
                _log.warning(
                    f"spatial_join: LLM requested join_fields={jf} "
                    f"but none exist in join layer; falling back to all fields"
                )
                jf = None
            elif len(valid_jf) != len(jf):
                invalid = set(jf) - set(valid_jf)
                _log.warning(
                    f"spatial_join: dropping invalid fields {invalid}, "
                    f"keeping {valid_jf}"
                )
                jf = valid_jf

        _log.info(
            f"spatial_join: target={tl.name()}({target_count} feat) "
            f"join={jl.name()}({jl.featureCount()} feat) "
            f"predicate={predicate}({pred_int}) method={method} "
            f"join_fields={jf} crs_target={tl.crs().authid()}"
        )

        params_dict = {
            "INPUT": tl,
            "JOIN": jl,
            "PREDICATE": [pred_int],
            "METHOD": method,
            "DISCARD_NONMATCHING": False,
            "PREFIX": "",
            "OUTPUT": "memory:",
        }
        if jf is not None:
            params_dict["JOIN_FIELDS"] = jf

        # ── DEBUG: 诊断字段传递 ──
        _debug_tl_fields = [f.name() for f in tl.fields()]
        _debug_jl_fields = [f.name() for f in jl.fields()]
        _log.warning(
            f"[SPATIAL_JOIN_DEBUG] BEFORE processing.run — "
            f"INPUT={tl.name()} fields({len(_debug_tl_fields)}): {_debug_tl_fields} | "
            f"JOIN={jl.name()} fields({len(_debug_jl_fields)}): {_debug_jl_fields} | "
            f"JOIN source={jl.source()} valid={jl.isValid()} feat={jl.featureCount()}"
        )

        try:
            result = processing.run("native:joinattributesbylocation", params_dict)
        except Exception as e:
            _log.exception("spatial_join 执行失败")
            return {"success": False, "message": f"空间关联执行失败：{e}"}

        joined_layer = result.get("OUTPUT")
        if joined_layer is None:
            return {"success": False, "message": "空间关联结果为空"}

        # ── DEBUG: 诊断结果字段 ──
        _debug_out_fields = [f.name() for f in joined_layer.fields()]
        _debug_new_fields = [fn for fn in _debug_out_fields if fn not in _debug_tl_fields]
        _log.warning(
            f"[SPATIAL_JOIN_DEBUG] AFTER processing.run — "
            f"OUTPUT fields({len(_debug_out_fields)}): {_debug_out_fields} | "
            f"NEW fields({len(_debug_new_fields)}): {_debug_new_fields}"
        )

        joined_count = joined_layer.featureCount()
        unmatched_count = 0
        join_field_names = [f.name() for f in joined_layer.fields()]
        new_fields = [fn for fn in join_field_names if fn not in [f.name() for f in tl.fields()]]
        if new_fields:
            first_new_idx = joined_layer.fields().indexOf(new_fields[0])
            for feat in joined_layer.getFeatures():
                if feat.attribute(first_new_idx) is None:
                    unmatched_count += 1

        tail_fields = join_field_names[-10:] if len(join_field_names) > 10 else join_field_names

        proj = project or QgsProject.instance()
        joined_layer.setName(f"{tl.name()}_join_{jl.name()}_{summary_mode}")
        proj.addMapLayer(joined_layer)

        if canvas:
            canvas.setExtent(joined_layer.extent())
            canvas.refresh()

        return {
            "success": True,
            "message": (
                f"空间关联完成，新增图层：{joined_layer.name()} "
                f"（共 {joined_count} 个要素，其中 {joined_count - unmatched_count} 个匹配 / {unmatched_count} 个未匹配）"
            ),
            "stats": {
                "target_count": target_count,
                "joined_count": joined_count,
                "matched_count": joined_count - unmatched_count,
                "unmatched_count": unmatched_count,
                "join_layer_fields": join_layer_fields,
                "result_tail_fields": tail_fields,
            },
            "joined_layer": joined_layer.name(),
        }

    def _handle_coverage_analysis(self, canvas=None, project=None,
                                   source_layer: str = "", boundary_layer: str = "",
                                   radius_m: float = 500.0, selected_only: bool = False,
                                   **kwargs) -> Dict[str, Any]:
        """覆盖率分析：声明式 Pipeline 引擎执行。

        路由到 PipelineExecutor，由 coverage_analysis.json 模板驱动 8 步全链路。
        """
        if not source_layer or not boundary_layer:
            return {"success": False,
                    "message": "coverage_analysis 需要 source_layer 和 boundary_layer 两个参数"}

        template_path = os.path.join(os.path.dirname(__file__), "templates", "coverage_analysis.json")

        from core.pipeline_executor import PipelineExecutor

        executor = PipelineExecutor()
        return executor.execute(
            template_path=template_path,
            source_layer_name=source_layer,
            boundary_layer_name=boundary_layer,
            radius_m=radius_m,
            project=project,
            canvas=canvas,
            _find_layer_fn=lambda name: self._find_layer(project, name),
        )

    def _handle_population_coverage(self, canvas=None, project=None,
                                     source_layer: str = "", boundary_layer: str = "",
                                     population_layer: str = "", population_field: str = "",
                                     radius_m: float = 500.0, selected_only: bool = False,
                                     **kwargs) -> Dict[str, Any]:
        """人口覆盖率分析：声明式 Pipeline 引擎执行。

        路由到 PipelineExecutor，由 population_coverage.json 模板驱动 9 步全链路。
        """
        if not source_layer or not boundary_layer:
            return {"success": False,
                    "message": "population_coverage 需要 source_layer 和 boundary_layer 两个参数"}
        if not population_layer:
            return {"success": False,
                    "message": "population_coverage 需要 population_layer 参数"}
        if not population_field:
            return {"success": False,
                    "message": "population_coverage 需要 population_field 参数"}

        template_path = os.path.join(os.path.dirname(__file__), "templates", "population_coverage.json")

        from core.pipeline_executor import PipelineExecutor

        executor = PipelineExecutor()
        return executor.execute(
            template_path=template_path,
            source_layer_name=source_layer,
            boundary_layer_name=boundary_layer,
            population_layer_name=population_layer,
            population_field=population_field,
            radius_m=radius_m,
            project=project,
            canvas=canvas,
            _find_layer_fn=lambda name: self._find_layer(project, name),
        )

    def _handle_building_risk_analysis(self, canvas=None, project=None,
                                        intensity_layer: str = "",
                                        population_layer: str = "",
                                        population_field: str = "",
                                        intensity_field: str = "",
                                        boundary_layer: str = "",
                                        **kwargs) -> Dict[str, Any]:
        """建筑倒塌风险分析：声明式 Pipeline 引擎执行。

        路由到 PipelineExecutor，由 building_risk.json 模板驱动 4 步全链路：
        震度裁剪 → 震度×人口交集 → 风险统计 → 输出图层。
        """
        if not intensity_layer:
            return {"success": False, "message": "building_risk_analysis 需要 intensity_layer 参数"}
        if not population_layer:
            return {"success": False, "message": "building_risk_analysis 需要 population_layer 参数"}

        # 如果未指定人口字段，从人口图层中自动探测
        if not population_field:
            pop_layer = self._find_layer(project, population_layer)
            if pop_layer and hasattr(pop_layer, 'fields'):
                field_hints = ["pop", "population", "人口", "T_POP", "total", "SUM"]
                for hint in field_hints:
                    idx = pop_layer.fields().indexFromName(hint)
                    if idx >= 0:
                        population_field = hint
                        _log.info("自动探测人口字段：%s", population_field)
                        break
                if not population_field:
                    # 兜底：尝试找第一个数值型字段
                    for f in pop_layer.fields():
                        if f.isNumeric():
                            population_field = f.name()
                            _log.info("兜底人口字段（首个数值型）：%s", population_field)
                            break

        if not population_field:
            return {"success": False, "message": "building_risk_analysis 需要 population_field 参数，无法自动探测"}

        # 建筑风险模式：自动探测震度字段（J-SHIS T30_I50_PS 等格式）
        if not intensity_field:
            int_layer = self._find_layer(project, intensity_layer)
            if int_layer and hasattr(int_layer, 'fields'):
                from core.seismic_situation_map import JMA_INTENSITY_FIELD_PATTERN
                for f in int_layer.fields():
                    if JMA_INTENSITY_FIELD_PATTERN.match(f.name()):
                        intensity_field = f.name()
                        _log.info("自动探测震度字段：%s", intensity_field)
                        break

        template_path = os.path.join(os.path.dirname(__file__), "templates", "building_risk.json")

        from core.pipeline_executor import PipelineExecutor

        executor = PipelineExecutor()
        return executor.execute(
            template_path=template_path,
            intensity_layer_name=intensity_layer,
            population_layer_name=population_layer,
            population_field=population_field,
            intensity_field=intensity_field,
            boundary_layer_name=boundary_layer,
            project=project,
            canvas=canvas,
            _find_layer_fn=lambda name: self._find_layer(project, name),
        )

    def _handle_gap_analysis(self, canvas=None, project=None,
                              source_layer: str = "", boundary_layer: str = "",
                              radius_m: float = 500.0, selected_only: bool = False,
                              **kwargs) -> Dict[str, Any]:
        """盲区分析：声明式 Pipeline 引擎执行。

        路由到 PipelineExecutor，由 gap_analysis.json 模板驱动 9 步全链路。
        """
        if not source_layer or not boundary_layer:
            return {"success": False,
                    "message": "gap_analysis 需要 source_layer 和 boundary_layer 两个参数"}

        template_path = os.path.join(os.path.dirname(__file__), "templates", "gap_analysis.json")

        from core.pipeline_executor import PipelineExecutor

        executor = PipelineExecutor()
        return executor.execute(
            template_path=template_path,
            source_layer_name=source_layer,
            boundary_layer_name=boundary_layer,
            radius_m=radius_m,
            project=project,
            canvas=canvas,
            _find_layer_fn=lambda name: self._find_layer(project, name),
        )
