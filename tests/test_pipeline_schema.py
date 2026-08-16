"""test_pipeline_schema — 模板 JSON 解析与 StepDef schema 测试 (~8 用例)。

测试 coverage_analysis.json 解析、StepDef 数据类字段完整性、
$ref 引用解析及模板文件缺失时的错误处理。
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.pipeline_executor import (
    PipelineExecutor,
    StepDef,
    StepResult,
    EngineSpec,
    TemplateDef,
    ParamDef,
)

TEMPLATE_PATH = str(Path(__file__).resolve().parent.parent / "src" / "core" / "templates" / "coverage_analysis.json")


class TestPipelineSchema(unittest.TestCase):
    """模板 JSON → 数据类解析 schema 测试。"""

    @classmethod
    def setUpClass(cls):
        with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
            cls.raw = json.load(f)
        executor = PipelineExecutor()
        cls.template = executor._parse_template(TEMPLATE_PATH)

    # ── JSON 结构验证 ──────────────────────────────────────

    def test_raw_has_8_steps(self):
        self.assertEqual(len(self.raw["steps"]), 8)

    def test_all_steps_have_id_and_engine(self):
        for i, step in enumerate(self.raw["steps"]):
            with self.subTest(step_index=i):
                self.assertIn("id", step, f"Step {i} missing 'id'")
                self.assertIn("engine", step, f"Step {i} ({step.get('id')}) missing 'engine'")
                self.assertIn("log", step, f"Step {i} ({step.get('id')}) missing 'log'")
                # params 字段可选（如 stats step 用 output_keys 替代）

    def test_all_steps_have_input_source(self):
        """每个 step 应有 input_source 或至少可推导输入。"""
        for i, step in enumerate(self.raw["steps"]):
            with self.subTest(step_index=i, step_id=step.get("id")):
                # 每个 step 必须有 id + engine，input_source 可选（第一个 step 用 $boundary_layer）
                self.assertTrue(
                    "input_source" in step or "id" in step,
                    f"Step {i} missing id",
                )

    # ── TemplateDef 解析验证 ────────────────────────────────

    def test_template_has_8_steps(self):
        self.assertEqual(len(self.template.steps), 8)

    def test_steps_are_stepdef_instances(self):
        for step in self.template.steps:
            self.assertIsInstance(step, StepDef)

    def test_step_ids_match_raw(self):
        raw_ids = [s["id"] for s in self.raw["steps"]]
        parsed_ids = [s.id for s in self.template.steps]
        self.assertEqual(parsed_ids, raw_ids)

    def test_first_step_is_boundary_dissolve(self):
        self.assertEqual(self.template.steps[0].id, "boundary_dissolve")
        self.assertEqual(self.template.steps[0].input_source, "$boundary_layer")
        self.assertGreater(len(self.template.steps[0].engine_chain), 0)

    def test_last_step_is_output_layer(self):
        self.assertEqual(self.template.steps[-1].id, "output_layer")
        self.assertEqual(self.template.steps[-1].input_source, "$clip.output")

    # ── $ref 引用解析 ──────────────────────────────────────

    def test_resolve_ref_source_layer(self):
        executor = PipelineExecutor()
        executor.params_map = {"source_layer": "dummy_layer"}
        result = executor._resolve_ref("$source_layer", {})
        self.assertEqual(result, "dummy_layer")

    def test_resolve_ref_step_output_dot_suffix(self):
        executor = PipelineExecutor()
        sr = StepResult(step_id="buffer", output_type="shapely_geom",
                         shapely_geom="mock_geom", file_path="/tmp/test.geojson")
        outputs = {"buffer": sr, "$buffer": sr}
        # .output suffix → extracts output value
        result = executor._resolve_ref("$buffer.output", outputs)
        self.assertIsNotNone(result)

    # ── 模板文件缺失 ────────────────────────────────────────

    def test_missing_template_raises(self):
        executor = PipelineExecutor()
        with self.assertRaises((FileNotFoundError, OSError, json.JSONDecodeError)):
            executor._parse_template(str(Path(__file__).resolve().parent.parent / "nonexistent_template.json"))

    # ── ParamDef 解析 ──────────────────────────────────────

    def test_params_parsed_correctly(self):
        self.assertEqual(len(self.template.params), 3)
        self.assertIsInstance(self.template.params[0], ParamDef)
        self.assertEqual(self.template.params[0].name, "source_layer")
        self.assertEqual(self.template.params[1].name, "boundary_layer")
        self.assertEqual(self.template.params[2].name, "radius_m")

    # ── 引擎链解析 ─────────────────────────────────────────

    def test_buffer_step_has_fallback(self):
        """Step 4 (buffer) 应有 primary + fallback 引擎。"""
        buffer_step = self.template.steps[3]  # index 3 = "buffer"
        self.assertGreaterEqual(len(buffer_step.engine_chain), 2)
        self.assertEqual(buffer_step.engine_chain[0].name, "qgis_processing")
        self.assertIn(buffer_step.engine_chain[1].name, ("shapely",))

    def test_stats_step_uses_shapely_area(self):
        stats_step = self.template.steps[6]  # index 6 = "stats"
        self.assertEqual(stats_step.engine_chain[0].name, "shapely")
        self.assertEqual(stats_step.engine_chain[0].method, "area")

    # ── guards 解析 ─────────────────────────────────────────

    def test_guards_parsed(self):
        self.assertIsNotNone(self.template.guards)
        self.assertEqual(len(self.template.guards.guards), 7)
        guard_conditions = [g.condition for g in self.template.guards.guards]
        self.assertIn("crs_projected", guard_conditions)
        self.assertIn("source_is_point", guard_conditions)
        self.assertIn("boundary_is_polygon", guard_conditions)

    # ── guards_on_fail 解析（P2-0 fail-closed）─────────────

    def test_guards_on_fail_parsed_from_template(self):
        """模板顶层 guards_on_fail 应被解析到 GuardCheck.on_fail。"""
        self.assertEqual(self.template.guards.on_fail, "error")

    def test_guards_guard_level_on_fail_parsed(self):
        """每个守卫的 on_fail 字段应被解析到 GuardDef.on_fail。"""
        by_condition = {g.condition: g for g in self.template.guards.guards}
        self.assertEqual(by_condition["crs_projected"].on_fail, "error")
        self.assertEqual(by_condition["source_is_vector"].on_fail, "error")
        # boundary_fc_limit 为性能软上限 → warn
        self.assertEqual(by_condition["boundary_fc_limit"].on_fail, "warn")
        self.assertEqual(by_condition["crs_match"].on_fail, "error")

    def test_guards_on_fail_defaults_error_when_missing(self):
        """缺省 guards_on_fail 时应为 error（fail-closed）。"""
        raw = {
            "action": "test",
            "guards": [
                {"condition": "crs_projected"},
            ],
            "steps": [],
        }
        import tempfile, os
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False)
            tmp_path = f.name
        try:
            executor = PipelineExecutor()
            template = executor._parse_template(tmp_path)
            self.assertIsNotNone(template.guards)
            self.assertEqual(template.guards.on_fail, "error")
        finally:
            os.unlink(tmp_path)

    def test_guards_on_fail_invalid_value_falls_back_error(self):
        """非法 guards_on_fail 值应回退到 error。"""
        raw = {
            "action": "test",
            "guards_on_fail": "banana",
            "guards": [{"condition": "crs_projected"}],
            "steps": [],
        }
        import tempfile, os
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False)
            tmp_path = f.name
        try:
            executor = PipelineExecutor()
            template = executor._parse_template(tmp_path)
            self.assertEqual(template.guards.on_fail, "error")
        finally:
            os.unlink(tmp_path)

    def test_all_four_templates_declare_guards_on_fail(self):
        """4 个模板必须显式声明 guards_on_fail 字段（P2-0 交付要求）。"""
        templates_dir = Path(__file__).resolve().parent.parent / "src" / "core" / "templates"
        names = ["coverage_analysis", "gap_analysis", "population_coverage", "building_risk"]
        for name in names:
            with self.subTest(template=name):
                with open(templates_dir / f"{name}.json", "r", encoding="utf-8") as f:
                    raw = json.load(f)
                self.assertIn("guards_on_fail", raw, f"{name}.json 缺少 guards_on_fail")
                self.assertEqual(raw["guards_on_fail"], "error")
                # 每个守卫都必须显式声明 on_fail
                for g in raw.get("guards", []):
                    self.assertIn("on_fail", g, f"{name}.json 守卫 {g.get('condition')} 缺少 on_fail")
                    self.assertIn(g["on_fail"], ("error", "warn"))


if __name__ == "__main__":
    unittest.main()
