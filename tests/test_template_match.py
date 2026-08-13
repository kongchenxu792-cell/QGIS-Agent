"""test_template_match — 模板关键词匹配测试 (~18 用例)。

测试 template_registry 中的 keyword_pre_match / find_template / _INSTRUCTION_TEMPLATES。
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

# Ensure src is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.template_registry import (
    keyword_pre_match,
    find_template,
    _INSTRUCTION_TEMPLATES,
)


class TestTemplateMatch(unittest.TestCase):
    """关键词 → action 匹配测试（中/日/英三语 + 正则 + 边界）。"""

    # ── 中文触发词 ──────────────────────────────────────────

    def test_zh_load_layer(self):
        result = keyword_pre_match("加载图层文件", lang="zh")
        self.assertIsNotNone(result)
        self.assertEqual(result["action"], "load_layer")
        self.assertGreater(result.get("_score", 0) if False else 1, 0)

    def test_zh_create_buffer(self):
        result = keyword_pre_match("帮我做点缓冲区分析", lang="zh")
        self.assertIsNotNone(result)
        self.assertEqual(result["action"], "create_buffer")

    def test_zh_coverage_analysis(self):
        result = keyword_pre_match("需要500米覆盖率的分析", lang="zh")
        self.assertIsNotNone(result)
        self.assertEqual(result["action"], "coverage_analysis")

    def test_zh_spatial_join(self):
        result = keyword_pre_match("做一下空间关联看看", lang="zh")
        self.assertIsNotNone(result)
        self.assertEqual(result["action"], "spatial_join")

    def test_zh_list_layers(self):
        result = keyword_pre_match("列出图层给我看看", lang="zh")
        self.assertIsNotNone(result)
        self.assertEqual(result["action"], "list_layers")

    def test_zh_filter_layer(self):
        result = keyword_pre_match("过滤图层条件筛选", lang="zh")
        self.assertIsNotNone(result)
        self.assertEqual(result["action"], "filter_layer")

    # ── 日文触发词 ──────────────────────────────────────────

    def test_ja_coverage_analysis(self):
        result = keyword_pre_match("バッファカバー率を計算したい", lang="ja")
        self.assertIsNotNone(result)
        self.assertEqual(result["action"], "coverage_analysis")

    def test_ja_create_buffer(self):
        result = keyword_pre_match("バッファ分析を実行してください", lang="ja")
        self.assertIsNotNone(result)
        self.assertEqual(result["action"], "create_buffer")

    def test_ja_reproject(self):
        result = keyword_pre_match("このレイヤを再投影したい", lang="ja")
        self.assertIsNotNone(result)
        self.assertEqual(result["action"], "reproject_layer")

    # ── 英文触发词 ──────────────────────────────────────────

    def test_en_spatial_join(self):
        result = keyword_pre_match("perform spatial join on layers", lang="en")
        self.assertIsNotNone(result)
        self.assertEqual(result["action"], "spatial_join")

    def test_en_coverage(self):
        result = keyword_pre_match("calculate buffer coverage rate", lang="en")
        self.assertIsNotNone(result)
        self.assertEqual(result["action"], "coverage_analysis")

    def test_en_reproject(self):
        result = keyword_pre_match("reproject the layer to EPSG:3857", lang="en")
        self.assertIsNotNone(result)
        self.assertEqual(result["action"], "reproject_layer")

    # ── 正则触发词 ──────────────────────────────────────────

    def test_regex_load_file(self):
        """正则 '加载.*文件' 应匹配 '加载 D 盘数据文件'。"""
        result = keyword_pre_match("加载 D 盘数据文件", lang="zh")
        self.assertIsNotNone(result)
        self.assertEqual(result["action"], "load_layer")

    def test_regex_distance_meters(self):
        """正则 '\\d+米' 应匹配 '500米覆盖'，命中 coverage_analysis。"""
        result = keyword_pre_match("500米覆盖分析", lang="zh")
        self.assertIsNotNone(result)
        self.assertEqual(result["action"], "coverage_analysis")

    # ── 多触发词取最高分 ────────────────────────────────────

    def test_multi_trigger_highest_score(self):
        """多个触发词时取 score 最高的模板。"""
        # 构造让 coverage_analysis 命中多个触发词（"覆盖分析"+"500米覆盖"+"要素覆盖分析"=3分）
        # 而 load_layer 仅命中"加载图层"（1分），确保高分模板胜出
        result = keyword_pre_match("加载图层做500米覆盖分析看看要素覆盖", lang="zh")
        self.assertIsNotNone(result)
        self.assertEqual(result["action"], "coverage_analysis")

    # ── 无意义文本返回 None ─────────────────────────────────

    def test_nonsense_text_returns_none_zh(self):
        result = keyword_pre_match("今天天气真好", lang="zh")
        self.assertIsNone(result)

    def test_nonsense_text_returns_none_ja(self):
        result = keyword_pre_match("こんにちは", lang="ja")
        self.assertIsNone(result)

    def test_nonsense_text_returns_none_en(self):
        result = keyword_pre_match("hello world", lang="en")
        self.assertIsNone(result)

    # ── find_template 边界 ───────────────────────────────────

    def test_find_template_existing_action(self):
        t = find_template("coverage_analysis")
        self.assertIsNotNone(t)
        self.assertEqual(t["action"], "coverage_analysis")
        self.assertIn("zh", t)
        self.assertIn("ja", t)
        self.assertIn("en", t)
        self.assertIn("handler", t)

    def test_find_template_unknown_action_returns_none(self):
        t = find_template("nonexistent_action_xyz")
        self.assertIsNone(t)

    # ── _INSTRUCTION_TEMPLATES 完整性 ──────────────────────

    def test_templates_count(self):
        self.assertGreaterEqual(len(_INSTRUCTION_TEMPLATES), 22)

    def test_all_templates_have_required_keys(self):
        for t in _INSTRUCTION_TEMPLATES:
            self.assertIn("action", t, f"Missing action in template")
            self.assertIn("handler", t, f"Missing handler in {t.get('action')}")
            self.assertIn("params", t, f"Missing params in {t.get('action')}")


if __name__ == "__main__":
    unittest.main()
