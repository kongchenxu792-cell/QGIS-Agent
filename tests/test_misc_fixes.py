"""test_misc_fixes — 杂项修复测试。

覆盖：
- 人口图层兜底（单候选自动兜底 + info 消息、多候选歧义返回澄清请求（P2-4）、
  无候选报错、非空参数不受影响）
- bootstrap_qgis 便携引擎失败回退系统 QGIS 的显式警告 message 断言
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


# ── 人口图层兜底 ──────────────────────────────────────────────

class TestPolygonLayerAutoFallback(unittest.TestCase):
    """_auto_select_polygon_layer 单候选兜底判定。"""

    def _make_layer(self, kind: str, name: str):
        """构造真实 QGIS 内存图层（Polygon / Point）。"""
        from qgis.core import QgsVectorLayer
        if kind == "polygon":
            layer = QgsVectorLayer("Polygon?crs=EPSG:4326", name, "memory")
        else:
            layer = QgsVectorLayer("Point?crs=EPSG:4326", name, "memory")
        if not layer.isValid():
            self.skipTest(f"内存图层创建失败: {name}")
        return layer

    def _mapper(self):
        from src.core.instruction_mapper import InstructionMapper
        return InstructionMapper()

    def test_single_polygon_candidate_returns_name(self):
        mapper = self._mapper()
        poly = self._make_layer("polygon", "东京行政区")
        proj = MagicMock()
        proj.mapLayers.return_value = {"1": poly}
        self.assertEqual(mapper._auto_select_polygon_layer(proj), "东京行政区")

    def test_polygon_plus_point_single_candidate(self):
        """面候选=1、点图层不干扰 → 仍返回面图层名。"""
        mapper = self._mapper()
        poly = self._make_layer("polygon", "东京行政区")
        point = self._make_layer("point", "避难所")
        proj = MagicMock()
        proj.mapLayers.return_value = {"1": point, "2": poly}
        self.assertEqual(mapper._auto_select_polygon_layer(proj), "东京行政区")

    def test_multi_polygon_candidates_empty(self):
        """多个矢量面候选 → 歧义，返回空（不擅自选）。"""
        mapper = self._mapper()
        poly1 = self._make_layer("polygon", "东京行政区")
        poly2 = self._make_layer("polygon", "大阪行政区")
        proj = MagicMock()
        proj.mapLayers.return_value = {"1": poly1, "2": poly2}
        self.assertEqual(mapper._auto_select_polygon_layer(proj), "")

    def test_no_polygon_candidate_empty(self):
        mapper = self._mapper()
        point = self._make_layer("point", "避难所")
        proj = MagicMock()
        proj.mapLayers.return_value = {"1": point}
        self.assertEqual(mapper._auto_select_polygon_layer(proj), "")

    def test_no_project_empty(self):
        mapper = self._mapper()
        self.assertEqual(mapper._auto_select_polygon_layer(None), "")


class TestPopulationCoverageFallback(unittest.TestCase):
    """_handle_population_coverage 兜底集成：info 消息 + message 前缀。"""

    def setUp(self):
        from qgis.core import QgsVectorLayer
        self.poly = QgsVectorLayer("Polygon?crs=EPSG:4326", "东京行政区", "memory")
        self.poly2 = QgsVectorLayer("Polygon?crs=EPSG:4326", "大阪行政区", "memory")
        self.point = QgsVectorLayer("Point?crs=EPSG:4326", "避难所", "memory")
        if not (self.poly.isValid() and self.poly2.isValid() and self.point.isValid()):
            self.skipTest("内存图层创建失败")

    def _mapper(self):
        from src.core.instruction_mapper import InstructionMapper
        return InstructionMapper()

    def _proj(self, layers):
        proj = MagicMock()
        proj.mapLayers.return_value = {str(i): layer for i, layer in enumerate(layers, 1)}
        return proj

    @patch("core.pipeline_executor.PipelineExecutor.execute",
           return_value={"success": True, "message": "人口覆盖率分析完成"})
    def test_auto_fallback_info_message(self, mock_execute):
        mapper = self._mapper()
        proj = self._proj([self.point, self.poly])
        result = mapper._handle_population_coverage(
            project=proj, source_layer="避难所", boundary_layer="东京行政区",
            population_layer="", population_field="population",
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["info"], "已自动选择人口图层：东京行政区")
        self.assertTrue(result["message"].startswith("已自动选择人口图层：东京行政区；"))
        # 确认 pipeline 收到的 population_layer_name 是自动选择的图层
        kwargs = mock_execute.call_args[1]
        self.assertEqual(kwargs["population_layer_name"], "东京行政区")

    @patch("core.pipeline_executor.PipelineExecutor.execute",
           return_value={"success": True, "message": "人口覆盖率分析完成"})
    def test_no_fallback_when_param_provided(self, mock_execute):
        mapper = self._mapper()
        proj = self._proj([self.poly, self.poly2])
        result = mapper._handle_population_coverage(
            project=proj, source_layer="s", boundary_layer="b",
            population_layer="大阪行政区", population_field="population",
        )
        self.assertTrue(result["success"])
        # 参数显式提供 → 不追加兜底 info 前缀
        self.assertEqual(result["message"], "人口覆盖率分析完成")
        self.assertNotIn("info", result)
        kwargs = mock_execute.call_args[1]
        self.assertEqual(kwargs["population_layer_name"], "大阪行政区")

    def test_multi_candidate_requests_clarification(self):
        """P2-4：多面候选歧义且无 population_layer → 返回澄清请求结构（不执行）。"""
        mapper = self._mapper()
        proj = self._proj([self.poly, self.poly2])
        result = mapper._handle_population_coverage(
            project=proj, source_layer="s", boundary_layer="b",
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
        # 澄清轮不执行、不记录 run（P3-2 衔接）
        self.assertNotIn("output_layer", result)

    def test_no_polygon_candidate_still_errors(self):
        mapper = self._mapper()
        proj = self._proj([self.point])
        result = mapper._handle_population_coverage(
            project=proj, source_layer="s", boundary_layer="b",
            population_layer="", population_field="population",
        )
        self.assertFalse(result["success"])
        self.assertIn("population_layer", result["message"])


# ── bootstrap_qgis 回退警告 ──────────────────────────────────

class TestBootstrapFallbackWarning(unittest.TestCase):
    """便携引擎加载失败回退系统 QGIS 时，message 显式标注。"""

    def setUp(self):
        # 保存环境变量以便恢复
        self._saved_env = {
            k: os.environ.get(k)
            for k in ("QGIS_PREFIX_PATH", "GDAL_DATA", "AIQGIS_GDAL_DATA",
                      "PROJ_DATA", "PROJ_LIB", "AIQGIS_PROJ_DATA", "PATH", "PYTHONPATH")
        }
        # 确保 qgis.core 已缓存，bootstrap 对伪造系统候选可直接命中 sys.modules
        try:
            from qgis.core import QgsApplication  # noqa: F401
        except ImportError:
            self.skipTest("QGIS 环境不可用")

    def tearDown(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _make_portable_candidate(self):
        """构造一个路径含 qgis-portable、但无 python 子目录的失败便携候选。"""
        tmp = tempfile.mkdtemp(prefix="fake_portable_")
        base = Path(tmp) / "qgis-portable" / "apps" / "qgis-ltr"
        base.mkdir(parents=True)
        return str(base), tmp

    @patch("src.core.qgis_env.discover_qgis_prefix_candidates")
    def test_fallback_to_system_adds_warning(self, mock_discover):
        portable, tmp = self._make_portable_candidate()
        # 第二个候选：伪造系统路径（无 qgis-portable 字样，python 目录存在）
        fake_system = Path(tempfile.mkdtemp(prefix="fake_system_")) / "apps" / "qgis-ltr"
        fake_system.mkdir(parents=True)
        (fake_system / "python").mkdir()
        mock_discover.return_value = [portable, str(fake_system)]

        from src.core.qgis_env import bootstrap_qgis
        result = bootstrap_qgis()

        self.assertTrue(result.available)
        self.assertIn("便携引擎加载失败，已回退系统 QGIS：", result.message)
        self.assertIn("python 目录不存在", result.message)
        self.assertEqual(len(result.candidate_paths), 2)

    @patch("src.core.qgis_env.discover_qgis_prefix_candidates")
    def test_no_warning_when_portable_succeeds(self, mock_discover):
        # 候选唯一且为当前真实便携环境（QGIS_PREFIX_PATH）→ 不标注回退
        real_prefix = os.environ.get("QGIS_PREFIX_PATH")
        if not real_prefix:
            self.skipTest("无 QGIS_PREFIX_PATH 环境变量")
        mock_discover.return_value = [real_prefix]

        from src.core.qgis_env import bootstrap_qgis
        result = bootstrap_qgis()
        self.assertTrue(result.available)
        self.assertNotIn("便携引擎加载失败", result.message)


if __name__ == "__main__":
    unittest.main()
