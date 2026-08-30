# -*- coding: utf-8 -*-
r"""src/core/disaster_registry.py — 多灾种注册表（灾种可插拔，Solo 片A）

产品定位「多灾种快速评估，灾种可插拔」：灾种 = 危险区图层集 + 模板实例。
引擎不认灾种，只认「危险区集 + 边界 + 避难所」；灾种差异全部由本注册表承载。

注册表字段说明：
- disaster_id    : 稳定标识（英文小写，跨会话不变）
- names          : 三语名称 {zh, ja, en}
- risk_zone_layer: 危险区图层文件名（默认位于 temp/multi_disaster/，与
                   scripts/gen_multi_disaster_data.py 输出一致；条目可带
                   data_dir 字段指定独立数据目录）
- data_dir       : 可选；条目级数据目录（相对默认危险区目录的覆盖），
                   用于 CN 场景等非合成数据条目
- country        : 可选；场景国家/地区标识（如 CN）
- template       : 模板实例名（src/core/templates/{template}.json）
- triggers       : 三语触发词（供片B UI 下拉 / 指令映射扩展使用）
- description    : 一句话说明

扩展新灾种：新增一条注册 + 在对应数据目录下放置危险区 GPKG 即可，
引擎与模板零改动（灾种可插拔）。
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

_log = logging.getLogger("disaster_registry")

# 危险区数据目录（与 scripts/gen_multi_disaster_data.py 输出对齐）
_DISASTER_DATA_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                 "temp", "multi_disaster")
)

# 成都正式数据目录（Solo 批复「中国数据正式跑通」；条目级 data_dir 覆盖默认目录）
_CHENGDU_DATA_DIR = r"D:\桌面\项目测试数据\中国\成都"

# 模板实例目录
_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")

# ── 多灾种注册表 ──────────────────────────────────────────────
DISASTER_REGISTRY: Dict[str, Dict[str, Any]] = {
    "earthquake": {
        "disaster_id": "earthquake",
        "names": {"zh": "地震", "ja": "地震", "en": "Earthquake"},
        "risk_zone_layer": "震度分布.gpkg",
        "template": "risk_zone_coverage",
        "triggers": {
            "zh": ["地震", "震度", "震度分布", "地震覆盖"],
            "ja": ["地震", "震度", "震度分布"],
            "en": ["earthquake", "seismic", "intensity"],
        },
        "description": "地震危险区 = 震度分布面图层（如震度6弱区域）",
    },
    "flood": {
        "disaster_id": "flood",
        "names": {"zh": "洪涝", "ja": "洪水", "en": "Flood"},
        "risk_zone_layer": "淹没区.gpkg",
        "template": "risk_zone_coverage",
        "triggers": {
            "zh": ["洪涝", "洪水", "淹没", "淹没区", "洪涝覆盖"],
            "ja": ["洪水", "浸水", "浸水区域"],
            "en": ["flood", "inundation", "submerged"],
        },
        "description": "洪涝危险区 = 淹没区面图层（水深字段 depth）",
    },
    "landslide": {
        "disaster_id": "landslide",
        "names": {"zh": "滑坡", "ja": "地すべり", "en": "Landslide"},
        "risk_zone_layer": "滑坡风险区.gpkg",
        "template": "risk_zone_coverage",
        "triggers": {
            "zh": ["滑坡", "滑坡风险", "滑坡风险区", "滑坡覆盖"],
            "ja": ["地すべり", "がけ崩れ"],
            "en": ["landslide", "landslide risk"],
        },
        "description": "滑坡危险区 = 滑坡风险区面图层（风险等级字段 risk_level）",
    },
    "wildfire": {
        "disaster_id": "wildfire",
        "names": {"zh": "火灾", "ja": "火災", "en": "Wildfire"},
        "risk_zone_layer": "火灾风险区.gpkg",
        "template": "risk_zone_coverage",
        "triggers": {
            "zh": ["火灾", "火災", "火灾风险", "火灾风险区", "火灾覆盖"],
            "ja": ["火災", "火災リスク"],
            "en": ["wildfire", "fire", "fire risk"],
        },
        "description": "火灾危险区 = 火灾风险区面图层（风险等级字段 risk_level）",
    },
    "chengdu": {
        "disaster_id": "chengdu",
        "names": {"zh": "成都", "ja": "成都", "en": "Chengdu"},
        "country": "CN",
        "risk_zone_layer": "行政区_3857.gpkg",
        "data_dir": _CHENGDU_DATA_DIR,
        "template": "risk_zone_coverage",
        "triggers": {
            "zh": ["成都", "成都覆盖", "成都风险评估"],
            "ja": ["成都"],
            "en": ["chengdu", "chengdu coverage"],
        },
        "description": "成都场景：危险区边界 = 成都市行政区（country=CN 示例条目，Solo 批复正式数据跑通）",
    },
}

# 初始注册灾种顺序（结果表 / UI 下拉展示顺序）
DEFAULT_DISASTER_ORDER: List[str] = ["earthquake", "flood", "landslide", "wildfire"]


# ── 查询函数 ──────────────────────────────────────────────────

def list_disasters() -> List[Dict[str, Any]]:
    """按默认顺序返回全部灾种注册信息。"""
    return [DISASTER_REGISTRY[did] for did in DEFAULT_DISASTER_ORDER
            if did in DISASTER_REGISTRY]


def get_disaster(disaster_id: str) -> Optional[Dict[str, Any]]:
    """按 disaster_id 获取注册信息；未注册返回 None。"""
    return DISASTER_REGISTRY.get(disaster_id)


def get_disaster_name(disaster_id: str, lang: str = "zh") -> str:
    """获取灾种三语名称，缺省回落中文。"""
    info = DISASTER_REGISTRY.get(disaster_id)
    if not info:
        return disaster_id
    return info["names"].get(lang, info["names"].get("zh", disaster_id))


def get_risk_zone_layer(disaster_id: str) -> str:
    """获取灾种危险区图层文件名（相对 temp/multi_disaster/）。"""
    info = DISASTER_REGISTRY.get(disaster_id)
    return info["risk_zone_layer"] if info else ""


def get_risk_zone_path(disaster_id: str) -> str:
    """获取灾种危险区图层绝对路径。

    条目含 data_dir 字段时使用条目级数据目录（如成都 CN 场景），
    否则使用默认危险区数据目录（temp/multi_disaster/）。
    """
    info = DISASTER_REGISTRY.get(disaster_id)
    if not info:
        return ""
    layer = info.get("risk_zone_layer", "")
    if not layer:
        return ""
    data_dir = info.get("data_dir") or _DISASTER_DATA_DIR
    return os.path.join(data_dir, layer)


def get_template_name(disaster_id: str) -> str:
    """获取灾种使用的模板实例名（不含 .json）。"""
    info = DISASTER_REGISTRY.get(disaster_id)
    return info["template"] if info else ""


def get_template_path(disaster_id: str) -> str:
    """获取灾种使用的模板实例绝对路径。"""
    tpl = get_template_name(disaster_id)
    if not tpl:
        return ""
    return os.path.join(_TEMPLATES_DIR, f"{tpl}.json")


def find_disaster_by_text(user_text: str, lang: str = "zh") -> Optional[Dict[str, Any]]:
    """按触发词在用户文本中匹配灾种（lang 为 zh/ja/en）。

    命中多条时返回匹配触发词最多的灾种；无命中返回 None。
    供片B 指令映射 / UI 下拉扩展使用。
    """
    import re
    best, best_score = None, 0
    for info in list_disasters():
        score = 0
        for trigger in info.get("triggers", {}).get(lang, []):
            try:
                if re.search(trigger, user_text):
                    score += 1
            except re.error:
                if trigger in user_text:
                    score += 1
        if score > best_score:
            best, best_score = info, score
    return best if best_score >= 1 else None


if __name__ == "__main__":
    # 自检：打印注册表
    print(f"危险区数据目录: {_DISASTER_DATA_DIR}")
    print(f"模板实例目录 : {_TEMPLATES_DIR}")
    print("")
    print(f"{'disaster_id':<12}{'名称(zh)':<8}{'危险区图层':<16}{'模板实例'}")
    print("-" * 60)
    for info in list_disasters():
        print(f"{info['disaster_id']:<12}{info['names']['zh']:<8}"
              f"{info['risk_zone_layer']:<16}{info['template']}")
    print("")
    print("REGISTRY_SELFTEST_DONE")
