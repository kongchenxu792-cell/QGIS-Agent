"""test_gap_analysis — gap_analysis 模板解析与验证测试 (3 用例)。

验证刚创建的 gap_analysis.json 声明式模板：
1. 模板解析：9 steps，action=gap_analysis
2. guard 链：7 条与 coverage 一致
3. $ref 引用链：stats 和 output_layer 的 input_source 正确
"""

import json
import os
import sys
import unittest

sys.path.insert(0, r"D:\桌面\AIQGIS_APP")


class TestGapAnalysis(unittest.TestCase):
    """gap_analysis 模板单元测试。"""

    @classmethod
    def setUpClass(cls):
        template_path = os.path.join(
            os.path.dirname(__file__), "..", "src", "core", "templates", "gap_analysis.json"
        )
        with open(template_path, "r", encoding="utf-8") as f:
            cls.template = json.load(f)

    # ── 1. 模板解析 ────────────────────────────────────────

    def test_template_parse(self):
        """验证 action、步骤数、每步必要字段、difference step 的 input_boundary。"""
        tpl = self.template

        self.assertEqual(tpl["action"], "gap_analysis")

        steps = tpl["steps"]
        self.assertEqual(len(steps), 9, "gap_analysis 应为 9 步管线")

        required_fields = {"id", "engine"}
        for i, step in enumerate(steps):
            for field in required_fields:
                self.assertIn(field, step, f"Step {i} ('{step.get('id', '?')}') 缺失字段: {field}")

        # difference step 应使用 input_boundary（而非 input_overlay）
        diff_step = [s for s in steps if s["id"] == "difference"][0]
        self.assertNotIn("input_overlay", diff_step,
                         "difference step 不应使用 input_overlay（StepDef 无此字段）")
        self.assertIn("input_boundary", diff_step,
                      "difference step 必须有 input_boundary 字段")
        self.assertEqual(diff_step["input_boundary"], "$clip.output",
                         "difference step 的 input_boundary 应指向 $clip.output")

    # ── 2. guard 链 ────────────────────────────────────────

    def test_guard_chain(self):
        """验证 guards 列表 7 条与 coverage 一致。"""
        guards = self.template["guards"]
        self.assertEqual(len(guards), 7, "gap_analysis 应有 7 条 guards")

        expected_conditions = [
            "crs_projected",
            "source_is_vector",
            "source_is_point",
            "boundary_is_polygon",
            "boundary_fc_limit",
            "shapely_available",
            "crs_match",
        ]
        actual_conditions = [g["condition"] for g in guards]
        self.assertEqual(actual_conditions, expected_conditions,
                         "guard 条件列表应与 coverage 完全一致")

        # 逐条验证有 params 字段（可为空或无）
        for g in guards:
            self.assertIsInstance(g.get("params", {}), dict,
                                  f"guard {g['condition']} 的 params 应为 dict")

    # ── 3. $ref 引用链 ─────────────────────────────────────

    def test_ref_chain(self):
        """验证 $ref 引用链完整性。

        - stats step: 引用 $clip.output + $difference.output
        - output_layer step: 引用 $difference.output
        """
        steps = {s["id"]: s for s in self.template["steps"]}

        # stats step
        stats_step = steps["stats"]
        self.assertEqual(stats_step["input_source"], "$clip.output",
                         "stats step 的 input_source 应引用 $clip.output")
        self.assertEqual(stats_step["input_boundary"], "$boundary_dissolve.output",
                         "stats step 的 input_boundary 应引用 $boundary_dissolve.output")

        # 验证 stats 的 output_keys 包含 gap 专属字段
        output_keys = stats_step.get("output_keys", [])
        self.assertIn("gap_area", output_keys, "stats output_keys 应包含 gap_area")
        self.assertIn("gap_rate", output_keys, "stats output_keys 应包含 gap_rate")
        self.assertIn("coverage_rate", output_keys, "stats output_keys 应包含 coverage_rate")

        # output_layer step
        output_step = steps["output_layer"]
        self.assertEqual(output_step["input_source"], "$difference.output",
                         "output_layer step 应引用 $difference.output（盲区图层）")

        # 输出名称包含 _gap_ 后缀
        name_tpl = output_step["params"].get("name_template", "")
        self.assertIn("_gap_", name_tpl,
                      "输出图层 name_template 应包含 _gap_ 后缀以区别于 coverage")


if __name__ == "__main__":
    unittest.main()
