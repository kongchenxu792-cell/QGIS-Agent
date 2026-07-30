"""矢量图层与栅格图层的拖放加载辅助工具。

策略对齐 QGIS 原生拖放：不依赖扩展名白名单，直接委托 GDAL/OGR 驱动尝试打开。
QGIS 能打开的数据格式，AIQGIS 画布同样支持。
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, List, Tuple

from osgeo import gdal
from qgis.core import QgsProject, QgsRasterLayer, QgsVectorLayer


#: QGIS 项目文件扩展名集合（需特殊处理：调用 QgsProject.read）。
PROJECT_EXTENSIONS = {".qgz", ".qgs"}

#: 表格数据文件扩展名集合（Excel/CSV — 非 GIS 图层，走独立加载路径）。
TABLE_EXTENSIONS = {".xlsx", ".xls", ".csv"}


def is_supported_path(file_path: str) -> bool:
    """判断给定文件路径是否可尝试加载。

    不再依赖硬编码扩展名白名单。只要文件存在且非系统隐藏文件，
    即可交给 GDAL/OGR 尝试打开。

    参数
    ----
    file_path : str
        待检测的文件路径。

    返回
    ----
    bool
        若文件存在且非系统文件则返回 ``True``。
    """

    path = Path(file_path)
    if not path.exists():
        return False
    # 跳过明显的非数据文件（系统文件、快捷方式等）
    if path.suffix.lower() in {".lnk", ".exe", ".dll", ".sys", ".bat", ".cmd"}:
        return False
    return True


def is_table_path(file_path: str) -> bool:
    """判断给定文件路径是否为表格数据文件（Excel/CSV）。

    参数
    ----
    file_path : str
        待检测的文件路径。

    返回
    ----
    bool
        若文件扩展名属于表格格式则返回 ``True``。
    """

    return Path(file_path).suffix.lower() in TABLE_EXTENSIONS


def create_layer_from_path(file_path: str):
    """从本地文件路径创建 QGIS 图层对象。

    策略与 QGIS 原生拖放对齐：先尝试 OGR 矢量驱动，失败则回退 GDAL 栅格驱动。
    不依赖扩展名白名单 — 让 GDAL/OGR 自己判断能否识别该格式。

    参数
    ----
    file_path : str
        本地 GIS 数据文件的绝对路径。

    返回
    ----
    QgsVectorLayer 或 QgsRasterLayer
        创建并验证后的图层对象。

    异常
    ----
    ValueError
        若 OGR 和 GDAL 均无法识别该文件时抛出。
    """

    path = Path(file_path)
    layer_name = path.stem
    path_str = str(path)

    # 启用 SHAPE_RESTORE_SHX：当 .shx 文件缺失时，GDAL 可从 .shp 自动恢复
    gdal.SetConfigOption("SHAPE_RESTORE_SHX", "YES")

    # 先尝试 OGR 矢量驱动（覆盖 SHP/GPKG/GeoJSON/KML/GML/TAB/MIF/DXF/VRT/...）
    vector_layer = QgsVectorLayer(path_str, layer_name, "ogr")
    if vector_layer.isValid():
        return vector_layer

    # 回退 GDAL 栅格驱动（覆盖 GeoTIFF/IMG/JPEG/PNG/GDAL VRT/...）
    raster_layer = QgsRasterLayer(path_str, layer_name)
    if raster_layer.isValid():
        return raster_layer

    # 收集 GDAL/OGR 实际错误信息
    detail_parts = []
    vec_err = vector_layer.dataProvider().error().message() if vector_layer.dataProvider() else ""
    if vec_err:
        detail_parts.append(f"OGR: {vec_err}")
    ras_err = raster_layer.dataProvider().error().message() if raster_layer.dataProvider() else ""
    if ras_err:
        detail_parts.append(f"GDAL: {ras_err}")
    detail = "；".join(detail_parts) if detail_parts else "OGR 和 GDAL 均无法识别"
    raise ValueError(f"无法加载图层文件：{path}（{detail}）")


def load_layers_from_paths(file_paths: Iterable[str]) -> Tuple[List[object], List[str]]:
    """批量加载文件路径并将其注册到当前 QGIS 项目中。

    对于矢量/栅格图层文件调用 create_layer_from_path 添加到当前项目；
    对于 .qgz/.qgs 项目文件调用 QgsProject.read()，由 QGIS 原生引擎处理。

    参数
    ----
    file_paths : Iterable[str]
        待加载的 GIS 文件路径可迭代对象。

    返回
    ----
    Tuple[List[object], List[str]]
        二元组：``(已成功加载的图层列表, 错误信息列表)``。
    """

    loaded_layers = []
    errors: List[str] = []

    for file_path in file_paths:
        try:
            suffix = Path(file_path).suffix.lower()

            if suffix in PROJECT_EXTENSIONS:
                # 交给 QGIS 原生读取引擎，项目内的图层由 QGIS 自己管理
                QgsProject.instance().read(file_path)
                for layer in QgsProject.instance().mapLayers().values():
                    loaded_layers.append(layer)
                continue

            if not is_supported_path(file_path):
                errors.append(f"已跳过不支持的文件：{file_path}")
                continue

            layer = create_layer_from_path(file_path)
            QgsProject.instance().addMapLayer(layer)
            loaded_layers.append(layer)
        except Exception as exc:  # pragma: no cover - 依赖本地数据质量
            errors.append(f"{file_path}：{exc}")

    return loaded_layers, errors
