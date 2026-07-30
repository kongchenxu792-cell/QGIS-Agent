"""
Tier0 模板注册与调度表

支持关键词 → 闭环路由，离线模式下直接映射执行。
"""

from typing import Dict, Any, Callable, Optional


# ================================================================
# 闭环注册表
# ================================================================

LOOP_REGISTRY: Dict[str, Dict[str, Any]] = {
    "loop_a": {
        "name": "震度影响区态势图",
        "keywords": [
            "震度", "地震", "影响区", "态势图", "强度区",
            "intensity", "earthquake", "seismic",
        ],
        "handler": "src.skills.tier0_templates.loop_a.run_loop_a",
        "input_params": {
            "intensity_zone": {"type": "layer_path", "required": True, "label": "震度区图层"},
            "poi": {"type": "layer_path", "required": True, "label": "兴趣点图层"},
            "admin": {"type": "layer_path", "required": False, "label": "行政区划图层"},
        },
    },
    "loop_b": {
        "name": "避难所覆盖盲区",
        "keywords": [
            "避难所", "盲区", "覆盖", "疏散", "避难",
            "shelter", "evacuation", "coverage",
        ],
        "handler": "src.skills.tier0_templates.loop_b.run_loop_b",
        "input_params": {
            "shelter": {"type": "layer_path", "required": True, "label": "避难所图层"},
            "radius_m": {"type": "int", "required": False, "default": 500, "label": "服务半径(米)"},
            "admin_boundary": {"type": "layer_path", "required": False, "label": "行政区划图层"},
        },
    },
}


# ================================================================
# 关键词匹配
# ================================================================

def match_loop(user_query: str) -> Optional[str]:
    """根据用户输入匹配闭环名。返回 loop_a / loop_b 或 None。"""
    if not user_query:
        return None
    q = user_query.lower()
    for loop_id, meta in LOOP_REGISTRY.items():
        for kw in meta["keywords"]:
            if kw.lower() in q:
                return loop_id
    return None


# ================================================================
# 动态加载 handler
# ================================================================

def _import_handler(handler_path: str):
    """动态导入 handler 函数，例如 'src.skills.tier0_templates.loop_a.run_loop_a'"""
    module_path, func_name = handler_path.rsplit(".", 1)
    import importlib
    mod = importlib.import_module(module_path)
    return getattr(mod, func_name)


def execute_loop(loop_id: str, **params) -> Dict[str, Any]:
    """执行指定闭环。"""
    if loop_id not in LOOP_REGISTRY:
        return {
            "layers": [], "files": [], "messages": [
                {"level": "error", "content": f"未知闭环: {loop_id}"}
            ], "stats": {},
        }
    meta = LOOP_REGISTRY[loop_id]
    handler = _import_handler(meta["handler"])
    return handler(**params)
