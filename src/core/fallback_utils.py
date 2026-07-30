"""
确定性空间算法降级库 — 当 QGIS 原生水文算子缺失时的硬编码安全替代。

架构原则：
- 所有算法函数自带完整的边缘填充守卫，严禁大模型现场盲写 Numpy 滑窗
- 输入/输出均为标准 GeoTIFF 文件路径，内部处理 NaN、边界、格式对齐
- 通过 System Prompt 的「核心降级调用契约」引导 AI 直接 import 调用
"""

import logging
import os

_log = logging.getLogger(__name__)
import time
import heapq
import numpy as np
from osgeo import gdal


# ── 安全输出写入（处理文件被 QGIS 锁定的情况） ──

def _safe_create_output(output_path, cols, rows, bands, gdal_type, geotransform, projection):
    """创建 GeoTIFF 输出文件。若目标被锁定则自动生成带时间戳的替代文件名。"""
    driver = gdal.GetDriverByName('GTiff')

    # 先尝试常规删除
    if os.path.exists(output_path):
        try:
            driver.Delete(output_path)
        except Exception:
            try:
                os.remove(output_path)
            except Exception:
                pass

    # 尝试创建
    ds = None
    actual_path = output_path
    try:
        ds = driver.Create(output_path, cols, rows, bands, gdal_type)
    except RuntimeError as e:
        if "Permission denied" in str(e) or "Deleting" in str(e):
            # 文件被锁定，换名
            base, ext = os.path.splitext(output_path)
            actual_path = f"{base}_{int(time.time() * 1000)}{ext}"
            _log.info(f"[safe_write] 目标锁定，改用: {actual_path}")
            ds = driver.Create(actual_path, cols, rows, bands, gdal_type)
        else:
            raise

    if ds is None:
        raise RuntimeError(f"无法创建输出文件: {output_path}")

    ds.SetGeoTransform(geotransform)
    ds.SetProjection(projection)
    return ds, actual_path


# ── 通用参数解析（终极容错） ──

def _resolve_paths(args, kwargs, func_name):
    """从任意形参组合中解析输入/输出路径对。"""
    input_path = args[0] if len(args) > 0 else None
    output_path = args[1] if len(args) > 1 else None

    if not input_path:
        input_path = (kwargs.get('input_tif_path') or
                      kwargs.get('dem_path') or
                      kwargs.get('input_raster') or
                      kwargs.get('input_path') or
                      kwargs.get('input_layer') or
                      kwargs.get('in_path') or
                      kwargs.get('source_path') or
                      kwargs.get('input_dem'))

    if not output_path:
        output_path = (kwargs.get('output_tif_path') or
                       kwargs.get('output_path') or
                       kwargs.get('out_path') or
                       kwargs.get('output_raster') or
                       kwargs.get('output_layer') or
                       kwargs.get('dest_path'))

    if not input_path or not output_path:
        raise ValueError(
            f"{func_name} 解析参数失败！"
            f"请提供两个路径参数（输入 DEM → 输出栅格）。"
            f"当前收到: args={args}, kwargs={kwargs}"
        )
    return input_path, output_path


def _write_geotiff(output_path, array, ref_ds, gdal_type=gdal.GDT_Float32):
    """将 numpy 数组写入标准 GeoTIFF，继承参考数据集的地理变换和投影。"""
    rows, cols = array.shape
    out_ds, actual_path = _safe_create_output(
        output_path, cols, rows, 1, gdal_type,
        ref_ds.GetGeoTransform(), ref_ds.GetProjection(),
    )
    out_ds.GetRasterBand(1).WriteArray(array)
    out_ds.FlushCache()
    out_ds = None
    return actual_path


# ── 确定性水文算法 ──

def safe_fill_sinks(*args, **kwargs):
    """
    确定性优先洪水洼地填充降级算法（Priority-Flood, Barnes 2014）。

    不依赖任何 QGIS processing 算子，纯 numpy + heapq 实现。
    对所有内部洼地进行确定性填充，确保水可以无障碍流向边缘。

    调用方式（全部支持）：
        safe_fill_sinks(in_tif, out_tif)
        safe_fill_sinks(dem_path=..., output_path=...)
        safe_fill_sinks(input_raster=..., out_path=...)
        ...
    """
    input_path, output_path = _resolve_paths(args, kwargs, 'safe_fill_sinks')

    ds = gdal.Open(input_path)
    if ds is None:
        raise FileNotFoundError(f"无法打开输入栅格: {input_path}")
    geotransform = ds.GetGeoTransform()
    projection = ds.GetProjection()
    band = ds.GetRasterBand(1)
    dem = band.ReadAsArray().astype(np.float32)
    rows, cols = dem.shape
    no_data = band.GetNoDataValue()
    ds = None  # 立即释放，防止后续写入时文件锁冲突

    # 创建 filled DEM 副本
    filled = dem.copy()

    # 标记已处理像素
    closed = np.zeros((rows, cols), dtype=bool)

    # 最小堆优先队列
    pq = []

    # ── 初始化：将所有边缘像素推入优先队列 ──
    for r in range(rows):
        for c in [0, cols - 1]:
            if not closed[r, c]:
                if no_data is not None and filled[r, c] == no_data:
                    continue
                if np.isnan(filled[r, c]):
                    continue
                heapq.heappush(pq, (filled[r, c], r, c))
                closed[r, c] = True
    for c in range(cols):
        for r in [0, rows - 1]:
            if not closed[r, c]:
                if no_data is not None and filled[r, c] == no_data:
                    continue
                if np.isnan(filled[r, c]):
                    continue
                heapq.heappush(pq, (filled[r, c], r, c))
                closed[r, c] = True

    # ── 优先洪水主体：从边缘向内扩散 ──
    neighbors = [(-1, 0), (1, 0), (0, -1), (0, 1),
                 (-1, -1), (-1, 1), (1, -1), (1, 1)]

    while pq:
        elev, r, c = heapq.heappop(pq)
        for dr, dc in neighbors:
            nr, nc = r + dr, c + dc
            if nr < 0 or nr >= rows or nc < 0 or nc >= cols:
                continue
            if closed[nr, nc]:
                continue
            if no_data is not None and filled[nr, nc] == no_data:
                continue
            if np.isnan(filled[nr, nc]):
                continue

            # 核心填充逻辑：如果邻居低于当前水位，抬升到当前水位
            if filled[nr, nc] < elev:
                filled[nr, nc] = elev
            heapq.heappush(pq, (filled[nr, nc], nr, nc))
            closed[nr, nc] = True

    # 写出
    out_ds, actual_path = _safe_create_output(
        output_path, cols, rows, 1, gdal.GDT_Float32,
        geotransform, projection,
    )
    out_band = out_ds.GetRasterBand(1)
    if no_data is not None:
        out_band.SetNoDataValue(no_data)
    out_band.WriteArray(filled)
    out_ds.FlushCache()
    out_ds = None

    return actual_path


def safe_d8_flow_direction(*args, **kwargs):
    """
    终极容错版 D8 流向降级算法 — 不管 AI 助手用什么名字传参，都能完美接收。

    支持调用方式：
        safe_d8_flow_direction(in_tif, out_tif)         # 位置参数
        safe_d8_flow_direction(input_tif_path=..., ...)  # 标准关键字
        safe_d8_flow_direction(dem_path=..., ...)       # AI 瞎写的别名
        safe_d8_flow_direction(input_raster=..., ...)   # 更多别名
    """
    input_path, output_path = _resolve_paths(args, kwargs, 'safe_d8_flow_direction')

    ds = gdal.Open(input_path)
    if ds is None:
        raise FileNotFoundError(f"无法打开输入栅格: {input_path}")
    geotransform = ds.GetGeoTransform()
    projection = ds.GetProjection()
    band = ds.GetRasterBand(1)
    dem = band.ReadAsArray().astype(np.float32)
    ds = None  # 立即释放，防止后续写入时文件锁冲突

    padded = np.pad(dem, pad_width=1, mode='edge')
    flow_dir = np.zeros_like(dem, dtype=np.uint8)

    directions = [
        (0, 1, 1), (1, 1, 2), (1, 0, 4), (1, -1, 8),
        (0, -1, 16), (-1, -1, 32), (-1, 0, 64), (-1, 1, 128)
    ]

    rows, cols = dem.shape
    for r in range(rows):
        for c in range(cols):
            pr, pc = r + 1, c + 1
            current_val = padded[pr, pc]
            if np.isnan(current_val):
                continue
            max_slope, best_dir = 0, 0
            for dr, dc, code in directions:
                neighbor_val = padded[pr + dr, pc + dc]
                if np.isnan(neighbor_val):
                    continue
                slope = current_val - neighbor_val
                if slope > max_slope:
                    max_slope = slope
                    best_dir = code
            flow_dir[r, c] = best_dir

    out_ds, actual_path = _safe_create_output(
        output_path, cols, rows, 1, gdal.GDT_Byte,
        geotransform, projection,
    )
    out_ds.GetRasterBand(1).WriteArray(flow_dir)
    out_ds.FlushCache()
    out_ds = None

    return actual_path


# ── D8 方向编码 ↔ 偏移量映射（全局常量，所有下游函数共用）──

_D8_TO_OFFSET = {
    1: (0, 1), 2: (1, 1), 4: (1, 0), 8: (1, -1),
    16: (0, -1), 32: (-1, -1), 64: (-1, 0), 128: (-1, 1)
}

# 反向：给定偏移量→编码，用于计算 indegree
_OFFSET_TO_D8 = {v: k for k, v in _D8_TO_OFFSET.items()}

# 8 邻域偏移（用于 indegree 检测：哪些邻居可能流入当前格点）
_NEIGHBOR_INFLOW_OFFSETS = [
    (0, -1, 1), (-1, -1, 2), (-1, 0, 4), (-1, 1, 8),
    (0, 1, 16), (1, 1, 32), (1, 0, 64), (1, -1, 128)
]


def safe_flow_accumulation(*args, **kwargs):
    """
    确定性 D8 汇流累积降级算法（拓扑排序）。

    输入：D8 流向栅格（safe_d8_flow_direction 的输出）
    输出：汇流累积栅格（每个格点的上游贡献单元数）

    调用方式（全部支持）：
        safe_flow_accumulation(flow_dir_tif, accum_tif)
        safe_flow_accumulation(input_raster=..., output_path=...)
    """
    input_path, output_path = _resolve_paths(args, kwargs, 'safe_flow_accumulation')

    ds = gdal.Open(input_path)
    if ds is None:
        raise FileNotFoundError(f"无法打开输入栅格: {input_path}")
    geotransform = ds.GetGeoTransform()
    projection = ds.GetProjection()
    flow_dir = ds.GetRasterBand(1).ReadAsArray().astype(np.uint8)
    ds = None

    rows, cols = flow_dir.shape
    accum = np.ones((rows, cols), dtype=np.float32)  # 每个格点自身贡献 1
    indegree = np.zeros((rows, cols), dtype=np.int32)

    # ── 第 1 遍：计算每个格点的入度 ──
    for r in range(rows):
        for c in range(cols):
            fd = flow_dir[r, c]
            if fd == 0:
                continue
            dr, dc = _D8_TO_OFFSET.get(fd, (0, 0))
            nr, nc = r + dr, c + dc
            if 0 <= nr < rows and 0 <= nc < cols:
                indegree[nr, nc] += 1

    # ── 第 2 遍：拓扑排序传播累积量 ──
    from collections import deque
    q = deque()
    for r in range(rows):
        for c in range(cols):
            if indegree[r, c] == 0:
                q.append((r, c))

    while q:
        r, c = q.popleft()
        fd = flow_dir[r, c]
        if fd == 0:
            continue
        dr, dc = _D8_TO_OFFSET.get(fd, (0, 0))
        nr, nc = r + dr, c + dc
        if 0 <= nr < rows and 0 <= nc < cols:
            accum[nr, nc] += accum[r, c]
            indegree[nr, nc] -= 1
            if indegree[nr, nc] == 0:
                q.append((nr, nc))

    # 写出
    out_ds, actual_path = _safe_create_output(
        output_path, cols, rows, 1, gdal.GDT_Float32,
        geotransform, projection,
    )
    out_ds.GetRasterBand(1).WriteArray(accum)
    out_ds.FlushCache()
    out_ds = None

    return actual_path


def safe_stream_network(*args, **kwargs):
    """
    确定性河网提取降级算法（汇流累积阈值法）。

    输入：汇流累积栅格（safe_flow_accumulation 的输出）
    输出：二值河网栅格（1=河网格点，0=非河网）

    调用方式（全部支持）：
        safe_stream_network(accum_tif, stream_tif, threshold=100)
        safe_stream_network(input_raster=..., output_path=..., threshold=...)
    """
    input_path = args[0] if len(args) > 0 else None
    output_path = args[1] if len(args) > 1 else None

    if not input_path:
        input_path = (kwargs.get('input_tif_path') or
                      kwargs.get('input_raster') or
                      kwargs.get('input_path') or
                      kwargs.get('accum_path') or
                      kwargs.get('source_path'))
    if not output_path:
        output_path = (kwargs.get('output_tif_path') or
                       kwargs.get('output_path') or
                       kwargs.get('out_path') or
                       kwargs.get('output_raster') or
                       kwargs.get('dest_path'))

    threshold = kwargs.get('threshold', 100)

    if not input_path or not output_path:
        raise ValueError(
            f"safe_stream_network 解析参数失败！"
            f"当前收到: args={args}, kwargs={kwargs}"
        )

    ds = gdal.Open(input_path)
    if ds is None:
        raise FileNotFoundError(f"无法打开输入栅格: {input_path}")
    geotransform = ds.GetGeoTransform()
    projection = ds.GetProjection()
    accum = ds.GetRasterBand(1).ReadAsArray().astype(np.float32)
    ds = None

    stream = (accum >= threshold).astype(np.uint8)

    out_ds, actual_path = _safe_create_output(
        output_path, accum.shape[1], accum.shape[0], 1, gdal.GDT_Byte,
        geotransform, projection,
    )
    out_ds.GetRasterBand(1).WriteArray(stream)
    out_ds.FlushCache()
    out_ds = None

    return actual_path


def safe_basin(*args, **kwargs):
    """
    确定性流域（汇水区）划分降级算法 — 基于 D8 流向的下游追踪 + 记忆化。

    每个像素沿 D8 流向追踪至 DEM 边缘或汇点，共享同一出口的像素归入同一流域。
    不依赖任何 QGIS processing 算子，纯 numpy + gdal 实现。

    调用方式（全部支持）：
        safe_basin(flow_dir_tif, basin_tif)
        safe_basin(input_raster=..., output_path=...)
    """
    input_path, output_path = _resolve_paths(args, kwargs, 'safe_basin')

    ds = gdal.Open(input_path)
    if ds is None:
        raise FileNotFoundError(f"无法打开输入栅格: {input_path}")
    geotransform = ds.GetGeoTransform()
    projection = ds.GetProjection()
    flow_dir = ds.GetRasterBand(1).ReadAsArray().astype(np.uint8)
    ds = None

    rows, cols = flow_dir.shape
    basin = np.full((rows, cols), -1, dtype=np.int32)

    next_basin_id = 1

    for r in range(rows):
        for c in range(cols):
            if basin[r, c] >= 0:
                continue

            path = [(r, c)]
            cr, cc = r, c
            visited = {(r, c)}
            resolved_basin = -1

            while True:
                fd = flow_dir[cr, cc]
                if fd == 0 or fd not in _D8_TO_OFFSET:
                    break
                dr, dc = _D8_TO_OFFSET[fd]
                nr, nc = cr + dr, cc + dc
                if not (0 <= nr < rows and 0 <= nc < cols):
                    break
                if (nr, nc) in visited:
                    break
                if basin[nr, nc] >= 0:
                    resolved_basin = basin[nr, nc]
                    break
                visited.add((nr, nc))
                path.append((nr, nc))
                cr, cc = nr, nc

            if resolved_basin < 0:
                resolved_basin = next_basin_id
                next_basin_id += 1

            for pr, pc in path:
                basin[pr, pc] = resolved_basin

    out_ds, actual_path = _safe_create_output(
        output_path, cols, rows, 1, gdal.GDT_Int32,
        geotransform, projection,
    )
    out_ds.GetRasterBand(1).WriteArray(basin)
    out_ds.FlushCache()
    out_ds = None

    return actual_path


# ── 完整水文分析管道（确定性，不依赖 processing） ──

def safe_complete_hydrological_analysis(dem_path, output_dir, xzq_path, stream_threshold=100):
    """
    完整的确定性水文分析管道。从 DEM 到沟壑密度的一站式处理。

    参数
    ----
    dem_path : str
        输入 DEM GeoTIFF 路径
    output_dir : str
        所有中间和最终输出文件的目录
    xzq_path : str
        行政区划 shapefile 路径
    stream_threshold : int
        汇流累积阈值（默认 100 → 河网起点）

    返回
    ----
    dict
        {
            "dem_filled": str,
            "flow_dir": str,
            "flow_accum": str,
            "stream_raster": str,
            "stream_vector": str,
            "stream_intersect": str,
            "gully_stats_csv": str,
            "gully_density_csv": str,
        }
    """
    import os
    from osgeo import ogr, osr, gdal as _gdal_local

    os.makedirs(output_dir, exist_ok=True)

    # ── 强制 SHAPE_ENCODING 解决中文村名乱码 ──
    os.environ["SHAPE_ENCODING"] = "UTF-8"
    _gdal_local.SetConfigOption("SHAPE_ENCODING", "UTF-8")

    # ── 步骤 1-4：调降级库 ──
    dem_filled = os.path.join(output_dir, "dem_filled.tif")
    flow_dir = os.path.join(output_dir, "flow_dir.tif")
    flow_accum = os.path.join(output_dir, "flow_accum.tif")
    stream_raster = os.path.join(output_dir, "stream_raster.tif")

    _log.info(f"[管道] 步骤 1/8: 洼地填充 → {dem_filled}")
    dem_filled = safe_fill_sinks(dem_path, dem_filled)

    _log.info(f"[管道] 步骤 2/8: 水流方向 → {flow_dir}")
    flow_dir = safe_d8_flow_direction(dem_filled, flow_dir)

    _log.info(f"[管道] 步骤 3/8: 汇流累积 → {flow_accum}")
    flow_accum = safe_flow_accumulation(flow_dir, flow_accum)

    _log.info(f"[管道] 步骤 4/8: 河网提取 (threshold={stream_threshold}) → {stream_raster}")
    stream_raster = safe_stream_network(flow_accum, stream_raster, threshold=stream_threshold)

    # ── 步骤 5: 河网栅格 → 矢量面 → 矢量线 ──
    stream_vec_shp = os.path.join(output_dir, "stream_vector.shp")
    _log.info(f"[管道] 步骤 5/8: 栅格河网转矢量 → {stream_vec_shp}")

    # 5a. Polygonize 栅格 → 面
    stream_ds = gdal.Open(stream_raster)
    band = stream_ds.GetRasterBand(1)
    src_srs = osr.SpatialReference()
    src_srs.ImportFromWkt(stream_ds.GetProjection())

    drv_mem = ogr.GetDriverByName("MEM")
    poly_ds = drv_mem.CreateDataSource("")
    poly_layer = poly_ds.CreateLayer("stream_poly", srs=src_srs, geom_type=ogr.wkbPolygon)
    field_id = ogr.FieldDefn("id", ogr.OFTInteger)
    poly_layer.CreateField(field_id)

    gdal.Polygonize(band, band, poly_layer, 0, [], callback=None)
    stream_ds = None

    # 5b. 面 → 线（提取边界）
    drv_esri = ogr.GetDriverByName("ESRI Shapefile")
    if os.path.exists(stream_vec_shp):
        drv_esri.DeleteDataSource(stream_vec_shp)
    line_ds = drv_esri.CreateDataSource(stream_vec_shp)
    line_layer = line_ds.CreateLayer("stream_lines", srs=src_srs, geom_type=ogr.wkbLineString)
    line_fid = ogr.FieldDefn("id", ogr.OFTInteger)
    line_layer.CreateField(line_fid)

    fid = 0
    poly_layer.ResetReading()
    for poly_feat in poly_layer:
        geom = poly_feat.GetGeometryRef()
        if geom is None:
            continue
        boundary = geom.Boundary()
        if boundary is None:
            continue
        # Boundary 可能返回 MultiLineString 或单个 LineString
        if boundary.GetGeometryName() == "MULTILINESTRING":
            for i in range(boundary.GetGeometryCount()):
                sub_geom = boundary.GetGeometryRef(i)
                feature = ogr.Feature(line_layer.GetLayerDefn())
                feature.SetField("id", fid)
                feature.SetGeometry(sub_geom.Clone())
                line_layer.CreateFeature(feature)
                fid += 1
        elif boundary.GetGeometryName() == "LINESTRING":
            feature = ogr.Feature(line_layer.GetLayerDefn())
            feature.SetField("id", fid)
            feature.SetGeometry(boundary.Clone())
            line_layer.CreateFeature(feature)
            fid += 1
    line_ds = None
    poly_ds = None

    # ── 步骤 6: 叠加行政区划 ──
    stream_intersect_shp = os.path.join(output_dir, "stream_intersect.shp")
    _log.info(f"[管道] 步骤 6/8: 河网与行政区叠加 → {stream_intersect_shp}")

    # 读取河网线
    stream_src = ogr.Open(stream_vec_shp)
    stream_lyr = stream_src.GetLayer(0)

    # 读取行政区面
    xzq_src = ogr.Open(xzq_path)
    xzq_lyr = xzq_src.GetLayer(0)

    # 创建输出
    if os.path.exists(stream_intersect_shp):
        drv_esri.DeleteDataSource(stream_intersect_shp)
    intersect_ds = drv_esri.CreateDataSource(stream_intersect_shp)
    intersect_lyr = intersect_ds.CreateLayer("stream_xzq", srs=src_srs, geom_type=ogr.wkbLineString)

    # 复制 xzq 的属性字段
    xzq_defn = xzq_lyr.GetLayerDefn()
    xzq_field_names = []
    for i in range(xzq_defn.GetFieldCount()):
        field_defn = xzq_defn.GetFieldDefn(i)
        intersect_lyr.CreateField(ogr.FieldDefn(field_defn.GetName(), field_defn.GetType()))
        xzq_field_names.append(field_defn.GetName())

    # 新建长度字段
    len_field = ogr.FieldDefn("gully_len", ogr.OFTReal)
    intersect_lyr.CreateField(len_field)

    # 空间叠加：每条河网线与每个行政区求交
    xzq_lyr.ResetReading()
    for xzq_feat in xzq_lyr:
        xzq_geom = xzq_feat.GetGeometryRef()
        if xzq_geom is None:
            continue
        stream_lyr.SetSpatialFilter(xzq_geom)
        stream_lyr.ResetReading()
        for stream_feat in stream_lyr:
            stream_geom = stream_feat.GetGeometryRef()
            if stream_geom is None:
                continue
            try:
                inter_geom = stream_geom.Intersection(xzq_geom)
            except Exception:
                continue
            if inter_geom is None or inter_geom.IsEmpty():
                continue
            # 只保留线要素（Intersection 可能返回点）
            geom_name = inter_geom.GetGeometryName()
            if geom_name == "MULTILINESTRING":
                for i in range(inter_geom.GetGeometryCount()):
                    sub = inter_geom.GetGeometryRef(i)
                    feat = ogr.Feature(intersect_lyr.GetLayerDefn())
                    for j, fname in enumerate(xzq_field_names):
                        feat.SetField(fname, xzq_feat.GetField(j))
                    feat.SetField("gully_len", sub.Length())
                    feat.SetGeometry(sub.Clone())
                    intersect_lyr.CreateFeature(feat)
            elif geom_name == "LINESTRING":
                feat = ogr.Feature(intersect_lyr.GetLayerDefn())
                for j, fname in enumerate(xzq_field_names):
                    feat.SetField(fname, xzq_feat.GetField(j))
                feat.SetField("gully_len", inter_geom.Length())
                feat.SetGeometry(inter_geom.Clone())
                intersect_lyr.CreateFeature(feat)

    stream_src = None
    xzq_src = None
    intersect_ds = None

    # ── 步骤 7: 按村统计沟谷长度 ──
    gully_stats_csv = os.path.join(output_dir, "gully_stats_by_village.csv")
    _log.info(f"[管道] 步骤 7/8: 统计各村沟谷长度 → {gully_stats_csv}")

    # 找到区划名称字段（优先中文字段名）
    area_field = None
    for fname in xzq_field_names:
        if fname.upper() in ("NAME", "MC", "XZQMC", "CUN", "CUNMING", "村", "名称", "行政村"):
            area_field = fname
            break
    if area_field is None:
        area_field = xzq_field_names[0] if xzq_field_names else "FID"

    # 统计各村沟谷长度
    village_stats = {}
    inter_src = ogr.Open(stream_intersect_shp)
    inter_lyr = inter_src.GetLayer(0)
    for feat in inter_lyr:
        village = feat.GetField(area_field)
        if village is None:
            village = "未知"
        length = feat.GetField("gully_len") or 0
        village_stats[village] = village_stats.get(village, 0) + length
    inter_src = None

    # 计算各村面积（用于沟壑密度）
    village_area = {}
    xzq_src2 = ogr.Open(xzq_path)
    xzq_lyr2 = xzq_src2.GetLayer(0)
    for feat in xzq_lyr2:
        village = feat.GetField(area_field)
        if village is None:
            village = "未知"
        geom = feat.GetGeometryRef()
        if geom is not None:
            village_area[village] = village_area.get(village, 0) + geom.Area()
    xzq_src2 = None

    # 写 CSV
    import csv
    with open(gully_stats_csv, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["村名", "沟谷总长度(m)", "行政区面积(m²)", "沟壑密度(km/km²)"])
        for village in sorted(village_stats.keys()):
            total_len = village_stats[village]
            area = village_area.get(village, 1.0)
            density = (total_len / 1000.0) / (area / 1_000_000.0) if area > 0 else 0
            writer.writerow([village, round(total_len, 2), round(area, 2), round(density, 4)])

    # ── 步骤 8: 沟壑密度汇总 ──
    gully_density_csv = os.path.join(output_dir, "gully_density_summary.csv")
    _log.info(f"[管道] 步骤 8/8: 沟壑密度汇总 → {gully_density_csv}")
    # 复制到 density 文件
    import shutil
    shutil.copy2(gully_stats_csv, gully_density_csv)

    _log.info(f"\n[管道] ── 全部完成 ──")
    _log.info(f"  填充 DEM:    {dem_filled}")
    _log.info(f"  水流方向:    {flow_dir}")
    _log.info(f"  汇流累积:    {flow_accum}")
    _log.info(f"  栅格河网:    {stream_raster}")
    _log.info(f"  矢量河网:    {stream_vec_shp}")
    _log.info(f"  叠加结果:    {stream_intersect_shp}")
    _log.info(f"  沟谷统计:    {gully_stats_csv}")

    return {
        "dem_filled": dem_filled,
        "flow_dir": flow_dir,
        "flow_accum": flow_accum,
        "stream_raster": stream_raster,
        "stream_vector": stream_vec_shp,
        "stream_intersect": stream_intersect_shp,
        "gully_stats_csv": gully_stats_csv,
        "gully_density_csv": gully_density_csv,
    }
