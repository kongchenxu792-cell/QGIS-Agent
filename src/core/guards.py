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
    """单条前置守卫条件。"""
    condition: str                       # crs_projected | source_is_vector | source_is_point | ...
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class GuardCheck:
    """模板级 guards 声明。"""
    guards: List[GuardDef] = field(default_factory=list)
    on_fail: str = "warn"               # "warn" | "error"


@dataclass
class GuardResult:
    """单条守卫检查结果。"""
    condition: str
    passed: bool
    message: str = ""                   # 失败时的诊断信息
    auto_skipped: bool = False          # 被 auto_reproject 等机制跳过


@dataclass
class GuardReport:
    """批量守卫检查汇总报告。"""
    all_passed: bool
    results: List[GuardResult] = field(default_factory=list)

    @property
    def failed(self) -> List[GuardResult]:
        return [r for r in self.results if not r.passed and not r.auto_skipped]


# ── 守卫函数（统一签名：(params_map, guard_def) → GuardResult）─

def _guard_source_is_vector(params_map: Dict[str, Any],
                            guard_def: GuardDef) -> GuardResult:
    """源图层必须为矢量类型（默认通过，名称占位）。"""
    return GuardResult(condition=guard_def.condition, passed=True)


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
    """源图层与边界图层的坐标系必须一致。"""
    # auto_reproject 时直接跳过检查
    if guard_def.params.get("auto_reproject"):
        return GuardResult(
            condition=guard_def.condition, passed=True,
            auto_skipped=True, message="auto_reproject 启用，跳过 CRS 检查"
        )

    source = params_map.get("source_layer")
    boundary = params_map.get("boundary_layer")
    if (source and boundary
            and hasattr(source, 'crs') and hasattr(boundary, 'crs')
            and source.crs().authid() != boundary.crs().authid()):
        return GuardResult(
            condition=guard_def.condition, passed=False,
            message=f"CRS 不匹配: source={source.crs().authid()}, boundary={boundary.crs().authid()}"
        )
    return GuardResult(condition=guard_def.condition, passed=True)


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
        """遍历所有守卫条件并返回汇总报告。"""
        if not guard_check or not guard_check.guards:
            return GuardReport(all_passed=True)

        results: List[GuardResult] = []
        for guard_def in guard_check.guards:
            result = self._check_one(guard_def)
            results.append(result)

        all_passed = all(r.passed for r in results)
        return GuardReport(all_passed=all_passed, results=results)

    def _check_one(self, guard_def: GuardDef) -> GuardResult:
        func = GUARD_REGISTRY.get(guard_def.condition)
        if func is None:
            return GuardResult(
                condition=guard_def.condition, passed=True,
                message=f"未知守卫条件 '{guard_def.condition}'，默认通过"
            )
        return func(self.params_map, guard_def)
