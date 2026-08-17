# -*- coding: utf-8 -*-
r"""src/core/run_queue.py — P3-3 运行队列 + 资源预检（P3 收官片，Solo APPROVED）。

设计定稿（Solo 控制复杂度）：
1. 单例串行队列：enqueue(task) → 后台 QThread 逐个执行（分析链 handler 或任意
   可调用任务）→ 完成/失败回调（走声带 message；自动 record_run 已由 P3-2 的
   _record_analysis_run 挂钩负责，队列不重复记录）。
2. 队列状态查询：pending / running / queue_length / completed（完成历史）。
3. 明确不做：真并行双链（QGIS 单例线程地雷，绕开）；引擎级步骤挂起（侵入
   PipelineExecutor，复杂度高收益低——秒级链无挂起意义）。

资源预检（enqueue 前）：
- 内存粗估：任务涉及源文件大小合计 × GDAL/Shapely 加载系数 + 基准占用
- 显存粗估：任务含 LLM 调用时按模型参数规模（4b ≈ 3GB 基线）
- 不足 → 声带警告「资源紧张，排队执行：预计等待 X」，但不阻断入队
  （串行队列天然限流）；只做粗上限估算，不承诺精确。

红线遵守：零新依赖（仅 PyQt5.QtCore）；不改 PipelineExecutor / guards / 模板
JSON / CRS / 引擎链 / 现有 action 语义。
"""

from __future__ import annotations

import logging
import os
import re
import threading
from typing import Any, Callable, Dict, List, Optional

from PyQt5.QtCore import QObject, QThread, pyqtSignal

_log = logging.getLogger("run_queue")

# ---------------------------------------------------------------------------
# 资源预检 — 粗估常量（只做粗上限估算，不承诺精确；估算逻辑 ≤40 行）
# ---------------------------------------------------------------------------
BASE_MEM_MB = 512.0          # 基准内存占用：QGIS 引擎 + 画布 + 已加载图层
LOAD_FACTOR = 10.0           # GDAL/Shapely 加载放大系数（源文件大小的峰值内存粗估）
MB = 1048576.0
_LLM_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*b", re.IGNORECASE)


def estimate_vram_mb(model_name: str) -> float:
    """按 LLM 模型参数规模粗估显存占用（MB）。

    - 空 / 未知模型 → 0（任务不含 LLM 调用或无法识别，不估算）
    - 7b 及以上 → 6GB；4b~7b → 3GB 基线；2b~4b → 1.5GB；1b~2b → 1GB
    """
    if not model_name:
        return 0.0
    m = _LLM_SIZE_RE.search(model_name)
    if not m:
        return 0.0
    size = float(m.group(1))
    if size >= 7.0:
        return 6144.0
    if size >= 4.0:
        return 3072.0
    if size >= 2.0:
        return 1536.0
    return 1024.0


def estimate_memory_mb(sources: List[str]) -> float:
    """粗估任务峰值内存：基准 + 源文件大小合计 × 加载系数。"""
    total_bytes = 0
    for path in sources or []:
        try:
            total_bytes += os.path.getsize(path)
        except OSError:
            continue  # 文件不存在/不可读：不参与估算（保守不报错）
    return BASE_MEM_MB + total_bytes / MB * LOAD_FACTOR


def estimate_resources(task: Dict[str, Any],
                       memory_limit_mb: Optional[float] = None,
                       vram_limit_mb: Optional[float] = None,
                       wait_count: int = 0,
                       avg_task_seconds: float = 30.0) -> Dict[str, Any]:
    """enqueue 前资源预检（只做粗上限估算，不承诺精确）。

    Parameters
    ----------
    task : dict
        任务描述，字段：sources（源文件路径列表，可选）、llm_model（模型名，可选）。
    memory_limit_mb / vram_limit_mb : float, optional
        预检阈值；None 表示不限制（测试可注入小阈值触发警告）。
    wait_count : int
        预计还需排队等待的任务数（用于生成等待提示）。
    avg_task_seconds : float
        单任务平均执行时长粗估（秒），用于「预计等待 X」提示。

    Returns
    -------
    dict
        {"memory_mb": float, "vram_mb": float, "warnings": list[str]}
        估算值仅用于声带警告提示，不阻断入队。
    """
    memory_mb = estimate_memory_mb(task.get("sources") or [])
    vram_mb = estimate_vram_mb(task.get("llm_model") or "")
    warnings: List[str] = []
    if memory_limit_mb is not None and memory_mb > memory_limit_mb:
        warnings.append(
            f"资源紧张，排队执行：预计等待约 {max(wait_count, 0) * avg_task_seconds:.0f} 秒"
            f"（内存估算 {memory_mb:.0f}MB 超限 {memory_limit_mb:.0f}MB）"
        )
    if vram_limit_mb is not None and vram_mb > vram_limit_mb:
        warnings.append(
            f"资源紧张，排队执行：预计等待约 {max(wait_count, 0) * avg_task_seconds:.0f} 秒"
            f"（显存估算 {vram_mb:.0f}MB 超限 {vram_limit_mb:.0f}MB）"
        )
    return {"memory_mb": memory_mb, "vram_mb": vram_mb, "warnings": warnings}


# ---------------------------------------------------------------------------
# 队列工作线程 — 单任务执行（每次任务重建线程，避免任务间状态残留）
# ---------------------------------------------------------------------------
class QueueWorker(QThread):
    """后台执行单个可调用任务。任务异常不抛出线程，统一转 done 信号。"""

    done = pyqtSignal(str, bool, str, object)  # name, ok, message, result

    def __init__(self, name: str, fn: Callable[[], Any], parent: QObject = None) -> None:
        super().__init__(parent)
        self._name = name
        self._fn = fn

    def run(self) -> None:
        try:
            result = self._fn()
            message = ""
            if isinstance(result, dict):
                message = str(result.get("message") or "")
            self.done.emit(self._name, True, message, result)
        except Exception as exc:  # noqa: BLE001 - 队列边界统一捕获
            _log.exception("队列任务 %s 执行失败", self._name)
            self.done.emit(self._name, False, f"{type(exc).__name__}: {exc}", None)


# ---------------------------------------------------------------------------
# 运行队列 — 单例串行
# ---------------------------------------------------------------------------
class RunQueue(QObject):
    """后台串行任务队列（单例通过 get_run_queue 获取，测试可独立构造）。

    信号（跨线程自动 queued 到主线程）：
    - state_changed(str)：队列状态变化消息（「队列：X 在跑 / Y 排队」），供 UI 状态栏消费
    - task_finished(str, bool, str)：任务结束（name, ok, message），供调用方收口
    """

    state_changed = pyqtSignal(str)
    task_finished = pyqtSignal(str, bool, str)

    def __init__(self,
                 memory_limit_mb: Optional[float] = None,
                 vram_limit_mb: Optional[float] = None,
                 avg_task_seconds: float = 30.0,
                 parent: QObject = None) -> None:
        super().__init__(parent)
        self.memory_limit_mb = memory_limit_mb
        self.vram_limit_mb = vram_limit_mb
        self.avg_task_seconds = avg_task_seconds
        self._mutex = threading.RLock()
        self._pending: List[Dict[str, Any]] = []
        self._running: Optional[Dict[str, Any]] = None
        self._worker: Optional[QueueWorker] = None
        self._completed: List[Dict[str, Any]] = []  # 完成历史（近 100 条）

    # ---- 对外 API --------------------------------------------------------
    def enqueue(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """入队一个任务并触发后台串行执行。

        task 字段：
        - name: str 必填，任务名（状态显示用）
        - fn: Callable 必填，无参可调用任务（分析链 handler 闭包或任意可调用）
        - sources: list[str] 可选，涉及源文件路径（资源预检）
        - llm_model: str 可选，含 LLM 调用时填模型名（显存预检）
        - on_done(result) / on_failed(message, result) / on_warning(message): 可选回调

        Returns
        -------
        dict : {"success": True, "queued": True, "queue_length": int,
                "warnings": list[str], "resource": dict}
        """
        if not callable(task.get("fn")):
            return {"success": False, "queued": False,
                    "message": "task['fn'] 必须为可调用对象"}

        with self._mutex:
            wait_count = len(self._pending) + (1 if self._running else 0)
            self._pending.append(task)
            queue_length = len(self._pending) + (1 if self._running else 0)

        # 资源预检（enqueue 前）：不足走声带警告，不阻断入队
        resource = estimate_resources(
            task,
            memory_limit_mb=self.memory_limit_mb,
            vram_limit_mb=self.vram_limit_mb,
            wait_count=wait_count,
            avg_task_seconds=self.avg_task_seconds,
        )
        for warning in resource["warnings"]:
            _log.warning("队列资源预检[%s]：%s", task.get("name", "?"), warning)
            self._emit_warning(task, warning)

        self._emit_state()
        self._start_next()
        return {"success": True, "queued": True, "queue_length": queue_length,
                "warnings": resource["warnings"], "resource": resource}

    def status(self) -> Dict[str, Any]:
        """队列状态查询：pending / running / queue_length / completed 完成历史。"""
        with self._mutex:
            running_name = self._running.get("name") if self._running else None
            pending_names = [t.get("name", "?") for t in self._pending]
            completed = list(self._completed)
        return {
            "running": running_name,
            "pending": pending_names,
            "queue_length": len(pending_names) + (1 if running_name else 0),
            "completed": completed,
        }

    # ---- 内部实现 --------------------------------------------------------
    def _start_next(self) -> None:
        with self._mutex:
            if self._running is not None:
                return  # 已有任务在跑：串行，等完成再取下一个
            if not self._pending:
                self._emit_state()
                return
            task = self._pending.pop(0)
            self._running = task

        self._emit_state()
        worker = QueueWorker(task.get("name", "task"), task["fn"], self)
        worker.done.connect(self._on_worker_done)
        self._worker = worker
        worker.start()

    def _on_worker_done(self, name: str, ok: bool, message: str,
                        result: Any) -> None:
        with self._mutex:
            task = self._running
            self._running = None
            if task is not None:
                self._completed.append({
                    "name": task.get("name", name), "ok": ok,
                    "message": message, "result": result,
                })
                self._completed = self._completed[-100:]

        # 走声带：完成/失败回调（record_run 已由 P3-2 handler 挂钩负责）
        if task is not None:
            try:
                if ok and task.get("on_done"):
                    task["on_done"](result)
                elif not ok and task.get("on_failed"):
                    task["on_failed"](message, result)
            except Exception:  # noqa: BLE001 - 回调是旁路，不允许影响队列
                _log.exception("队列任务 %s 回调失败（已忽略）", name)

        self.task_finished.emit(name, ok, message)
        self._emit_state()
        self._start_next()

    def _emit_warning(self, task: Dict[str, Any], warning: str) -> None:
        """资源预检警告的声带出口：优先任务级 on_warning，再降级队列信号。"""
        try:
            cb = task.get("on_warning")
            if cb:
                cb(warning)
                return
        except Exception:  # noqa: BLE001
            _log.exception("on_warning 回调失败（已忽略）")
        self.state_changed.emit(warning)

    def _emit_state(self) -> None:
        with self._mutex:
            running_name = self._running.get("name") if self._running else None
            pending_count = len(self._pending)
        if running_name:
            msg = f"队列：{running_name} 在跑 / {pending_count} 排队"
        else:
            msg = f"队列：空闲 / {pending_count} 排队"
        self.state_changed.emit(msg)


# ---------------------------------------------------------------------------
# 单例
# ---------------------------------------------------------------------------
_instance: Optional[RunQueue] = None
_instance_lock = threading.Lock()


def get_run_queue(memory_limit_mb: Optional[float] = None,
                  vram_limit_mb: Optional[float] = None) -> RunQueue:
    """获取全局单例运行队列。首次调用创建；测试可独立构造 RunQueue。"""
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = RunQueue(memory_limit_mb=memory_limit_mb,
                                 vram_limit_mb=vram_limit_mb)
    return _instance
