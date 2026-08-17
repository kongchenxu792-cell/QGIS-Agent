"""clarification — P2-4 运行前澄清：图层角色多候选交互点选。

纯确定性逻辑 + 常量，零外部依赖（仅可选导入现有 qgis.core）。
LLM 零参与提问：候选枚举、问题拼接、降级文案全部由代码生成。

设计边界（写进代码注释防范围爆炸，任务书裁决点）：
- 不做运行中途暂停询问（路线图红线）
- 不做 action=unknown 的兜底扩面（裁决点 2）
- 不做字段级澄清（population_field 多候选数值字段不弹窗，维持自动探测）
- 不做澄清结果入 RunQueue（当前 AI 运行流本就不经队列）
- 不做澄清超时自动降级（模态对话框由用户决策）
"""

from __future__ import annotations

from typing import Any, Dict, List

# 状态标记常量：澄清请求（区别于 failed / degraded / ok）
CLARIFICATION_STATUS = "clarification"

# 6 个图层角色 × 三语显示名（与 i18n 三语惯例一致）
ROLE_LABELS: Dict[str, Dict[str, str]] = {
    "source_layer": {"zh": "源图层", "ja": "ソースレイヤ", "en": "source layer"},
    "boundary_layer": {"zh": "边界图层", "ja": "境界レイヤ", "en": "boundary layer"},
    "population_layer": {"zh": "人口图层", "ja": "人口レイヤ", "en": "population layer"},
    "intensity_layer": {"zh": "震度图层", "ja": "震度レイヤ", "en": "intensity layer"},
    "target_layer": {"zh": "目标图层", "ja": "ターゲットレイヤ", "en": "target layer"},
    "join_layer": {"zh": "关联图层", "ja": "結合レイヤ", "en": "join layer"},
}

# 角色 → 期望几何类型：None 表示不限（全部矢量图层）
# source→Point，boundary/population/intensity→Polygon，target/join→不限
ROLE_GEOMETRY: Dict[str, Any] = {
    "source_layer": "Point",
    "boundary_layer": "Polygon",
    "population_layer": "Polygon",
    "intensity_layer": "Polygon",
    "target_layer": None,
    "join_layer": None,
}


def find_layer_candidates(project, layer_name: str,
                          case_insensitive: bool = True) -> List[str]:
    """按名称匹配图层候选：精确匹配优先，无精确时子串模糊匹配返回全部。

    语义与 handlers_basic._find_layer 完全一致，仅从「取第一个」改为「返回全部」。
    - 精确命中 ≥1 时只返回精确候选；
    - 无精确时返回全部子串模糊候选；
    - 保持项目图层顺序；project 为 None 返回 []。
    """
    if project is None or not layer_name:
        return []
    try:
        layers = list(project.mapLayers().values())
    except Exception:
        return []
    target = layer_name.lower() if case_insensitive else layer_name
    exact: List[str] = []
    fuzzy: List[str] = []
    for layer in layers:
        try:
            cur = layer.name()
        except Exception:
            continue
        if not cur:
            continue
        cmp_name = cur.lower() if case_insensitive else cur
        if cmp_name == target:
            exact.append(cur)
        elif target in cmp_name:
            fuzzy.append(cur)
    if exact:
        return exact
    return fuzzy


def role_candidates(project, param_key: str) -> List[str]:
    """按角色几何类型枚举候选图层名列表（保持项目图层顺序）。

    - source_layer → 仅点图层
    - boundary_layer / population_layer / intensity_layer → 仅面图层
    - target_layer / join_layer → 全部矢量图层
    - 未知角色 / project 为 None → []
    是否触发澄清（候选 ≥2）由调用方判断，本函数仅枚举。
    """
    if project is None or param_key not in ROLE_GEOMETRY:
        return []
    geometry = ROLE_GEOMETRY[param_key]
    try:
        from qgis.core import QgsMapLayer, QgsWkbTypes
    except ImportError:
        return []
    candidates: List[str] = []
    try:
        layers = list(project.mapLayers().values())
    except Exception:
        return []
    for layer in layers:
        try:
            if layer.type() != QgsMapLayer.VectorLayer:
                continue
            if geometry is None:
                candidates.append(layer.name())
                continue
            if geometry == "Point" and layer.geometryType() == QgsWkbTypes.PointGeometry:
                candidates.append(layer.name())
            elif geometry == "Polygon" and layer.geometryType() == QgsWkbTypes.PolygonGeometry:
                candidates.append(layer.name())
        except Exception:
            continue
    return candidates


def build_clarification(action: str, param_key: str, candidates: List[str],
                        params: Dict[str, Any]) -> Dict[str, Any]:
    """构造澄清请求结构（question 与 message 全部由代码拼接，含候选名）。

    message 可直接作为降级报错文案；params 携带完整参数供审计与重跑回填。
    """
    role_label = ROLE_LABELS.get(param_key, {}).get("zh", param_key)
    candidate_text = "、".join(str(c) for c in candidates)
    return {
        "success": False,
        "status": CLARIFICATION_STATUS,
        "action": action,
        "message": f"需要澄清：「{role_label}」存在多个候选（{candidate_text}），请选择。",
        "clarification": {
            "action": action,
            "param_key": param_key,
            "question": f"「{role_label}」角色存在多个候选图层，请选择：",
            "candidates": list(candidates),
            "params": dict(params),
        },
    }


def is_clarification_result(result) -> bool:
    """判断结果是否为结构完整的澄清请求。

    结构完整 = status == CLARIFICATION_STATUS 且 clarification 含非空 param_key /
    非空 candidates 列表。
    """
    if not isinstance(result, dict):
        return False
    if result.get("status") != CLARIFICATION_STATUS:
        return False
    clar = result.get("clarification")
    if not isinstance(clar, dict):
        return False
    if not clar.get("param_key"):
        return False
    candidates = clar.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return False
    return True


def format_cancel_message(clarification) -> str:
    """取消时确定性报错文案：未选择「{角色}」图层，操作已取消。候选图层：{候选名逗号列表}。"""
    param_key = clarification.get("param_key", "")
    role_label = ROLE_LABELS.get(param_key, {}).get("zh", param_key)
    candidates = clarification.get("candidates", [])
    candidate_text = "、".join(str(c) for c in candidates)
    return f"未选择「{role_label}」图层，操作已取消。候选图层：{candidate_text}"
