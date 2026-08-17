"""test_runs — P3-2 运行日志 + 回滚测试。

覆盖：
- WorkspaceManager record_run / list_runs / get_run / replay_run 往返
- status 三态记录（ok / degraded / failed）
- source_hashes 变更 → replay 返回 warning（不阻断）
- 无工作区（或工作区不存在）→ 落 user_data/workspaces/_recent/
- outputs / result 摘要记录
- 分析链四 handler 收口挂钩：coverage / population / building_risk / gap 自动 record
- 参数校验类提前 return 不记录；记录失败不阻断主流程
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


def _write_source(tmp_root: str, name: str = "shelters.geojson", content: str = "abc") -> str:
    """写一个真实源文件，用于 source_hashes 计算。"""
    path = os.path.join(tmp_root, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def _make_ws_with_source(mgr: WorkspaceManager, tmp_root: str, src: str) -> str:
    """创建工作区并把 manifest.layers 指向真实源文件（模拟 save 后状态）。"""
    r = mgr.create(name="run_ws", country="jp")
    ws_id = r["workspace_id"]
    manifest_path = os.path.join(tmp_root, ws_id, "workspace.json")
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    manifest["layers"] = [{"name": "shelters", "source": src,
                           "crs": "EPSG:4326", "style": {}}]
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    return ws_id


class TestRunRecords(unittest.TestCase):
    """WorkspaceManager runs API 往返测试（纯文件系统，无需 QGIS）。"""

    def setUp(self):
        self.tmp_root = tempfile.mkdtemp(prefix="runs_test_")
        self.mgr = WorkspaceManager(root_dir=self.tmp_root)

    def tearDown(self):
        shutil.rmtree(self.tmp_root, ignore_errors=True)

    def _ok_result(self, **overrides):
        base = {
            "success": True, "status": "ok",
            "message": "覆盖率分析完成，新增图层：coverage_out（120 个要素）",
            "feature_count": 120,
            "output_file": os.path.join(self.tmp_root, "coverage_out.geojson"),
            "stats": {"coverage_rate": 32.7, "total_area": 1000},
        }
        base.update(overrides)
        return base

    def test_record_list_get_replay_roundtrip(self):
        src = _write_source(self.tmp_root)
        ws_id = _make_ws_with_source(self.mgr, self.tmp_root, src)

        rec = self.mgr.record_run(
            ws_id, "coverage_analysis",
            {"source_layer": "shelters", "boundary_layer": "wards", "radius_m": 500},
            self._ok_result(),
        )
        self.assertTrue(rec["success"])
        run_id = rec["run_id"]
        self.assertTrue(run_id)

        # 落盘路径：<root>/<id>/runs/<run_id>.json
        run_path = os.path.join(self.tmp_root, ws_id, "runs", f"{run_id}.json")
        self.assertTrue(os.path.isfile(run_path))

        # list_runs
        listing = self.mgr.list_runs(ws_id)
        self.assertTrue(listing["success"])
        self.assertEqual(len(listing["runs"]), 1)
        entry = listing["runs"][0]
        self.assertEqual(entry["run_id"], run_id)
        self.assertEqual(entry["template_id"], "coverage_analysis")
        self.assertEqual(entry["status"], "ok")
        self.assertTrue(entry["created_at"])

        # get_run 完整记录
        got = self.mgr.get_run(ws_id, run_id)
        self.assertTrue(got["success"])
        self.assertEqual(got["record"]["run_id"], run_id)
        self.assertEqual(got["record"]["params"]["radius_m"], 500)

        # replay_run：hash 未变 → ok，无 warning
        replay = self.mgr.replay_run(ws_id, run_id)
        self.assertTrue(replay["ok"])
        self.assertEqual(replay["template_id"], "coverage_analysis")
        self.assertEqual(replay["params"]["boundary_layer"], "wards")
        self.assertNotIn("warning", replay)

    def test_status_tri_state(self):
        src = _write_source(self.tmp_root)
        ws_id = _make_ws_with_source(self.mgr, self.tmp_root, src)
        cases = [
            ("ok", {"success": True, "status": "ok"}),
            ("degraded", {"success": True, "status": "degraded",
                          "message": "结果为空，请检查数据/参数"}),
            ("failed", {"success": False, "status": "failed",
                        "message": "输出步骤失败"}),
            # 无 status 字段时按 success 推断
            ("failed", {"success": False, "message": "图层未找到"}),
        ]
        run_ids = []
        for expect, result in cases:
            r = self.mgr.record_run(ws_id, "coverage_analysis", {}, result)
            self.assertTrue(r["success"])
            run_ids.append(r["run_id"])
            got = self.mgr.get_run(ws_id, r["run_id"])
            self.assertEqual(got["record"]["status"], expect,
                             f"status={result} 应记录为 {expect}")

        listing = self.mgr.list_runs(ws_id)
        statuses = {e["run_id"]: e["status"] for e in listing["runs"]}
        for rid in run_ids:
            self.assertIn(rid, statuses)

    def test_hash_change_warning(self):
        src = _write_source(self.tmp_root, content="original-data-v1")
        ws_id = _make_ws_with_source(self.mgr, self.tmp_root, src)
        r = self.mgr.record_run(ws_id, "coverage_analysis", {}, self._ok_result())
        run_id = r["run_id"]

        replay_ok = self.mgr.replay_run(ws_id, run_id)
        self.assertTrue(replay_ok["ok"])
        self.assertNotIn("warning", replay_ok)

        # 改画布等价场景：修改源文件内容 → hash 变更
        with open(src, "w", encoding="utf-8") as f:
            f.write("changed-data-v2")

        replay = self.mgr.replay_run(ws_id, run_id)
        self.assertTrue(replay["ok"], "hash 变更不应阻断重放")
        self.assertIn("warning", replay)
        self.assertIn("源数据已变更", replay["warning"])
        self.assertIn(src, replay["changed_sources"])

    def test_no_workspace_goes_recent(self):
        """无工作区：记录落 user_data/workspaces/_recent/，list_runs(None) 可列出。"""
        src = _write_source(self.tmp_root)
        r = self.mgr.record_run(
            None, "population_coverage",
            {"source_layer": "shelters", "boundary_layer": "wards"},
            self._ok_result(feature_count=88),
            project=None,
        )
        self.assertTrue(r["success"])
        recent_path = os.path.join(self.tmp_root, "_recent", f"{r['run_id']}.json")
        self.assertTrue(os.path.isfile(recent_path), "无工作区应落 _recent 目录")

        listing = self.mgr.list_runs(None)
        self.assertTrue(listing["success"])
        self.assertEqual(len(listing["runs"]), 1)
        self.assertEqual(listing["runs"][0]["template_id"], "population_coverage")

        replay = self.mgr.replay_run(None, r["run_id"])
        self.assertTrue(replay["ok"])

    def test_unknown_workspace_falls_back_recent(self):
        """workspace_id 存在但 manifest 不存在 → 视同无工作区，落 _recent。"""
        r = self.mgr.record_run("no_such_ws", "gap_analysis", {}, self._ok_result())
        self.assertTrue(r["success"])
        self.assertTrue(os.path.isfile(
            os.path.join(self.tmp_root, "_recent", f"{r['run_id']}.json")))

    def test_outputs_recorded(self):
        src = _write_source(self.tmp_root)
        ws_id = _make_ws_with_source(self.mgr, self.tmp_root, src)
        out_file = os.path.join(self.tmp_root, "exports", "coverage_out.geojson")
        r = self.mgr.record_run(ws_id, "coverage_analysis", {}, self._ok_result(
            output_file=out_file))
        got = self.mgr.get_run(ws_id, r["run_id"])
        self.assertEqual(got["record"]["outputs"], [out_file])

    def test_summary_extraction(self):
        src = _write_source(self.tmp_root)
        ws_id = _make_ws_with_source(self.mgr, self.tmp_root, src)
        r = self.mgr.record_run(ws_id, "coverage_analysis", {}, self._ok_result())
        got = self.mgr.get_run(ws_id, r["run_id"])
        summary = got["record"]["result"]
        self.assertEqual(summary["feature_count"], 120)
        self.assertEqual(summary["stats"]["coverage_rate"], 32.7)
        self.assertIn("覆盖率分析完成", summary["message"])

    def test_source_hashes_from_manifest(self):
        src = _write_source(self.tmp_root, content="hash-me")
        ws_id = _make_ws_with_source(self.mgr, self.tmp_root, src)
        r = self.mgr.record_run(ws_id, "coverage_analysis", {}, self._ok_result())
        got = self.mgr.get_run(ws_id, r["run_id"])
        hashes = got["record"]["source_hashes"]
        self.assertIn(src, hashes)
        self.assertEqual(len(hashes[src]), 16, "sha256 前 16 位")

    def test_get_run_not_found(self):
        got = self.mgr.get_run("ws1", "no_such_run")
        self.assertFalse(got["success"])
        self.assertIn("不存在", got["message"])

    def test_replay_not_found(self):
        replay = self.mgr.replay_run("ws1", "no_such_run")
        self.assertFalse(replay["success"])

    def test_list_runs_empty(self):
        listing = self.mgr.list_runs("ws1")
        self.assertTrue(listing["success"])
        self.assertEqual(listing["runs"], [])


class TestAnalysisHook(unittest.TestCase):
    """分析链四 handler 收口挂钩：执行后自动 record_run。"""

    def setUp(self):
        from src.core.instruction_mapper import InstructionMapper
        self.mapper = InstructionMapper()

    def _patch_executor(self, result):
        """mock PipelineExecutor.execute 返回给定 result。"""
        mock_cls = MagicMock()
        mock_cls.return_value.execute.return_value = result
        patcher = patch("core.pipeline_executor.PipelineExecutor", mock_cls)
        return patcher, mock_cls

    def _patch_record(self):
        """patch WorkspaceManager.record_run，避免污染真实 user_data。"""
        mock_cls = MagicMock()
        mock_cls.return_value.record_run.return_value = {
            "success": True, "message": "ok", "run_id": "r1"}
        patcher = patch("core.workspace.WorkspaceManager", mock_cls)
        return patcher, mock_cls

    def test_coverage_hook_records_ok(self):
        patcher_exec, _ = self._patch_executor(
            {"success": True, "status": "ok", "message": "覆盖率分析完成",
             "feature_count": 10, "stats": {}, "output_file": ""})
        patcher_rec, mock_ws = self._patch_record()
        with patcher_exec, patcher_rec:
            result = self.mapper._handle_coverage_analysis(
                project=None, source_layer="shelters", boundary_layer="wards",
                radius_m=500)
        self.assertTrue(result["success"])
        mock_ws.return_value.record_run.assert_called_once()
        args, kwargs = mock_ws.return_value.record_run.call_args
        self.assertEqual(args[0], None)          # 无工作区 → _recent
        self.assertEqual(args[1], "coverage_analysis")
        self.assertEqual(args[2]["source_layer"], "shelters")
        self.assertEqual(args[2]["radius_m"], 500)
        self.assertEqual(args[3]["status"], "ok")

    def test_population_hook_records_degraded(self):
        patcher_exec, _ = self._patch_executor(
            {"success": True, "status": "degraded", "message": "结果为空",
             "feature_count": 0, "stats": {}, "output_file": ""})
        patcher_rec, mock_ws = self._patch_record()
        with patcher_exec, patcher_rec:
            result = self.mapper._handle_population_coverage(
                project=None, source_layer="shelters", boundary_layer="wards",
                population_layer="population", population_field="T_POP",
                radius_m=500)
        self.assertTrue(result["success"])
        mock_ws.return_value.record_run.assert_called_once()
        args, _ = mock_ws.return_value.record_run.call_args
        self.assertEqual(args[1], "population_coverage")
        self.assertEqual(args[2]["population_field"], "T_POP")

    def test_building_risk_hook_records_failed(self):
        patcher_exec, _ = self._patch_executor(
            {"success": False, "status": "failed", "message": "输出步骤失败",
             "feature_count": 0, "stats": {}, "output_file": ""})
        patcher_rec, mock_ws = self._patch_record()
        with patcher_exec, patcher_rec:
            result = self.mapper._handle_building_risk_analysis(
                project=None, intensity_layer="intensity",
                population_layer="population", population_field="T_POP",
                intensity_field="T30_I50_PS")
        self.assertFalse(result["success"])
        mock_ws.return_value.record_run.assert_called_once()
        args, _ = mock_ws.return_value.record_run.call_args
        self.assertEqual(args[1], "building_risk_analysis")
        self.assertEqual(args[2]["intensity_layer"], "intensity")

    def test_gap_hook_records(self):
        patcher_exec, _ = self._patch_executor(
            {"success": True, "status": "ok", "message": "盲区分析完成",
             "feature_count": 3, "stats": {}, "output_file": ""})
        patcher_rec, mock_ws = self._patch_record()
        with patcher_exec, patcher_rec:
            result = self.mapper._handle_gap_analysis(
                project=None, source_layer="shelters", boundary_layer="wards",
                radius_m=300)
        self.assertTrue(result["success"])
        mock_ws.return_value.record_run.assert_called_once()
        args, _ = mock_ws.return_value.record_run.call_args
        self.assertEqual(args[1], "gap_analysis")

    def test_param_error_not_recorded(self):
        """参数校验类提前 return（未进入引擎链）→ 不记录。"""
        patcher_rec, mock_ws = self._patch_record()
        with patcher_rec:
            result = self.mapper._handle_coverage_analysis(
                project=None, source_layer="", boundary_layer="wards")
        self.assertFalse(result["success"])
        mock_ws.return_value.record_run.assert_not_called()

    def test_record_failure_does_not_block(self):
        """record_run 抛异常 → 告警但主流程结果不受影响。"""
        patcher_exec, _ = self._patch_executor(
            {"success": True, "status": "ok", "message": "覆盖率分析完成",
             "feature_count": 10, "stats": {}, "output_file": ""})
        mock_cls = MagicMock()
        mock_cls.return_value.record_run.side_effect = RuntimeError("disk full")
        patcher_rec = patch("core.workspace.WorkspaceManager", mock_cls)
        with patcher_exec, patcher_rec:
            result = self.mapper._handle_coverage_analysis(
                project=None, source_layer="shelters", boundary_layer="wards")
        self.assertTrue(result["success"])
        self.assertEqual(result["feature_count"], 10)


if __name__ == "__main__":
    unittest.main()
