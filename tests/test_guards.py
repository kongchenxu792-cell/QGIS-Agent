"""test_guards — 守卫模块边界条件测试 (~12 用例)。

测试 GuardChecker / GUARD_REGISTRY 各项守卫条件，
使用 mock 对象替代真实 QGIS 图层。
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.guards import (
    GuardChecker,
    GuardDef,
    GuardCheck,
    GuardResult,
    GuardReport,
    GUARD_REGISTRY,
    _guard_source_is_vector,
    _guard_source_is_point,
    _guard_boundary_is_polygon,
    _guard_crs_projected,
    _guard_crs_match,
    _guard_shapely_available,
    _guard_boundary_fc_limit,
)


def make_mock_layer(name="test_layer", geom_type=0, fields=None, crs_authid="EPSG:3857"):
    """构造 mock 图层对象。

    geom_type: 0=Point, 1=Line, 2=Polygon
    """
    layer = MagicMock()
    layer.name.return_value = name
    layer.geometryType.return_value = geom_type
    layer.fields.return_value = MagicMock()
    if fields is not None:
        layer.fields().names.return_value = fields
    else:
        layer.fields().names.return_value = ["id", "name", "population"]

    crs = MagicMock()
    crs.authid.return_value = crs_authid
    crs.isValid.return_value = True
    crs.isGeographic.return_value = ("4326" in crs_authid)
    layer.crs.return_value = crs

    layer.featureCount.return_value = 100
    return layer


class TestGuards(unittest.TestCase):
    """守卫条件单元测试。"""

    # ── source_is_point ─────────────────────────────────────

    def test_source_is_point_passes_for_point_layer(self):
        layer = make_mock_layer(geom_type=0)
        params_map = {"source_layer": layer}
        guard_def = GuardDef(condition="source_is_point", params={"geometry_type": 0})
        result = _guard_source_is_point(params_map, guard_def)
        self.assertTrue(result.passed)

    def test_source_is_point_rejects_polygon_layer(self):
        layer = make_mock_layer(geom_type=2)
        params_map = {"source_layer": layer}
        guard_def = GuardDef(condition="source_is_point", params={"geometry_type": 0})
        result = _guard_source_is_point(params_map, guard_def)
        self.assertFalse(result.passed)

    # ── boundary_is_polygon ─────────────────────────────────

    def test_boundary_is_polygon_passes_for_polygon(self):
        layer = make_mock_layer(geom_type=2)
        params_map = {"boundary_layer": layer}
        guard_def = GuardDef(condition="boundary_is_polygon", params={"geometry_type": 2})
        result = _guard_boundary_is_polygon(params_map, guard_def)
        self.assertTrue(result.passed)

    def test_boundary_is_polygon_rejects_point(self):
        layer = make_mock_layer(geom_type=0)
        params_map = {"boundary_layer": layer}
        guard_def = GuardDef(condition="boundary_is_polygon", params={"geometry_type": 2})
        result = _guard_boundary_is_polygon(params_map, guard_def)
        self.assertFalse(result.passed)

    # ── crs_projected ───────────────────────────────────────

    def test_crs_projected_passes_epsg3857(self):
        layer = make_mock_layer(crs_authid="EPSG:3857")
        params_map = {"source_layer": layer}
        guard_def = GuardDef(condition="crs_projected")
        result = _guard_crs_projected(params_map, guard_def)
        self.assertTrue(result.passed)

    def test_crs_projected_rejects_epsg4326(self):
        layer = make_mock_layer(crs_authid="EPSG:4326")
        params_map = {"source_layer": layer}
        guard_def = GuardDef(condition="crs_projected")
        result = _guard_crs_projected(params_map, guard_def)
        self.assertFalse(result.passed)

    # ── crs_match ────────────────────────────────────────────

    def test_crs_match_passes_when_same(self):
        source = make_mock_layer(crs_authid="EPSG:3857")
        boundary = make_mock_layer(crs_authid="EPSG:3857")
        params_map = {"source_layer": source, "boundary_layer": boundary}
        guard_def = GuardDef(condition="crs_match")
        result = _guard_crs_match(params_map, guard_def)
        self.assertTrue(result.passed)

    def test_crs_match_fails_when_different(self):
        source = make_mock_layer(crs_authid="EPSG:3857")
        boundary = make_mock_layer(crs_authid="EPSG:4326")
        params_map = {"source_layer": source, "boundary_layer": boundary}
        guard_def = GuardDef(condition="crs_match")
        result = _guard_crs_match(params_map, guard_def)
        self.assertFalse(result.passed)

    def test_crs_match_auto_reproject_failure_fails_closed(self):
        """auto_reproject=true 但重投影失败时显式 failed（fail-closed，禁止静默跳过）。

        baseline 旧测试断言 auto_skipped=True 跳过；实现已演进为真实重投影，
        mock 图层无法完成重投影 → 必须 failed 且带诊断信息。
        """
        source = make_mock_layer(crs_authid="EPSG:3857")
        boundary = make_mock_layer(crs_authid="EPSG:4326")
        params_map = {"source_layer": source, "boundary_layer": boundary}
        guard_def = GuardDef(condition="crs_match", params={"auto_reproject": True})
        result = _guard_crs_match(params_map, guard_def)
        self.assertFalse(result.passed)
        self.assertFalse(result.auto_skipped)
        self.assertIn("重投影失败", result.message)

    # ── boundary_fc_limit ───────────────────────────────────

    def test_boundary_fc_limit_passes_under_limit(self):
        layer = make_mock_layer()
        layer.featureCount.return_value = 500
        params_map = {"boundary_layer": layer}
        guard_def = GuardDef(condition="boundary_fc_limit", params={"max": 10000})
        result = _guard_boundary_fc_limit(params_map, guard_def)
        self.assertTrue(result.passed)

    def test_boundary_fc_limit_fails_over_limit(self):
        layer = make_mock_layer()
        layer.featureCount.return_value = 15000
        params_map = {"boundary_layer": layer}
        guard_def = GuardDef(condition="boundary_fc_limit", params={"max": 10000})
        result = _guard_boundary_fc_limit(params_map, guard_def)
        self.assertFalse(result.passed)

    # ── source_is_vector（P2-0 补全真实检查）──────────────

    def test_source_is_vector_passes_for_vector(self):
        layer = make_mock_layer(geom_type=0)
        layer.type.return_value = 0  # QgsMapLayer.VectorLayer
        params_map = {"source_layer": layer}
        guard_def = GuardDef(condition="source_is_vector")
        result = _guard_source_is_vector(params_map, guard_def)
        self.assertTrue(result.passed)

    def test_source_is_vector_rejects_raster(self):
        layer = make_mock_layer(geom_type=0)
        layer.type.return_value = 1  # QgsMapLayer.RasterLayer
        params_map = {"source_layer": layer}
        guard_def = GuardDef(condition="source_is_vector")
        result = _guard_source_is_vector(params_map, guard_def)
        self.assertFalse(result.passed)
        self.assertIn("栅格", result.message)

    def test_source_is_vector_rejects_none(self):
        params_map = {"source_layer": None}
        guard_def = GuardDef(condition="source_is_vector")
        result = _guard_source_is_vector(params_map, guard_def)
        self.assertFalse(result.passed)
        self.assertIn("不存在", result.message)

    def test_source_is_vector_rejects_object_without_type(self):
        params_map = {"source_layer": object()}
        guard_def = GuardDef(condition="source_is_vector")
        result = _guard_source_is_vector(params_map, guard_def)
        self.assertFalse(result.passed)
        self.assertIn("不是有效 QgsVectorLayer", result.message)

    # ── 未知守卫条件（P2-0 fail-closed）──────────────────

    def test_unknown_guard_fails_closed(self):
        checker = GuardChecker({})
        guard_def = GuardDef(condition="no_such_guard")
        result = checker._check_one(guard_def)
        self.assertFalse(result.passed)
        self.assertEqual(result.message, "未知守卫条件 'no_such_guard'")

    def test_unknown_guard_in_check_report(self):
        checker = GuardChecker({})
        report = checker.check(GuardCheck(guards=[GuardDef(condition="no_such_guard")]))
        self.assertFalse(report.all_passed)
        self.assertEqual(len(report.failed), 1)
        self.assertTrue(report.blocking_failed[0].blocking)

    # ── on_fail 默认值（P2-0 fail-closed）────────────────

    def test_guard_check_on_fail_defaults_to_error(self):
        guard_check = GuardCheck()
        self.assertEqual(guard_check.on_fail, "error")

    def test_guard_def_on_fail_defaults_empty(self):
        guard_def = GuardDef(condition="crs_projected")
        self.assertEqual(guard_def.on_fail, "")

    # ── blocking 判定（模板级 + 守卫级覆盖）─────────────

    def test_checker_blocking_uses_template_on_fail(self):
        source = make_mock_layer(geom_type=2, crs_authid="EPSG:3857")  # 面图层，非点
        params_map = {"source_layer": source}
        # 模板级 error → 失败即阻断
        guard_check = GuardCheck(
            guards=[GuardDef(condition="source_is_point", params={"geometry_type": 0})],
            on_fail="error",
        )
        report = GuardChecker(params_map).check(guard_check)
        self.assertFalse(report.all_passed)
        self.assertEqual(len(report.blocking_failed), 1)

    def test_checker_blocking_uses_guard_level_override(self):
        source = make_mock_layer(geom_type=2, crs_authid="EPSG:3857")  # 面图层，非点
        params_map = {"source_layer": source}
        # 守卫级 on_fail="warn" 覆盖模板级 error → 失败但不阻断
        guard_check = GuardCheck(
            guards=[GuardDef(condition="source_is_point",
                             params={"geometry_type": 0}, on_fail="warn")],
            on_fail="error",
        )
        report = GuardChecker(params_map).check(guard_check)
        self.assertFalse(report.all_passed)
        self.assertEqual(len(report.failed), 1)
        self.assertEqual(len(report.blocking_failed), 0)

    def test_checker_warn_level_failure_not_blocking(self):
        layer = make_mock_layer()
        layer.featureCount.return_value = 15000  # 超过 10000 上限
        params_map = {"boundary_layer": layer}
        # boundary_fc_limit 在模板中声明为 warn
        guard_check = GuardCheck(
            guards=[GuardDef(condition="boundary_fc_limit",
                             params={"max": 10000}, on_fail="warn")],
            on_fail="error",
        )
        report = GuardChecker(params_map).check(guard_check)
        self.assertFalse(report.all_passed)
        self.assertEqual(len(report.failed), 1)
        self.assertEqual(len(report.blocking_failed), 0)

    # ── GuardChecker.execute() 集成 ────────────────────────

    def test_checker_all_pass(self):
        source = make_mock_layer(geom_type=0, crs_authid="EPSG:3857")
        boundary = make_mock_layer(geom_type=2, crs_authid="EPSG:3857")
        params_map = {"source_layer": source, "boundary_layer": boundary}

        guard_check = GuardCheck(guards=[
            GuardDef(condition="source_is_point", params={"geometry_type": 0}),
            GuardDef(condition="boundary_is_polygon", params={"geometry_type": 2}),
            GuardDef(condition="crs_projected"),
            GuardDef(condition="crs_match"),
        ])

        checker = GuardChecker(params_map)
        report = checker.check(guard_check)
        self.assertTrue(report.all_passed)

    def test_checker_first_fail_stops_checking(self):
        """首个失败不阻塞后续检查，但 all_passed 为 False。"""
        source = make_mock_layer(geom_type=2, crs_authid="EPSG:3857")  # 面图层，非点
        boundary = make_mock_layer(geom_type=2, crs_authid="EPSG:3857")
        params_map = {"source_layer": source, "boundary_layer": boundary}

        guard_check = GuardCheck(guards=[
            GuardDef(condition="source_is_point", params={"geometry_type": 0}),
            GuardDef(condition="boundary_is_polygon", params={"geometry_type": 2}),
            GuardDef(condition="crs_projected"),
        ])

        checker = GuardChecker(params_map)
        report = checker.check(guard_check)
        self.assertFalse(report.all_passed)
        # 仍会检查全部 3 条
        self.assertEqual(len(report.results), 3)
        # 第一条失败
        self.assertFalse(report.results[0].passed)
        # 后两条通过
        self.assertTrue(report.results[1].passed)
        self.assertTrue(report.results[2].passed)

    def test_checker_empty_guards_passes(self):
        checker = GuardChecker({})
        report = checker.check(GuardCheck(guards=[]))
        self.assertTrue(report.all_passed)

    # ── GUARD_REGISTRY 完整性 ────────────────────────────────

    def test_registry_has_all_7_guards(self):
        expected = [
            "source_is_vector", "source_is_point", "boundary_is_polygon",
            "crs_projected", "shapely_available", "boundary_fc_limit", "crs_match",
        ]
        for name in expected:
            self.assertIn(name, GUARD_REGISTRY, f"Missing guard: {name}")


if __name__ == "__main__":
    unittest.main()
