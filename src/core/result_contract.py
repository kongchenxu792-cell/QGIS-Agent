"""
统一结果契约 (Result Contract) — OFFLINE_FIRST 兼容层

所有 SandboxExecutionWorker.finished 信号必须输出此 schema。
本模块提供 schema 定义、校验与 adapter 层，兼容历史 ad-hoc 返回格式。

Schema 字段约定：
- layers[]   : 产出图层列表（至少 1 个有效图层时非空）
- files[]    : 导出文件列表（PNG/PDF/GeoJSON/CSV 等）
- messages[] : 面向用户的可读消息（缺省至少 1 条 summary）
- stats{}    : 数值统计（允许空 dict）
"""

from typing import Any, Dict, List, Optional, Union
from qgis.core import QgsMapLayer, QgsVectorLayer, QgsRasterLayer

# ================================================================
# JSON Schema 定义
# ================================================================

LAYER_SCHEMA = {
    "layer_id": str,       # QgsMapLayer.id()
    "layer_name": str,     # 图层树显示名称
    "layer_type": str,     # "vector" | "raster"
    "source_path": str,    # 磁盘路径（TEMPORARY_OUTPUT 时为空字符串）
    "feature_count": int,  # 要素数（栅格为 -1）
    "geometry_type": str,  # "Point" / "Polygon" / ... 或 ""（栅格时）
}

FILE_SCHEMA = {
    "file_path": str,      # 绝对路径
    "file_type": str,      # MIME 类型或扩展名标识 "image/png" | "application/pdf"
    "description": str,    # 人类可读描述
}

MESSAGE_SCHEMA = {
    "level": str,          # "info" | "warning" | "error"
    "content": str,        # 消息正文
}

UNIFIED_RESULT = {
    "layers": List[Dict],   # List[LAYER_SCHEMA]
    "files": List[Dict],    # List[FILE_SCHEMA]
    "messages": List[Dict], # List[MESSAGE_SCHEMA]
    "stats": Dict,          # Dict[str, Any] 自由数值统计
}

# ================================================================
# 图层转换工具
# ================================================================

def _layer_to_schema(layer: QgsMapLayer, description: str = "") -> Optional[Dict]:
    """将 QgsMapLayer 转为 LAYER_SCHEMA 字典。"""
    if layer is None or not layer.isValid():
        return None
    try:
        lid = layer.id() or ""
        name = layer.name() or ""
        src = layer.source() or ""

        if isinstance(layer, QgsVectorLayer):
            ltype = "vector"
            fc = layer.featureCount() if layer.isValid() else 0
            gt = ""
            try:
                geom_type = layer.geometryType()
                from qgis.core import QgsWkbTypes
                gt = QgsWkbTypes.displayString(geom_type) if geom_type is not None else ""
            except Exception:
                gt = ""
        elif isinstance(layer, QgsRasterLayer):
            ltype = "raster"
            fc = -1
            gt = ""
        else:
            ltype = "unknown"
            fc = 0
            gt = ""

        return {
            "layer_id": lid,
            "layer_name": name,
            "layer_type": ltype,
            "source_path": src,
            "feature_count": fc,
            "geometry_type": gt,
        }
    except Exception:
        return None


def _extract_layers_from_value(value: Any) -> List[Dict]:
    """从任意返回值中提取图层列表。"""
    layers = []
    if value is None:
        return layers

    # 单个 QgsMapLayer
    if isinstance(value, (QgsVectorLayer, QgsRasterLayer)):
        entry = _layer_to_schema(value)
        if entry:
            layers.append(entry)

    # 列表: [layer1, layer2, ...]
    elif isinstance(value, list):
        for item in value:
            entry = _layer_to_schema(item)
            if entry:
                layers.append(entry)

    # 字典: {"name": layer, ...} 或 processing.run() 返回
    elif isinstance(value, dict):
        for v in value.values():
            entry = _layer_to_schema(v)
            if entry:
                # 去重
                if entry["layer_id"] not in {l["layer_id"] for l in layers}:
                    layers.append(entry)

    return layers


# ================================================================
# 核心 Adapter
# ================================================================

def adapt_finished_result(
    sandbox_result: Dict[str, Any],
    skill_name: str = "",
    user_query: str = "",
    extra_files: Optional[List[Dict]] = None,
    extra_stats: Optional[Dict] = None,
    extra_messages: Optional[List[Dict]] = None,
) -> Dict[str, Any]:
    """
    将 SandboxExecutionWorker.finished 的原始返回 + 沙箱上下文，
    适配为 UNIFIED_RESULT schema。

    Parameters
    ----------
    sandbox_result : dict
        finished 信号原始数据，包含:
        - result: Any (exec_globals["result"])
        - pending_layers: List[QgsMapLayer]
        - gc_removed: List[str]
        - stdout: str
        - retry_count: int
    skill_name : str
        执行的技能名称。
    user_query : str
        用户原始指令。
    extra_files : list | None
        额外文件列表（如 export_map 产生的 PNG）。
    extra_stats : dict | None
        额外统计。
    extra_messages : list | None
        额外消息。

    Returns
    -------
    dict
        符合 UNIFIED_RESULT schema 的字典。
    """
    layers = []
    files = list(extra_files) if extra_files else []
    messages = []
    stats = dict(extra_stats) if extra_stats else {}

    raw_result = sandbox_result.get("result")
    pending_layers = sandbox_result.get("pending_layers", [])
    gc_removed = sandbox_result.get("gc_removed", [])
    stdout = sandbox_result.get("stdout", "")
    retry_count = sandbox_result.get("retry_count", 0)

    # ── 1. 从 pending_layers 提取图层（主线程即将加载的图层）──
    for lyr in pending_layers:
        entry = _layer_to_schema(lyr)
        if entry:
            if entry["layer_id"] not in {l["layer_id"] for l in layers}:
                layers.append(entry)

    # ── 2. 从 raw_result 中提取图层 ──
    for entry in _extract_layers_from_value(raw_result):
        if entry["layer_id"] not in {l["layer_id"] for l in layers}:
            layers.append(entry)

    # ── 3. 构建 messages ──
    if layers:
        layer_names = [l["layer_name"] for l in layers]
        messages.append({
            "level": "info",
            "content": f"产出 {len(layers)} 个图层: {', '.join(layer_names)}",
        })

    if gc_removed:
        messages.append({
            "level": "info",
            "content": f"已清理 {len(gc_removed)} 个中间图层",
        })

    if stdout and stdout.strip():
        # stdout 截断到 500 字符，避免撑爆 JSON
        stdout_short = stdout.strip()[:500]
        messages.append({
            "level": "info",
            "content": f"[沙箱输出] {stdout_short}",
        })

    if retry_count > 0:
        messages.append({
            "level": "warning",
            "content": f"执行重试 {retry_count} 次后成功",
        })

    if raw_result is None and not layers and not files:
        messages.append({
            "level": "warning",
            "content": "执行完成但未检测到产出图层或文件",
        })

    # 追加额外消息
    if extra_messages:
        messages.extend(extra_messages)

    # ── 4. 补充 skill/user_query 上下文到 stats ──
    stats["_skill"] = skill_name
    stats["_layer_count"] = len(layers)
    stats["_file_count"] = len(files)
    stats["_retry_count"] = retry_count

    # ── 5. 如果没有图层也没有文件 → 添加兜底 message ──
    if not layers and not files:
        summary_msgs = [m["content"] for m in messages]
        # 检查是否已有 warning/error 级别消息
        has_explicit = any(m["level"] in ("warning", "error") for m in messages)
        if not has_explicit:
            messages.append({
                "level": "info",
                "content": f"技能 '{skill_name}' 执行完毕，指令: {user_query[:80]}",
            })

    return {
        "layers": layers,
        "files": files,
        "messages": messages,
        "stats": stats,
    }


def validate_schema(data: Dict[str, Any]) -> List[str]:
    """校验统一结果是否合法。返回错误列表，空列表表示通过。"""
    errors = []

    if not isinstance(data, dict):
        return ["结果不是字典类型"]

    for key in ("layers", "files", "messages", "stats"):
        if key not in data:
            errors.append(f"缺少字段: {key}")
            continue
        if key == "stats":
            if not isinstance(data[key], dict):
                errors.append(f"stats 必须是 dict，实际: {type(data[key]).__name__}")
        else:
            if not isinstance(data[key], list):
                errors.append(f"{key} 必须是 list，实际: {type(data[key]).__name__}")

    # layers 字段校验
    for i, layer in enumerate(data.get("layers", [])):
        for field in ("layer_id", "layer_name", "layer_type", "source_path"):
            if field not in layer:
                errors.append(f"layers[{i}] 缺少字段: {field}")

    # files 字段校验
    for i, f in enumerate(data.get("files", [])):
        for field in ("file_path", "file_type", "description"):
            if field not in f:
                errors.append(f"files[{i}] 缺少字段: {field}")

    # messages 字段校验
    for i, m in enumerate(data.get("messages", [])):
        for field in ("level", "content"):
            if field not in m:
                errors.append(f"messages[{i}] 缺少字段: {field}")
        if m.get("level") not in ("info", "warning", "error"):
            errors.append(f"messages[{i}].level 非法值: {m.get('level')}")

    return errors
