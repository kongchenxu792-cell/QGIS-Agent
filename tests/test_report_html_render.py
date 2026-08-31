"""test_report_html_render — 片B 报告 Markdown→HTML 渲染测试。

覆盖：
- _md_to_html：标题 / 无序列表 / 表格 / 预警块 / 加粗 / 行内代码 / 转义 / 空输入 / 段落
- 预警块高亮样式（覆盖率低于阈值时醒目色）
- _maybe_generate_disaster_report 渲染分支：
  HTML 模式 → setHtml；纯文本模式 → append；读取失败回退 append
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.ui.main_window import _md_to_html, MainWindow


class TestMdToHtml(unittest.TestCase):
    def test_headers(self):
        html = _md_to_html("# 标题1\n## 标题2\n### 标题3")
        self.assertIn("<h1>", html)
        self.assertIn("标题1", html)
        self.assertIn("<h2>", html)
        self.assertIn("标题2", html)
        self.assertIn("<h3>", html)
        self.assertIn("标题3", html)

    def test_list(self):
        html = _md_to_html("- **A**: 1\n- **B**: 2")
        self.assertIn("<ul>", html)
        self.assertIn("<li><b>A</b>", html)
        self.assertIn("<li><b>B</b>", html)

    def test_table(self):
        md = "| 列1 | 列2 |\n| --- | --- |\n| 10 | 20 |"
        html = _md_to_html(md)
        self.assertIn("<table", html)
        self.assertIn("background-color:#eef3fb", html)  # 表头高亮
        self.assertIn(">列1</th>", html)
        self.assertIn(">列2</th>", html)
        self.assertIn("<td>10</td>", html)
        self.assertIn("<td>20</td>", html)

    def test_warning_highlight(self):
        html = _md_to_html("> ⚠️ 覆盖率预警信息")
        self.assertIn("background-color:#fdecea", html)
        self.assertIn("color:#c0392b", html)
        self.assertIn("⚠️", html)

    def test_bold_and_code(self):
        html = _md_to_html("**粗体** 和 `code`")
        self.assertIn("<b>粗体</b>", html)
        self.assertIn("<code>code</code>", html)

    def test_escape(self):
        html = _md_to_html("<script>alert(1)</script>")
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_empty_input(self):
        html = _md_to_html("")
        self.assertTrue(html.startswith("<html>"))
        self.assertTrue(html.endswith("</html>"))

    def test_paragraph(self):
        html = _md_to_html("普通段落文字")
        self.assertIn("<p>普通段落文字</p>", html)

    def test_real_report_like(self):
        # 与 report_generator 产出的真实结构对齐
        md = (
            "# 风险评估报告\n"
            "\n"
            "- **生成时间**: 2026-08-31 19:35:54\n"
            "- **灾种**: 洪涝 (`chengdu`)\n"
            "\n"
            "## 统计概览\n"
            "\n"
            "| 源数量 | 半径 | 总面积 | 覆盖面积 | 覆盖率 |\n"
            "| --- | --- | --- | --- | --- |\n"
            "| 37 | 500 | 6180759065 | 9055286 | **0.1465%** |\n"
            "\n"
            "## 覆盖率预警\n"
            "\n"
            "> ⚠️ 「洪涝」危险区覆盖率仅为 0.15%，低于预警阈值 50.00%，建议核查避难所布点。\n"
            "\n"
            "### 备注\n"
            "\n"
            "本报告由系统自动生成。"
        )
        html = _md_to_html(md)
        self.assertIn("<h1>", html)
        self.assertIn("0.1465%", html)
        self.assertIn("background-color:#fdecea", html)
        self.assertIn("本报告由系统自动生成", html)


def _make_window_mock(plain_mode: bool):
    mw = MagicMock()
    mw._report_plain_mode = plain_mode
    mw.disaster_combo = None
    mw._lm.current_lang = "zh"
    mw._lm.tr = MagicMock(side_effect=lambda key, **kw: f"[{key}]")
    mw.statusBar = MagicMock()
    mw.ai_response_display = MagicMock()
    return mw


class TestRenderBranch(unittest.TestCase):
    FAKE_MD = "# 报告\n\n> ⚠️ 预警"

    @patch("src.ui.main_window.generate_report")
    @patch("src.ui.main_window.QMessageBox")
    @patch("src.ui.main_window.find_disaster_by_text")
    def test_html_mode_uses_setHtml(self, find_mock, qmb, gen_mock):
        find_mock.return_value = {"disaster_id": "chengdu"}
        gen_mock.return_value = {
            "success": True,
            "report_path": str(Path(__file__).resolve().parent / "_fake_report.md"),
            "warning": "预警内容",
            "disaster_name": "洪涝",
            "coverage_rate": 0.1465,
        }
        md_path = Path(__file__).resolve().parent / "_fake_report.md"
        md_path.write_text(self.FAKE_MD, encoding="utf-8")
        try:
            mw = _make_window_mock(plain_mode=False)
            MainWindow._maybe_generate_disaster_report(
                mw, {"stats": {"coverage_rate": 0.1465}}, "成都洪涝分析"
            )
            mw.ai_response_display.setHtml.assert_called_once()
            html_arg = mw.ai_response_display.setHtml.call_args[0][0]
            self.assertIn("background-color:#fdecea", html_arg)
            mw.ai_response_display.append.assert_not_called()
        finally:
            md_path.unlink(missing_ok=True)

    @patch("src.ui.main_window.generate_report")
    @patch("src.ui.main_window.QMessageBox")
    @patch("src.ui.main_window.find_disaster_by_text")
    def test_plain_mode_uses_append(self, find_mock, qmb, gen_mock):
        find_mock.return_value = {"disaster_id": "chengdu"}
        gen_mock.return_value = {
            "success": True,
            "report_path": str(Path(__file__).resolve().parent / "_fake_report.md"),
            "warning": "",
            "disaster_name": "洪涝",
            "coverage_rate": 0.1465,
        }
        md_path = Path(__file__).resolve().parent / "_fake_report.md"
        md_path.write_text(self.FAKE_MD, encoding="utf-8")
        try:
            mw = _make_window_mock(plain_mode=True)
            MainWindow._maybe_generate_disaster_report(
                mw, {"stats": {"coverage_rate": 0.1465}}, "成都洪涝分析"
            )
            mw.ai_response_display.setHtml.assert_not_called()
            mw.ai_response_display.append.assert_called_once()
        finally:
            md_path.unlink(missing_ok=True)

    @patch("src.ui.main_window.generate_report")
    @patch("src.ui.main_window.QMessageBox")
    @patch("src.ui.main_window.find_disaster_by_text")
    def test_read_fail_falls_back_append(self, find_mock, qmb, gen_mock):
        find_mock.return_value = {"disaster_id": "chengdu"}
        gen_mock.return_value = {
            "success": True,
            "report_path": r"C:\__no_such_file__.md",
            "warning": "",
            "disaster_name": "洪涝",
            "coverage_rate": 0.1465,
        }
        mw = _make_window_mock(plain_mode=False)
        MainWindow._maybe_generate_disaster_report(
            mw, {"stats": {"coverage_rate": 0.1465}}, "成都洪涝分析"
        )
        mw.ai_response_display.setHtml.assert_not_called()
        mw.ai_response_display.append.assert_called_once()

    def test_set_report_plain_mode_toggle(self):
        stub = MainWindow.__new__(MainWindow)
        stub._report_plain_mode = False
        stub.set_report_plain_mode(True)
        self.assertTrue(stub._report_plain_mode)
        stub.set_report_plain_mode(False)
        self.assertFalse(stub._report_plain_mode)


if __name__ == "__main__":
    unittest.main()
