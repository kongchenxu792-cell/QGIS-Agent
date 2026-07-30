"""
Tier1 约束层：参数解析 + 代码拒绝策略

约束：
- Tier1 本地模型仅能做参数解析 → 映射到 Tier0 模板
- 禁止生成自由 PyQGIS 代码
- 执行前硬校验：检测 import processing / exec / eval 等危险模式
- 被拒绝时返回可执行 messages 提示

架构：
  Tier1 模型输出结构化参数 JSON → validate_params() → execute_loop()
  若模型输出代码文本 → reject_as_code() 阻断并返回修正提示
"""

import re
import json
from typing import Any, Dict, List, Optional, Tuple

# ================================================================
# 代码危险模式（硬拒绝）
# ================================================================

_DANGEROUS_PATTERNS = [
    # 自由 QGIS processing 调用（绕过模板）
    r"processing\.run\s*\(",
    r"processing\.runAndLoadResults\s*\(",
    # 危险 Python 执行
    r"\bexec\s*\(",
    r"\beval\s*\(",
    r"\bcompile\s*\(",
    # 导入任意模块
    r"from\s+osgeo\s+import",
    r"import\s+processing",
    r"import\s+qgis\.core",
    r"import\s+PyQt",
    # 文件系统写入（绕过 output_persistence）
    r"open\s*\([^)]*['\"][wW]",
    r"with\s+open\s*\([^)]*['\"][wW]",
]

# Tier0 模板中允许的安全模式（不会被误杀）
_SAFE_TEMPLATE_SNIPPETS = [
    # run_loop_a / run_loop_b 中 explicit importing
    "from src.core.output_persistence import",
    "from src.skills.tier0_templates",
]


# ================================================================
# 拒绝策略
# ================================================================

def is_tier0_safe_template(code: str) -> bool:
    """检查代码是否为 Tier0 模板调用（允许的安全代码）。"""
    for safe in _SAFE_TEMPLATE_SNIPPETS:
        if safe in code:
            return True
    return False


def reject_as_code(code: str) -> Dict[str, Any]:
    """
    检测 Tier1 模型输出的代码文本，若命中危险模式则返回拒绝结果。
    若代码的意图是调用 Tier0 模板则不拒绝。

    Returns
    -------
    dict
        若被拒绝: {"rejected": True, "reason": str, "messages": [...]}
        若通过:   {"rejected": False}
    """
    if is_tier0_safe_template(code):
        return {"rejected": False}

    hits = []
    for pattern in _DANGEROUS_PATTERNS:
        if re.search(pattern, code):
            hits.append(pattern)

    if hits:
        return {
            "rejected": True,
            "reason": f"Tier1 模型输出包含 {len(hits)} 个危险模式，已拒绝执行。",
            "dangerous_patterns": hits,
            "messages": [
                {
                    "level": "error",
                    "content": (
                        "Tier1 约束：禁止生成自由 PyQGIS 代码。"
                        "请改用参数解析格式，示例如下：\n"
                        '{"loop": "loop_a", "params": {"intensity_zone": "/path/to/shp", "poi": "/path/to/shp"}}'
                    ),
                }
            ],
            "stats": {"rejected_patterns": len(hits)},
        }

    return {"rejected": False}


# ================================================================
# 参数解析
# ================================================================

def extract_loop_id(text: str) -> Optional[str]:
    """从用户输入或模型输出中提取闭环 ID。"""
    from skills.tier0_templates import match_loop
    return match_loop(text)


def parse_params_from_text(text: str, loop_id: str) -> Tuple[Dict[str, Any], List[str]]:
    """
    从文本中提取结构化参数。

    尝试顺序：
    1. JSON 格式: {"loop": "loop_a", "params": {"intensity_zone": "..."}}
    2. 常见中文关键词: 震度区=XX路径 / POI=XX路径

    Returns
    -------
    (params_dict, warnings_list)
    """
    from skills.tier0_templates import LOOP_REGISTRY

    meta = LOOP_REGISTRY.get(loop_id, {})
    input_params = meta.get("input_params", {})
    params = {}
    warnings = []

    # ── 尝试1: JSON 解析 ──
    json_pattern = r'\{(?:[^{}]|(?:\{[^{}]*\}))*\}'
    for match in re.finditer(json_pattern, text):
        try:
            data = json.loads(match.group())
            if isinstance(data, dict):
                # 嵌套结构: {"loop": "loop_a", "params": {...}}
                if "params" in data and isinstance(data["params"], dict):
                    for k, v in data["params"].items():
                        if k in input_params:
                            params[k] = v
                # 扁平结构: {"intensity_zone": "...", "poi": "..."}
                for k, v in data.items():
                    if k in input_params and k not in params:
                        params[k] = v
        except (json.JSONDecodeError, ValueError):
            pass

    # ── 尝试2: 中文关键词解析 ──
    if not params:
        param_aliases = {
            "intensity_zone": ["震度区", "强度区", "地震区", "影响区图层", "intensity_zone"],
            "poi": ["兴趣点", "POI", "poi", "设施"],
            "admin": ["行政区划", "admin", "行政区", "边界"],
            "admin_boundary": ["行政区划", "admin", "行政区", "边界"],
            "shelter": ["避难所", "shelter", "避难", "收容所"],
            "radius_m": ["半径", "radius", "距离"],
        }
        for param_key, aliases in param_aliases.items():
            if param_key not in input_params:
                continue
            for alias in aliases:
                # 模式: "关键词=/路径" 或 "关键词：/路径"
                pat = rf'{re.escape(alias)}\s*[=：:]\s*["\']?([^\s",，]+)["\']?'
                m = re.search(pat, text)
                if m:
                    val = m.group(1)
                    # 尝试转整型
                    if input_params[param_key]["type"] == "int":
                        try:
                            val = int(val)
                        except ValueError:
                            pass
                    params[param_key] = val
                    break

    # ── 检查必填字段 ──
    for pkey, pmeta in input_params.items():
        if pmeta.get("required") and pkey not in params:
            warnings.append(f"缺少必填参数: {pmeta['label']} ({pkey})")
        # 填充默认值
        if pkey not in params and "default" in pmeta:
            params[pkey] = pmeta["default"]

    return params, warnings


def tier1_dispatch(text: str) -> Dict[str, Any]:
    """
    Tier1 主入口：接收模型输出，拒绝代码或执行闭环。

    流程：
    1. reject_as_code() — 检测是否包含危险代码
    2. 提取 loop_id
    3. parse_params_from_text() — 提取参数
    4. execute_loop() — 执行 Tier0 模板
    5. 返回统一结果契约

    Parameters
    ----------
    text : str
        Tier1 模型输出的原始文本。

    Returns
    -------
    dict
        统一结果契约（含 rejection 信息）。
    """
    # 1. 拒绝代码检查
    rejection = reject_as_code(text)
    if rejection.get("rejected"):
        return {
            "layers": [],
            "files": [],
            "messages": rejection["messages"],
            "stats": rejection.get("stats", {}),
        }

    # 2. 提取闭环
    loop_id = extract_loop_id(text)
    if not loop_id:
        return {
            "layers": [], "files": [],
            "messages": [
                {
                    "level": "error",
                    "content": (
                        f"Tier1 无法识别闭环类型。支持: 震度影响区态势图 / 避难所覆盖盲区。"
                        f"输入: {text[:100]}"
                    ),
                }
            ],
            "stats": {},
        }

    # 3. 解析参数
    params, warnings = parse_params_from_text(text, loop_id)
    warn_msgs = [
        {"level": "warning", "content": w}
        for w in warnings
        if "必填" not in w
    ]
    required_msgs = [
        {"level": "error", "content": w}
        for w in warnings
        if "必填" in w
    ]

    required_missing = [
        w for w in warnings
        if "必填" in w
    ]
    if required_missing:
        return {
            "layers": [], "files": [],
            "messages": required_msgs,
            "stats": {"loop_id": loop_id, "missing_params": len(required_missing)},
        }

    # 4. 执行模板
    from skills.tier0_templates import execute_loop
    result = execute_loop(loop_id, **params)
    # 合并警告
    if "messages" in result:
        result["messages"].extend(warn_msgs)
    else:
        result["messages"] = warn_msgs

    return result
