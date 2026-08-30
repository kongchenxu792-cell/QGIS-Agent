"""后台异步工作线程 — Phase 3 升级：多轮对话记忆 + 多步任务流水线。

核心升级：
1. 模块级滑动记忆窗口（线程安全，最大 10 轮）+ mem0 向量持久化
2. AI 输出 JSON 数组格式，支持单次规划多步技能流水线
3. 主线程通过 pipeline_ready 信号接收任务列表，顺序执行并传递上下文
4. Phase 7: mem0 记忆桥接器 — 语义检索替代全量注入，跨会话持久化
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from PyQt5.QtCore import QThread, pyqtSignal

import core.ai_config as ai_config
from core.instruction_mapper import InstructionMapper

_log = logging.getLogger("ai_worker")

# ---------------------------------------------------------------------------
# P1 改造：离线模式全局标志（由 main_window 设置）
# P0 混动模式：三态 online / offline / hybrid
#   - offline：纯本地；online：纯云端；hybrid：本地优先、失败升级云端
# ---------------------------------------------------------------------------
_mode = "offline"


def set_mode(mode: str) -> None:
    """设置全局模式：'online' / 'offline' / 'hybrid'。"""
    global _mode
    if mode not in ("online", "offline", "hybrid"):
        mode = "offline"
    _mode = mode


def get_mode() -> str:
    """查询当前全局模式。"""
    return _mode


def set_offline_mode(value: bool) -> None:
    """兼容旧调用：True → offline，False → online。"""
    global _mode
    _mode = "offline" if value else "online"


def is_offline_mode() -> bool:
    """离线/混动均视为「本地优先」：返回 True 表示先走本地大模型。"""
    return _mode in ("offline", "hybrid")


def is_hybrid_mode() -> bool:
    """查询当前是否为混动模式（本地失败可升级云端）。"""
    return _mode == "hybrid"


# ---------------------------------------------------------------------------
# 模块级对话记忆（跨 Worker 实例持久化）
# Phase 7: 本地 deque 保留为短时缓冲，持久化委托给 MemoryBridge
# ---------------------------------------------------------------------------

_history_lock = threading.RLock()
_conversation_history: List[Dict[str, str]] = []
MAX_HISTORY_TURNS = 10  # 每个 turn = 1 user + 1 assistant，共 20 条消息

# 最近一轮对话的缓存（用于完成一轮后持久化到 mem0）
_last_user_text: str = ""
_last_assistant_text: str = ""


def append_to_history(role: str, content: str) -> None:
    """线程安全地向对话记忆追加一条消息。

    自动裁剪至 MAX_HISTORY_TURNS 轮。
    同时缓存最近一轮 user/assistant 对话，供 persist_conversation_turn() 持久化。
    """
    global _last_user_text, _last_assistant_text
    with _history_lock:
        _conversation_history.append({"role": role, "content": content})
        # 每轮 = user + assistant，保留最近 N 轮
        max_messages = MAX_HISTORY_TURNS * 2
        if len(_conversation_history) > max_messages:
            _conversation_history[:] = _conversation_history[-max_messages:]

        # 缓存最近一轮
        if role == "user":
            _last_user_text = content
        elif role == "assistant":
            _last_assistant_text = content


def persist_conversation_turn() -> bool:
    """将最近一轮 user + assistant 对话持久化到 mem0 向量库。

    应在每轮对话完成后调用。

    Returns
    -------
    bool
        是否成功持久化。
    """
    global _last_user_text, _last_assistant_text

    if not _last_user_text or not _last_assistant_text:
        return False

    try:
        from core.memory_bridge import get_memory_bridge

        bridge = get_memory_bridge()
        if not bridge.ready:
            _log.debug("记忆桥接器未就绪，跳过快照持久化")
            return False

        ok = bridge.add_experience(
            user_query=_last_user_text,
            agent_reasoning=_last_assistant_text,
            session_id="default",
        )
        if ok:
            _log.debug("对话轮次已持久化到 mem0")
        return ok
    except Exception as exc:
        _log.warning("对话持久化失败: %s", exc)
        return False


def get_conversation_history() -> List[Dict[str, str]]:
    """线程安全地获取当前对话记忆的副本。"""
    with _history_lock:
        return list(_conversation_history)


def clear_conversation_history() -> None:
    """清空对话记忆（例如新建工程时调用）。"""
    with _history_lock:
        _conversation_history.clear()
        _log.info("对话记忆已清空")


def _format_history_for_prompt(user_query: str = "") -> str:
    """获取格式化历史文本块。

    Phase 7 升级：优先从 mem0 语义检索相关记忆；
    若记忆桥接器不可用，降级为最近 5 轮 FIFO 历史。

    Parameters
    ----------
    user_query : str
        当前用户输入，用于语义匹配。空字符串时降级为 FIFO。

    Returns
    -------
    str
        格式化的历史文本块。
    """
    # ── Phase 7: mem0 语义检索优先 ──
    if user_query:
        try:
            from core.memory_bridge import get_memory_bridge

            bridge = get_memory_bridge()
            if bridge.ready:
                results = bridge.search_relevant_history(user_query, top_k=5)
                if results:
                    lines = []
                    for i, item in enumerate(results, 1):
                        memory = item.get("memory", "")
                        score = item.get("score", 0.0)
                        short = memory[:250].replace("\n", " ").strip()
                        lines.append(f"{i}. [相关度 {score:.2f}] {short}")
                    _log.debug("mem0 语义检索命中 %d 条记忆", len(results))
                    return "\n".join(lines)
        except Exception as exc:
            _log.warning("mem0 检索降级: %s，回退到本地 FIFO", exc)

    # ── 降级：本地 FIFO 最近 5 轮 ──
    with _history_lock:
        if not _conversation_history:
            return "（无历史对话）"

        recent = _conversation_history[-10:]  # 最近 5 轮 = 10 条消息
        lines = []
        for msg in recent:
            role = msg["role"]
            if role == "user":
                role_label = "用户"
            elif role == "system":
                role_label = "系统"
            else:
                role_label = "助手"
            content = msg["content"]
            if len(content) > 300:
                content = content[:300] + "..."
            lines.append(f"{role_label}: {content}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 系统提示词构建
# ---------------------------------------------------------------------------

def build_system_prompt(layer_metadata: Optional[List[Dict[str, Any]]] = None, 
                        pipeline_context: Optional[Dict[str, Any]] = None,
                        user_query: str = "") -> str:
    """动态构建技能路由系统提示词（含技能清单 + 图层状态 + 语义记忆 + 流水线上下文）。

    Parameters
    ----------
    layer_metadata : list of dict, optional
        当前已加载图层的元数据列表。
    pipeline_context : dict, optional
        流水线上下文，包含前序步骤的输出图层等信息。
    user_query : str
        当前用户输入，用于 mem0 语义检索相关记忆。
    """

    from skills.skill_manager import get_skill_manager

    mgr = get_skill_manager()
    skills_section = mgr.build_system_prompt_skills_section()

    # 图层状态
    if layer_metadata:
        layer_lines = []
        for i, meta in enumerate(layer_metadata, 1):
            active_mark = " [当前活动]" if meta.get("is_active") else ""
            layer_lines.append(
                f"  {i}. {meta['name']} ({meta['type']}, "
                f"provider: {meta.get('provider', 'unknown')}){active_mark}"
            )
        layer_state = "\n".join(layer_lines)
    else:
        layer_state = "（当前无已加载图层）"

    # 流水线上下文（前序步骤的输出）
    context_section = ""
    if pipeline_context:
        context_lines = []
        for key, value in pipeline_context.items():
            if key.startswith("last_output"):
                context_lines.append(f"  - {key}: {value}")
        if context_lines:
            context_section = (
                "## 流水线上下文（前序步骤的输出）\n\n"
                + "\n".join(context_lines) + "\n\n"
            )

    # Phase 7: 语义检索历史（优先 mem0，降级 FIFO）
    history_text = _format_history_for_prompt(user_query)

    # 历史记忆区块标签
    history_section_label = "## 相关历史记忆" if user_query else "## 对话历史"

    return (
        "你是 AIQGIS 的 GIS 智能体调度中心（Agent Coordinator）。\n"
        "你的职责是：根据用户的自然语言指令，规划并输出一个有序的技能执行流水线。\n\n"
        "## 当前图层树状态\n\n"
        f"{layer_state}\n\n"
        f"{context_section}"
        f"{history_section_label}\n\n"
        f"{history_text}\n\n"
        "## 可用技能清单\n\n"
        f"{skills_section}\n"
        "## PyQGIS Processing 算法速查\n\n"
        "### 核心向量算子（native: 前缀）\n\n"
        "1. native:buffer\n"
        "   参数: INPUT(矢量图层), DISTANCE(浮点数,CRS单位), SEGMENTS(整数1-99,默认8),\n"
        "         END_CAP_STYLE(0=Round 1=Flat 2=Square), JOIN_STYLE(0=Round 1=Miter 2=Bevel),\n"
        "         DISSOLVE(布尔,默认False), OUTPUT(路径 或 'TEMPORARY_OUTPUT')\n"
        "   ⚠️ 经纬度图层(如EPSG:4326)须先 reproject 到米制投影(如EPSG:3857)，\n"
        "      否则 DISTANCE 是度不是米，结果完全错误！\n\n"
        "2. native:clip\n"
        "   参数: INPUT(矢量图层), OVERLAY(多边形图层,裁剪边界), OUTPUT\n"
        "   ⚠️ OVERLAY 必须为多边形类型，点/线图层会直接报错\n\n"
        "3. native:intersection\n"
        "   参数: INPUT(矢量图层), OVERLAY(矢量图层), OUTPUT\n"
        "   ⚠️ 两图层几何类型必须兼容（点∩面=点, 线∩面=线, 面∩面=面）\n\n"
        "4. native:dissolve\n"
        "   参数: INPUT(矢量图层), FIELD(字符串列表,按字段融合,空列表=全部融合),\n"
        "         DISSOLVE_ALL(布尔,默认False), OUTPUT\n"
        "   ⚠️ 融合前建议先跑 native:fixgeometries，无效几何会导致融合失败\n\n"
        "5. native:difference\n"
        "   参数: INPUT(矢量图层), OVERLAY(矢量图层), OUTPUT\n"
        "   ⚠️ 结果 = INPUT 减去与 OVERLAY 相交的部分\n\n"
        "6. native:union\n"
        "   参数: INPUT(矢量图层), OVERLAY(矢量图层), OUTPUT\n"
        "   ⚠️ 两图层 CRS 必须一致，否则结果错位\n\n"
        "7. native:fieldcalculator\n"
        "   参数: INPUT(矢量图层), FIELD_NAME(字符串,新字段名),\n"
        "         FIELD_TYPE(0=Float 1=Integer 2=String 3=Date),\n"
        "         FIELD_LENGTH(整数), FIELD_PRECISION(整数,仅Float), FORMULA(表达式), OUTPUT\n"
        "   ⚠️ $area 返回 CRS 单位的面积；经纬度图层 $area 是平方度，无意义！\n"
        "   ⚠️ 字段名含中文/特殊字符时，表达式用双引号括起: \"字段名\"\n\n"
        "8. native:reprojectlayer\n"
        "   参数: INPUT(图层), TARGET_CRS(QgsCoordinateReferenceSystem对象), OUTPUT\n"
        "   示例: TARGET_CRS = QgsCoordinateReferenceSystem('EPSG:3857')\n\n"
        "9. native:fixgeometries\n"
        "   参数: INPUT(矢量图层), OUTPUT\n"
        "   ⚠️ 始终在处理链第一步调用，清除自交/空洞/重复节点\n\n"
        "10. native:centroids\n"
        "    参数: INPUT(矢量图层), ALL_PARTS(布尔,多部件是否返回全部质心), OUTPUT\n\n"
        "11. native:extractbyexpression / native:selectbyexpression\n"
        "    参数: INPUT(矢量图层), EXPRESSION(SQL风格表达式), OUTPUT\n"
        "    ⚠️ 字段名含中文或空格时须用双引号: \"行政区\" LIKE '%成都%'\n"
        "    ⚠️ 字符串值用单引号: \"name\" = 'Tokyo'\n\n"
        "12. native:joinattributestable\n"
        "    参数: INPUT(主图层), FIELD(主图层连接字段名),\n"
        "          INPUT_2(附表图层), FIELD_2(附表连接字段名),\n"
        "          FIELDS_TO_COPY(可选,要复制的字段列表,空=全部),\n"
        "          METHOD(0=一对一 1=一对多,默认0), OUTPUT\n"
        "   ⚠️ 附表可能含编码问题，连接后检查字段名是否乱码\n"
        "   ⚠️ FIELD 和 FIELD_2 是字段名（字符串），不是字段值！\n\n"
        "### 栅格分析（gdal: 前缀）\n\n"
        "13. gdal:slope\n"
        "    参数: INPUT(DEM栅格图层), Z_FACTOR(浮点,高程/水平单位比,默认1), OUTPUT\n"
        "    ⚠️ 经纬度DEM需要 Z_FACTOR≈111320.0（1度≈111km），否则坡度值无意义\n\n"
        "14. gdal:aspect\n"
        "    参数: INPUT(DEM栅格图层), OUTPUT\n"
        "    ⚠️ 输出 0-360，0=北 90=东 180=南 270=西\n\n"
        "15. gdal:hillshade\n"
        "    参数: INPUT(DEM), Z_FACTOR(浮点,默认1), AZIMUTH(光照方位角0-360,默认315),\n"
        "          ALTITUDE(太阳高度角0-90,默认45), OUTPUT\n\n"
        "16. gdal:cliprasterbymasklayer\n"
        "    参数: INPUT(栅格), MASK(矢量多边形), SOURCE_CRS(可选), TARGET_CRS(可选),\n"
        "          NODATA(浮点,默认-9999), CROP_TO_CUTLINE(布尔,默认True), OUTPUT\n"
        "    ⚠️ 如果 INPUT 和 MASK 坐标系不同，必须填 SOURCE_CRS / TARGET_CRS\n\n"
        "17. gdal:contour\n"
        "    参数: INPUT(DEM), INTERVAL(浮点,等高距), FIELD_NAME(字符串,默认'ELEV'), OUTPUT\n\n"
        "### 核密度与统计（qgis: 前缀）\n\n"
        "18. qgis:heatmapkerneldensityestimation\n"
        "    参数: INPUT(点图层), RADIUS(整数,搜索半径,CRS单位), PIXEL_SIZE(整数,输出像元大小), OUTPUT\n"
        "    ⚠️ 需要米制投影坐标系，经纬度点图层必须先 reproject\n"
        "    ⚠️ 本项目前端渲染已接管该算法（HeatmapRenderSuccessException），不必恐慌\n\n"
        "19. qgis:statisticsbycategories\n"
        "    参数: INPUT(矢量图层), VALUES_FIELD_NAME(统计目标字段),\n"
        "          CATEGORIES_FIELD_NAME(分类字段), OUTPUT\n\n"
        "### 调用范式（生成 PyQGIS 代码时严格遵循）\n"
        "```python\n"
        "import processing\n"
        "from qgis.core import QgsCoordinateReferenceSystem\n\n"
        "# 范式 1：单步操作\n"
        "result = processing.run('native:buffer', {\n"
        "    'INPUT': layers_by_name['roads'],\n"
        "    'DISTANCE': 500.0,\n"
        "    'SEGMENTS': 12,\n"
        "    'OUTPUT': generate_output_path('buffer', 'roads')\n"
        "})\n\n"
        "# 范式 2：链式处理（前步输出直接当后步输入）\n"
        "step1 = processing.run('native:reprojectlayer', {\n"
        "    'INPUT': layers_by_name['points'],\n"
        "    'TARGET_CRS': QgsCoordinateReferenceSystem('EPSG:3857'),\n"
        "    'OUTPUT': 'TEMPORARY_OUTPUT'\n"
        "})\n"
        "# 引用前步输出的图层对象（用字典键 'OUTPUT'，不是图层名）\n"
        "step2 = processing.run('native:buffer', {\n"
        "    'INPUT': step1['OUTPUT'],\n"
        "    'DISTANCE': 1000.0,\n"
        "    'SEGMENTS': 16,\n"
        "    'OUTPUT': generate_output_path('buffer_m', 'points')\n"
        "})\n"
        "result = step2\n"
        "```\n\n"
        "## 输出格式要求（极其重要）\n\n"
        "你必须**只输出**一个严格的 JSON 数组，不要输出任何其他内容。\n"
        "数组中每个元素代表流水线中的一个步骤，按执行顺序排列：\n\n"
        "[\n"
        "  {\n"
        '    "skill": "技能名称",\n'
        '    "arguments": "{\\"param1\\": value1, \\"param2\\": value2}",\n'
        '    "reasoning": "该步骤的简短理由（中文）"\n'
        "  },\n"
        "  ...\n"
        "]\n\n"
        "### arguments 字段强制规则（违反将导致执行失败）\n\n"
        "arguments 必须是**严格的 JSON 对象字符串**，包含该技能所需的全部参数。\n"
        "**绝对禁止**在 arguments 中放入自然语言、中文句子、描述性文本或散文。\n"
        "错误示例：\"对 roads 图层生成 500 米缓冲区\" ← 这是自然语言，禁止！\n"
        "正确示例：\"{\\\"input_layer\\\": \\\"roads\\\", \\\"distance\\\": 500.0}\" ← 这是 JSON，正确！\n\n"
        "### 各技能 arguments 格式速查\n\n"
        "- buffer:       {{\"input_layer\": \"图层名\", \"distance\": 数值, \"segments\": 整数}}\n"
        "  示例: {{\"input_layer\": \"roads\", \"distance\": 500.0, \"segments\": 8}}\n"
        "- clip:         {{\"input_layer\": \"图层名\", \"overlay_layer\": \"边界图层名\"}}\n"
        "  示例: {{\"input_layer\": \"roads\", \"overlay_layer\": \"boundary\"}}\n"
        "- centroid:     {{\"input_layer\": \"图层名\"}}\n"
        "  示例: {{\"input_layer\": \"buildings\"}}\n"
        "- dissolve:     {{\"input_layer\": \"图层名\", \"field\": \"字段名\"}}\n"
        "  示例: {{\"input_layer\": \"parcels\", \"field\": \"district\"}}\n"
        "- intersect:    {{\"input_layer\": \"图层名\", \"overlay_layer\": \"叠加图层名\"}}\n"
        "  示例: {{\"input_layer\": \"roads\", \"overlay_layer\": \"admin_boundary\"}}\n"
        "- open_project: {{\"file_path\": \"项目文件路径\"}}\n"
        "  示例: {{\"file_path\": \"D:/data/project.qgz\"}}\n"
        "- save_project: {{}}\n"
        "- open_table:   {{\"layer_name\": \"图层名\"}}\n"
        "  示例: {{\"layer_name\": \"[Buffer] roads\"}}\n"
        "- navigation:   {{\"action\": \"zoom_layer\"|\"zoom_extent\"|\"center\"|\"scale\"|\"refresh\""
        "|\"zoom_in\"|\"zoom_out\", ...}}\n"
        "  示例: {{\"action\": \"zoom_layer\", \"layer_name\": \"成都市_市\"}}\n"
        "  示例: {{\"action\": \"zoom_extent\", \"west\": 103.0, \"south\": 30.0, \"east\": 105.0, \"north\": 31.5}}\n"
        "  示例: {{\"action\": \"center\", \"lat\": 30.57, \"lon\": 104.07, \"scale\": 50000}}\n"
        "- inspect:      {{\"action\": \"list_layers\"|\"fields\"|\"summary\"|\"selected\"|\"select\""
        "|\"clear_selection\", \"layer_name\": \"...\", \"expression\": \"...\"}}\n"
        "  示例: {{\"action\": \"fields\", \"layer_name\": \"成都市_市\"}}\n"
        "  示例: {{\"action\": \"summary\", \"layer_name\": \"points\"}}\n"
        "- raster_style: {{\"layer_name\": \"图层名\", \"palette\": \"terrain\"|\"viridis\""
        "|\"grayscale\", \"min_value\": 数值, \"max_value\": 数值}}\n"
        "  示例: {{\"layer_name\": \"DEM\", \"palette\": \"terrain\"}}\n"
        "- layer_control: {{\"action\": \"add_vector\"|\"add_raster\"|\"add_xyz\"|\"set_visibility\""
        "|\"set_opacity\"|\"remove\"|\"hillshade\", ...}}\n"
        "  示例: {{\"action\": \"add_xyz\", \"source\": \"osm\"}}\n"
        "  示例: {{\"action\": \"set_visibility\", \"layer_name\": \"边界\", \"visible\": false}}\n"
        "  示例: {{\"action\": \"remove\", \"layer_name\": \"临时图层\"}}\n\n"
        "## 流水线规划规则\n\n"
        "1. 如果用户请求只需一步完成 → 输出单元素数组\n"
        "2. 如果用户请求需要多步链式处理（如裁剪→导出、缓冲区→样式→导出）→ 按依赖顺序排列\n"
        "3. 后续步骤的 arguments 中引用前序步骤输出时，使用图层名前缀如 [Buffer]、[Clip] 等\n"
        "4. 无法匹配任何技能 → 输出 [{\"skill\": \"unknown\", \"arguments\": \"{}\", "
        '"reasoning": "解释原因"}]\n'
        "5. 禁止输出单元素数组以外的任何 JSON 对象格式"
    )


def build_user_prompt(user_text: str) -> str:
    """构建用户指令提示词。"""

    return user_text


# ---------------------------------------------------------------------------
# API 调用工具
# ---------------------------------------------------------------------------

def build_chat_completions_url(base_url: str) -> str:
    """将兼容 OpenAI 格式的基础 URL 规范化为对话补全端点。"""

    normalized = base_url.rstrip("/")
    if normalized.endswith("/chat/completions"):
        return normalized
    return f"{normalized}/chat/completions"


# ---------------------------------------------------------------------------
# AI 工作线程
# ---------------------------------------------------------------------------

class AIProcessingWorker(QThread):
    """后台工作线程：调用 LLM API，解析 JSON 流水线，通过信号发送任务列表。

    Phase 5.1 升级：Perception-Action Loop — 每次 API 调用前强制同步 QGIS 实时状态，
    确保系统提示词始终反映画布上最新的图层快照，消除 LLM 对话记忆漂移。

    信号
    ----
    pipeline_ready : pyqtSignal(list)
        流水线解析完成，携带任务列表 [(skill_name, arguments, reasoning), ...]。
    failed : pyqtSignal(str)
        API 调用或解析失败时触发。
    """

    pipeline_ready = pyqtSignal(list)
    failed = pyqtSignal(str)

    def __init__(self, user_text: str, layer_metadata: List[Dict[str, Any]],
                 pipeline_context: Optional[Dict[str, Any]] = None,
                 project_manager=None, active_layer_name: str = "",
                 viewport_snapshots: Optional[List[Dict[str, Any]]] = None,
                 canvas=None,
                 mapper: Optional[InstructionMapper] = None,
                 params_override: Optional[Dict[str, Any]] = None) -> None:
        super().__init__()
        self.user_text = user_text
        self.layer_metadata = layer_metadata
        self.pipeline_context = pipeline_context
        self.project_manager = project_manager
        self.active_layer_name = active_layer_name
        self.viewport_snapshots = viewport_snapshots  # Phase 2+3: 多模态视口截图
        self.canvas = canvas
        self.mapper = mapper  # Phase 5 Block 1: 在线/离线统一匹配执行
        self.params_override = params_override  # P2-4: 澄清重跑的图层角色覆盖值

    def run(self) -> None:
        try:
            if not is_offline_mode():
                self._validate_config()
            _log.info("发起 AI 流水线规划请求，用户指令长度：%d 字符", len(self.user_text))
            response_text = self._request_llm_pipeline()
            _log.debug("AI 流水线响应：%s", response_text[:300])
            pipeline = parse_pipeline_response(response_text)
            _log.info("解析到 %d 步流水线任务", len(pipeline))
            self.pipeline_ready.emit(pipeline)
        except Exception as exc:
            _log.error("AI 流水线请求失败：%s", exc)
            self.failed.emit(str(exc))

    def _validate_config(self) -> None:
        """验证 AI 配置是否已替换为真实值。"""

        invalid_values = {
            "YOUR_API_KEY_HERE",
            "https://your-openai-compatible-endpoint.example.com/v1",
            "your-model-name",
        }
        if ai_config.API_KEY in invalid_values or ai_config.BASE_URL in invalid_values or ai_config.MODEL_NAME in invalid_values:
            raise RuntimeError(
                "AI 配置尚未完成。请先在 src/core/ai_config.py 中填写 API_KEY、BASE_URL 和 MODEL_NAME。"
            )

    def _capture_live_qgis_state(self) -> List[Dict[str, Any]]:
        """从 ProjectManager 获取 QGIS 画布的实时图层快照。

        这是 Perception-Action Loop 的核心：在每次 LLM 调用前，
        强制读取 QGIS 当前物理状态，生成最新的图层元数据列表，
        确保系统提示词始终与画布底层物理状态对齐，
        彻底消除 LLM 对话记忆与真实画布之间的漂移。

        Returns
        -------
        list of dict
            每个 dict 包含 name / type / path / provider / is_active。
            当 project_manager 不可用时，回退到构造时传入的 layer_metadata。
        """
        if self.project_manager is None:
            _log.warning("project_manager 不可用，回退到构造时快照")
            return self.layer_metadata

        try:
            metadata: List[Dict[str, Any]] = []

            # 矢量图层
            for layer in self.project_manager.get_vector_layers():
                metadata.append({
                    "name": layer.name(),
                    "type": "矢量图层",
                    "path": layer.source(),
                    "provider": layer.providerType(),
                    "is_active": layer.name() == self.active_layer_name,
                })

            # 栅格图层
            for layer in self.project_manager.get_raster_layers():
                metadata.append({
                    "name": layer.name(),
                    "type": "栅格图层",
                    "path": layer.source(),
                    "provider": layer.providerType(),
                    "is_active": layer.name() == self.active_layer_name,
                })

            _log.debug(
                "实时状态捕获完成：%d 个图层（矢 %d，栅 %d），活动图层：%s",
                len(metadata),
                len(self.project_manager.get_vector_layers()),
                len(self.project_manager.get_raster_layers()),
                self.active_layer_name or "无",
            )
            return metadata

        except Exception as exc:
            _log.error("实时状态捕获失败，回退到构造时快照：%s", exc)
            return self.layer_metadata

    def _request_llm_pipeline(self) -> str:
        """调度 LLM 流水线：按全局模式路由。

        - online：走云端（在线逻辑）
        - offline：走本地大模型
        - hybrid：本地优先；本地失败（unknown/异常）时升级云端
        """

        # ========== 实时状态感知（Perception） ==========
        current_layer_metadata = self._capture_live_qgis_state()

        if is_offline_mode():
            try:
                return self._request_local_pipeline(current_layer_metadata)
            except Exception as exc:
                if is_hybrid_mode():
                    _log.info("混动模式：本地调用失败，升级云端：%s", exc)
                    return self._request_online_pipeline(current_layer_metadata)
                raise
        return self._request_online_pipeline(current_layer_metadata)

    def _request_local_pipeline(self, current_layer_metadata: List[Dict[str, Any]]) -> str:
        """本地大模型流水线：LocalLLMClient + InstructionMapper。

        混动模式下，本地无法完成（unknown/失败）时升级云端。
        """
        try:
            from core.local_llm import LocalLLMClient
            from core.instruction_mapper import InstructionMapper
            from core.config_manager import config_manager as cm
            from qgis.core import QgsProject
            from qgis.utils import iface
        except ImportError as e:
            raise RuntimeError(f"离线模式模块加载失败：{e}")

        mapper = InstructionMapper()
        lang = "zh"
        try:
            lang = cm.language
        except Exception:
            pass

        system_prompt = mapper.get_system_prompt(lang)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": self.user_text},
        ]

        client = LocalLLMClient()
        try:
            response_text = client.chat(messages, expect_json=True)
        except ConnectionError as e:
            raise RuntimeError(str(e))
        except TimeoutError as e:
            raise RuntimeError(str(e))
        except Exception as e:
            raise RuntimeError(f"本地大模型调用失败：{e}")

        # 尝试匹配并执行指令
        canvas = self.canvas if self.canvas else None
        project = QgsProject.instance()

        result = mapper.match_and_execute(
            response_text,
            canvas=canvas,
            project=project,
            user_text=self.user_text,
            params_override=self.params_override,
        )

        # 混动模式：本地无法完成（unknown / 失败）→ 升级云端。
        # P2-4：澄清结果（多候选交互点选）不触发云端升级——澄清由壳侧确定性完成，
        # LLM 零参与提问；升级云端也无法替代用户点选。
        from core.clarification import is_clarification_result
        if is_hybrid_mode() and not is_clarification_result(result):
            _log.info(
                "混动模式：本地无法完成（action=%s），升级云端",
                result.get("action"),
            )
            return self._request_online_pipeline(current_layer_metadata)

        # 返回一个特殊流水线，由 _execute_pipeline 处理离线响应
        # 片B：透传 stats / output_file，供报告生成挂接（只读透传，不改执行逻辑）
        return json.dumps([{
            "skill": "_offline_response",
            "arguments": json.dumps({
                "success": result.get("success", False),
                "status": result.get("status", "ok" if result.get("success", False) else "failed"),
                "message": result.get("message", ""),
                "action": result.get("action", ""),
                "clarification": result.get("clarification"),
                "stats": result.get("stats") or {},
                "output_file": result.get("output_file", ""),
            }),
            "reasoning": f"离线模式：{result.get('action', '问答')}",
        }], ensure_ascii=False)

    def _request_online_pipeline(self, current_layer_metadata: List[Dict[str, Any]]) -> str:
        """云端大模型流水线：多模态优先，否则纯文本走 InstructionMapper。"""

        # ── 分支：多模态 vs 纯文本 ──
        if self.viewport_snapshots:
            try:
                return self._request_multimodal_pipeline(current_layer_metadata)
            except RuntimeError as exc:
                if "unknown variant" in str(exc) and "image_url" in str(exc):
                    _log.warning(
                        "当前模型 %s 不支持多模态输入，自动降级为纯文本模式",
                        ai_config.MODEL_NAME,
                    )
                else:
                    raise

        # ========== 纯文本路径 — Phase 5 Block 1 改造：统一走 InstructionMapper ==========
        if self.mapper is None:
            raise RuntimeError(
                "在线模式需要 InstructionMapper 实例，请在构造 AIProcessingWorker 时传入 mapper 参数"
            )

        from core.config_manager import config_manager as cm

        lang = "zh"
        try:
            lang = cm.language
        except Exception:
            pass

        system_prompt = self.mapper.get_system_prompt(lang)
        messages: List[Dict[str, str]] = [
            {"role": "system", "content": system_prompt},
        ]

        with _history_lock:
            messages.extend(_conversation_history)

        messages.append({"role": "user", "content": build_user_prompt(self.user_text)})

        request_body = {
            "model": ai_config.MODEL_NAME,
            "temperature": 0.1,
            "messages": messages,
        }

        request = urllib.request.Request(
            build_chat_completions_url(ai_config.BASE_URL),
            data=json.dumps(request_body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {ai_config.API_KEY}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            _log.error("AI HTTP %s：%s", exc.code, detail[:500])
            raise RuntimeError(f"AI 接口请求失败，HTTP {exc.code}：{detail}") from exc
        except urllib.error.URLError as exc:
            _log.error("AI 连接失败：%s", exc.reason)
            raise RuntimeError(f"AI 接口连接失败：{exc.reason}") from exc

        try:
            response_text = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"AI 接口返回格式异常：{payload}") from exc

        # Phase 5 Block 1: 在线 LLM 输出 {action, params} JSON → 统一走 mapper
        try:
            from qgis.core import QgsProject
        except ImportError:
            QgsProject = None

        canvas = self.canvas if self.canvas else None
        project = QgsProject.instance() if QgsProject else None

        result = self.mapper.match_and_execute(
            response_text,
            canvas=canvas,
            project=project,
            user_text=self.user_text,
            params_override=self.params_override,
        )

        # 返回统一格式的 _offline_response pipeline
        return json.dumps([{
            "skill": "_offline_response",
            "arguments": json.dumps({
                "success": result.get("success", False),
                "status": result.get("status", "ok" if result.get("success", False) else "failed"),
                "message": result.get("message", ""),
                "action": result.get("action", ""),
                "clarification": result.get("clarification"),
                "output_file": result.get("output_file", ""),
                "output_layer_name": result.get("output_layer", None).name() if result.get("output_layer") else "",
            }),
            "reasoning": f"在线模式：{result.get('action', '问答')}",
        }], ensure_ascii=False)

    def _request_multimodal_pipeline(
        self, current_layer_metadata: List[Dict[str, Any]]
    ) -> str:
        """多模态 LLM 调用分支 — Phase 2+3 新增。

        使用 OpenAI Vision API 兼容格式，将画布截图和视口空间元数据
        注入到 messages 中，让视觉模型获得空间尺度感知。
        """
        from core.multimodal.prompt_builder import MultimodalPromptBuilder
        from skills.skill_manager import get_skill_manager

        # 技能清单
        mgr = get_skill_manager()
        skills_section = mgr.build_system_prompt_skills_section()

        # 对话历史
        history_text = _format_history_for_prompt(self.user_text)

        # 构建系统提示词（含空间尺度感知规则）
        system_prompt = MultimodalPromptBuilder.build_system_prompt(
            skills_section=skills_section,
            layer_metadata=current_layer_metadata,
            history_text=history_text,
            pipeline_context=self.pipeline_context,
        )

        # 构建多模态 user content（含视口元数据注入）
        user_content = MultimodalPromptBuilder.build_user_content(
            user_text=self.user_text,
            viewport_snapshots=self.viewport_snapshots,
        )

        # 组装完整 messages
        with _history_lock:
            history_messages = list(_conversation_history)
        messages = MultimodalPromptBuilder.build_messages(
            system_prompt=system_prompt,
            user_content=user_content,
            history_messages=history_messages,
        )

        _log.info(
            "多模态请求：%d 张截图，%d 条历史消息",
            len(self.viewport_snapshots),
            len(history_messages),
        )

        request_body = {
            "model": ai_config.MODEL_NAME,
            "temperature": 0.1,
            "messages": messages,
        }

        request = urllib.request.Request(
            build_chat_completions_url(ai_config.BASE_URL),
            data=json.dumps(request_body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {ai_config.API_KEY}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            _log.error("多模态 AI HTTP %s：%s", exc.code, detail[:500])
            raise RuntimeError(f"AI 接口请求失败，HTTP {exc.code}：{detail}") from exc
        except urllib.error.URLError as exc:
            _log.error("多模态 AI 连接失败：%s", exc.reason)
            raise RuntimeError(f"AI 接口连接失败：{exc.reason}") from exc

        try:
            return payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"AI 接口返回格式异常：{payload}") from exc


# ---------------------------------------------------------------------------
# 响应解析
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 参数规范化器 — 防御 LLM 在 arguments 中放入自然语言
# ---------------------------------------------------------------------------

# 各技能的参数 schema：{skill_name: [required_params, optional_params]}
_SKILL_PARAM_SCHEMA: Dict[str, tuple] = {
    "buffer":          (["input_layer", "distance"], ["segments"]),
    "clip":            (["input_layer", "overlay_layer"], []),
    "centroid":        (["input_layer"], []),
    "dissolve":        (["input_layer"], ["field"]),
    "intersect":       (["input_layer", "overlay_layer"], []),
    "open_project":    (["file_path"], []),
    "save_project":    ([], []),
    "open_table":      (["layer_name"], []),
    "navigation":      (["action"], ["layer_name", "west", "south", "east", "north", "lat", "lon", "scale", "crs"]),
    "inspect":         (["action"], ["layer_name", "expression"]),
    "raster_style":    (["layer_name"], ["palette", "min_value", "max_value"]),
    "layer_control":   (["action"], ["layer_name", "file_path", "visible", "opacity", "source"]),
}

# 自然语言 → 结构化参数的正则提取器
import re as _re

# 距离提取：匹配 "数字 + 可选单位" 模式
# 覆盖：500米、150 值、0.5 度、100公里、3cm、200、扩大 150、建个 250 值的
_NL_DISTANCE_PATTERN = _re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:米|m|公里|km|千米|厘米|cm|毫米|mm|度|°|值)?"
)

# 图层名提取：匹配 "XXX 图层" 或 "图层 XXX" 或 "对 XXX 图层"
_NL_LAYER_PATTERN = _re.compile(
    r"(?:对|给|把|将)?\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*(?:图层|layer)"
)


def _normalize_arguments(skill: str, raw_arguments) -> str:
    """规范化 arguments 字段：检测自然语言并尝试提取结构化 JSON。

    这是防御层：当 LLM 违反 prompt 指令在 arguments 中放入中文散文时，
    尝试从中提取数值和图层名等关键参数，构造合法的 JSON 对象。

    同时处理 dict 输入（LLM 直接在 JSON 中内嵌对象而非字符串）。

    Parameters
    ----------
    skill : str
        技能名称。
    raw_arguments : str | dict
        LLM 输出的原始 arguments（可能是字符串或已解析的 dict）。

    Returns
    -------
    str
        规范化后的 arguments 字符串（保证是合法 JSON 或空字符串）。
    """
    # ---------- 0. 类型归一化：dict → JSON 字符串 ----------
    if isinstance(raw_arguments, dict):
        # LLM 在 JSON 中直接内嵌了对象而非字符串，直接序列化
        _log.info("arguments 已是 dict，直接序列化为 JSON：%s", raw_arguments)
        return json.dumps(raw_arguments, ensure_ascii=False)

    if not raw_arguments or not isinstance(raw_arguments, str):
        return "{}"

    s = raw_arguments.strip()
    if not s:
        return "{}"

    # ---------- 1. 已是合法 JSON 字符串 → 直接放行 ----------
    try:
        json.loads(s)
        return s
    except (json.JSONDecodeError, ValueError):
        pass

    # ---------- 2. 自然语言检测 ----------
    has_chinese = bool(_re.search(r"[\u4e00-\u9fff]", s))
    if not has_chinese:
        _log.warning(
            "arguments 不是合法 JSON 且不含中文，保留原文：%s", s[:200]
        )
        return s

    _log.warning(
        "检测到自然语言 arguments（skill=%s），尝试提取结构化参数：%s",
        skill, s[:200],
    )

    # ---------- 3. 按技能 schema 提取参数 ----------
    schema = _SKILL_PARAM_SCHEMA.get(skill)
    if schema is None:
        _log.warning("未知技能 %s，无法规范化 arguments，保留原文", skill)
        return s

    required, optional = schema
    extracted: Dict[str, Any] = {}

    # --- 提取距离（buffer 技能） ---
    if "distance" in required or "distance" in optional:
        dist_match = _NL_DISTANCE_PATTERN.search(s)
        if dist_match:
            try:
                extracted["distance"] = float(dist_match.group(1))
            except (ValueError, TypeError):
                _log.warning("距离值转换失败：%s", dist_match.group(1))

    # --- 提取 input_layer ---
    if "input_layer" in required or "input_layer" in optional:
        layer_match = _NL_LAYER_PATTERN.search(s)
        if layer_match:
            extracted["input_layer"] = layer_match.group(1)

    # --- 提取 overlay_layer ---
    if "overlay_layer" in required or "overlay_layer" in optional:
        overlay_match = _re.search(
            r"(?:和|与|跟|同|用|以)\s*[「「\s]*([^\s」」,，、]+)",
            s,
        )
        if overlay_match:
            extracted["overlay_layer"] = overlay_match.group(1)

    # --- 提取 segments ---
    if "segments" in required or "segments" in optional:
        seg_match = _re.search(r"(\d+)\s*(?:段|分段|segments?)", s)
        if seg_match:
            try:
                extracted["segments"] = int(seg_match.group(1))
            except (ValueError, TypeError):
                pass

    # --- 提取 field ---
    if "field" in required or "field" in optional:
        field_match = _re.search(r"(?:字段|field|按)\s*[「「\s]*([^\s」」,，、]+)", s)
        if field_match:
            extracted["field"] = field_match.group(1)

    # --- 提取 file_path ---
    if "file_path" in required or "file_path" in optional:
        path_match = _re.search(r"([A-Za-z]:[^\s,，]+)", s)
        if path_match:
            extracted["file_path"] = path_match.group(1)

    # --- 提取 layer_name ---
    if "layer_name" in required or "layer_name" in optional:
        name_match = _re.search(r"(?:图层|layer|打开)\s*[「「\s]*([^\s」」,，、]+)", s)
        if name_match:
            extracted["layer_name"] = name_match.group(1)

    if not extracted:
        _log.warning("无法从自然语言中提取任何参数，保留原文")
        return s

    _log.info("从自然语言中提取参数：%s", extracted)
    return json.dumps(extracted, ensure_ascii=False)


def parse_pipeline_response(response_text: str) -> List[Dict[str, str]]:
    """解析 AI 返回的 JSON 流水线数组。

    返回
    ----
    list of dict
        每个元素包含 skill / arguments / reasoning 三个键。
        始终返回列表（单步任务也是单元素列表）。

    异常
    ----
    RuntimeError
        JSON 解析失败或格式不符合预期。
    """

    text = response_text.strip()

    # 去掉 markdown 代码块标记
    if text.startswith("```"):
        lines = text.split("\n")
        # 去掉首行 ```json 或 ```
        if lines[0].startswith("```"):
            lines = lines[1:]
        # 去掉尾行 ```
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    # 尝试直接解析
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # 尝试用正则提取 JSON 数组
        import re
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group())
            except json.JSONDecodeError:
                raise RuntimeError(
                    f"AI 返回的不是合法 JSON 数组。\n\n原始响应：\n{response_text[:500]}"
                )
        else:
            raise RuntimeError(
                f"AI 返回的不是合法 JSON 数组。\n\n原始响应：\n{response_text[:500]}"
            )

    # 兼容单对象 → 包装为数组
    if isinstance(parsed, dict):
        parsed = [parsed]

    if not isinstance(parsed, list):
        raise RuntimeError(
            f"AI 返回的 JSON 不是数组格式。\n\n原始响应：\n{response_text[:500]}"
        )

    # 标准化每个元素 + 参数规范化
    result = []
    for i, item in enumerate(parsed):
        if not isinstance(item, dict):
            raise RuntimeError(f"流水线第 {i+1} 步不是有效的 JSON 对象")

        skill = item.get("skill", "unknown")
        raw_args = item.get("arguments", "")

        # 防御层：检测并修复自然语言 arguments
        normalized_args = _normalize_arguments(skill, raw_args)

        result.append({
            "skill": skill,
            "arguments": normalized_args,
            "reasoning": item.get("reasoning", ""),
        })

    return result


# ---------------------------------------------------------------------------
# 空间分析代码生成（保留，供 spatial_analysis skill 内部使用）
# ---------------------------------------------------------------------------

def build_code_generation_prompt(
    spatial_tips: str = "",
    user_query: str = "",
    fix_mode: bool = False,
    broken_code: str = "",
    error_line: int = 0,
    exception_type: str = "",
    exception_msg: str = "",
) -> str:
    """构建空间分析代码生成的系统提示词（第二轮调用 / 自愈回炉）。

    Phase 4 强化：内置核密度分析、投影转换、表格空间化硬防错机制。
    Phase 7: 接收 mem0 空间分析历史避坑经验并注入提示词。
    Phase 8: 动态精细化过滤——根据 user_query 仅注入相关 API 参考，杜绝全量 Token 浪费。
    Phase 9: fix_mode 回炉模式——注入崩溃代码与错误上下文，要求 LLM 诊断并修正。
    """

    # ── Phase 9: fix_mode 回炉分支 ──
    if fix_mode:
        return _build_fix_mode_prompt(
            broken_code=broken_code,
            error_line=error_line,
            exception_type=exception_type,
            exception_msg=exception_msg,
            user_query=user_query,
        )

    # ── Phase 7: 历史经验注入块 ──
    tips_section = ""
    if spatial_tips:
        tips_section = "\n" + spatial_tips + "\n"

    # ── Phase 8: opengis-skills 动态知识注入（按意图精细化过滤） ──
    try:
        from knowledge.gis_reference import build_reference_injection
        gis_reference = build_reference_injection(user_query)
    except ImportError:
        gis_reference = ""

    return (
        "你是 PyQGIS 代码生成专家，同时能用中文简要回答用户的问题。\n\n"
        "## 【硬性输出契约】—— 最高优先级，压倒一切规则\n\n"
        "无论你对图层格式有任何疑问、建议或自我反思，你必须在回答的最终结尾，\n"
        "输出包含在 ```python ... ``` 格式中的、完整且可运行的 Python 代码。\n"
        "严禁仅输出解释性文本而不提供代码块！\n"
        "你的回答格式必须严格为：1-2 句中文简述 → 空行 → ```python 代码块。\n"
        "如果你对图层类型有疑问，在简述中提出，但仍必须输出代码块（使用\n"
        "QgsProject.instance().mapLayersByName() 在运行时动态获取图层）。\n\n"
        "## 【栅格输出与路径硬性契约】—— 违反任意一条将导致 Output format is invalid\n\n"
        "1. 任何算法（无论是 PyQGIS 原生算子、降级方案还是 GDAL/Numpy 手写实现）\n"
        "   生成的栅格结果，在指定输出路径时，必须在本地磁盘路径末尾拼接\n"
        "   标准小写 `.tif` 后缀，示例：\n"
        '   output_tif = os.path.join(output_dir, "flow_direction.tif")\n'
        "   或使用 generate_output_path 后追加 `.tif`：\n"
        "   out = generate_output_path('flowdir', layer_name) + '.tif'\n\n"
        "2. 严禁直接使用不带扩展名的路径（如 generate_output_path(...) 不加 .tif），\n"
        "   严禁使用 'memory:' 或 'memory_layer' 作为栅格算子的输出接收端。\n"
        "   QGIS 的栅格算子必须写入磁盘文件，内存图层仅限矢量。\n\n"
        "3. 必须确保输入栅格和输出栅格的格式完全对齐。调用任何 processing.run()\n"
        "   时，必须严格检查参数字典中 OUTPUT 键对应的文件后缀是否为 `.tif`。\n"
        "   如果 generate_output_path() 默认生成 .shp 后缀，必须用 + '.tif' 覆盖。\n\n"
        "## 【多步串行流水线上游依赖认知】\n\n"
        "在多步串行流水线（如填洼 → 流向 → 汇流累积）中，前序步骤的输出图层\n"
        "已经作为真实图层加载到了 QGIS 当前项目中，**不要**自行编造本地 .shp / .tif\n"
        "文件路径。必须通过以下标准方式获取上游图层：\n"
        "```python\n"
        "# 正确：从 QGIS 项目运行时查询（前序步骤已加载到画布）\n"
        "fill_sinks_layer = QgsProject.instance().mapLayersByName('fill_sinks_DEM')[0]\n"
        "# 从图层获取数据源路径（栅格用 source()，矢量用 source()）\n"
        "raster_path = fill_sinks_layer.source()\n"
        "```\n"
        "**严禁**将栅格图层误判为矢量 .shp、或编造不存在的本地文件路径。\n"
        "上一步产出的图层类型已在 active_layers 的 type 字段明确标注为\n"
        "「栅格图层」或「矢量图层」—— 请严格据此选择正确的 processing 算法。\n\n"
        "## 复合任务拆解最高优先级（Level 0）\n\n"
        "在输出任何 PyQGIS 代码之前，你必须先执行复合任务检测与**拓扑依赖编排**。\n\n"
        "**触发条件**：用户请求包含**多个不同的目标图层**或**多步链式空间操作**时。\n"
        "典型复合请求示例：\n"
        "- \"裁剪出道路和建筑数据\" → 两个目标图层（道路、建筑）+ 同一个叠加图层\n"
        "- \"先帮我算出裁剪后的面积，然后再把道路和建筑裁剪出来\" → 用户文本顺序混乱，你必须自主排序\n"
        "- \"对水系和绿地做500米缓冲区\" → 两个目标图层 + 同一个 buffer 操作\n\n"
        "## 拓扑依赖自主编排（Dependency Resolution）—— 最高优先级铁律\n\n"
        "**严禁**按照用户文本提及的先后顺序死板排列步骤。你必须执行 GIS 空间拓扑分析，\n"
        "识别哪些步骤是\"数据生成（上游）\"，哪些是\"消费/计算/渲染（下游）\"。\n\n"
        "**自主排序规则**：\n"
        "1. 所有数据生成类操作（clip / buffer / dissolve / intersect / centroid / convex_hull）→ 排在**上游**\n"
        "2. 所有依赖前序输出的计算/分析/渲染操作 → 排在**下游**\n"
        "3. 同类型的数据生成操作可以按任意顺序排列，互不依赖的步骤可并行语义\n\n"
        "**自检机制（输出前必须执行）**：\n"
        "遍历你生成的每个步骤，确认：该步骤的 target_layer 引用的图层，在它之前的 step 中是否已生成？\n"
        "如果是用户原始图层（从 active_layers 查找），可以直接引用；如果是前序步骤的输出，\n"
        "该步骤的 depends_on 必须包含对应的 step 编号。\n\n"
        "**逻辑错误示例（绝对禁止）**：\n"
        "用户说：\"先帮我算出裁剪后的面积，然后再把道路和建筑裁剪出来\"\n"
        "❌ 错误排序：[step1: 计算面积, step2: 裁剪道路, step3: 裁剪建筑]\n"
        "   → step1 引用的目标图层不存在（还未裁剪），必崩溃！\n"
        "✅ 正确排序：[step1: 裁剪道路, step2: 裁剪建筑, step3: 计算裁剪后的建筑面积]\n"
        "   → step3 的 depends_on: [1, 2]，target_layer 引用 step2 的输出\n\n"
        "**如果命中复合任务**，你必须**只输出**以下 JSON 数组格式，严禁输出任何其他文本或代码：\n"
        "[\n"
        '  {"step": 1, "description": "操作描述（中文）", "target_layer": "目标图层名", '
        '"overlay_layer": "叠加/边界图层名（仅 clip/intersect 需要）", "action": "操作类型", '
        '"depends_on": [], '
        '"output_var": "逻辑输出变量名"},\n'
        "  ...\n"
        "]\n\n"
        "action 映射表：\n"
        "- clip: native:clip（裁剪）\n"
        "- buffer: native:buffer（缓冲）\n"
        "- dissolve: native:dissolve（融合）\n"
        "- intersect: native:intersection（相交）\n"
        "- centroid: native:centroids（质心）\n"
        "- convex_hull: native:convexhull（凸包）\n"
        "- calculate_area: 面积计算（依赖前序裁剪/融合输出）\n"
        "- calculate_length: 长度计算（依赖前序裁剪/融合输出）\n\n"
        "字段填写铁律：\n"
        "- target_layer：从 active_layers 精确匹配的图层名，或引用前序步骤 output_var 时使用 {output_var} 占位符\n"
        "  例：step3 计算面积，target_layer 应填 \"{clipped_buildings}\"（引用 step2 的 output_var）\n"
        "- overlay_layer：仅 clip / intersect 需要，填边界图层名（来自 active_layers）\n"
        "- depends_on：整数数组，列出本步骤依赖的前序 step 编号（数据生成步骤）\n"
        "  数据生成步骤（clip/buffer/dissolve 等）的 depends_on 一般为 []\n"
        "  下游计算步骤（calculate_area 等）的 depends_on 必须包含其所依赖的所有上游 step 编号\n"
        "- output_var：英文小写+下划线命名，如 clipped_roads / buffered_water / dissolved_parcels\n"
        "  数据生成步骤必须填 output_var（供下游步骤引用）\n"
        "- description：一句话中文，如\"裁剪道路数据\"\n"
        "- action：只能从映射表中取值\n\n"
        "**如果是单任务**（只有一个目标图层，或不满足复合条件），按原有规则输出中文回答 + ```python 代码块。\n\n"
        "你的输出必须严格满足以下要求：\n"
        f"{tips_section}"
        "1. 先用中文简要回答用户的问题（如计算结果、分析结论等）。\n"
        "2. 然后在 ```python ... ``` 代码块中给出执行的 PyQGIS 代码。\n"
        "3. 代码必须以 processing.run() 结尾，返回值直接赋给 result。\n"
        "4. result = processing.run(...)，不得修改或包装。\n"
        "5. 优先使用 active_layer 作为输入图层。\n"
        "6. 输出图层必须使用 generate_output_path('skill_prefix', active_layer.name()) 生成持久化路径。\n"
        "   例如：generate_output_path('buffer', active_layer.name()) → user_data/exports/shapefiles/buffer_20260526_093012_layerA.shp\n"
        "   可用前缀：buffer, clip, dissolve, centroid, intersect, union, convex_hull 等。\n"
        "7. 禁止使用 'TEMPORARY_OUTPUT' 或 'memory:'——这会导致应用重启后数据丢失。\n"
        "8. 禁止调用 print、input、sys、subprocess、eval、exec、open、__import__。\n"
        "   【严禁变量污染】禁止将任何 Python 内置函数名（exec, eval, print, id, type, input, open）用作变量名！\n"
        "   尤其是：exec = processing.run(...) 这类写法会永久污染命名空间，导致沙箱引擎崩溃。\n"
        "   正确写法：result = processing.run(...)  ← 结果必须赋值给 result\n"
        "9. 禁止定义类和函数，只输出顺序代码。\n"
        "10. 禁止使用 iface — 这是独立 QGIS 应用，iface 不存在。用 QgsProject.instance() 代替。\n"
        "11. 禁止仅做图层显隐操作。所有空间操作（提取、裁剪、筛选、缓冲等）都必须通过 processing.run() 完成。\n\n"
        "## 表格空间化硬防错规则（违反任意一条将导致程序崩溃）\n\n"
        "A. 【严禁第三方表格库】绝对禁止在生成的代码中使用以下任何模块来读取 Excel/CSV 文件：\n"
        "   - import pandas（❌ 环境未安装，必崩溃）\n"
        "   - import openpyxl / from openpyxl import ...（❌ 环境未安装，必崩溃）\n"
        "   - import xlrd（❌ 环境未安装，必崩溃）\n"
        "   - import csv（❌ 禁止：csv 只能读纯文本 CSV，无法处理 Excel，不可靠）\n"
        "   - 任何手动逐行解析表格内容的做法（❌ 禁止）\n\n"
        "B. 【强制标准建点流水线】处理 .xlsx / .xls / .csv 表格文件时，必须且只能使用以下三步：\n"
        "   ```python\n"
        "   import processing\n"
        "   from qgis.core import QgsVectorLayer, QgsProject\n"
        "\n"
        "   # 第1步：用 OGR 驱动加载表格为无几何图层（QGIS 原生能力，无需任何第三方库）\n"
        "   raw_table = QgsVectorLayer(r'C:\\path\\to\\file.xlsx', 'raw_table', 'ogr')\n"
        "   columns = [field.name() for field in raw_table.fields()]\n"
        "   # 通过语义理解 columns，判断哪一列是经度（X）哪一列是纬度（Y）\n"
        "\n"
        "   # 第2步：调用 QGIS 原生建点算子\n"
        "   result = processing.run('native:createpointslayerfromtable', {\n"
        "       'INPUT': raw_table,\n"
        "       'XFIELD': '经度',   # ← 通过 columns 语义推断得出，可能是 'longitude'/'lon'/'X' 等\n"
        "       'YFIELD': '纬度',   # ← 通过 columns 语义推断得出，可能是 'latitude'/'lat'/'Y' 等\n"
        "       'TARGET_CRS': QgsCoordinateReferenceSystem('EPSG:4326'),\n"
        "       'OUTPUT': generate_output_path('points', 'tablename')\n"
        "   })\n"
        "   ```\n\n"
        "C. 【表头语义推断规则】拿到 columns 列表后，你必须自行判断 X/Y 字段：\n"
        "   - 经度列常见列名：'经度'、'lon'、'longitude'、'lng'、'X'、'x'、'jd'\n"
        "   - 纬度列常见列名：'纬度'、'lat'、'latitude'、'Y'、'y'、'wd'\n"
        "   - 大小写不敏感，优先匹配含中文列名的\n"
        "   - 如果无法确定，在回答中列出所有列名让用户选择\n\n"
        "D. 【表格路径处理】用户拖入的表格路径已在 user_request 中给出（例如：\n"
        "   '我拖入了表格文件，路径是: D:\\data\\poi.xlsx，请帮我把它解析并建立点数据。'），\n"
        "   直接从 user_request 中提取路径，不要凭空编造。\n\n"
        "## 核密度分析硬防错规则（违反任意一条将导致程序崩溃）\n\n"
        "A. 【投影转换强制】核密度分析（heatmapkerneldensityestimation）的 RADIUS 参数单位为米。\n"
        "   - 禁止对 EPSG:4326 / EPSG:4490 等地理坐标系（度为单位）直接跑核密度！\n"
        "   - 必须先用 native:reprojectlayer 将图层投影到 EPSG:3857（Web Mercator 米制），再对投影后的图层执行核密度。\n"
        "   - RADIUS 必须设置为米制数值（如 500、1000），禁止使用 0.01 等度单位值。\n"
        "   - 核密度示例流水线：\n"
        "     ```python\n"
        "     reprojected = processing.run(\"native:reprojectlayer\", {\n"
        "         'INPUT': source_layer,\n"
        "         'TARGET_CRS': QgsCoordinateReferenceSystem('EPSG:3857'),\n"
        "         'OUTPUT': generate_output_path('reproject', source_layer.name())\n"
        "     })\n"
        "     result = processing.run(\"native:heatmapkerneldensityestimation\", {\n"
        "         'INPUT': reprojected['OUTPUT'],\n"
        "         'RADIUS': 500,\n"
        "         'PIXEL_SIZE': 50,\n"
        "         'KERNEL': 0,\n"
        "         'DECAY': 0,\n"
        "         'OUTPUT_VALUE': 0,\n"
        "         'OUTPUT': generate_output_path('heatmap', source_layer.name()) + '.tif'\n"
        "     })\n"
        "     ```\n\n"
        "B. 【算法 ID 严格限定】QGIS 3.x 核密度算法 ID 有且仅有：\n"
        "   - native:heatmapkerneldensityestimation ← 正确！\n"
        "   - 绝对禁止写成 qgis:heatmapkerneldensityestimation 或 qgis:heatmap 等变体 —— 这些算法均不存在！\n\n"
        "C. 【栅格输出必须加 .tif 后缀】核密度分析输出的是栅格（GeoTIFF），不是矢量。\n"
        "   - generate_output_path() 默认生成 .shp 后缀，必须在其返回的路径末尾手动拼接 + '.tif'\n"
        "   - 正确写法：generate_output_path('heatmap', layer_name) + '.tif'\n"
        "   - 错误写法：generate_output_path('heatmap', layer_name)  ← 缺少 .tif，将导致 Could not create destination layer！\n\n"
        "D. 【createpointslayerfromtable 输出为矢量】表格空间化后的点图层是矢量 .shp，不需要 + '.tif'。\n"
        "   只有核密度（heatmapkerneldensityestimation）和栅格相关算法才需要追加 .tif。\n\n"
        "## 自动符号化渲染契约（违反将导致地图一片灰白）\n\n"
        "E. 【强制调用 style_manager】所有空间分析代码在 processing.run() 生成最终图层后，\n"
        "   必须立即调用 style_manager 为输出图层上色，严禁直接结束代码。\n"
        "   - 核密度 / 栅格输出：style_manager.apply_raster_pseudo_color(result_layer, \"Magma\")\n"
        "     推荐色带：Magma（火山）、Viridis（翠绿）、Inferno（烈焰）、Plasma（等离子）、Spectral（光谱）\n"
        "   - 矢量分析输出：style_manager.apply_vector_graduated_renderer(result_layer, \"字段名\", \"YlOrRd\")\n"
        "     推荐色带：YlOrRd（黄橙红）、Blues（蓝）、Greens（绿）、OrRd（橙红）、PuBu（紫蓝）\n"
        "   - 不指定字段时使用：style_manager.auto_style(result_layer)\n"
        "   - 核密度完整示例（含渲染）：\n"
        "     ```python\n"
        "     reprojected = processing.run(\"native:reprojectlayer\", {…})['OUTPUT']\n"
        "     result = processing.run(\"native:heatmapkerneldensityestimation\", {…})\n"
        "     result_layer = QgsRasterLayer(result['OUTPUT'], '核密度')\n"
        "     QgsProject.instance().addMapLayer(result_layer)\n"
        "     style_manager.apply_raster_pseudo_color(result_layer, 'Magma')\n"
        "     style_manager.save_project()  # 自动保存 .qgz 工程\n"
        "     ```\n\n"
        "F. 【工程自动保存】在代码最后调用 style_manager.save_project()\n"
        "   将当前所有图层、样式、视口打包保存为 output/projects/ 下的 .qgz 文件，\n"
        "   让用户可直接双击打开进行排版打印。\n\n"
        "## 【栅格样式容错规范】—— 安全落图优先于美化\n\n"
        "样式美化属于非核心的\"装饰性代码\"，严禁因样式代码翻车导致整个空间分析流水线崩溃。\n"
        "对生成的栅格/矢量图层应用自定义样式、色带时，必须遵守以下容错契约：\n\n"
        "1. 【强制 try-except 包裹】所有渲染代码（style_manager.apply_xxx / auto_style 等）\n"
        "   必须包裹在独立的 try...except 块中。渲染失败只允许 print() 打印警告，\n"
        "   严禁 raise / 抛出异常 / 让异常向上传播。\n"
        "   ```python\n"
        "   # 正确写法\n"
        "   try:\n"
        "       style_manager.apply_raster_pseudo_color(result_layer, 'Magma')\n"
        "   except Exception as e:\n"
        "       print(f'[WARNING] 样式渲染失败: {e}')\n"
        "   QgsProject.instance().addMapLayer(result_layer)  # 确保图层已加载\n"
        "   ```\n\n"
        "2. 【禁止捏造不存在的方法】style_manager 仅提供以下 3 个样式方法 + save_project：\n"
        "   - style_manager.apply_raster_pseudo_color(layer, color_ramp_name)\n"
        "   - style_manager.apply_vector_graduated_renderer(layer, field_name, color_ramp_name)\n"
        "   - style_manager.auto_style(layer)\n"
        "   - style_manager.save_project()\n"
        "   绝对禁止调用 apply_dem_style / apply_hillshade_style / apply_slope_style 等\n"
        "   任何上述列表之外的方法名！这些方法均不存在，调用必抛 AttributeError！\n\n"
        "3. 【默认落图兜底】如果不确定某个样式方法是否存在、或不知道标准 PyQGIS 渲染写法，\n"
        "   直接使用 QgsProject.instance().addMapLayer(layer) 加载默认样式。\n"
        "   数据安全落图高于一切！\n\n"
        "## GRASS 算子降级红线（违反任意一条将导致算法缺失崩溃）\n\n"
        "便携版 QGIS 环境**不存在 grass7:** 算子库**（provider id=grass7）。任何使用 grass7: 前缀\n"
        "的代码都会因 QgsProcessingException: Algorithm grass7:xxx not found 崩溃。\n\n"
        "G. 【绝对禁止 grass7: 命名空间】在水文分析、地形分析、栅格计算等任务中：\n"
        "   禁止使用以下任何算法 ID：\n"
        "   - grass7:r.fill.dir (洼地填充)\n"
        "   - grass7:r.watershed (流域划分)\n"
        "   - grass7:r.flow (流向计算)\n"
        "   - grass7:r.stream.extract (河网提取)\n"
        "   - 以及任何以 grass7: 开头的算法\n\n"
        "H. 【强制使用 native: 原生算子】所有水文/地形分析必须改用原生 native: 算子：\n"
        "   - 洼地填充: native:fill sinks (wang & liu), 备选 native:fillsinks (planchon/darboux)\n"
        "   - 流向计算: native:flowdirection (d8 / mfd 等流向算法)\n"
        "   - 汇流累积: native:flowaccumulation (d8)\n"
        "   - 河网分级: native:channelnetwork 或 native:streamorder (strahler)\n"
        "   - 流域提取: native:upslopearea 或 native:basin\n"
        "   - 地形因子: native:slope / native:aspect / native:roughness / native:relief\n\n"
        "I. 【核心降级调用契约】—— 算子缺失时的唯一合法路径\n"
        "   若原生 native: 算子也不可用，你**严禁**自行编写任何基于 numpy 循环、\n"
        "   矩阵切片或手抠的算法逻辑！你必须且只能引入系统静态降级库：\n"
        "   ```python\n"
        "   from src.core.fallback_utils import (\n"
        "       safe_fill_sinks, safe_d8_flow_direction,\n"
        "       safe_flow_accumulation, safe_stream_network\n"
        "   )\n\n"
        "   # 1. 洼地填充\n"
        "   filled = safe_fill_sinks(dem_path, fill_output)\n"
        "   # 2. D8 水流方向\n"
        "   flow_dir = safe_d8_flow_direction(fill_output, fd_output)\n"
        "   # 3. 汇流累积\n"
        "   accum = safe_flow_accumulation(fd_output, acc_output)\n"
        "   # 4. 河网提取（默认阈值 100，可调整）\n"
        "   stream = safe_stream_network(acc_output, strm_output, threshold=100)\n"
        "   ```\n"
        "   【调用禁止令】所有函数均使用位置参数传入，严禁编写自创形参名。\n"
        "   safe_fill_sinks: Priority-Flood 确定性洼地填充\n"
        "   safe_d8_flow_direction: 内置 np.pad 边缘填充 + NaN 拦截 + GeoTIFF 写出\n"
        "   safe_flow_accumulation: 拓扑排序 D8 汇流累积\n"
        "   safe_stream_network: 阈值化河网提取（threshold 默认 100）\n\n"
        # ── Phase 8: opengis-skills 知识注入 ──
        f"{gis_reference}\n"
        "12. 示例：\n"
        "该图层共有 15 个要素，总面积约 320.5 平方公里。\n"
        "```python\n"
        "result = processing.run(\"native:fieldcalculator\", {\n"
        "    'INPUT': active_layer,\n"
        "    'FIELD_NAME': 'area',\n"
        "    'FORMULA': '$area',\n"
        "    'OUTPUT': generate_output_path('fieldcalc', active_layer.name())\n"
        "})\n"
        "```\n"
        "13. 如果无法生成代码，返回解释原因的中文文本。"
    )


# ---------------------------------------------------------------------------
# Phase 9: fix_mode 回炉提示词构建器
# ---------------------------------------------------------------------------

def _build_fix_mode_prompt(
    broken_code: str,
    error_line: int,
    exception_type: str,
    exception_msg: str,
    user_query: str = "",
    retry_count: int = 0,
) -> str:
    """构建自愈回炉修正提示词。

    与正向生成提示词完全不同：聚焦于诊断崩溃代码、输出修正版本。
    长度控制在 ~400 tokens，不注入 GIS API 参考（模型应专注于修复）。
    """
    code_block = f"```python\n{broken_code}\n```"

    # ── 诊断缺失算子前缀，避免自相矛盾指引 ──
    _missing_prefix = ""
    _algo_name_hint = ""
    _mt = _re.search(r"Algorithm\s+(\S+)\s+not found", exception_msg)
    if _mt:
        _missing_algo = _mt.group(1)
        _missing_prefix = _missing_algo.split(":")[0] if ":" in _missing_algo else ""
        # 常见算法名拼写纠正（含 typo 变体）
        # 注意：仅收录经验证存在的纠正映射；已确认不存在的算法（如填坑系列）
        # 不放在此处——交由 _missing_prefix 分支处理，避免死胡同纠正浪费 retry。
        _CORRECTIONS = {
            "native:flow_direction": ("native:flowdirection",),
            "native:flow_accumulation": ("native:flowaccumulation",),
            "native:channel_network": ("native:channelnetwork",),
            "native:raster_to_vector": ("native:polygonize",),
            "native:stream_order": ("native:strahlerorder",),
            "gdal:warp": ("gdal:warpreproject",),
        }
        # 便携版 QGIS 明确不存在的算法：直接标记为 native 前缀，触发强制降级
        _KNOWN_MISSING = {
            "native:fill_sinks", "native:fillssinks",
            "saga:fillssinksxxlwangbrennan", "saga:fillsinksxxlwangbrennan",
        }
        if _missing_algo in _KNOWN_MISSING:
            _missing_prefix = "native"
        _fixes = _CORRECTIONS.get(_missing_algo)
        if _fixes:
            _algo_name_hint = (
                f"「{_missing_algo}」不存在，正确写法是「{_fixes[0]}」"
            )
            if len(_fixes) > 1:
                _algo_name_hint += f"（备选 SAGA：「{_fixes[1]}」）"

    _native_fix_guidance = ""
    # ── retry_count >= 1 且仍是 Algorithm not found → native 和 SAGA 均已耗尽 ──
    if retry_count >= 1 and _missing_prefix in ("native", "saga"):
        _native_fix_guidance = (
            "【强制降级】native: 和 SAGA 等效算子均不存在于当前 QGIS 环境，"
            "第 {n} 次重试仍然失败。\n"
            "从此刻起：\n"
            "  1. 禁止使用任何 processing.run() 调用——native: / saga: / grass7: 全部禁用\n"
            "  2. 严禁自行手写 numpy 滑窗算法！必须使用系统静态降级库：\n"
            "     from src.core.fallback_utils import safe_fill_sinks, safe_d8_flow_direction, safe_flow_accumulation, safe_stream_network\n"
            "     调用时使用位置参数，禁止自创形参名\n"
            "  3. 使用 generate_output_path() 生成输出路径\n"
            "  4. 加载到 QGIS 画布可使用 QgsRasterLayer(path, name)\n"
            "严禁再次尝试任何 processing 算法 ID 或自行手写算法。这是最后机会。\n"
        ).format(n=retry_count + 1)
    elif _missing_prefix == "native":
        _native_fix_guidance = (
            "修正策略：native: 前缀的算法不存在，可能是拼写错误或该 QGIS 版本未内置。\n"
            "  1. 检查算法名拼写（QGIS Processing 算法 ID 不使用下划线分割单词）\n"
            "  2. 改用 SAGA 或 GDAL 等效算子（若已知）\n"
            "  3. 若上一步不确定或 SAGA/GDAL 也不可用，使用系统静态降级库：\n"
            "     from src.core.fallback_utils import safe_fill_sinks, safe_d8_flow_direction, safe_flow_accumulation, safe_stream_network\n"
            "     调用时使用位置参数，禁止带形参名\n"
        )
    elif _missing_prefix:
        _native_fix_guidance = (
            "修正策略：立即放弃「{prefix}:」前缀的算法，改用 native: 原生等效算子。\n"
        ).format(prefix=_missing_prefix)

    # ── 样式方法幻觉检测 ──
    _style_attr_error = ""
    _st = _re.search(r"'StyleManager' object has no attribute '(\w+)'", exception_msg)
    if _st:
        _hallucinated_method = _st.group(1)
        _style_attr_error = (
            f"【样式方法幻觉】style_manager.{_hallucinated_method} 不存在！\n"
            "style_manager 仅提供以下方法：\n"
            "  - apply_raster_pseudo_color(layer, color_ramp_name)\n"
            "  - apply_vector_graduated_renderer(layer, field_name, color_ramp_name)\n"
            "  - auto_style(layer)\n"
            "  - save_project()\n"
            "修正：删除不存在的样式调用，改用 QgsProject.instance().addMapLayer(layer) 默认落图。\n"
        )

    return (
        _style_attr_error + "\n" if _style_attr_error else "") + (
        "你是一名 PyQGIS 代码调试专家。以下代码在 QGIS 沙箱中执行时崩溃。\n"
        "请分析崩溃原因，并输出修正后的完整代码。\n\n"
        "## 【硬性输出契约】—— 最高优先级\n\n"
        "无论你如何反思崩溃原因，你必须在回答的最终结尾，输出包含在\n"
        "```python ... ``` 格式中的、完整且可运行的 Python 代码。\n"
        "严禁仅输出解释性文本而不提供代码块！你的回答格式严格为：\n"
        "1-2 句根因分析（中文）→ 空行 → ```python 代码块。\n\n"
        "## 用户原始需求\n"
        f"{user_query or '(未提供)'}\n\n"
        "## 崩溃信息\n"
        f"- 异常类型：{exception_type}\n"
        f"- 异常消息：{exception_msg}\n"
        f"- 崩溃行号：第 {error_line} 行\n"
    ) + (
        f"【关键】{_algo_name_hint}\n" if _algo_name_hint else ""
    ) + (
        "## 崩溃代码\n"
        f"{code_block}\n\n"
        "## 异常分类与降级策略\n"
        "根据崩溃类型选择修正策略：\n\n"
        "### 【环境缺失型异常】\n"
        "当异常消息包含以下任一特征时为环境缺失型，这意味着代码逻辑可能正确，\n"
        "但当前便携版 QGIS 环境缺少该算法库，绝不能再次尝试相同算法：\n"
        '  - "Algorithm xxx not found"\n'
        '  - "provider" 相关错误\n'
    ) + (
        f"{_native_fix_guidance}\n"
        "如没有任何等效算子，使用系统静态降级库 safe_fill_sinks / safe_d8_flow_direction / safe_flow_accumulation / safe_stream_network。\n\n"
        if _missing_prefix else
        "修正策略：使用系统静态降级库 safe_fill_sinks / safe_d8_flow_direction / safe_flow_accumulation / safe_stream_network。\n\n"
    ) + (
        "### 【代码逻辑型异常】\n"
        '  - NameError / SyntaxError / TypeError / ValueError / AttributeError\n'
        "修正策略：分析根因，修正语法/变量引用/类型/投影等错误。\n\n"
        "## 修正要求\n"
        "1. 先简要说明崩溃根因和异常分类（1-2 句中文）。\n"
        "2. 然后在 ```python ... ``` 代码块中给出修正后的完整代码。\n"
        "3. 代码必须以 processing.run() 结尾，返回值直接赋给 result。\n"
        "4. result = processing.run(...)，不得修改或包装。\n"
        "5. 优先使用 active_layer 作为输入图层。\n"
        "6. 输出图层必须使用 generate_output_path('skill_prefix', active_layer.name()) 生成持久化路径。\n"
        "7. 所有空间分析代码必须调用 style_manager 为输出图层上色，但必须用 try-except 包裹：\n"
        "   try:\n"
        "       style_manager.apply_raster_pseudo_color(result_layer, 'Magma')\n"
        "   except Exception as e:\n"
        "       print(f'[WARNING] 样式渲染失败: {e}')\n"
        "   QgsProject.instance().addMapLayer(result_layer)  # 确保图层已加载\n"
        "   严禁调用 apply_dem_style / apply_hillshade_style 等不存在的方法！\n"
        "   样式失败只打印警告，严禁抛异常导致流水线崩溃。\n"
        "8. 核密度分析输出必须追加 .tif 后缀。\n"
        "9. 不要引入与原始代码无关的新功能。\n"
        "【严禁变量污染】禁止将 Python 内置函数名（exec, eval, print, id, type）用作变量名。\n"
        "尤其是：exec = processing.run(...) 这类写法会永久污染命名空间，直接导致沙箱崩溃！\n"
        "结果必须严格赋值给 result 变量。\n"
    )


def request_spatial_code(user_text: str, layer_metadata: List[Dict[str, Any]]) -> str:
    """向 API 请求空间分析代码（第二轮调用）。

    供 spatial_analysis skill 内部使用。
    Phase 7: 注入 mem0 空间分析历史避坑经验。
    """

    # ── Phase 7: 检索空间分析历史经验 ──
    spatial_tips = ""
    try:
        from core.memory_bridge import get_memory_bridge

        bridge = get_memory_bridge()
        if bridge.ready:
            # 从 layer_metadata 提取图层名
            layer_name = ""
            if layer_metadata:
                active_layers = [m for m in layer_metadata if m.get("is_active")]
                if active_layers:
                    layer_name = active_layers[0].get("name", "")
                elif len(layer_metadata) > 0:
                    layer_name = layer_metadata[0].get("name", "")

            spatial_tips = bridge.search_spatial_experience(
                layer_name=layer_name,
                skill_name="spatial_analysis",
            )
            if spatial_tips:
                _log.info("代码生成注入 %d 条历史避坑经验", spatial_tips.count("\n-"))
    except Exception as exc:
        _log.debug("空间经验检索跳过: %s", exc)

    # 构建带经验的提示词（传入 user_text 用于动态 API 参考过滤）
    prompt = build_code_generation_prompt(spatial_tips=spatial_tips, user_query=user_text)

    _log.info("发起空间分析代码生成请求")
    body = {
        "model": ai_config.MODEL_NAME,
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": json.dumps(
                    {"user_request": user_text, "active_layers": layer_metadata},
                    ensure_ascii=False,
                    indent=2,
                ),
            },
        ],
    }

    url = build_chat_completions_url(ai_config.BASE_URL)
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {ai_config.API_KEY}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        result = data["choices"][0]["message"]["content"]
        _log.debug("空间分析代码响应：%s", result[:200])
        return result
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        _log.error("空间分析代码 HTTP %s：%s", exc.code, detail[:500])
        raise RuntimeError(f"AI 接口请求失败，HTTP {exc.code}：{detail}") from exc
    except urllib.error.URLError as exc:
        _log.error("空间分析代码连接失败：%s", exc.reason)
        raise RuntimeError(f"AI 接口连接失败：{exc.reason}") from exc


# ---------------------------------------------------------------------------
# Phase 9: 自愈回炉接口
# ---------------------------------------------------------------------------

def request_code_fix(
    broken_code: str,
    error_line: int,
    exception_type: str,
    exception_msg: str,
    user_query: str = "",
    retry_count: int = 0,
) -> str:
    """自愈回炉接口：将崩溃代码 + 错误上下文发送 LLM，获取修正代码。

    调用方（spatial_analysis skill 或 main_window）在收到 SandboxExecutionWorker
    的 fix_needed 信号后调用此函数。复用与 request_spatial_code 相同的 LLM
    调用基础设施（URL / API Key / 超时）。

    Parameters
    ----------
    broken_code : str
        在沙箱中执行崩溃的原始 PyQGIS 代码。
    error_line : int
        崩溃行号（来自 traceback 或 SyntaxError.lineno）。
    exception_type : str
        异常类型，如 SyntaxError / NameError / TypeError / RuntimeError。
    exception_msg : str
        完整的异常消息文本。
    user_query : str
        用户原始空间分析问题，供 LLM 理解修正上下文。
    retry_count : int
        当前重试次数（0-based），用于感知 native→SAGA 均已耗尽的情况。

    Returns
    -------
    str
        LLM 返回的修正后 PyQGIS 代码（含崩溃根因说明 + ```python 代码块）。
    """
    # 构建 fix_mode 系统提示词
    prompt = _build_fix_mode_prompt(
        broken_code=broken_code,
        error_line=error_line,
        exception_type=exception_type,
        exception_msg=exception_msg,
        user_query=user_query,
        retry_count=retry_count,
    )

    _log.info("发起自愈回炉修正请求（%s 第 %d 行）", exception_type, error_line)

    body = {
        "model": ai_config.MODEL_NAME,
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "mode": "fix",
                        "broken_code": broken_code,
                        "error_line": error_line,
                        "exception_type": exception_type,
                        "exception_msg": exception_msg,
                        "user_query": user_query,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            },
        ],
    }

    url = build_chat_completions_url(ai_config.BASE_URL)
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {ai_config.API_KEY}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        result = data["choices"][0]["message"]["content"]
        _log.debug("自愈回炉响应：%s", result[:200])
        return result
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        _log.error("自愈回炉 HTTP %s：%s", exc.code, detail[:500])
        raise RuntimeError(f"自愈回炉接口请求失败，HTTP {exc.code}：{detail}") from exc
    except urllib.error.URLError as exc:
        _log.error("自愈回炉连接失败：%s", exc.reason)
        raise RuntimeError(f"自愈回炉接口连接失败：{exc.reason}") from exc


# ---------------------------------------------------------------------------
# 向后兼容：保留旧版 parse_agent_response（单对象解析）
# ---------------------------------------------------------------------------

def parse_agent_response(response_text: str) -> Dict[str, str]:
    """解析 AI 返回的单个 JSON 路由指令（向后兼容）。

    优先使用 parse_pipeline_response 获取流水线数组。
    """

    text = response_text.strip()

    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:] if lines[0].startswith("```") else lines
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        import re
        match = re.search(r'\{[^{}]*"skill"[^{}]*\}', response_text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        _log.warning("AI 响应不是合法 JSON，原始内容前 500 字符：%s", response_text[:500])
        raise RuntimeError(
            f"AI 返回的不是合法 JSON。\n\n原始响应：\n{response_text[:500]}"
        )
