"""test_geom_role_correction — 图层角色几何类型纠偏（A为主 + B兜底）。

覆盖：
- _correct_layer_geometry_roles：源/边界填反（CEO 场景）→ 按几何类型重新分配
- 关键词优先（避难所→点源、行政区/边界→面边界）多候选确定性选择
- 多候选仍歧义 → 清空参数交由 P2-4 澄清（不静默选）
- 无候选 → 保持原值交 Guard 兜底提示
- 几何正确 → 不纠偏
- match_and_execute 集成：LLM 填反 → handler 收到纠偏后的图层参数
- B 兜底：Guard 失败消息列出「应为什么几何图层 + 当前候选」
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from core.guards import GuardDef, GuardCheck, GuardResult, GuardReport  # noqa: E402
from core.instruction_mapper import (  # noqa: E402
    _correct_layer_geometry_roles,
    _correct_layer_params,
)
from src.core.pipeline_executor import PipelineExecutor  # noqa: E402

TEMPLATE_PATH = str(Path(__file__).resolve().parent.parent / "src" / "core" / "templates" / "coverage_analysis.json")


def _make_layer(kind: str, name: str):
    """构造真实 QGIS 内存图层（polygon / point）。"""
    from qgis.core import QgsVectorLayer
    if kind == "polygon":
        layer = QgsVectorLayer("Polygon?crs=EPSG:4326", name, "memory")
    else:
        layer = QgsVectorLayer("Point?crs=EPSG:4326", name, "memory")
    if not layer.isValid():
        raise RuntimeError(f"内存图层创建失败: {name}")
    return layer


def _proj(layers):
    proj = MagicMock()
    proj.mapLayers.return_value = {str(i): layer for i, layer in enumerate(layers, 1)}
    return proj


def _find_in_proj(proj, name):
    for layer in proj.mapLayers().values():
        if layer.name() == name:
            return layer
    return None


def _mapper():
    from src.core.instruction_mapper import InstructionMapper
    return InstructionMapper()


# ── _correct_layer_geometry_roles 单元 ─────────────────────────

class TestCorrectLayerGeometryRoles(unittest.TestCase):
    def setUp(self):
        self.point = _make_layer("point", "避难所")
        self.poly = _make_layer("polygon", "行政区")

    def test_swapped_source_boundary_corrected(self):
        """CEO 场景：源填了面、边界填了点 → 纠偏为源=点、边界=面。"""
        proj = _proj([self.point, self.poly])
        params = {"source_layer": "行政区", "boundary_layer": "避难所"}
        _correct_layer_geometry_roles(proj, params)
        self.assertEqual(params["source_layer"], "避难所")
        self.assertEqual(params["boundary_layer"], "行政区")

    def test_geometry_correct_unchanged(self):
        """几何正确不纠偏。"""
        proj = _proj([self.point, self.poly])
        params = {"source_layer": "避难所", "boundary_layer": "行政区"}
        _correct_layer_geometry_roles(proj, params)
        self.assertEqual(params["source_layer"], "避难所")
        self.assertEqual(params["boundary_layer"], "行政区")

    def test_hint_preferred_over_other_candidates(self):
        """多候选含关键词 → 确定性选择关键词图层（避难所→点源）。"""
        other_point = _make_layer("point", "学校")
        proj = _proj([self.point, other_point, self.poly])
        params = {"source_layer": "行政区"}
        _correct_layer_geometry_roles(proj, params)
        self.assertEqual(params["source_layer"], "避难所")

    def test_boundary_hint_picks_admin(self):
        """多面候选含行政区关键词 → 选行政区（行政区/边界→面边界）。"""
        pop_poly = _make_layer("polygon", "人口")
        proj = _proj([self.point, pop_poly, self.poly])
        params = {"boundary_layer": "避难所"}
        _correct_layer_geometry_roles(proj, params)
        self.assertEqual(params["boundary_layer"], "行政区")

    def test_multi_candidate_ambiguous_cleared(self):
        """多候选且无关键词命中 → 清空参数交由澄清（不静默选）。"""
        p1 = _make_layer("point", "poi_a")
        p2 = _make_layer("point", "poi_b")
        proj = _proj([p1, p2, self.poly])
        params = {"source_layer": "行政区"}
        _correct_layer_geometry_roles(proj, params)
        self.assertEqual(params["source_layer"], "")

    def test_no_candidate_keeps_value(self):
        """无几何正确候选 → 保持原值交 Guard 兜底提示。"""
        proj = _proj([self.poly])
        params = {"source_layer": "行政区", "boundary_layer": "行政区"}
        _correct_layer_geometry_roles(proj, params)
        self.assertEqual(params["source_layer"], "行政区")
        # boundary 几何正确，不纠偏
        self.assertEqual(params["boundary_layer"], "行政区")

    def test_project_none_noop(self):
        params = {"source_layer": "行政区"}
        _correct_layer_geometry_roles(None, params)
        self.assertEqual(params["source_layer"], "行政区")

    def test_unknown_layer_skipped(self):
        """图层名不存在 → 跳过（由名称校验清空，不误判）。"""
        proj = _proj([self.point, self.poly])
        params = {"source_layer": "不存在的图层"}
        _correct_layer_geometry_roles(proj, params)
        self.assertEqual(params["source_layer"], "不存在的图层")


# ── _correct_layer_params 全流程 ───────────────────────────────

class TestCorrectLayerParamsFlow(unittest.TestCase):
    def setUp(self):
        self.point = _make_layer("point", "避难所")
        self.poly = _make_layer("polygon", "行政区")

    def test_full_flow_swapped(self):
        """名称校验 + 几何纠偏全流程：填反 → 纠偏。"""
        proj = _proj([self.point, self.poly])
        params = {"source_layer": "行政区", "boundary_layer": "避难所",
                  "radius_m": 500}
        params = _correct_layer_params(proj, params, "计算避难所500米范围内的人口覆盖率")
        self.assertEqual(params["source_layer"], "避难所")
        self.assertEqual(params["boundary_layer"], "行政区")

    def test_full_flow_fantasy_name_cleared_then_geometry(self):
        """LLM 幻想图层名（不存在）→ 名称校验清空 → auto_detect 按文本匹配 + 几何纠偏。"""
        proj = _proj([self.point, self.poly])
        params = {"source_layer": "source_points", "boundary_layer": "避难所",
                  "radius_m": 500}
        params = _correct_layer_params(proj, params, "计算避难所500米范围内的人口覆盖率")
        # auto_detect 按文本出现顺序填 source=避难所（点），boundary 几何不符被纠偏
        self.assertEqual(params["source_layer"], "避难所")
        self.assertNotEqual(params["boundary_layer"], "避难所")


# ── match_and_execute 集成（CEO 场景）──────────────────────────

class TestMatchAndExecuteCorrection(unittest.TestCase):
    def setUp(self):
        self.point = _make_layer("point", "避难所")
        self.poly = _make_layer("polygon", "行政区")
        self.pop = _make_layer("polygon", "人口")

    def test_ceo_scenario_corrected_params_passed_to_handler(self):
        """CEO 场景：LLM 把源/边界填反 → 纠偏后 handler 收到正确参数。"""
        mapper = _mapper()
        proj = _proj([self.point, self.poly, self.pop])
        llm_json = json.dumps({
            "action": "population_coverage",
            "params": {
                "source_layer": "行政区", "boundary_layer": "避难所",
                "population_layer": "人口", "population_field": "population",
                "radius_m": 500,
            },
        })
        with patch.object(mapper, "_handle_population_coverage",
                          return_value={"success": True, "message": "ok"}) as mock_handler:
            result = mapper.match_and_execute(
                llm_json,
                project=proj,
                user_text="计算避难所500米范围内的人口覆盖率",
            )
        self.assertTrue(result["success"])
        kwargs = mock_handler.call_args[1]
        self.assertEqual(kwargs["source_layer"], "避难所")
        self.assertEqual(kwargs["boundary_layer"], "行政区")
        self.assertEqual(kwargs["population_layer"], "人口")

    def test_correct_geometry_not_touched(self):
        """LLM 填对 → handler 收到原参数。"""
        mapper = _mapper()
        proj = _proj([self.point, self.poly, self.pop])
        llm_json = json.dumps({
            "action": "population_coverage",
            "params": {
                "source_layer": "避难所", "boundary_layer": "行政区",
                "population_layer": "人口", "population_field": "population",
                "radius_m": 500,
            },
        })
        with patch.object(mapper, "_handle_population_coverage",
                          return_value={"success": True, "message": "ok"}) as mock_handler:
            result = mapper.match_and_execute(
                llm_json,
                project=proj,
                user_text="计算避难所500米范围内的人口覆盖率",
            )
        self.assertTrue(result["success"])
        kwargs = mock_handler.call_args[1]
        self.assertEqual(kwargs["source_layer"], "避难所")
        self.assertEqual(kwargs["boundary_layer"], "行政区")


# ── B 兜底：Guard 失败提示 ─────────────────────────────────────

class TestGuardRoleHint(unittest.TestCase):
    def setUp(self):
        self.point = _make_layer("point", "避难所")
        self.poly = _make_layer("polygon", "行政区")

    def _executor_with_project(self, layers):
        executor = PipelineExecutor()
        executor.project = _proj(layers)
        return executor

    def test_hint_lists_candidates(self):
        executor = self._executor_with_project([self.point, self.poly])
        report = GuardReport(
            all_passed=False,
            results=[
                GuardResult(condition="source_is_point", passed=False,
                            message="源图层 geometryType=2，期望=0 (Point)", blocking=True),
            ],
        )
        hint = executor._build_guard_role_hint(report)
        self.assertIn("源图层应为Point图层", hint)
        self.assertIn("避难所", hint)

    def test_hint_boundary(self):
        executor = self._executor_with_project([self.point, self.poly])
        report = GuardReport(
            all_passed=False,
            results=[
                GuardResult(condition="boundary_is_polygon", passed=False,
                            message="边界图层 geometryType=0，期望=2 (Polygon)", blocking=True),
            ],
        )
        hint = executor._build_guard_role_hint(report)
        self.assertIn("边界图层应为Polygon图层", hint)
        self.assertIn("行政区", hint)

    def test_hint_no_candidates(self):
        # 只有面图层，无点候选
        executor = self._executor_with_project([self.poly])
        report = GuardReport(
            all_passed=False,
            results=[
                GuardResult(condition="source_is_point", passed=False,
                            message="源图层 geometryType=2，期望=0 (Point)", blocking=True),
            ],
        )
        hint = executor._build_guard_role_hint(report)
        self.assertIn("源图层应为Point图层", hint)
        self.assertIn("无可用候选", hint)

    def test_hint_ignores_non_geom_guards(self):
        executor = self._executor_with_project([self.point, self.poly])
        report = GuardReport(
            all_passed=False,
            results=[
                GuardResult(condition="crs_projected", passed=False,
                            message="源图层为地理坐标系", blocking=True),
            ],
        )
        self.assertEqual(executor._build_guard_role_hint(report), "")

    def test_hint_empty_report(self):
        executor = self._executor_with_project([self.point, self.poly])
        self.assertEqual(executor._build_guard_role_hint(None), "")

    def test_guard_failed_message_contains_hint(self):
        """真实 execute Guard 失败路径：消息含角色候选提示。"""
        executor = PipelineExecutor()
        proj = _proj([self.point, self.poly])
        result = executor.execute(
            template_path=TEMPLATE_PATH,
            source_layer_name="行政区",   # 面 → source_is_point 失败
            boundary_layer_name="避难所",  # 点 → boundary_is_polygon 失败
            radius_m=500,
            project=proj,
            _find_layer_fn=lambda name: _find_in_proj(proj, name),
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("Guard check failed", result["message"])
        self.assertIn("源图层应为Point图层", result["message"])
        self.assertIn("边界图层应为Polygon图层", result["message"])


if __name__ == "__main__":
    unittest.main()
