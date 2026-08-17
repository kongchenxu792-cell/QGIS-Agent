"""test_run_queue — P3-3 运行队列 + 资源预检测试。

覆盖（验收要求）：
- 入队 / 串行执行顺序（后台 QThread 逐个执行，不并发）
- 资源预检阈值（内存按源文件大小粗估、显存按 LLM 模型 4b≈3GB 基线）
- 资源不足时声带警告（不阻断入队）
- 工作区切换不打断队列（running 中入队新任务，前任务不终止、后任务排队）
- 队列状态查询（pending / running / queue_length / completed 完成历史）
- 失败任务不阻断后续任务；state_changed 信号

测试环境：PyQt5 QCoreApplication（qgis-portable Python312 自带）。
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.run_queue import (
    RunQueue,
    estimate_memory_mb,
    estimate_resources,
    estimate_vram_mb,
)


def _qapp():
    from PyQt5.QtCore import QCoreApplication
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication(["test_run_queue"])
    return app


def _write_sources(tmp_root: str, sizes_mb: dict) -> list:
    """写若干指定大小的源文件，返回路径列表（用于内存预检）。"""
    paths = []
    for name, mb in sizes_mb.items():
        path = os.path.join(tmp_root, name)
        with open(path, "wb") as f:
            f.write(b"x" * int(mb * 1048576))
        paths.append(path)
    return paths


def _wait_finished(queue: RunQueue, expected: int,
                   timeout_s: float = 20.0) -> list:
    """等待 expected 个任务全部结束，返回 [(name, ok, message)]（完成顺序）。

    通过 QCoreApplication.processEvents() 轮询驱动跨线程 queued 信号，
    不依赖嵌套事件循环，避免 exec_ 死锁风险。
    """
    from PyQt5.QtCore import QCoreApplication
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        QCoreApplication.processEvents()
        if len(queue.status()["completed"]) >= expected:
            break
        time.sleep(0.03)
    QCoreApplication.processEvents()
    return [(c["name"], c["ok"], c["message"])
            for c in queue.status()["completed"]][:expected]


class TestResourcePrecheck(unittest.TestCase):
    """资源预检：内存粗估 + 显存按模型估算 + 阈值警告。"""

    def setUp(self):
        self.tmp_root = tempfile.mkdtemp(prefix="rq_precheck_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp_root, ignore_errors=True)

    def test_memory_estimation_scales_with_source_size(self):
        paths = _write_sources(self.tmp_root, {"a.geojson": 10, "b.shp": 20})
        est = estimate_memory_mb(paths)
        # 基准 512MB + 30MB × 10 系数 = 812MB 附近
        self.assertGreaterEqual(est, 512.0)
        self.assertGreater(est, 800.0)
        self.assertLess(est, 900.0)

    def test_memory_estimation_skips_missing_file(self):
        est = estimate_memory_mb([os.path.join(self.tmp_root, "no_such.shp")])
        self.assertAlmostEqual(est, 512.0, places=0)

    def test_vram_4b_baseline(self):
        self.assertAlmostEqual(estimate_vram_mb("qwen3.5-4b"), 3072.0)
        self.assertAlmostEqual(estimate_vram_mb("ollama/llama3.1:4b"), 3072.0)

    def test_vram_model_tiers(self):
        self.assertAlmostEqual(estimate_vram_mb("qwen2.5-8b"), 6144.0)
        self.assertAlmostEqual(estimate_vram_mb("gemma2:2b"), 1536.0)
        self.assertAlmostEqual(estimate_vram_mb("tiny-1b"), 1024.0)

    def test_vram_unknown_or_empty_model(self):
        self.assertEqual(estimate_vram_mb(""), 0.0)
        self.assertEqual(estimate_vram_mb("qwen-plus"), 0.0)  # 云模型不估算

    def test_memory_threshold_warning(self):
        paths = _write_sources(self.tmp_root, {"big.geojson": 100})
        est = estimate_resources(
            {"sources": paths, "llm_model": ""},
            memory_limit_mb=300.0, vram_limit_mb=None, wait_count=1,
        )
        self.assertGreater(est["memory_mb"], 300.0)
        self.assertTrue(est["warnings"])
        joined = "；".join(est["warnings"])
        self.assertIn("资源紧张，排队执行", joined)
        self.assertIn("预计等待", joined)

    def test_vram_threshold_warning(self):
        est = estimate_resources(
            {"sources": [], "llm_model": "qwen3.5-4b"},
            memory_limit_mb=None, vram_limit_mb=2048.0, wait_count=2,
        )
        self.assertAlmostEqual(est["vram_mb"], 3072.0)
        self.assertTrue(est["warnings"])
        self.assertIn("资源紧张，排队执行", est["warnings"][0])

    def test_no_warning_when_within_limits(self):
        est = estimate_resources(
            {"sources": [], "llm_model": ""},
            memory_limit_mb=2048.0, vram_limit_mb=4096.0,
        )
        self.assertEqual(est["warnings"], [])


class TestRunQueueSerial(unittest.TestCase):
    """运行队列：入队 / 串行顺序 / 状态查询 / 失败 / 切换不打断。"""

    @classmethod
    def setUpClass(cls):
        # 必须保存引用：QCoreApplication 实例若被 GC 回收，跨线程信号无法投递
        cls._qapp_ref = _qapp()

    def setUp(self):
        self.queue = RunQueue(memory_limit_mb=None, vram_limit_mb=None,
                              avg_task_seconds=30.0)
        self.order: list = []
        self.warnings: list = []
        self.failures: list = []
        self.state_msgs: list = []
        self.queue.state_changed.connect(self.state_msgs.append)

    def _task(self, name, fn, **extra):
        base = {"name": name, "fn": fn}
        base.update(extra)
        return base

    def test_serial_execution_order(self):
        """3 个任务必须按入队顺序串行执行（严格先后，不并发）。"""
        barrier = []

        def make(name, delay):
            def fn():
                # 并发检测：若同时进入则 barrier 会被破坏
                barrier.append(name)
                time.sleep(delay)
                return {"success": True, "message": f"{name} done"}
            return fn

        self.queue.enqueue(self._task("t1", make("t1", 0.2),
                                      on_done=lambda r: self.order.append("t1")))
        self.queue.enqueue(self._task("t2", make("t2", 0.05),
                                      on_done=lambda r: self.order.append("t2")))
        self.queue.enqueue(self._task("t3", make("t3", 0.05),
                                      on_done=lambda r: self.order.append("t3")))
        seen = _wait_finished(self.queue, 3)
        self.assertEqual([s[0] for s in seen], ["t1", "t2", "t3"])
        # 串行：barrier 中元素必然顺序出现且无并发交叉
        self.assertEqual(barrier, ["t1", "t2", "t3"])
        self.assertEqual(self.order, ["t1", "t2", "t3"])

    def test_status_query(self):
        """pending / running / queue_length / completed 完成历史。"""
        # 空队列
        st = self.queue.status()
        self.assertIsNone(st["running"])
        self.assertEqual(st["pending"], [])
        self.assertEqual(st["queue_length"], 0)
        self.assertEqual(st["completed"], [])

        slow_holder = {}

        def slow():
            time.sleep(0.4)
            return {"success": True, "message": "slow done"}

        self.queue.enqueue(self._task("slow", slow))
        self.queue.enqueue(self._task("fast", lambda: {"success": True}))
        self.queue.enqueue(self._task("last", lambda: {"success": True}))
        # 等待第一个任务真正开始（worker 启动）
        deadline = time.time() + 5
        while time.time() < deadline:
            st = self.queue.status()
            if st["running"] == "slow":
                break
            time.sleep(0.02)
        st = self.queue.status()
        self.assertEqual(st["running"], "slow")
        self.assertEqual(st["pending"], ["fast", "last"])
        self.assertEqual(st["queue_length"], 3)
        self.assertEqual(st["completed"], [])

        _wait_finished(self.queue, 3)
        st = self.queue.status()
        self.assertIsNone(st["running"])
        self.assertEqual(st["pending"], [])
        self.assertEqual(st["queue_length"], 0)
        self.assertEqual(len(st["completed"]), 3)
        self.assertEqual([c["name"] for c in st["completed"]],
                         ["slow", "fast", "last"])

    def test_failure_does_not_block_next(self):
        """任务抛异常 → on_failed 声带回调 + 完成历史记 failed，后续任务继续。"""
        def boom():
            raise RuntimeError("模拟失败")

        self.queue.enqueue(self._task(
            "boom", boom,
            on_failed=lambda msg, res: self.failures.append(msg),
        ))
        self.queue.enqueue(self._task(
            "after", lambda: {"success": True, "message": "after done"},
        ))
        seen = _wait_finished(self.queue, 2)
        self.assertEqual(seen[0][0], "boom")
        self.assertFalse(seen[0][1])
        self.assertIn("RuntimeError", seen[0][2])
        self.assertEqual(len(self.failures), 1)
        self.assertIn("RuntimeError", self.failures[0])
        # 后续任务正常完成
        self.assertEqual(seen[1], ("after", True, "after done"))
        completed = self.queue.status()["completed"]
        self.assertEqual([c["name"] for c in completed], ["boom", "after"])
        self.assertFalse(completed[0]["ok"])
        self.assertTrue(completed[1]["ok"])

    def test_switch_workspace_does_not_interrupt(self):
        """模拟「A 分析入队执行中 → 切换到工作区 B」：队列不打断，A 跑完才跑 B。"""
        events: list = []

        def task_a():
            events.append("A_start")
            time.sleep(0.4)
            events.append("A_finish")
            return {"success": True, "message": "A done"}

        def task_b():
            events.append("B_start")
            return {"success": True, "message": "B done"}

        self.queue.enqueue(self._task("A", task_a))
        # 等 A 已进入 running（对应「正在跑时切换工作区」的时刻）
        deadline = time.time() + 5
        while time.time() < deadline:
            if self.queue.status()["running"] == "A":
                break
            time.sleep(0.02)
        self.assertEqual(self.queue.status()["running"], "A")
        # 此时「切换工作区」= 入队 B（队列任务与画布解耦，不打断 A）
        self.queue.enqueue(self._task("B", task_b))
        st = self.queue.status()
        self.assertEqual(st["running"], "A")
        self.assertEqual(st["pending"], ["B"])

        seen = _wait_finished(self.queue, 2)
        self.assertEqual([s[0] for s in seen], ["A", "B"])
        # A 完整跑完后才开始 B：严格串行
        self.assertLess(events.index("A_finish"), events.index("B_start"))
        self.assertEqual(self.queue.status()["completed"][0]["name"], "A")

    def test_warning_on_warning_callback(self):
        """资源不足时声带警告回调触发，但任务仍入队并执行完成（不阻断）。"""
        import shutil
        tmp_root = tempfile.mkdtemp(prefix="rq_warn_")
        try:
            paths = _write_sources(tmp_root, {"huge.geojson": 100})
            queue = RunQueue(memory_limit_mb=300.0, vram_limit_mb=None)
            queue.state_changed.connect(self.state_msgs.append)
            result = queue.enqueue(self._task(
                "heavy", lambda: {"success": True, "message": "heavy done"},
                sources=paths,
                on_warning=lambda msg: self.warnings.append(msg),
            ))
            self.assertTrue(result["queued"])
            self.assertTrue(result["warnings"])
            self.assertIn("资源紧张，排队执行", result["warnings"][0])
            self.assertEqual(len(self.warnings), 1)
            self.assertIn("资源紧张，排队执行", self.warnings[0])

            seen = _wait_finished(queue, 1)
            self.assertEqual(seen, [("heavy", True, "heavy done")])
        finally:
            shutil.rmtree(tmp_root, ignore_errors=True)

    def test_vram_warning_4b_over_limit(self):
        """显存预检：4b 模型 ≈3GB，阈值 2GB 时声带警告但不阻断。"""
        result = self.queue.enqueue(self._task(
            "llm_task", lambda: {"success": True, "message": "ok"},
            llm_model="qwen3.5-4b",
            on_warning=lambda msg: self.warnings.append(msg),
        ))
        # 本队列未设显存阈值（None）→ 不告警
        self.assertEqual(result["warnings"], [])
        self.assertEqual(self.warnings, [])
        # 单独验证阈值场景
        est = estimate_resources({"llm_model": "qwen3.5-4b"},
                                 memory_limit_mb=None, vram_limit_mb=2048.0)
        self.assertEqual(len(est["warnings"]), 1)
        _wait_finished(self.queue, 1)

    def test_state_changed_emitted(self):
        """state_changed 信号：入队/开始/完成均有队列状态消息。"""
        self.queue.enqueue(self._task(
            "only", lambda: {"success": True, "message": "ok"}))
        _wait_finished(self.queue, 1)
        joined = "\n".join(self.state_msgs)
        self.assertIn("队列：only 在跑", joined)
        self.assertIn("队列：空闲", joined)


if __name__ == "__main__":
    unittest.main()
