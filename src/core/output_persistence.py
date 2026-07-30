"""
持久化输出工具 — 为所有空间分析技能提供统一的磁盘持久化路径生成。

所有矢量生成技能（clip、centroid、dissolve 等）通过此模块生成
Shapefile 输出路径，确保数据在应用重启后不会丢失。
"""

import os
from datetime import datetime
from typing import Optional


# 输出根目录（相对于项目根目录）
_OUTPUT_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "user_data",
    "exports",
    "shapefiles",
)


def ensure_output_dir() -> str:
    """确保输出目录存在，返回绝对路径。"""
    os.makedirs(_OUTPUT_ROOT, exist_ok=True)
    return _OUTPUT_ROOT


def generate_output_path(
    skill_prefix: str,
    layer_name: str,
    extension: str = ".shp",
) -> str:
    """生成带时间戳的持久化输出路径。

    Parameters
    ----------
    skill_prefix : str
        技能前缀，如 "clip"、"centroid"、"dissolve"。
    layer_name : str
        原始图层名称（用于标识）。
    extension : str
        输出文件扩展名，默认 ".shp"。

    Returns
    -------
    str
        格式：user_data/exports/shapefiles/{skill_prefix}_{timestamp}_{sanitized_name}{extension}
    """
    ensure_output_dir()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # 清理图层名称中的特殊字符
    safe_name = _sanitize_filename(layer_name)

    filename = f"{skill_prefix}_{timestamp}_{safe_name}{extension}"
    return os.path.join(_OUTPUT_ROOT, filename)


def generate_geojson_output_path(
    skill_prefix: str,
    layer_name: str,
) -> str:
    """生成 GeoJSON 格式的输出路径（UTF-8 编码，避免 Shapefile 中文乱码）。"""
    return generate_output_path(skill_prefix, layer_name, extension=".geojson")


def _sanitize_filename(name: str, max_length: int = 80) -> str:
    """清理文件名中的非法字符。

    Windows 文件名非法字符：< > : " / \\ | ? *
    """
    illegal_chars = '<>:"/\\|?*'
    safe = name
    for char in illegal_chars:
        safe = safe.replace(char, "_")
    # 移除多余空格
    safe = " ".join(safe.split())
    # 截断过长名称
    if len(safe) > max_length:
        safe = safe[:max_length]
    return safe


def get_output_root() -> str:
    """获取输出根目录路径。"""
    return _OUTPUT_ROOT
