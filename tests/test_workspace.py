"""test_workspace — P3-1 工作区 manifest 测试。

覆盖：
- WorkspaceManager create/save/open/list/delete 往返
- manifest 完整性（id/name/country/created_at/updated_at/notes/layers）
- open 源文件缺失显式报错（不静默）
- template_registry 触发词（中/日/英）与 system prompt 动作清单追加
- InstructionMapper 路由到 workspace_* handler
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.workspace import WorkspaceManager
from src.core.template_registry import (
    _INSTRUCTION_TEMPLATES,
    _SYSTEM_PROMPT_EN,
    _SYSTEM_PROMPT_JA,
    _SYSTEM_PROMPT_ZH,
    keyword_pre_match,
)


class TestWorkspaceManager(unittest.TestCase):
    """WorkspaceManager 核心 API 往返测试。"""

    def setUp(self):
        self.tmp_root = tempfile.mkdtemp(prefix="ws_test_")
        self.mgr = WorkspaceManager(root_dir=self.tmp_root)

    def tearDown(self):
        shutil.rmtree(self.tmp_root, ignore_errors=True)

    def test_create_manifest_completeness(self):
        result = self.mgr.create(name="神户案例", country="jp", notes="测试")
        self.assertTrue(result["success"])
        ws_id = result["workspace_id"]
        self.assertTrue(ws_id)

        manifest_path = os.path.join(self.tmp_root, ws_id, "workspace.json")
        self.assertTrue(os.path.isfile(manifest_path), "workspace.json 应已创建")
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)

        self.assertEqual(manifest["id"], ws_id)
        self.assertEqual(manifest["name"], "神户案例")
        self.assertEqual(manifest["country"], "jp")
        self.assertEqual(manifest["notes"], "测试")
        self.assertTrue(manifest["created_at"])
        self.assertEqual(manifest["updated_at"], manifest["created_at"])
        self.assertEqual(manifest["layers"], [])

    def test_create_list_roundtrip(self):
        r1 = self.mgr.create(name="A", country="jp")
        r2 = self.mgr.create(name="B", country="cn")
        listing = self.mgr.list()
        self.assertTrue(listing["success"])
        self.assertEqual(len(listing["workspaces"]), 2)
        ids = {w["id"] for w in listing["workspaces"]}
        self.assertEqual(ids, {r1["workspace_id"], r2["workspace_id"]})
        # 按 updated_at 倒序
        times = [w["updated_at"] for w in listing["workspaces"]]
        self.assertEqual(times, sorted(times, reverse=True))

    def test_list_empty(self):
        listing = self.mgr.list()
        self.assertTrue(listing["success"])
        self.assertEqual(listing["workspaces"], [])

    def test_save_with_qgis_layers(self):
        """真实 QGIS：添加内存图层后 save，manifest.layers 有记录。"""
        try:
            from qgis.core import QgsProject, QgsVectorLayer
        except ImportError:
            self.skipTest("QGIS 环境不可用")

        proj = QgsProject.instance()
        for lid in list(proj.mapLayers().keys()):
            proj.removeMapLayer(lid)

        layer = QgsVectorLayer("Point?crs=EPSG:4326", "shelters", "memory")
        self.assertTrue(layer.isValid(), "内存图层创建失败")
        proj.addMapLayer(layer)

        r = self.mgr.create(name="save_ws", country="jp")
        ws_id = r["workspace_id"]
        s = self.mgr.save(ws_id, project=proj)
        self.assertTrue(s["success"], s["message"])
        self.assertEqual(s["layer_count"], 1)

        with open(os.path.join(self.tmp_root, ws_id, "workspace.json"), "r", encoding="utf-8") as f:
            manifest = json.load(f)
        self.assertEqual(len(manifest["layers"]), 1)
        entry = manifest["layers"][0]
        self.assertEqual(entry["name"], "shelters")
        self.assertIn("crs", entry)
        self.assertEqual(entry["style"], {})

    def test_save_unknown_workspace(self):
        s = self.mgr.save("not_exist_ws")
        self.assertFalse(s["success"])

    def test_open_missing_file_errors(self):
        """manifest 指向不存在的源文件 → open 显式报错，不静默。"""
        r = self.mgr.create(name="ws_missing", country="jp")
        ws_id = r["workspace_id"]
        manifest_path = os.path.join(self.tmp_root, ws_id, "workspace.json")
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        manifest["layers"] = [
            {"name": "ghost", "source": os.path.join(self.tmp_root, "no_such_file.shp"),
             "crs": "EPSG:4326", "style": {}}
        ]
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)

        result = self.mgr.open(ws_id)
        self.assertFalse(result["success"])
        self.assertIn("缺失", result["message"])
        self.assertEqual(len(result["missing"]), 1)
        self.assertEqual(result["missing"][0]["name"], "ghost")
        self.assertEqual(result["loaded"], [])

    def test_open_geojson_roundtrip(self):
        """真实 QGIS：临时 GeoJSON 验证 save → open 往返恢复图层。"""
        try:
            from qgis.core import QgsProject, QgsVectorLayer
        except ImportError:
            self.skipTest("QGIS 环境不可用")

        geojson_path = os.path.join(self.tmp_root, "shelters.geojson")
        with open(geojson_path, "w", encoding="utf-8") as f:
            json.dump({
                "type": "FeatureCollection",
                "features": [
                    {"type": "Feature", "properties": {"name": "A"},
                     "geometry": {"type": "Point", "coordinates": [139.0, 35.0]}}
                ],
            }, f)

        proj = QgsProject.instance()
        for lid in list(proj.mapLayers().keys()):
            proj.removeMapLayer(lid)
        layer = QgsVectorLayer(geojson_path, "shelters_geo", "ogr")
        self.assertTrue(layer.isValid(), "GeoJSON 加载失败")
        proj.addMapLayer(layer)

        r = self.mgr.create(name="ws_geo", country="jp")
        ws_id = r["workspace_id"]
        self.mgr.save(ws_id, project=proj)

        # 清空项目模拟切换场景
        for lid in list(proj.mapLayers().keys()):
            proj.removeMapLayer(lid)

        result = self.mgr.open(ws_id, project=proj)
        self.assertTrue(result["success"], result["message"])
        self.assertEqual(len(result["loaded"]), 1)
        names = [l.name() for l in proj.mapLayers().values()]
        self.assertIn("shelters_geo", names)

    def test_delete(self):
        r = self.mgr.create(name="del_ws", country="jp")
        ws_id = r["workspace_id"]
        self.assertTrue(os.path.isdir(os.path.join(self.tmp_root, ws_id)))
        d = self.mgr.delete(ws_id)
        self.assertTrue(d["success"])
        self.assertFalse(os.path.exists(os.path.join(self.tmp_root, ws_id)))


class TestWorkspaceTemplates(unittest.TestCase):
    """template_registry 触发词 + system prompt 动作清单（仅追加验证）。"""

    def test_zh_trigger_words(self):
        cases = [
            ("新建一个工作区", "workspace_new"),
            ("帮我创建工作区", "workspace_new"),
            ("保存工作区", "workspace_save"),
            ("把当前状态保存到工作区", "workspace_save"),
            ("打开工作区", "workspace_open"),
            ("恢复工作区", "workspace_open"),
            ("列出工作区", "workspace_list"),
            ("有哪些工作区", "workspace_list"),
        ]
        for text, expected in cases:
            result = keyword_pre_match(text, lang="zh")
            self.assertIsNotNone(result, f"触发词未命中：{text}")
            self.assertEqual(result["action"], expected, f"触发词映射错误：{text}")

    def test_ja_trigger_words(self):
        cases = [
            ("ワークスペースを新規作成", "workspace_new"),
            ("ワークスペースを保存", "workspace_save"),
            ("ワークスペースを開く", "workspace_open"),
            ("ワークスペース一覧", "workspace_list"),
        ]
        for text, expected in cases:
            result = keyword_pre_match(text, lang="ja")
            self.assertIsNotNone(result, f"JA 触发词未命中：{text}")
            self.assertEqual(result["action"], expected, f"JA 触发词映射错误：{text}")

    def test_en_trigger_words(self):
        cases = [
            ("create workspace", "workspace_new"),
            ("save workspace", "workspace_save"),
            ("open workspace", "workspace_open"),
            ("list workspace", "workspace_list"),
        ]
        for text, expected in cases:
            result = keyword_pre_match(text, lang="en")
            self.assertIsNotNone(result, f"EN 触发词未命中：{text}")
            self.assertEqual(result["action"], expected, f"EN 触发词映射错误：{text}")

    def test_system_prompt_action_list(self):
        for prompt in (_SYSTEM_PROMPT_ZH, _SYSTEM_PROMPT_JA, _SYSTEM_PROMPT_EN):
            for action in ("workspace_new", "workspace_save", "workspace_open", "workspace_list"):
                self.assertIn(action, prompt, f"{prompt[:20]}... 缺少 {action}")

    def test_templates_have_four_workspace_actions(self):
        actions = {t["action"] for t in _INSTRUCTION_TEMPLATES}
        for a in ("workspace_new", "workspace_save", "workspace_open", "workspace_list"):
            self.assertIn(a, actions)

    def test_existing_templates_untouched(self):
        """原有 22 条模板仍在（仅追加语义验证）。"""
        actions = {t["action"] for t in _INSTRUCTION_TEMPLATES}
        for a in ("load_layer", "save_project", "coverage_analysis",
                  "population_coverage", "building_risk_analysis"):
            self.assertIn(a, actions)


class TestWorkspaceHandlers(unittest.TestCase):
    """InstructionMapper 路由：workspace_* 指令被正确路由到 handler。"""

    def _mapper(self):
        from src.core.instruction_mapper import InstructionMapper
        return InstructionMapper()

    def test_route_workspace_new(self):
        with patch("core.workspace.WorkspaceManager") as MockWS:
            inst = MockWS.return_value
            inst.create.return_value = {"success": True, "message": "ok", "workspace_id": "x"}
            mapper = self._mapper()
            result = mapper.match_and_execute(
                '{"action":"workspace_new","params":{"name":"神户"}}',
                project=None,
            )
            self.assertTrue(result["success"])
            self.assertEqual(result["action"], "workspace_new")
            inst.create.assert_called_once_with(name="神户", country="jp", notes="")

    def test_route_workspace_list(self):
        with patch("core.workspace.WorkspaceManager") as MockWS:
            inst = MockWS.return_value
            inst.list.return_value = {"success": True, "message": "共 0 个工作区", "workspaces": []}
            mapper = self._mapper()
            result = mapper.match_and_execute(
                '{"action":"workspace_list","params":{}}',
                project=None,
            )
            self.assertTrue(result["success"])
            self.assertEqual(result["action"], "workspace_list")
            inst.list.assert_called_once()

    def test_route_workspace_save_without_id(self):
        """workspace_save 缺省 workspace_id → 取最近工作区。"""
        with patch("core.workspace.WorkspaceManager") as MockWS:
            inst = MockWS.return_value
            inst.list.return_value = {
                "success": True, "message": "共 1 个工作区",
                "workspaces": [{"id": "ws1", "name": "A", "country": "jp", "updated_at": "t"}],
            }
            inst.save.return_value = {"success": True, "message": "已保存", "workspace_id": "ws1"}
            mapper = self._mapper()
            result = mapper.match_and_execute(
                '{"action":"workspace_save","params":{}}',
                project=None,
            )
            self.assertTrue(result["success"])
            inst.save.assert_called_once()
            args, kwargs = inst.save.call_args
            self.assertEqual(args[0], "ws1")
            self.assertIn("project", kwargs)

    def test_route_workspace_open(self):
        with patch("core.workspace.WorkspaceManager") as MockWS:
            inst = MockWS.return_value
            inst.open.return_value = {"success": True, "message": "已打开", "loaded": ["a"]}
            mapper = self._mapper()
            result = mapper.match_and_execute(
                '{"action":"workspace_open","params":{"workspace_id":"ws1"}}',
                project=None,
            )
            self.assertTrue(result["success"])
            inst.open.assert_called_once()
            args, kwargs = inst.open.call_args
            self.assertEqual(args[0], "ws1")

    def test_unknown_action_still_unknown(self):
        mapper = self._mapper()
        result = mapper.match_and_execute(
            '{"action":"unknown","message":"识别不了"}',
            project=None,
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["action"], "unknown")


if __name__ == "__main__":
    unittest.main()
