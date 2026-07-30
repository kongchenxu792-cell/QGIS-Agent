"""
sandbox_worker.py —— QThread 隔离沙箱执行器

四面防御架构核心：
  Pain 1: QThread 隔离 → exec() 搬离主线程，杜绝 UI 假死
  Pain 2: 自愈循环 → 崩溃后通过 fix_needed 信号回炉 LLM（最多 3 次，调用方编排）
  Pain 3: 图层 GC → exec 前后快照对比，卸载中间图层
  Pain 4: CRS 防御 → 以 active_layer 为准前置强制重投影

QGIS 非线程安全约束：
  QgsProject.instance().addMapLayer() 绝不在 QThread 中直接触发。
  采用 Monkey-patch 拦截到 _deferred_layers 列表，finished 信号携带回主线程安全加载。

SandboxStdoutBridge：
  io.StringIO 子类，exec 代码中 _log.info() 输出逐行回调通知 Worker，
  Worker 通过 stdout_line 信号透传到主线程 UI。

自愈编排设计（调用方负责）：
  本 Worker 单次执行一条代码。调用方连接 fix_needed 信号，
  在槽函数中调用 request_code_fix() 获取修正代码，重新实例化 Worker 执行。
  调用方维护 retry_count，达到 MAX_RETRY=3 后放弃。
"""

from __future__ import annotations

import io
import logging
import os
import re
import sys

# P0: 统一结果契约 adapter
from src.core.result_contract import adapt_finished_result
import tempfile
import traceback
from typing import Any, Callable, Dict, List, Optional

# ── PROJ 环境硬编码激活：必须在任何 GIS/GDAL 导入前执行 ──
def _initialize_portable_env():
    """硬编码便携版 PROJ 路径，不再动态巡检。

    基于 QGIS portable 标准布局，qgis-portable/share/proj 是 PROJ 数据目录。
    必须在 from osgeo import gdal 之前调用，否则 GDAL 初始化会缓存空路径。
    """
    try:
        _this_file = os.path.abspath(__file__)
        _search_dir = os.path.dirname(_this_file)
        _qgis_root = None
        for _ in range(4):
            _candidate = os.path.join(_search_dir, "qgis-portable")
            if os.path.isdir(_candidate):
                _qgis_root = _candidate
                break
            _parent = os.path.dirname(_search_dir)
            if _parent == _search_dir:
                break
            _search_dir = _parent

        if _qgis_root is None:
            return

        _proj_path = os.path.join(_qgis_root, "share", "proj")
        if os.path.isdir(_proj_path):
            os.environ["PROJ_LIB"] = _proj_path
            os.environ["PROJ_DATA"] = _proj_path
            _log.info(f"[Sandbox环境激活] PROJ 路径: {_proj_path}")
    except Exception as _e:
        _log.info(f"[Sandbox环境激活] 异常: {_e}")

# 标记：初始化函数需在 import gdal 之前调用
_INITIALIZE_LATER = True

from PyQt5.QtCore import QThread, pyqtSignal

try:
    import processing as _processing
except ImportError:
    _processing = None

# ── numpy/gdal 手写算法前置注入 ──
# 必须在 from osgeo import gdal 之前完成 PROJ 激活
if _INITIALIZE_LATER:
    _initialize_portable_env()
    _INITIALIZE_LATER = False

try:
    import numpy as np
except ImportError:
    np = None
try:
    from osgeo import gdal
    from osgeo import osr
except ImportError:
    gdal = None
    osr = None

from qgis.core import (
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsMapLayer,
    QgsPointXY,
    QgsProject,
    QgsRasterLayer,
    QgsVectorLayer,
)

# ── 命名空间前置注入：沙箱全局武器库 ──
# 将 PyQGIS 常用基础类和 Python 原生安全库静态预注入 exec_globals，
# 剥夺大模型手写高危 import 的机会。大模型代码中可直接使用这些符号，
# 无需自行 import，杜绝 hallucinated import（如 QgsTemporaryDir）。
SANDBOX_CORE_GLOBALS: Dict[str, Any] = {
    "tempfile": tempfile,
    "QgsApplication": QgsApplication,
    "QgsCoordinateReferenceSystem": QgsCoordinateReferenceSystem,
    "QgsCoordinateTransform": QgsCoordinateTransform,
    "QgsFeature": QgsFeature,
    "QgsField": QgsField,
    "QgsFields": QgsFields,
    "QgsGeometry": QgsGeometry,
    "QgsMapLayer": QgsMapLayer,
    "QgsPointXY": QgsPointXY,
    "QgsProject": QgsProject,
    "QgsRasterLayer": QgsRasterLayer,
    "QgsVectorLayer": QgsVectorLayer,
}
# ── numpy/gdal 注入：供 LLM 手写算法直接使用，禁止 import gdal ──
if np is not None:
    SANDBOX_CORE_GLOBALS["np"] = np
if gdal is not None:
    SANDBOX_CORE_GLOBALS["gdal"] = gdal
if osr is not None:
    SANDBOX_CORE_GLOBALS["osr"] = osr
if _processing is not None:
    SANDBOX_CORE_GLOBALS["processing"] = _processing

_log = logging.getLogger("sandbox_worker")

# ---------------------------------------------------------------------------
# SandboxStdoutBridge
# ---------------------------------------------------------------------------

class SandboxStdoutBridge(io.StringIO):
    """捕获 exec_globals 中 _log.info() 输出，每行回调通知。

    Worker 侧在回调中发射 stdout_line 信号，实现跨线程逐行透传。
    避免一次性 dump 整块 stdout 导致 UI 渲染假死。
    """

    def __init__(self, callback: Callable[[str], None]):
        super().__init__()
        self._callback = callback
        self._buf = ""

    def write(self, s: str) -> int:
        written = super().write(s)
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                try:
                    self._callback(line.strip())
                except Exception:
                    pass  # 回调异常不阻塞沙箱
        return written

    def flush(self) -> None:
        super().flush()


# ---------------------------------------------------------------------------
# SandboxExecutionWorker
# ---------------------------------------------------------------------------

class SandboxExecutionWorker(QThread):
    """单次沙箱执行 Worker。

    信号：
        progress(str)     — 阶段进度通知（CRS/快照/执行/GC）
        stdout_line(str)  — exec 代码中 _log.info() 输出的每一行（主线程安全）
        finished(dict)    — 执行成功。
            {'result': Any, 'pending_layers': [...], 'gc_removed': [...],
             'stdout': str, 'retry_count': int}
        error(str)        — 执行失败（SyntaxError / RuntimeError / Exception）
        fix_needed(dict)  — 需要 LLM 修正。
            {'broken_code': str, 'error_line': int, 'exception_type': str,
             'exception_msg': str, 'user_query': str, 'retry_count': int}

    调用方编排自愈循环：
        1. 实例化 Worker → start()
        2. fix_needed → request_code_fix() → 新 Worker(修正代码) → start()
        3. error → 3 次重试耗尽，放弃并报告
    """

    # ── 信号定义 ──
    progress = pyqtSignal(str)
    stdout_line = pyqtSignal(str)
    finished = pyqtSignal(dict)
    error = pyqtSignal(str)
    fix_needed = pyqtSignal(dict)

    def __init__(
        self,
        code: str,
        exec_globals: Dict[str, Any],
        active_layer=None,
        layers_by_name: Optional[Dict[str, Any]] = None,
        user_query: str = "",
        skill_name: str = "",
        retry_count: int = 0,
    ):
        """初始化沙箱执行 Worker。

        Parameters
        ----------
        code : str
            待执行的 PyQGIS 代码字符串。
        exec_globals : dict
            注入 exec() 的全局命名空间（含 QgsProject, processing, iface 等）。
        active_layer :
            QGIS 当前活动矢量/栅格图层，作为 CRS 防御基准。
        layers_by_name : dict | None
            {图层名: QgsMapLayer} 映射，供 CRS 防御在校验时对齐。
        user_query : str
            用户原始问题，供 fix_needed 上下文回传给 LLM。
        skill_name : str
            技能名称，透传到 result_contract.stats["_skill"]。
        retry_count : int
            当前是第几次重试（0 = 首次执行），透传到 finished / fix_needed。
        """
        super().__init__()
        self._code = code
        self._exec_globals = exec_globals
        self._active_layer = active_layer
        self._layers_by_name = layers_by_name or {}
        self._user_query = user_query
        self._skill_name = skill_name
        self._retry_count = retry_count

        # 内部状态
        self._deferred_layers: list = []  # Monkey-patch 拦截暂存的图层
        self._bridge: Optional[SandboxStdoutBridge] = None

    # ── 外部控制 ──

    def cancel(self) -> None:
        """设置取消标志。

        注意：QThread 无法真正中断正在执行的 Python exec() 代码。
        此标志仅阻止后续阶段（GC 清理等），不阻止运行中的沙箱代码。
        """
        self._cancelled = True

    # ── 安全执行器 ──

    def _safe_exec(self, code_str, exec_globals):
        """C++ 穿透防御版安全执行器。

        os.environ 写入无法实时同步到 GDAL 底层 C++ DLL 的运行时缓存，
        导致降级手写 WriteArray 时 C++ 层仍报 Cannot find proj.db。
        此处使用 GDAL 官方 CPLSetConfigOption 接口直接击穿语言栈屏障，
        把 PROJ_LIB / PROJ_DATA / GDAL_DATA 写入 C++ 内部配置存储。
        """
        if not code_str or not code_str.strip():
            raise ValueError("沙箱接收到的待执行代码为空！")

        import sys
        import os
        from osgeo import gdal

        # 1. 计算便携版根路径
        root_path = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        portable_path = os.path.join(root_path, "qgis-portable")
        proj_dir = os.path.join(portable_path, "share", "proj")

        # 2. C++ 级注入：通过 GDAL CPLSetConfigOption 直接写入 C++ DLL 内部配置
        #    GDAL 所有 C++ 代码通过 CPLGetConfigOption 读取配置，此路径优先于 os.environ
        gdal.SetConfigOption("PROJ_LIB", proj_dir)
        gdal.SetConfigOption("PROJ_DATA", proj_dir)
        _gdal_data = os.path.join(portable_path, "apps", "qgis-ltr", "share", "gdal")
        if os.path.isdir(_gdal_data):
            gdal.SetConfigOption("GDAL_DATA", _gdal_data)

        # 3. sys.path 防御：补全便携版 Python 路径，抵御 QGIS 底层冲刷
        essential_paths = [
            os.path.join(portable_path, "apps", "qgis-ltr", "python"),
            os.path.join(portable_path, "apps", "qgis-ltr", "python", "plugins"),
            os.path.join(portable_path, "apps", "Python312", "Lib", "site-packages"),
            root_path,
        ]
        for p in reversed(essential_paths):
            if os.path.exists(p) and p not in sys.path:
                sys.path.insert(0, p)

        # 4. os.environ 双保险（同步更新 Python 层环境变量）
        os.environ["PROJ_LIB"] = proj_dir
        os.environ["PROJ_DATA"] = proj_dir

        # 5. 注入核心类 + os/sys/gdal 到执行命名空间
        from qgis.core import QgsApplication, QgsProject, QgsVectorLayer, QgsRasterLayer, QgsCoordinateReferenceSystem
        exec_globals.update({
            'QgsApplication': QgsApplication,
            'QgsProject': QgsProject,
            'QgsVectorLayer': QgsVectorLayer,
            'QgsRasterLayer': QgsRasterLayer,
            'QgsCoordinateReferenceSystem': QgsCoordinateReferenceSystem,
            'os': os,
            'sys': sys,
            'gdal': gdal,
        })

        # ── 5.5. processing.run() 透明降级路由 ──
        # 前置路由：对于已知在便携版 QGIS 中缺失的算法，直接路由到静态降级库，
        # 不调用原始 processing.run()，彻底避免 QgsProcessingException 异常链。
        # 对未知算法保持原始 processing.run() 调用。
        _original_processing_run = None
        if _processing is not None:
            try:
                from src.core.fallback_utils import (
                    safe_fill_sinks, safe_d8_flow_direction,
                    safe_flow_accumulation, safe_stream_network,
                    safe_basin,
                )

                _FALLBACK_ROUTES = {
                    # Fill Sinks 系列（含 typo 变体 + 函数名直调）
                    "native:fillsinks": (safe_fill_sinks, "INPUT", "OUTPUT"),
                    "native:fillssinks": (safe_fill_sinks, "INPUT", "OUTPUT"),
                    "native:fill_sinks": (safe_fill_sinks, "INPUT", "OUTPUT"),
                    "saga:fillssinksxxlwangbrennan": (safe_fill_sinks, "INPUT", "OUTPUT"),
                    "saga:fillsinksxxlwangbrennan": (safe_fill_sinks, "INPUT", "OUTPUT"),
                    "native:fillsink": (safe_fill_sinks, "INPUT", "OUTPUT"),
                    "safe_fill_sinks": (safe_fill_sinks, "INPUT", "OUTPUT"),
                    # Flow Direction（含 d8 变体 + 函数名直调）
                    "native:flowdirection": (safe_d8_flow_direction, "INPUT", "OUTPUT"),
                    "native:flow_direction": (safe_d8_flow_direction, "INPUT", "OUTPUT"),
                    "native:d8flowdir": (safe_d8_flow_direction, "INPUT", "OUTPUT"),
                    "native:d8_flow_direction": (safe_d8_flow_direction, "INPUT", "OUTPUT"),
                    "safe_d8_flow_direction": (safe_d8_flow_direction, "INPUT", "OUTPUT"),
                    # Flow Accumulation（含函数名直调）
                    "native:flowaccumulation": (safe_flow_accumulation, "INPUT", "OUTPUT"),
                    "native:flow_accumulation": (safe_flow_accumulation, "INPUT", "OUTPUT"),
                    "safe_flow_accumulation": (safe_flow_accumulation, "INPUT", "OUTPUT"),
                    # Stream / Channel Network（含函数名直调）
                    "native:channelnetwork": (safe_stream_network, "INPUT", "OUTPUT"),
                    "native:channel_network": (safe_stream_network, "INPUT", "OUTPUT"),
                    "native:stream_network": (safe_stream_network, "INPUT", "OUTPUT"),
                    "safe_stream_network": (safe_stream_network, "INPUT", "OUTPUT"),
                    # GDAL 前缀 → 降级到 native（便携版 QGIS 可能缺 GDAL 算子）
                    "gdal:fill_sinks": (safe_fill_sinks, "INPUT", "OUTPUT"),
                    "gdal:fillsinks": (safe_fill_sinks, "INPUT", "OUTPUT"),
                    "gdal:d8flowdirection": (safe_d8_flow_direction, "INPUT", "OUTPUT"),
                    "gdal:flowdirection": (safe_d8_flow_direction, "INPUT", "OUTPUT"),
                    "gdal:d8_flow_direction": (safe_d8_flow_direction, "INPUT", "OUTPUT"),
                    "gdal:flowaccumulation": (safe_flow_accumulation, "INPUT", "OUTPUT"),
                    "gdal:flow_accumulation": (safe_flow_accumulation, "INPUT", "OUTPUT"),
                    "gdal:channelnetwork": (safe_stream_network, "INPUT", "OUTPUT"),
                    "gdal:channel_network": (safe_stream_network, "INPUT", "OUTPUT"),
                    "gdal:stream_network": (safe_stream_network, "INPUT", "OUTPUT"),
                    # Basin / Watershed（汇水区划分 + 函数名直调）
                    "native:basin": (safe_basin, "INPUT", "OUTPUT"),
                    "native:watershed": (safe_basin, "INPUT", "OUTPUT"),
                    "native:basins": (safe_basin, "INPUT", "OUTPUT"),
                    "safe_basin": (safe_basin, "INPUT", "OUTPUT"),
                    "gdal:basin": (safe_basin, "INPUT", "OUTPUT"),
                    "gdal:watershed": (safe_basin, "INPUT", "OUTPUT"),
                }

                def _resolve_to_path(value):
                    """从 QgsRasterLayer / QgsVectorLayer / 字符串中提取文件路径。"""
                    if value is None:
                        return ""
                    # QgsMapLayer 子类有 source() 方法返回数据源路径
                    if hasattr(value, "source"):
                        try:
                            src = value.source()
                            if src and isinstance(src, str):
                                return src
                        except Exception:
                            pass
                    # 兜底：直接转字符串（裸文件路径）
                    return str(value)

                def _patched_run(algo_id, params=None, **kwargs):
                    if params is None:
                        params = {}
                    # ── 前置路由：已知缺失算法直接走降级库 ──
                    if algo_id in _FALLBACK_ROUTES:
                        func, in_key, out_key = _FALLBACK_ROUTES[algo_id]
                        input_path = _resolve_to_path(params.get(in_key, ""))
                        output_path = _resolve_to_path(params.get(out_key, ""))
                        if input_path and output_path:
                            _log.info(f"[降级路由] {algo_id} -> "
                                  f"{func.__name__}({os.path.basename(input_path)}, "
                                  f"{os.path.basename(output_path)})")
                            try:
                                ret = func(input_path, output_path)
                                actual_output = ret if isinstance(ret, str) and ret else output_path
                                return {out_key: actual_output}
                            except Exception as _fe:
                                raise RuntimeError(
                                    f"[降级路由失败] {algo_id} -> {func.__name__}: {_fe}"
                                ) from _fe
                        raise RuntimeError(
                            f"[降级路由] {algo_id} 参数缺失: "
                            f"{in_key}={repr(params.get(in_key))}, "
                            f"{out_key}={repr(params.get(out_key))}"
                        )
                    # ── 未知算法：调用原始 processing.run() ──
                    return _original_processing_run(algo_id, params, **kwargs)

                # ── 水文管道自愈状态追踪 ──
                _fallback_tracker = {"count": 0, "last_output": None, "dem_path": None}

                def _patched_run_with_track(algo_id, params=None, **kwargs):
                    result = _patched_run(algo_id, params, **kwargs)
                    # 记录成功降级的算法
                    if algo_id in _FALLBACK_ROUTES:
                        _fallback_tracker["count"] += 1
                        out_key = _FALLBACK_ROUTES[algo_id][2]
                        if result and isinstance(result, dict) and out_key in result:
                            _fallback_tracker["last_output"] = result[out_key]
                    return result

                _original_processing_run = _processing.run
                _processing.run = _patched_run_with_track
                exec_globals["processing"] = _processing
            except ImportError:
                pass  # 降级库不可用时保持原有行为

        # 6. 最终合闸执行
        try:
            exec(code_str, exec_globals)
        except Exception as _exec_exc:
            # ── 水文管道自愈：检测到 AI 代码触发了已知 fail pattern 后自动注入完整管道 ──
            _exc_msg = str(_exec_exc)
            # 关键词检测：代码中是否包含水文分析相关操作
            _hydro_keywords = ("fillsink", "flowdirection", "flowaccumulation",
                              "channelnetwork", "stream_network", "flow_direction",
                              "flow_accumulation", "channel_network", "basin",
                              "watershed",
                              "d8flowdir", "safe_d8", "safe_fill", "safe_flow",
                              "safe_stream", "safe_basin", "safe_complete",
                              "gdal:", "fillnodata",
                              "洼地", "水流方向", "汇流", "河网", "沟谷", "沟壑", "汇水", "流域")
            _code_lower = code_str.lower()
            _has_hydro_keywords = any(kw in _code_lower for kw in _hydro_keywords)
            _is_hydro_fail = (
                _has_hydro_keywords
                and ("QgsProcessingException" in _exc_msg
                     or "not found" in _exc_msg
                     or "TypeError" in type(_exec_exc).__name__)
                and (_fallback_tracker.get("count", 0) >= 1
                     or "Algorithm" in _exc_msg)
            )
            if _is_hydro_fail:
                _log.info("[自愈] 检测到水文分析管道中断，自动切换确定性管道...")
                _data_dir = None
                # 从 AI 代码中提取所有引号包裹的文件路径，找到 DEM 所在目录
                _path_matches = re.findall(r"""["']([A-Za-z]:\\[^"'\n]*?\.[a-zA-Z0-9]{3,4})["']""", code_str)
                for _p in _path_matches:
                    _pd = os.path.dirname(_p)
                    _parent = os.path.dirname(_pd)
                    # 优先匹配：父目录含 DEM.tif 或 xzq.shp 即认定
                    if os.path.isdir(_parent) and (
                        os.path.exists(os.path.join(_parent, "DEM.tif"))
                        or os.path.exists(os.path.join(_parent, "xzq.shp"))
                    ):
                        _data_dir = _parent
                        break
                    # 兜底：目录本身含 DEM.tif
                    if os.path.isdir(_pd) and os.path.exists(os.path.join(_pd, "DEM.tif")):
                        _data_dir = _pd
                        break

                if _data_dir and os.path.isdir(_data_dir):
                    _dem_path = os.path.join(_data_dir, "DEM.tif")
                    _xzq_path = os.path.join(_data_dir, "xzq.shp")
                    if os.path.exists(_dem_path) and os.path.exists(_xzq_path):
                        try:
                            from src.core.fallback_utils import safe_complete_hydrological_analysis
                            _log.info(f"[自愈] DEM: {_dem_path}")
                            _log.info(f"[自愈] XZQ: {_xzq_path}")
                            _log.info(f"[自愈] 输出: {_data_dir}")
                            _result = safe_complete_hydrological_analysis(
                                dem_path=_dem_path,
                                output_dir=_data_dir,
                                xzq_path=_xzq_path,
                            )
                            exec_globals["_pipeline_result"] = _result
                            exec_globals["result"] = _result
                            _log.info("[自愈] 确定性水文管道执行成功！")
                            # 不重新抛出，让 run() 认为执行成功
                        except Exception as _pipeline_exc:
                            _log.info(f"[自愈] 确定性管道执行失败: {_pipeline_exc}")
                            raise  # 管道也失败，继续抛出原始异常
                    else:
                        _log.info(f"[自愈] 找不到 DEM 或 xzq，跳过自愈。DEM={_dem_path}, XZQ={_xzq_path}")
                        raise
                else:
                    _log.info(f"[自愈] 无法从代码中提取数据路径，跳过自愈。")
                    raise
            else:
                raise
        finally:
            if _original_processing_run is not None:
                _processing.run = _original_processing_run

    # ── 主入口 ──

    def run(self) -> None:
        """Worker 主入口。

        执行流程：
        1. Monkey-patch QgsProject.instance().addMapLayer → _deferred_layers
        2. SandboxStdoutBridge 替换 sys.stdout
        3. Pain 4: CRS 防御（active_layer 为准 reproject）
        4. Pain 3: 执行前快照
        5. 执行代码（单次）
        6. Pain 3: 执行后 GC 清理
        7. 恢复 patch，发射 finished / error / fix_needed
        """
        self._cancelled = False

        # ── 1. Monkey-patch QgsProject.instance().addMapLayer ──
        #    只拦截 addMapLayer，所有其他方法（mapLayers/mapLayer/CRS 变换上下文）
        #    透传到真实 QgsProject 单例，确保 QgsCoordinateTransform 正常工作。
        real_project = QgsProject.instance()
        original_add = real_project.addMapLayer
        self._deferred_layers.clear()

        def _intercept_add(layer, **kwargs):
            # QGIS processing 框架内部调用 addMapLayer(layer, addToLegend=True)
            # 等带 **kwargs，拦截层必须兼容这些额外参数
            self._deferred_layers.append(layer)
            return True

        real_project.addMapLayer = _intercept_add

        # ── 2. Stdout Bridge ──
        bridge = SandboxStdoutBridge(lambda line: self.stdout_line.emit(line))
        self._bridge = bridge
        old_stdout = sys.stdout
        sys.stdout = bridge

        result = None
        gc_removed: List[str] = []

        try:
            # ── Pain 4: CRS 防御 ──
            self._crs_defense()

            # ── Pain 3: 执行前快照 ──
            before_snapshot = self._snapshot_map_layers()
            self.progress.emit("图层快照完成，开始执行...")

            # ── 执行 ──
            self.progress.emit(f"执行代码（第 {self._retry_count + 1} 次）...")

            # 沙箱武器库注入（np/gdal/osr/processing/QGIS 全量工具）
            self._exec_globals.update(SANDBOX_CORE_GLOBALS)
            # 安全执行器接管 sys.path 修复 + PROJ 强制覆盖 + 5 核心类临门注入 + exec
            self._safe_exec(self._code, self._exec_globals)
            result = self._exec_globals.get("result")

            # ── Pain 3: GC 清理（独立 try-except，防止 GC 异常吞噬 finished 信号）──
            try:
                gc_removed = self._gc_cleanup(before_snapshot, result)
                if gc_removed:
                    self.progress.emit(f"GC 清除 {len(gc_removed)} 个中间图层")
            except Exception as gc_exc:
                _log.warning("GC 清理阶段异常（不阻断主流程）: %s", gc_exc)
                gc_removed = []

            # ── 成功 ──
            raw_data = {
                "result": result,
                "pending_layers": list(self._deferred_layers),
                "gc_removed": gc_removed,
                "stdout": bridge.getvalue(),
                "retry_count": self._retry_count,
            }
            # P0: 适配为统一结果契约
            contract_data = adapt_finished_result(
                sandbox_result=raw_data,
                skill_name=self._skill_name,
                user_query=self._user_query,
            )
            # 向后兼容：保留原始字段供下游消费者直接访问
            contract_data["_raw_pending_layers"] = list(self._deferred_layers)
            contract_data["_raw_result"] = result
            contract_data["_raw_stdout"] = bridge.getvalue()
            contract_data["pending_layers"] = list(self._deferred_layers)
            contract_data["result"] = result
            contract_data["stdout"] = bridge.getvalue()
            contract_data["retry_count"] = self._retry_count
            contract_data["gc_removed"] = gc_removed
            self.finished.emit(contract_data)

        except SyntaxError as exc:
            line_no = exc.lineno or 0
            msg = f"第 {line_no} 行语法错误: {exc.msg}"
            self.progress.emit(f"语法错误: {msg}")
            self.fix_needed.emit({
                "broken_code": self._code,
                "error_line": line_no,
                "exception_type": "SyntaxError",
                "exception_msg": msg,
                "user_query": self._user_query,
                "retry_count": self._retry_count,
            })

        except Exception as exc:
            tb = traceback.format_exc()
            line_no = self._extract_error_line(tb)
            exc_type = type(exc).__name__
            msg = f"{exc_type}: {exc}"
            _log.warning("沙箱执行异常（retry %d）: %s\n%s",
                         self._retry_count, msg, tb)
            self.progress.emit(f"执行失败: {msg}")
            self.fix_needed.emit({
                "broken_code": self._code,
                "error_line": line_no,
                "exception_type": exc_type,
                "exception_msg": msg,
                "user_query": self._user_query,
                "retry_count": self._retry_count,
            })

        finally:
            # ── 恢复 ──
            sys.stdout = old_stdout
            real_project.addMapLayer = original_add

    # ------------------------------------------------------------------
    # Pain 4: CRS 防御
    # ------------------------------------------------------------------

    def _crs_defense(self) -> None:
        """以 active_layer 为准，内存中对齐所有图层 CRS。

        策略：
        - active_layer 的 CRS 作为基准
        - 其他图层 CRS 不一致时，调用 native:reprojectlayer → memory:
        - 替换 _layers_by_name 中的引用（不影响原始图层）
        - 地理坐标系自动升级到 EPSG:3857（Web Mercator 米制投影，全球通用）
        - 日本区域推荐 EPSG:2459（JGD2000 / 平面直角座標系 IX 系，关东/东京）
        """
        if self._active_layer is None:
            return

        active_crs = self._active_layer.crs()
        if not active_crs.isValid():
            return

        # 地理坐标系自动升级为投影坐标系
        target_authid = active_crs.authid()
        if active_crs.isGeographic():
            # JGD2000 地理坐标系 → 统一使用 EPSG:3857（Web Mercator），
            # 如需精确日本平面直角坐标系，可改为 EPSG:2459（关东）等
            target_authid = "EPSG:3857"
            self.progress.emit(
                f"CRS 防御: 地理坐标系 {active_crs.authid()} → {target_authid}（投影升级）"
            )

        self.progress.emit(f"CRS 防御: 基准 {target_authid}")

        for name, layer in list(self._layers_by_name.items()):
            if layer is self._active_layer:
                continue
            layer_crs = layer.crs()
            if not layer_crs.isValid():
                continue
            if layer_crs.authid() == target_authid:
                continue

            self.progress.emit(f"CRS 对齐: {name} ({layer_crs.authid()}) → {target_authid}")

            try:
                # 在当前线程的 exec_globals 上下文中执行 reproject
                # 使用真实 QgsProject（仅在此时未被 patch addMapLayer）
                reproj_params = {
                    "INPUT": layer,
                    "TARGET_CRS": target_authid,
                    "OUTPUT": "memory:",
                }
                # 使用 exec_globals 中的 processing 模块
                processing_mod = self._exec_globals.get("processing")
                if processing_mod is not None:
                    reproj_result = processing_mod.run(
                        "native:reprojectlayer", reproj_params
                    )
                    self._layers_by_name[name] = reproj_result["OUTPUT"]
                    # 同步更新 exec_globals 中对应的图层引用
                    self._exec_globals["layers_by_name"] = self._layers_by_name
            except Exception as exc:
                _log.warning("CRS 对齐失败 %s: %s", name, exc)
                # 对齐失败不阻断执行，仅记录日志

    # ------------------------------------------------------------------
    # Pain 3: 图层快照与 GC
    # ------------------------------------------------------------------

    @staticmethod
    def _snapshot_map_layers() -> set:
        """返回当前 QgsProject 中所有图层 ID 的不可变集合。"""
        return frozenset(QgsProject.instance().mapLayers().keys())

    def _gc_cleanup(self, before: frozenset, result: Any = None) -> List[str]:
        """对比快照，卸载 exec 后新增但非成果的非中间图层。

        保护逻辑（三级白名单）：
        1. ID 级：_deferred_layers 中的图层 ID → 免杀（主线程即将 addMapLayer）
        2. ID 级：exec_globals["result"] 中的图层 ID → 免杀（成果图层）
        3. 名称级：含"核密度/缓冲区/结果/最终/output" → 视为结果图层，保留

        Returns
        -------
        list[str]
            被清除的图层名称列表。
        """
        after = frozenset(QgsProject.instance().mapLayers().keys())
        new_ids = after - before
        if not new_ids:
            return []

        # ── Pain 3 v1.4.1: 构建免杀白名单（ID 级精确匹配）──
        deferred_ids: set = set()
        for lyr in self._deferred_layers:
            try:
                lid = lyr.id()
                if lid:
                    deferred_ids.add(lid)
            except Exception:
                pass

        result_ids: set = set()
        try:
            if isinstance(result, dict):
                result_ids = {
                    lyr.id()
                    for lyr in result.values()
                    if hasattr(lyr, "id")
                }
            elif hasattr(result, "id"):
                rid = result.id()
                if rid:
                    result_ids.add(rid)
        except Exception:
            pass

        protected_ids = deferred_ids | result_ids

        # 名称级兜底（保守——拿不准时保留）
        deferred_names: set = set()
        for lyr in self._deferred_layers:
            try:
                deferred_names.add(lyr.name())
            except Exception:
                pass

        removed: List[str] = []
        project = QgsProject.instance()

        for lid in list(new_ids):
            if lid in protected_ids:
                continue  # ID 级白名单命中，无条件跳过

            layer = project.mapLayer(lid)
            if layer is None:
                continue
            try:
                name = layer.name() or ""
            except Exception:
                name = ""

            if name in deferred_names:
                continue  # 名称级兜底：主线程会重新加载
            if self._is_result_layer(layer):
                continue  # 结果图层保留

            project.removeMapLayer(lid)
            removed.append(name)

        return removed

    @staticmethod
    def _is_result_layer(layer) -> bool:
        """启发式判断：是否为结果图层而非中间产物。

        基于图层名称的关键词匹配。不精确但保守——拿不准时保留。
        """
        try:
            name = (layer.name() or "").lower()
        except Exception:
            return False
        return any(
            kw in name
            for kw in ["核密度", "缓冲区", "结果", "最终", "output", "result"]
        )

    # ------------------------------------------------------------------
    # 错误信息提取
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_error_line(tb_text: str) -> int:
        """从 traceback 中提取 <string> 文件（exec 的虚拟文件）的崩溃行号。

        exec(code, globals) 的 traceback 使用 '<string>' 作为文件名，
        这里精确匹配以定位沙箱代码中的具体出错行。
        """
        match = re.search(r'File "<string>", line (\d+)', tb_text)
        return int(match.group(1)) if match else 0
