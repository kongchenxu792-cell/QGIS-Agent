"""test_clarification — P2-4 运行前澄清：图层角色多候选交互点选。

覆盖：
- find_layer_candidates：精确唯一 / 无精确模糊多（返回全部、项目顺序）/ 无匹配 /
  project=None / 大小写不敏感
- role_candidates：population→仅面；source→仅点；target→全部矢量；无面图层→空
- build_clarification / is_clarification_result / format_cancel_message
- _handle_population_coverage 多面候选 → 澄清请求；PipelineExecutor.execute 与
  _record_analysis_run 均不被调用
- match_and_execute(params_override=...)：override 在 correction 之前合并、传给
  handler、其余参数保留、correction 后不被清空
- 单面候选维持自动兜底（既有行为）；0 面候选维持报错
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from core.clarification import (  # noqa: E402
    CLARIFICATION_STATUS,
    build_clarification,
    find_layer_candidates,
    format_cancel_message,
    is_clarification_result,
    role_candidates,
)


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


def _mapper():
    from src.core.instruction_mapper import InstructionMapper
    return InstructionMapper()


# ── find_layer_candidates ─────────────────────────────────────

class TestFindLayerCandidates(unittest.TestCase):
    def setUp(self):
        self.poly1 = _make_layer("polygon", "东京行政区")
        self.poly2 = _make_layer("polygon", "大阪行政区")
        self.point = _make_layer("point", "避难所")

    def test_exact_unique_returns_exact(self):
        proj = _proj([self.point, self.poly1, self.poly2])
        self.assertEqual(find_layer_candidates(proj, "东京行政区"), ["东京行政区"])

    def test_fuzzy_multi_returns_all_in_project_order(self):
        # 无精确匹配 → 返回全部子串模糊候选，保持项目图层顺序
        proj = _proj([self.point, self.poly1, self.poly2])
        self.assertEqual(find_layer_candidates(proj, "行政区"), ["东京行政区", "大阪行政区"])

    def test_fuzzy_single(self):
        proj = _proj([self.point, self.poly1])
        self.assertEqual(find_layer_candidates(proj, "行政区"), ["东京行政区"])

    def test_no_match(self):
        proj = _proj([self.point, self.poly1])
        self.assertEqual(find_layer_candidates(proj, "不存在"), [])

    def test_project_none(self):
        self.assertEqual(find_layer_candidates(None, "东京行政区"), [])

    def test_case_insensitive(self):
        proj = _proj([self.point, self.poly1])
        # 精确匹配大小写不敏感
        self.assertEqual(find_layer_candidates(proj, "东京行政区"), ["东京行政区"])
        # 关闭大小写不敏感后，不同大小写不视为精确 → 走模糊（这里中文无大小写差异，
        # 用英文图层名验证）
        eng = _make_layer("point", "Shelter")
        proj2 = _proj([eng])
        self.assertEqual(find_layer_candidates(proj2, "shelter"), ["Shelter"])
        self.assertEqual(find_layer_candidates(proj2, "shelter", case_insensitive=False), [])


# ── role_candidates ───────────────────────────────────────────

class TestRoleCandidates(unittest.TestCase):
    def setUp(self):
        self.poly1 = _make_layer("polygon", "东京行政区")
        self.poly2 = _make_layer("polygon", "人口")
        self.point = _make_layer("point", "避难所")

    def test_population_only_polygon(self):
        proj = _proj([self.point, self.poly1, self.poly2])
        self.assertEqual(role_candidates(proj, "population_layer"),
                         ["东京行政区", "人口"])

    def test_boundary_only_polygon(self):
        proj = _proj([self.point, self.poly1])
        self.assertEqual(role_candidates(proj, "boundary_layer"), ["东京行政区"])

    def test_intensity_only_polygon(self):
        proj = _proj([self.point, self.poly1])
        self.assertEqual(role_candidates(proj, "intensity_layer"), ["东京行政区"])

    def test_source_only_point(self):
        proj = _proj([self.point, self.poly1])
        self.assertEqual(role_candidates(proj, "source_layer"), ["避难所"])

    def test_target_all_vector(self):
        proj = _proj([self.point, self.poly1, self.poly2])
        self.assertEqual(role_candidates(proj, "target_layer"),
                         ["避难所", "东京行政区", "人口"])

    def test_join_all_vector(self):
        proj = _proj([self.point, self.poly1])
        self.assertEqual(role_candidates(proj, "join_layer"),
                         ["避难所", "东京行政区"])

    def test_no_polygon_empty(self):
        proj = _proj([self.point])
        self.assertEqual(role_candidates(proj, "population_layer"), [])

    def test_unknown_role_empty(self):
        proj = _proj([self.point, self.poly1])
        self.assertEqual(role_candidates(proj, "unknown_role"), [])

    def test_project_none_empty(self):
        self.assertEqual(role_candidates(None, "population_layer"), [])


# ── build_clarification / is_clarification_result / format_cancel_message ──

class TestClarificationStructure(unittest.TestCase):
    def test_build_clarification_full(self):
        clar = build_clarification(
            "population_coverage", "population_layer",
            ["东京行政区", "人口", "震度"],
            {"source_layer": "避难所", "population_field": "population"},
        )
        self.assertFalse(clar["success"])
        self.assertEqual(clar["status"], CLARIFICATION_STATUS)
        self.assertEqual(clar["action"], "population_coverage")
        # 候选名全量出现在 message
        self.assertIn("东京行政区", clar["message"])
        self.assertIn("人口", clar["message"])
        self.assertIn("震度", clar["message"])
        c = clar["clarification"]
        self.assertEqual(c["param_key"], "population_layer")
        self.assertEqual(c["candidates"], ["东京行政区", "人口", "震度"])
        self.assertEqual(c["params"]["source_layer"], "避难所")
        self.assertIn("人口图层", c["question"])

    def test_is_clarification_result_true(self):
        clar = build_clarification("a", "population_layer", ["x", "y"], {})
        self.assertTrue(is_clarification_result(clar))

    def test_is_clarification_result_false_missing_fields(self):
        self.assertFalse(is_clarification_result({}))
        self.assertFalse(is_clarification_result({"success": False, "status": "clarification"}))
        self.assertFalse(is_clarification_result(
            {"success": False, "status": "clarification",
             "clarification": {"param_key": "population_layer", "candidates": []}}))
        self.assertFalse(is_clarification_result(
            {"success": False, "status": "failed",
             "clarification": {"param_key": "population_layer", "candidates": ["x"]}}))
        self.assertFalse(is_clarification_result(None))

    def test_format_cancel_message(self):
        clar = build_clarification("a", "population_layer", ["东京行政区", "人口"], {})
        msg = format_cancel_message(clar["clarification"])
        self.assertIn("人口图层", msg)
        self.assertIn("东京行政区", msg)
        self.assertIn("人口", msg)
        self.assertIn("取消", msg)


# ── _handle_population_coverage 澄清集成 ───────────────────────

class TestPopulationCoverageClarification(unittest.TestCase):
    def setUp(self):
        self.poly1 = _make_layer("polygon", "东京行政区")
        self.poly2 = _make_layer("polygon", "大阪行政区")
        self.point = _make_layer("point", "避难所")

    @patch("core.pipeline_executor.PipelineExecutor.execute")
    def test_multi_candidate_returns_clarification_no_execute(self, mock_execute):
        """多面候选且无 population_layer → 澄清请求；不执行、不记录 run。"""
        mapper = _mapper()
        proj = _proj([self.point, self.poly1, self.poly2])
        with patch.object(mapper, "_record_analysis_run") as mock_record:
            result = mapper._handle_population_coverage(
                project=proj, source_layer="避难所", boundary_layer="东京行政区",
                population_layer="", population_field="population",
            )
        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "clarification")
        self.assertEqual(result["clarification"]["param_key"], "population_layer")
        self.assertEqual(
            sorted(result["clarification"]["candidates"]),
            ["东京行政区", "大阪行政区"],
        )
        self.assertIn("东京行政区", result["message"])
        self.assertIn("大阪行政区", result["message"])
        # 澄清轮不执行、不记录 run
        mock_execute.assert_not_called()
        mock_record.assert_not_called()
        # 澄清请求携带完整 params（LLM 原始参数 + 已探测字段）
        self.assertEqual(
            result["clarification"]["params"]["population_field"], "population",
        )

    def test_single_candidate_keeps_auto_fallback(self):
        """单面候选维持既有自动兜底（不触发澄清）。"""
        mapper = _mapper()
        proj = _proj([self.point, self.poly1])
        with patch("core.pipeline_executor.PipelineExecutor.execute",
                   return_value={"success": True, "message": "人口覆盖率分析完成"}), \
             patch.object(mapper, "_record_analysis_run"):
            result = mapper._handle_population_coverage(
                project=proj, source_layer="避难所", boundary_layer="东京行政区",
                population_layer="", population_field="population",
            )
        self.assertTrue(result["success"])
        self.assertNotIn("status", result)
        self.assertEqual(result["info"], "已自动选择人口图层：东京行政区")

    def test_no_candidate_keeps_error(self):
        """0 面候选维持既有报错（不触发澄清）。"""
        mapper = _mapper()
        proj = _proj([self.point])
        result = mapper._handle_population_coverage(
            project=proj, source_layer="避难所", boundary_layer="东京行政区",
            population_layer="", population_field="population",
        )
        self.assertFalse(result["success"])
        self.assertNotEqual(result.get("status"), "clarification")
        self.assertIn("population_layer", result["message"])

    def test_multi_candidate_with_exact_match_no_clarification(self):
        """参数非空且精确命中 → 不澄清（R2 精确优先）。"""
        mapper = _mapper()
        proj = _proj([self.point, self.poly1, self.poly2])
        with patch("core.pipeline_executor.PipelineExecutor.execute",
                   return_value={"success": True, "message": "人口覆盖率分析完成"}), \
             patch.object(mapper, "_record_analysis_run"):
            result = mapper._handle_population_coverage(
                project=proj, source_layer="避难所", boundary_layer="东京行政区",
                population_layer="大阪行政区", population_field="population",
            )
        self.assertTrue(result["success"])


# ── match_and_execute params_override ─────────────────────────

class TestParamsOverride(unittest.TestCase):
    def setUp(self):
        self.poly = _make_layer("polygon", "东京行政区")
        self.point = _make_layer("point", "避难所")

    @patch("src.core.instruction_mapper._correct_layer_params",
           side_effect=lambda project, params, user_text: params)
    def test_override_merged_before_correction_and_passed(self, mock_correct):
        """override 在 correction 之前合并、传给 handler、其余参数保留、不被清空。"""
        mapper = _mapper()
        llm_json = json.dumps({
            "action": "population_coverage",
            "params": {
                "source_layer": "避难所", "boundary_layer": "东京行政区",
                "population_layer": "", "population_field": "population",
                "radius_m": 500,
            },
        })
        with patch.object(mapper, "_handle_population_coverage",
                          return_value={"success": True, "message": "ok"}) as mock_handler:
            result = mapper.match_and_execute(
                llm_json,
                project=MagicMock(),
                params_override={"population_layer": "人口"},
            )
        self.assertTrue(result["success"])
        mock_handler.assert_called_once()
        kwargs = mock_handler.call_args[1]
        # override 生效并传给 handler
        self.assertEqual(kwargs["population_layer"], "人口")
        # 其余参数保留
        self.assertEqual(kwargs["source_layer"], "避难所")
        self.assertEqual(kwargs["radius_m"], 500)
        self.assertEqual(kwargs["population_field"], "population")
        # override 在 correction 之前合并（correction 收到的 params 已含 override 值）
        correction_params = mock_correct.call_args[0][1]
        self.assertEqual(correction_params["population_layer"], "人口")
        # correction 后不被清空（handler 收到的仍是 override 值）
        self.assertEqual(kwargs["population_layer"], "人口")

    @patch("src.core.instruction_mapper._correct_layer_params",
           side_effect=lambda project, params, user_text: params)
    def test_no_override_keeps_original(self, mock_correct):
        """params_override=None 既有调用零影响。"""
        mapper = _mapper()
        llm_json = json.dumps({
            "action": "population_coverage",
            "params": {
                "source_layer": "避难所", "boundary_layer": "东京行政区",
                "population_layer": "东京行政区", "population_field": "population",
            },
        })
        with patch.object(mapper, "_handle_population_coverage",
                          return_value={"success": True, "message": "ok"}) as mock_handler:
            result = mapper.match_and_execute(llm_json, project=MagicMock())
        self.assertTrue(result["success"])
        kwargs = mock_handler.call_args[1]
        self.assertEqual(kwargs["population_layer"], "东京行政区")


if __name__ == "__main__":
    unittest.main()
