"""guards — 前置守卫检查模块。

从 pipeline_executor 抽离，可注册/可扩展的守卫条件体系。
每个守卫条件通过 GUARD_REGISTRY 映射到检查函数，
GuardChecker 负责遍历执行并返回结构化报告。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List


# ── dataclasses ────────────────────────────────────────────────

@dataclass
class GuardDef:
    """单条前置守卫条件。

    on_fail 为空字符串时跟随模板级 guards_on_fail；
    显式声明 "error" / "warn" 时覆盖模板级（单条覆盖）。
    """
    condition: str                       # crs_projected | source_is_vector | source_is_point | ...
    params: Dict[str, Any] = field(default_factory=dict)
    on_fail: str = ""                    # ""=跟随模板级 | "error" | "warn"


@dataclass
class GuardCheck:
    """模板级 guards 声明（fail-closed：缺省 on_fail="error"）。"""
    guards: List[GuardDef] = field(default_factory=list)
    on_fail: str = "error"              # "warn" | "error"


@dataclass
class GuardResult:
    """单条守卫检查结果。"""
    condition: str
    passed: bool
    message: str = ""                   # 失败时的诊断信息
    auto_skipped: bool = False          # 被 auto_reproject 等机制跳过
    blocking: bool = True               # fail-closed：是否阻断执行（on_fail=="error" 时为 True）


@dataclass
class GuardReport:
    """批量守卫检查汇总报告。"""
    all_passed: bool
    results: List[GuardResult] = field(default_factory=list)

    @property
    def failed(self) -> List[GuardResult]:
        return [r for r in self.results if not r.passed and not r.auto_skipped]

    @property
    def blocking_failed(self) -> List[GuardResult]:
        """失败且需阻断执行的守卫（on_fail 判定为 "error"）。"""
        return [r for r in self.results if not r.passed and not r.auto_skipped and r.blocking]


# ── 守卫函数（统一签名：(params_map, guard_def) → GuardResult）─

def _guard_source_is_vector(params_map: Dict[str, Any],
                            guard_def: GuardDef) -> GuardResult:
    """源图层必须存在且为矢量图层（fail-closed：栅格/None/未知 → failed 并报具体原因）。

    P2-0 修复：原实现为空壳永远 passed，外部评审 P0-1 定性。
    现在检查 source_layer 是否存在，并通过 QGIS 原生 type() 接口
    区分矢量 / 栅格 / 其他类型；无 QGIS 环境时使用 QGIS 3.x 稳定常量。
    """
    source = params_map.get("source_layer")
    if source is None:
        return GuardResult(
            condition=guard_def.condition, passed=False,
            message="源图层不存在（source_layer 未提供或未匹配到图层）"
        )

    # QGIS 图层类型常量（QGIS 3.x 稳定值：VectorLayer=0, RasterLayer=1）
    try:
        from qgis.core import QgsMapLayer
        vector_type = QgsMapLayer.VectorLayer
        raster_type = QgsMapLayer.RasterLayer
    except ImportError:
        vector_type, raster_type = 0, 1

    # 优先 QGIS 原生 type() 接口（真实图层与测试 mock 均可用）
    if hasattr(source, "type"):
        try:
            layer_type = source.type()
        except Exception as e:
            return GuardResult(
                condition=guard_def.condition, passed=False,
                message=f"源图层 type() 检查异常: {e}"
            )
        if layer_type == vector_type:
            return GuardResult(condition=guard_def.condition, passed=True)
        if layer_type == raster_type:
            return GuardResult(
                condition=guard_def.condition, passed=False,
                message=f"源图层为栅格图层（type()={layer_type}），需要矢量图层"
            )
        return GuardResult(
            condition=guard_def.condition, passed=False,
            message=f"源图层类型不支持（type()={layer_type}），需要矢量图层"
        )

    # 无 type() 接口的对象 → 按无效处理（fail-closed，不再默认通过）
    return GuardResult(
        condition=guard_def.condition, passed=False,
        message=f"源图层不是有效 QgsVectorLayer（对象类型={type(source).__name__}）"
    )


def _guard_source_is_point(params_map: Dict[str, Any],
                           guard_def: GuardDef) -> GuardResult:
    """源图层必须为点图层。"""
    source = params_map.get("source_layer")
    expected = guard_def.params.get("geometry_type", 0)
    if source and hasattr(source, 'geometryType') and source.geometryType() != expected:
        return GuardResult(
            condition=guard_def.condition, passed=False,
            message=f"源图层 geometryType={source.geometryType()}，期望={expected} (Point)"
        )
    return GuardResult(condition=guard_def.condition, passed=True)


def _guard_boundary_is_polygon(params_map: Dict[str, Any],
                                guard_def: GuardDef) -> GuardResult:
    """边界图层必须为面图层。"""
    boundary = params_map.get("boundary_layer")
    expected = guard_def.params.get("geometry_type", 2)
    if boundary and hasattr(boundary, 'geometryType') and boundary.geometryType() != expected:
        return GuardResult(
            condition=guard_def.condition, passed=False,
            message=f"边界图层 geometryType={boundary.geometryType()}，期望={expected} (Polygon)"
        )
    return GuardResult(condition=guard_def.condition, passed=True)


def _guard_crs_projected(params_map: Dict[str, Any],
                          guard_def: GuardDef) -> GuardResult:
    """源图层必须是投影坐标系。"""
    source = params_map.get("source_layer")
    if source and hasattr(source, 'crs') and source.crs().isGeographic():
        return GuardResult(
            condition=guard_def.condition, passed=False,
            message="源图层为地理坐标系，需要投影坐标系"
        )
    return GuardResult(condition=guard_def.condition, passed=True)


def _guard_shapely_available(params_map: Dict[str, Any],
                              guard_def: GuardDef) -> GuardResult:
    """检查 shapely 库是否可导入。"""
    try:
        import shapely  # noqa
        return GuardResult(condition=guard_def.condition, passed=True)
    except ImportError:
        return GuardResult(
            condition=guard_def.condition, passed=False,
            message="shapely 库未安装"
        )


def _guard_boundary_fc_limit(params_map: Dict[str, Any],
                              guard_def: GuardDef) -> GuardResult:
    """边界图层要素数不得超过上限。"""
    boundary = params_map.get("boundary_layer")
    limit = guard_def.params.get("max", 10000)
    if boundary and hasattr(boundary, 'featureCount') and boundary.featureCount() > limit:
        return GuardResult(
            condition=guard_def.condition, passed=False,
            message=f"边界图层要素数 {boundary.featureCount()} 超过上限 {limit}"
        )
    return GuardResult(condition=guard_def.condition, passed=True)


def _guard_crs_match(params_map: Dict[str, Any],
                      guard_def: GuardDef) -> GuardResult:
    """源图层与边界图层的坐标系必须一致；auto_reproject=true 时真实重投影边界图层。"""
    source = params_map.get("source_layer")
    boundary = params_map.get("boundary_layer")
    if not (source and boundary
            and hasattr(source, 'crs') and hasattr(boundary, 'crs')):
        return GuardResult(condition=guard_def.condition, passed=True)

    source_crs = source.crs()
    boundary_crs = boundary.crs()

    # CRS 为空无法判断/重投影时显式失败，禁止静默跳过
    if not source_crs.isValid() or not boundary_crs.isValid():
        return GuardResult(
            condition=guard_def.condition, passed=False,
            message=(
                f"CRS 无效: source={source_crs.authid() or 'unknown'}, "
                f"boundary={boundary_crs.authid() or 'unknown'}，无法执行 CRS 匹配/重投影"
            )
        )

    if source_crs.authid() == boundary_crs.authid():
        return GuardResult(condition=guard_def.condition, passed=True)

    # 未启用 auto_reproject 时按严格匹配失败
    if not guard_def.params.get("auto_reproject"):
        return GuardResult(
            condition=guard_def.condition, passed=False,
            message=f"CRS 不匹配: source={source_crs.authid()}, boundary={boundary_crs.authid()}"
        )

    # auto_reproject=true：真实重投影 boundary 到 source CRS，并写回 params_map
    try:
        from qgis.core import (
            QgsCoordinateTransform, QgsVectorLayer, QgsProject, QgsWkbTypes,
            QgsFeature, QgsGeometry,
        )
        xform = QgsCoordinateTransform(boundary_crs, source_crs, QgsProject.instance())
        mem_layer = QgsVectorLayer(
            f"{QgsWkbTypes.displayString(boundary.wkbType())}?crs={source_crs.authid()}",
            f"{boundary.name() or 'boundary'}_reprojected", "memory"
        )
        mem_layer.dataProvider().addAttributes(boundary.fields())
        mem_layer.updateFields()

        feats = []
        for feat in boundary.getFeatures():
            geom = feat.geometry()
            if geom is None or geom.isEmpty():
                continue
            # 显式拷贝 QgsFeature：getFeatures() 迭代复用同一对象，直接 append 会导致多要素退化
            copied = QgsFeature(feat)
            copied_geom = QgsGeometry(copied.geometry())
            copied_geom.transform(xform)
            copied.setGeometry(copied_geom)
            feats.append(copied)

        if not feats:
            return GuardResult(
                condition=guard_def.condition, passed=False,
                message=(
                    f"auto_reproject 重投影后边界图层无有效要素 "
                    f"(boundary CRS={boundary_crs.authid()} -> {source_crs.authid()})"
                )
            )

        mem_layer.dataProvider().addFeatures(feats)
        mem_layer.updateExtents()
        params_map["boundary_layer"] = mem_layer
        return GuardResult(
            condition=guard_def.condition, passed=True,
            message=(
                f"auto_reproject: boundary 已从 {boundary_crs.authid()} "
                f"重投影到 {source_crs.authid()}"
            )
        )
    except Exception as e:
        return GuardResult(
            condition=guard_def.condition, passed=False,
            message=f"auto_reproject 重投影失败: {e}"
        )


def _guard_population_is_polygon(params_map: Dict[str, Any],
                                  guard_def: GuardDef) -> GuardResult:
    """人口图层必须为矢量面图层。"""
    pop_layer = params_map.get("population_layer")
    expected = guard_def.params.get("geometry_type", 2)
    if pop_layer and hasattr(pop_layer, 'geometryType') and pop_layer.geometryType() != expected:
        return GuardResult(
            condition=guard_def.condition, passed=False,
            message=f"人口图层 geometryType={pop_layer.geometryType()}，期望={expected} (Polygon)"
        )
    return GuardResult(condition=guard_def.condition, passed=True)


def _guard_intensity_is_polygon(params_map: Dict[str, Any],
                                 guard_def: GuardDef) -> GuardResult:
    """震度图层必须为矢量面图层。"""
    intensity_layer = params_map.get("intensity_layer")
    expected = guard_def.params.get("geometry_type", 2)
    if intensity_layer and hasattr(intensity_layer, 'geometryType') and intensity_layer.geometryType() != expected:
        return GuardResult(
            condition=guard_def.condition, passed=False,
            message=f"震度图层 geometryType={intensity_layer.geometryType()}，期望={expected} (Polygon)"
        )
    return GuardResult(condition=guard_def.condition, passed=True)


# ── 注册表 ─────────────────────────────────────────────────────

GUARD_REGISTRY: Dict[str, Callable] = {
    "source_is_vector":        _guard_source_is_vector,
    "source_is_point":         _guard_source_is_point,
    "boundary_is_polygon":     _guard_boundary_is_polygon,
    "population_is_polygon":   _guard_population_is_polygon,
    "intensity_is_polygon":    _guard_intensity_is_polygon,
    "crs_projected":           _guard_crs_projected,
    "shapely_available":       _guard_shapely_available,
    "boundary_fc_limit":       _guard_boundary_fc_limit,
    "crs_match":               _guard_crs_match,
}


def register_guard(condition: str, func: Callable) -> None:
    """注册自定义守卫条件。"""
    GUARD_REGISTRY[condition] = func


# ── GuardChecker ───────────────────────────────────────────────

class GuardChecker:
    """守卫检查器。

    用法:
        checker = GuardChecker(params_map)
        report = checker.check(template.guards)
        if not report.all_passed and template.guards.on_fail == "error":
            return False
    """

    def __init__(self, params_map: Dict[str, Any]):
        self.params_map = params_map

    def check(self, guard_check: GuardCheck) -> GuardReport:
        """遍历所有守卫条件并返回汇总报告。

        fail-closed：逐条判定 effective on_fail（守卫级覆盖模板级，
        未声明时跟随模板级），决定该条失败是否阻断执行。
        """
        if not guard_check or not guard_check.guards:
            return GuardReport(all_passed=True)

        results: List[GuardResult] = []
        for guard_def in guard_check.guards:
            result = self._check_one(guard_def)
            effective_on_fail = guard_def.on_fail or guard_check.on_fail
            result.blocking = (effective_on_fail == "error")
            results.append(result)

        all_passed = all(r.passed for r in results)
        return GuardReport(all_passed=all_passed, results=results)

    def _check_one(self, guard_def: GuardDef) -> GuardResult:
        func = GUARD_REGISTRY.get(guard_def.condition)
        if func is None:
            # P2-0 fail-closed：未知守卫条件不再默认通过
            return GuardResult(
                condition=guard_def.condition, passed=False,
                message=f"未知守卫条件 '{guard_def.condition}'"
            )
        return func(self.params_map, guard_def)
