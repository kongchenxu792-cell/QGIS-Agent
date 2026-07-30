"""
Tier0 闭环 A：震度影响区态势图（离线固定模板）

输入：
    intensity_zone  — 震度区域多边形图层 (路径或已加载图层)
    poi             — 兴趣点图层 (路径或已加载图层)
    admin           — 行政区划图层 (路径或已加载图层，可选)

输出（统一结果契约）：
    layers[]  — 叠加分析结果图层
    files[]   — 态势图 PNG
    messages[]— 执行摘要
    stats{}   — count_by_zone / total_poi / affected_poi

离线策略：纯 QGIS processing 框架调用，零外部 API 依赖。
"""

import os
import datetime
from typing import Any, Dict, List, Optional

# ================================================================
# 默认参数表（Tier0 离线已固化）
# ================================================================

_DEFAULTS = {
    "intensity_zone": "",    # 必须由用户或 Tier1 参数解析填充
    "poi": "",               # 同上
    "admin": "",             # 可选
    "join_predicate": 0,     # intersects (QGIS 处理框架默认)
    "join_type": 1,          # one-to-many
    "output_prefix": "eq_intensity",
    "export_format": "png",
    "export_dpi": 150,
}


# ================================================================
# 核心模板
# ================================================================

def run_loop_a(
    intensity_zone: str = "",
    poi: str = "",
    admin: str = "",
    output_dir: str = "",
    **kwargs,
) -> Dict[str, Any]:
    """
    执行 Tier0 闭环 A 模板。所有参数包含默认值，离线可直接调用。

    Parameters
    ----------
    intensity_zone : str
        震度区图层路径 (.shp/.gpkg) 或已加载图层名。
    poi : str
        兴趣点图层路径 (.shp/.gpkg/.csv) 或已加载图层名。
    admin : str
        行政区划图层路径（可选，用于裁剪和美化）。
    output_dir : str
        输出目录，默认 user_data/exports/shapefiles/。
    **kwargs
        覆盖 _DEFAULTS 中的任意参数。

    Returns
    -------
    dict
        {layers, files, messages, stats} 统一结果契约。
    """
    from qgis.core import (
        QgsProject, QgsVectorLayer, QgsMapSettings,
        QgsMapRendererCustomPainterJob, QgsLayerTree,
        QgsRectangle, QgsCoordinateReferenceSystem,
    )
    from qgis import processing
    import processing as qgis_processing

    # ── 参数解析 ──
    params = dict(_DEFAULTS)
    params.update(kwargs)
    iz = intensity_zone or params["intensity_zone"]
    p = poi or params["poi"]
    adm = admin or params.get("admin", "")

    # ── 输出目录 ──
    from src.core.output_persistence import generate_output_path
    if not output_dir:
        output_dir = os.path.dirname(
            generate_output_path(params["output_prefix"], "_joined")
        )
    os.makedirs(output_dir, exist_ok=True)

    layers = []
    files = []
    messages = []
    stats = {}

    # ── 0. 输入验证 ──
    if not iz or not p:
        messages.append({
            "level": "error",
            "content": "闭环A缺少必要输入：intensity_zone 和 poi 均不可为空。",
        })
        return {"layers": layers, "files": files, "messages": messages, "stats": stats}

    iz_layer, iz_is_file = _resolve_layer(iz, "震度区")
    poi_layer, poi_is_file = _resolve_layer(p, "兴趣点")
    if not iz_layer or not poi_layer:
        msg = "无法加载输入图层。"
        if not iz_layer:
            msg += f" intensity_zone='{iz}' 无效。"
        if not poi_layer:
            msg += f" poi='{p}' 无效。"
        messages.append({"level": "error", "content": msg})
        return {"layers": layers, "files": files, "messages": messages, "stats": stats}

    # ── 1. 确保 CRS 一致 ──
    iz_crs = iz_layer.crs()
    if poi_layer.crs() != iz_crs:
        poi_reproj_path = os.path.join(output_dir, "_poi_reproj.shp")
        reproj_result = qgis_processing.run(
            "native:reprojectlayer",
            {
                "INPUT": poi_layer,
                "TARGET_CRS": iz_crs,
                "OUTPUT": poi_reproj_path,
            },
        )
        poi_layer = QgsVectorLayer(reproj_result["OUTPUT"], "POI_reproj", "ogr")
        if not poi_layer.isValid():
            messages.append({"level": "error", "content": "POI 重投影失败。"})
            return {"layers": layers, "files": files, "messages": messages, "stats": stats}

    # ── 2. 空间连接：POI ↔ 震度区 ──
    joined_path = os.path.join(output_dir, f"{params['output_prefix']}_joined.shp")
    join_result = qgis_processing.run(
        "native:joinattributesbylocation",
        {
            "INPUT": poi_layer,
            "JOIN": iz_layer,
            "PREDICATE": [params["join_predicate"]],
            "JOIN_FIELDS": [],
            "METHOD": params["join_type"],
            "DISCARD_NONMATCHING": False,
            "PREFIX": "",
            "OUTPUT": joined_path,
        },
    )
    joined_layer = QgsVectorLayer(join_result["OUTPUT"], "震度区_POI叠加", "ogr")
    if joined_layer.isValid():
        QgsProject.instance().addMapLayer(joined_layer)
        layers.append({
            "layer_id": joined_layer.id(),
            "layer_name": joined_layer.name(),
            "layer_type": "vector",
            "source_path": joined_layer.source(),
            "feature_count": joined_layer.featureCount(),
            "geometry_type": "Point",
        })

    # ── 3. 统计：每个震度区的 POI 数量 ──
    #    从 joined_layer 中按 zone 字段分组计数
    count_by_zone = {}
    try:
        iz_fields = [f.name() for f in iz_layer.fields()]
        zone_field = None
        for candidate in ("zone", "intensity", "震度", "name", "Name", "NAME"):
            if candidate in iz_fields:
                zone_field = candidate
                break
        if zone_field:
            zone_idx = joined_layer.fields().indexOf(zone_field)
            if zone_idx >= 0:
                for feat in joined_layer.getFeatures():
                    zone_name = str(feat[zone_idx]) if feat[zone_idx] is not None else "未知"
                    count_by_zone[zone_name] = count_by_zone.get(zone_name, 0) + 1
    except Exception:
        pass

    total_poi = joined_layer.featureCount() if joined_layer.isValid() else 0
    affected_poi = sum(count_by_zone.values())

    stats["count_by_zone"] = count_by_zone
    stats["total_poi"] = total_poi
    stats["affected_poi"] = affected_poi

    # ── 4. 导出态势图 ──
    export_path = _export_map(
        layers_to_render=[joined_layer, iz_layer],
        title="震度影响区态势图",
        output_dir=output_dir,
        prefix=params["output_prefix"],
        dpi=params["export_dpi"],
    )
    if export_path:
        files.append({
            "file_path": export_path,
            "file_type": "image/png",
            "description": "震度影响区态势图",
        })

    # ── 5. messages ──
    zone_str = "、".join(
        f"{k}({v}个POI)" for k, v in sorted(count_by_zone.items())
    ) if count_by_zone else "无分区数据"
    messages.append({
        "level": "info",
        "content": f"闭环A完成：{total_poi}个POI分布在{len(count_by_zone)}个震度区。分布: {zone_str}",
    })

    return {"layers": layers, "files": files, "messages": messages, "stats": stats}


# ================================================================
# 内部工具
# ================================================================

def _resolve_layer(path_or_name: str, label: str):
    """解析图层：先尝试从已加载项目查找，再尝试从磁盘加载。"""
    from qgis.core import QgsProject, QgsVectorLayer
    if not path_or_name:
        return None, False

    # 尝试从已加载图层按名称查找
    for lid, lyr in QgsProject.instance().mapLayers().items():
        if lyr.name() == path_or_name:
            return lyr, False

    # 尝试从磁盘加载
    if os.path.exists(path_or_name):
        if path_or_name.lower().endswith(".csv"):
            layer = QgsVectorLayer(f"file:///{path_or_name}?delimiter=,", label, "delimitedtext")
        else:
            layer = QgsVectorLayer(path_or_name, label, "ogr")
        if layer.isValid():
            return layer, True
    return None, False


def _export_map(
    layers_to_render: list,
    title: str,
    output_dir: str,
    prefix: str = "map",
    dpi: int = 150,
) -> str:
    """用 QgsMapSettings 渲染指定图层到 PNG。"""
    try:
        from qgis.core import (
            QgsMapSettings, QgsMapRendererCustomPainterJob,
            QgsRectangle, QgsCoordinateReferenceSystem,
        )
        from PyQt5.QtGui import QImage, QPainter
        from PyQt5.QtCore import Qt, QSize
    except ImportError:
        return ""

    if not layers_to_render:
        return ""

    # 计算图层组合的 extent
    extent = None
    for lyr in layers_to_render:
        if lyr is None or not lyr.isValid():
            continue
        ext = lyr.extent()
        if ext.isEmpty():
            continue
        if extent is None:
            extent = QgsRectangle(ext)
        else:
            extent.combineExtentWith(ext)

    if extent is None or extent.isEmpty():
        return ""

    # 构建 map settings
    settings = QgsMapSettings()
    settings.setLayers([lyr for lyr in layers_to_render if lyr and lyr.isValid()])
    settings.setExtent(extent)
    settings.setOutputSize(QSize(1200, 900))
    settings.setOutputDpi(dpi)
    if layers_to_render[0] and layers_to_render[0].isValid():
        settings.setDestinationCrs(layers_to_render[0].crs())
    settings.setBackgroundColor(Qt.white)

    # 渲染
    image = QImage(1200, 900, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.white)
    painter = QPainter(image)
    render_job = QgsMapRendererCustomPainterJob(settings, painter)
    render_job.start()
    render_job.waitForFinished()
    painter.end()

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = os.path.join(output_dir, f"{prefix}_{timestamp}.png")
    image.save(output_path, "PNG")
    return output_path
