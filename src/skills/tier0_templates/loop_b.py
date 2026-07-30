"""
Tier0 闭环 B：避难所覆盖盲区分析（离线固定模板）

输入：
    shelter_points  — 避难所点图层 (路径或已加载图层)
    radius_m        — 服务半径（米），默认 500
    admin_boundary  — 行政区划多边形图层 (路径或已加载图层，可选)

输出（统一结果契约）：
    layers[]  — coverage / uncovered / shelter_buffer 图层
    files[]   — 覆盖盲区图 PNG
    messages[]— 执行摘要
    stats{}   — coverage_rate / uncovered_area_sqkm / shelter_count / radius_m

离线策略：纯 QGIS processing 框架调用，零外部 API 依赖。
"""

import os
import datetime
from typing import Any, Dict, List

# ================================================================
# 默认参数表（Tier0 离线已固化）
# ================================================================

_DEFAULTS = {
    "shelter": "",           # 必须由用户或 Tier1 参数解析填充
    "radius_m": 500,         # 服务半径（米）
    "admin_boundary": "",    # 可选
    "output_prefix": "eq_shelter",
    "export_format": "png",
    "export_dpi": 150,
    "buffer_segments": 36,   # 圆形缓冲区分段数
    "merge_buffers": True,   # 合并所有缓冲区为一个覆盖多边形
}


# ================================================================
# 核心模板
# ================================================================

def run_loop_b(
    shelter: str = "",
    radius_m: int = 500,
    admin_boundary: str = "",
    output_dir: str = "",
    **kwargs,
) -> Dict[str, Any]:
    """
    执行 Tier0 闭环 B 模板。所有参数包含默认值，离线可直接调用。

    Parameters
    ----------
    shelter : str
        避难所点图层路径 (.shp/.gpkg) 或已加载图层名。
    radius_m : int
        服务半径（米），默认 500。
    admin_boundary : str
        行政区划图层路径（可选，用于裁剪和计算未覆盖面积）。
    output_dir : str
        输出目录，默认 user_data/exports/shapefiles/。
    **kwargs
        覆盖 _DEFAULTS 中的任意参数。

    Returns
    -------
    dict
        {layers, files, messages, stats} 统一结果契约。
    """
    from qgis.core import QgsProject, QgsVectorLayer, QgsDistanceArea, QgsUnitTypes
    from qgis import processing
    import processing as qgis_processing

    # ── 参数解析 ──
    params = dict(_DEFAULTS)
    params.update(kwargs)
    sh = shelter or params["shelter"]
    rm = radius_m or params["radius_m"]
    adm = admin_boundary or params.get("admin_boundary", "")

    # ── 输出目录 ──
    from src.core.output_persistence import generate_output_path
    if not output_dir:
        output_dir = os.path.dirname(
            generate_output_path(params["output_prefix"], "_coverage")
        )
    os.makedirs(output_dir, exist_ok=True)

    layers = []
    files = []
    messages = []
    stats = {}

    # ── 0. 输入验证 ──
    if not sh:
        messages.append({
            "level": "error",
            "content": "闭环B缺少必要输入：shelter 不可为空。",
        })
        return {"layers": layers, "files": files, "messages": messages, "stats": stats}

    shelter_layer, _ = _resolve_layer(sh, "避难所")
    if not shelter_layer:
        messages.append({"level": "error", "content": f"无法加载避难所图层: '{sh}'"})
        return {"layers": layers, "files": files, "messages": messages, "stats": stats}

    shelter_count = shelter_layer.featureCount()
    stats["shelter_count"] = shelter_count
    stats["radius_m"] = rm

    # ── 1. 缓冲区 ──
    buffer_path = os.path.join(output_dir, f"{params['output_prefix']}_buffer.shp")
    buffer_result = qgis_processing.run(
        "native:buffer",
        {
            "INPUT": shelter_layer,
            "DISTANCE": rm,
            "SEGMENTS": params["buffer_segments"],
            "DISSOLVE": params["merge_buffers"],
            "END_CAP_STYLE": 1,  # Round
            "JOIN_STYLE": 1,     # Round
            "MITER_LIMIT": 2,
            "OUTPUT": buffer_path,
        },
    )
    buffer_layer = QgsVectorLayer(buffer_result["OUTPUT"], "避难所覆盖区", "ogr")
    if buffer_layer.isValid():
        QgsProject.instance().addMapLayer(buffer_layer)
        layers.append({
            "layer_id": buffer_layer.id(),
            "layer_name": buffer_layer.name(),
            "layer_type": "vector",
            "source_path": buffer_layer.source(),
            "feature_count": buffer_layer.featureCount(),
            "geometry_type": "Polygon",
        })
        coverage_layer = buffer_layer
    else:
        messages.append({"level": "error", "content": "缓冲区生成失败。"})
        return {"layers": layers, "files": files, "messages": messages, "stats": stats}

    # ── 2. 如果提供了行政边界，裁剪并计算未覆盖区域 ──
    uncovered_layer = None
    if adm:
        admin_layer, _ = _resolve_layer(adm, "行政区划")
        if admin_layer and admin_layer.isValid():
            # 2a. 裁剪覆盖区到行政边界
            clipped_path = os.path.join(output_dir, f"{params['output_prefix']}_coverage.shp")
            clip_result = qgis_processing.run(
                "native:clip",
                {
                    "INPUT": coverage_layer,
                    "OVERLAY": admin_layer,
                    "OUTPUT": clipped_path,
                },
            )
            clipped_layer = QgsVectorLayer(clip_result["OUTPUT"], "有效覆盖区", "ogr")
            if clipped_layer.isValid():
                coverage_layer = clipped_layer
                # 更新 layers 列表中的覆盖图层
                if layers:
                    layers[0] = {
                        "layer_id": clipped_layer.id(),
                        "layer_name": clipped_layer.name(),
                        "layer_type": "vector",
                        "source_path": clipped_layer.source(),
                        "feature_count": clipped_layer.featureCount(),
                        "geometry_type": "Polygon",
                    }

            # 2b. 差集求盲区
            diff_path = os.path.join(output_dir, f"{params['output_prefix']}_uncovered.shp")
            diff_result = qgis_processing.run(
                "native:difference",
                {
                    "INPUT": admin_layer,
                    "OVERLAY": coverage_layer,
                    "OUTPUT": diff_path,
                },
            )
            uncovered_layer = QgsVectorLayer(diff_result["OUTPUT"], "覆盖盲区", "ogr")
            if uncovered_layer.isValid():
                QgsProject.instance().addMapLayer(uncovered_layer)
                layers.append({
                    "layer_id": uncovered_layer.id(),
                    "layer_name": uncovered_layer.name(),
                    "layer_type": "vector",
                    "source_path": uncovered_layer.source(),
                    "feature_count": uncovered_layer.featureCount(),
                    "geometry_type": "Polygon",
                })

    # ── 3. 面积统计 ──
    da = QgsDistanceArea()
    da.setEllipsoid("WGS84")

    total_area_sqkm = 0.0
    covered_area_sqkm = 0.0
    uncovered_area_sqkm = 0.0

    try:
        # 行政边界总面积
        if adm and admin_layer and admin_layer.isValid():
            for feat in admin_layer.getFeatures():
                geom = feat.geometry()
                if geom:
                    area = da.measureArea(geom)
                    total_area_sqkm += da.convertAreaMeasurement(area, QgsUnitTypes.AreaSquareKilometers)
        else:
            # 无行政边界时用缓冲区范围
            ext = coverage_layer.extent()
            if not ext.isEmpty():
                from qgis.core import QgsGeometry
                total_area_sqkm = ext.width() * ext.height() / 1e6  # 粗略估算

        # 覆盖面积
        for feat in coverage_layer.getFeatures():
            geom = feat.geometry()
            if geom:
                area = da.measureArea(geom)
                covered_area_sqkm += da.convertAreaMeasurement(area, QgsUnitTypes.AreaSquareKilometers)

        # 盲区面积
        if uncovered_layer and uncovered_layer.isValid():
            for feat in uncovered_layer.getFeatures():
                geom = feat.geometry()
                if geom:
                    area = da.measureArea(geom)
                    uncovered_area_sqkm += da.convertAreaMeasurement(area, QgsUnitTypes.AreaSquareKilometers)
    except Exception:
        pass

    coverage_rate = 0.0
    if total_area_sqkm > 0 and covered_area_sqkm > 0:
        coverage_rate = round(covered_area_sqkm / max(total_area_sqkm, covered_area_sqkm + uncovered_area_sqkm), 4)

    stats["total_area_sqkm"] = round(total_area_sqkm, 4)
    stats["covered_area_sqkm"] = round(covered_area_sqkm, 4)
    stats["uncovered_area_sqkm"] = round(uncovered_area_sqkm, 4)
    stats["coverage_rate"] = coverage_rate

    # ── 4. 导出覆盖盲区图 ──
    render_layers = [lyr for lyr in [coverage_layer, uncovered_layer, shelter_layer] if lyr and lyr.isValid()]
    export_path = _export_map(
        layers_to_render=render_layers,
        title="避难所覆盖盲区分析",
        output_dir=output_dir,
        prefix=params["output_prefix"],
        dpi=params["export_dpi"],
    )
    if export_path:
        files.append({
            "file_path": export_path,
            "file_type": "image/png",
            "description": "避难所覆盖盲区分析图",
        })

    # ── 5. messages ──
    pct = f"{coverage_rate * 100:.1f}%"
    desc = (
        f"闭环B完成：{shelter_count}个避难所，半径{rm}m，"
        f"覆盖率{pct}，盲区{uncovered_area_sqkm:.2f}km²"
    )
    messages.append({"level": "info", "content": desc})

    return {"layers": layers, "files": files, "messages": messages, "stats": stats}


# ================================================================
# 内部工具
# ================================================================

def _resolve_layer(path_or_name: str, label: str):
    """解析图层：先尝试从已加载项目查找，再尝试从磁盘加载。"""
    from qgis.core import QgsProject, QgsVectorLayer
    if not path_or_name:
        return None, False

    for lid, lyr in QgsProject.instance().mapLayers().items():
        if lyr.name() == path_or_name:
            return lyr, False

    if os.path.exists(path_or_name):
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
            QgsRectangle,
        )
        from PyQt5.QtGui import QImage, QPainter
        from PyQt5.QtCore import Qt, QSize
    except ImportError:
        return ""

    if not layers_to_render:
        return ""

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

    settings = QgsMapSettings()
    settings.setLayers([lyr for lyr in layers_to_render if lyr and lyr.isValid()])
    settings.setExtent(extent)
    settings.setOutputSize(QSize(1200, 900))
    settings.setOutputDpi(dpi)
    if layers_to_render[0] and layers_to_render[0].isValid():
        settings.setDestinationCrs(layers_to_render[0].crs())
    settings.setBackgroundColor(Qt.white)

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
