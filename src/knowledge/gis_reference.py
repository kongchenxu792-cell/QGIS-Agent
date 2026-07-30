"""
GIS Python API 知识库 — 来自 opengis-skills 的精选模式

本模块提取了 PyQGIS / GDAL / Shapely 在 AIQGIS 场景中最常用的 API 模式，
作为 AI 代码生成的参考卡片和沙箱执行时的运行时知识。

来源：opengis-skills 开源项目 (https://github.com/znlgis/opengis-skills)
按 AIQGIS 场景裁切：不引入 pandas，不依赖额外第三方包。
"""

import logging

_log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# 第一部分：PyQGIS Processing 核心模式
# ═══════════════════════════════════════════════════════════════

PYQGIS_PROCESSING_PATTERNS = """
## PyQGIS Processing Toolbox 调用契约（AI 代码生成必读）

### 1. processing.run() 参数格式
所有 native: / gdal: / qgis: 前缀的算法统一使用字典参数：
```python
import processing
from qgis.core import QgsProject, QgsVectorLayer, QgsRasterLayer, QgsCoordinateReferenceSystem

result = processing.run("native:buffer", {
    'INPUT': layer,                              # 图层或路径
    'DISTANCE': 500.0,                           # 始终带单位
    'SEGMENTS': 12,
    'END_CAP_STYLE': 1,                          # 0=Round 1=Flat 2=Square
    'JOIN_STYLE': 1,                             # 0=Round 1=Miter 2=Bevel
    'DISSOLVE': False,
    'OUTPUT': 'TEMPORARY_OUTPUT'                 # 或 generate_output_path(...)
})
# result = {'OUTPUT': 'path_or_layer'}
```

### 2. 图层引用：始终用图层对象，不用路径别名
错误: processing.run("native:buffer", {'INPUT': 'roads.shp'})
正确: processing.run("native:buffer", {'INPUT': layers_by_name['roads']})

### 3. 坐标变换
```python
from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProject
src_crs = QgsCoordinateReferenceSystem('EPSG:4612')  # JGD2000 地理坐标系
dst_crs = QgsCoordinateReferenceSystem('EPSG:3857')  # Web Mercator 米制投影
xform = QgsCoordinateTransform(src_crs, dst_crs, QgsProject.instance())
transformed_geom = geom  # QgsGeometry
transformed_geom.transform(xform)
```

### 4. 内存图层创建（无需文件 I/O 的中间图层）
```python
vl = QgsVectorLayer("Point?crs=EPSG:4612&field=id:integer&field=name:string(50)", "points", "memory")
dp = vl.dataProvider()
dp.addFeatures([feature])
vl.updateExtents()
QgsProject.instance().addMapLayer(vl)
```

### 5. 字段计算器（推荐走 processing，比直接迭代快 10-100x）
```python
result = processing.run("native:fieldcalculator", {
    'INPUT': layer,
    'FIELD_NAME': 'area_km2',
    'FIELD_TYPE': 0,         # 0=Float 1=Integer 2=String 3=Date
    'FIELD_LENGTH': 12,
    'FIELD_PRECISION': 4,
    'FORMULA': '$area / 1000000',
    'OUTPUT': 'TEMPORARY_OUTPUT'
})
```

### 6. GDAL:cliprasterbymasklayer（栅格裁剪核心算子）
```python
result = processing.run("gdal:cliprasterbymasklayer", {
    'INPUT': raster_layer,
    'MASK': mask_polygon_layer,
    'SOURCE_CRS': QgsCoordinateReferenceSystem('EPSG:4326'),
    'TARGET_CRS': QgsCoordinateReferenceSystem('EPSG:3857'),
    'NODATA': -9999,
    'ALPHA_BAND': False,
    'CROP_TO_CUTLINE': True,
    'KEEP_RESOLUTION': False,
    'OUTPUT': generate_output_path('clipped', 'dataset')
})
```

### 7. 核心向量算子速查表
| 算子 | 关键参数 | 常见陷阱 |
|------|---------|---------|
| native:buffer | DISTANCE, SEGMENTS, DISSOLVE | 经纬度图层需先 reproject |
| native:clip | INPUT, OVERLAY | OVERLAY 必须是多边形 |
| native:intersection | INPUT, OVERLAY | 几何类型必须兼容 |
| native:dissolve | FIELD (list), DISSOLVE_ALL | 不对无效几何自动修复 |
| native:fixgeometries | INPUT | 始终在处理前先跑一遍 |
| native:reprojectlayer | TARGET_CRS | 密度类分析前必须转投影坐标系 |
| native:multiparttosingleparts | INPUT | 拆分后每个 part 独立 feature |
| native:centroids | INPUT, ALL_PARTS | 多部件只返回主质心 |
| native:extractbyexpression | EXPRESSION | 用 SQL 语法，字段名需双引号 |
| native:fieldcalculator | FORMULA | $area 返回 CRS 单位面积 |
"""

# ═══════════════════════════════════════════════════════════════
# 第二部分：GDAL Python API（超越 QGIS processing 的栅格能力）
# ═══════════════════════════════════════════════════════════════

GDAL_API_PATTERNS = """
## GDAL Python API 核心模式（osgeo.gdal）

### 1. 栅格打开与元数据
```python
from osgeo import gdal, gdal_array
ds = gdal.Open(r'D:/data/dem.tif')
_log.info(ds.RasterXSize, ds.RasterYSize, ds.RasterCount)
_log.info(ds.GetProjection())
_log.info(ds.GetGeoTransform())  # (origin_x, pixel_w, 0, origin_y, 0, -pixel_h)
band = ds.GetRasterBand(1)
_log.info(band.GetNoDataValue(), band.DataType, band.GetStatistics(True, True))
arr = band.ReadAsArray()  # NumPy ndarray
ds = None  # 关闭
```

### 2. gdal.Warp（重投影 / 重采样 / 裁剪 三合一）
```python
gdal.Warp(
    destNameOrDestDS=output_path,
    srcDSOrSrcDSTab=src_path,
    dstSRS='EPSG:3857',
    xRes=30, yRes=30,
    resampleAlg=gdal.GRA_Bilinear,      # Nearest/Bilinear/Cubic/CubicSpline/Lanczos
    cutlineDSName=mask_path,             # 裁剪多边形
    cropToCutline=True,
    dstNodata=-9999,
    format='GTiff',
    creationOptions=['COMPRESS=LZW', 'TILED=YES', 'BIGTIFF=IF_SAFER'],
    callback=gdal.TermProgress_nocb     # 终端进度条
)
```

### 3. gdal.Translate（格式转换 / 缩放 / 拉伸）
```python
gdal.Translate(output_path, src_path,
    format='PNG',
    bandList=[1, 2, 3],
    width=2048, height=2048,             # 缩放
    scaleParams=[[0, 255, 0, 255]],      # 拉伸
    creationOptions=['WORLDFILE=YES']
)
```

### 4. 栅格代数（gdal_array + NumPy，比 QGIS raster calculator 更灵活）
```python
import numpy as np
arr = gdal.Open('dem.tif').ReadAsArray()
slope_deg = np.degrees(np.arctan(np.sqrt(
    np.gradient(arr.astype(float), 30, axis=1)**2 +
    np.gradient(arr.astype(float), 30, axis=0)**2
)))
# 回写
gdal_array.SaveArray(slope_deg.astype(np.float32), 'slope.tif',
    format='GTiff', prototype=gdal.Open('dem.tif'))
```

### 5. 栅格统计聚合
```python
from osgeo import gdal
ds = gdal.Open('landcover.tif')
band = ds.GetRasterBand(1)
hist = band.GetHistogram(min=-0.5, max=10.5, buckets=11)  # 分类统计
stats = band.GetStatistics(True, True)                     # (min, max, mean, stddev)
band.ComputeStatistics(False)                              # 强制重算，忽略 nodata
```

### 6. VRT 虚拟栅格（零拷贝拼接 / 波段合成）
```python
vrt = gdal.BuildVRT('merged.vrt', ['tile1.tif', 'tile2.tif', 'tile3.tif'])
# 直接作为数据集使用，零 I/O 开销
```

### 7. 栅格转矢量（等高线 / 多边形化）
```python
import processing
result = processing.run("gdal:contour", {
    'INPUT': dem_layer,
    'INTERVAL': 100.0,
    'FIELD_NAME': 'ELEV',
    'OUTPUT': generate_output_path('contour', 'dem')
})
```
"""

# ═══════════════════════════════════════════════════════════════
# 第三部分：Shapely 几何操作模式
# ═══════════════════════════════════════════════════════════════

SHAPELY_PATTERNS = """
## Shapely 几何操作核心模式（从 shapely 2.x 提取）

### 1. 几何构造（from shapely.geometry import ...）
```python
from shapely.geometry import Point, LineString, Polygon, MultiPoint, MultiLineString, MultiPolygon
from shapely import wkt, wkb, from_geojson, to_geojson

p = Point(104.06, 30.67)
line = LineString([(0, 0), (1, 1), (2, 0)])
poly = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])

# 从字符串反序列化
geom = wkt.loads('POINT(104.06 30.67)')
geom = wkb.loads(b'\x01\x01...')
geom = from_geojson('{"type":"Point","coordinates":[104.06,30.67]}')
```

### 2. 空间谓词（返回 bool）
```python
a.contains(b)       # a 完全包含 b
a.within(b)         # a 完全在 b 内部
a.intersects(b)     # 边界或内部相交即 True
a.touches(b)        # 仅边界接触
a.crosses(b)        # 内部相交但不包含
a.overlaps(b)       # 同维度几何部分重叠
a.disjoint(b)       # 完全不相交
a.covers(b)         # a 覆盖 b 全部（无点漏出）
```

### 3. 集合运算（返回新几何）
```python
a.intersection(b)           # 交集
a.union(b)                  # 并集
a.difference(b)             # A - B
a.symmetric_difference(b)   # (A-B) ∪ (B-A)
```

### 4. 常用几何操作
```python
geom.buffer(500.0, quad_segs=8)       # 缓冲区（CRS 单位）
geom.simplify(10.0, preserve_topology=True)  # 简化
geom.convex_hull              # 凸包
geom.centroid                 # 质心
geom.representative_point()   # 保证在内部的代表点
geom.envelope                 # 外接矩形
geom.area, geom.length, geom.bounds  # (minx, miny, maxx, maxy)
geom.distance(other)          # 最近距离
geom.hausdorff_distance(other) # 最大偏差
```

### 5. STRtree 空间索引（用于高效空间查询）
```python
from shapely import STRtree
tree = STRtree(polygons)
indices = tree.query(Point(104.06, 30.67))
hits = [polygons[i] for i in indices]  # 相交的多边形
```

### 6. QgsGeometry ↔ Shapely 互转（用于 PyQGIS 操作后处理）
```python
from shapely import wkb
# QGIS → Shapely
shapely_geom = wkb.loads(bytes(qgs_geom.asWkb()))
# Shapely → QGIS
qgs_geom = QgsGeometry.fromWkt(shapely_geom.wkt)
```

### 7. 批量操作（向量化，shapely 2.x 特性）
```python
from shapely import area, length, buffer, centroid, simplify, intersection
areas = area(polygons)                    # 返回浮点数数组
centroids = centroid(polygons)             # 返回 Point 数组
buffers = buffer(points, 500.0, quad_segs=8)
```
"""

# ═══════════════════════════════════════════════════════════════
# 第四部分：GIS 代码生成最佳实践
# ═══════════════════════════════════════════════════════════════

GIS_BEST_PRACTICES = """
## GIS Python 代码生成铁律（AI 生成代码前必读）

### 1. 坐标系铁律
- 空间分析（缓冲区/密度/距离）必须在米制投影坐标系下执行
- 中国区域推荐：EPSG:3857（Web墨卡托）或 EPSG:4527（CGCS2000 / 3-degree Gauss-Kruger zone 39）
- 显示/导出时可转回 EPSG:4326

### 2. 几何有效性铁律
- 处理前始终调用 native:fixgeometries
- 空洞/自交/重复节点会导致后续算子静默失败

### 3. 字段名铁律
- QGIS expression 中的字段名必须用双引号括起："field_name"
- processing fieldcalculator 的 FORMULA 直接用字段名，不带引号

### 4. 图层 vs 路径铁律
- 同一 processing 链中，前步输出传给下一步时优先用图层对象，避免反复写盘
- 跨会话或最终输出必须调用 generate_output_path() 持久化

### 5. result 变量铁律
- 代码最后一句必须是 result = ... 赋值
- result 必须是 processing.run() 返回的 dict 或 QgsMapLayer 对象

### 6. 错误处理
- 矢量图层打开后检查 layer.isValid()
- 栅格图层检查 ds is not None
- 操作后检查 result 字典中的 OUTPUT 键

### 7. 性能铁律
- 批量操作用 processing.run，不要手动循环 feature
- 栅格运算用 gdal.Warp / numpy 向量化，不要逐像素 Python 循环
- STRtree 用于 >1000 个几何的空间索引
"""

# ═══════════════════════════════════════════════════════════════
# 动态路由：根据用户查询意图精简化注入
# ═══════════════════════════════════════════════════════════════

# 每个路由分片的头行——用于 AI 快速识别当前启用的知识域
_ROUTE_HEADER = "## 技能参考文档（opengis-skills 动态注入）\n"

# ── 关键字 → 知识域映射 ────────────────────────────────────

_GDAL_KEYWORDS = [
    "栅格", "裁剪", "tif", "tiff", "raster", "gdal", "warp", "translate",
    "核密度", "热力图", "dem", "坡度", "坡向", "等高线", "重采样", "像元",
    "pixel", "ndvi", "遥感", "影像", "高程", "插值", "idw", "tin",
    "vrt", "buildvrt", "重投影", "投影转换", "拼接", "镶嵌",
]

_SHAPELY_KEYWORDS = [
    "相交", "缓冲区", "几何", "shapely", "包含", "距离", "合并", "交集",
    "差集", "并集", "空间索引", "凸包", "质心", "简化", "点在多边形",
    "空间查询", "最近邻", "邻接", "拓扑", "strtree", "面积", "周长",
    "合并", "融合", "dissolve", "交集", "相交", "擦除", "union",
]

# ── 精简后的分片（每个 < 250 tokens，仅保留方法签名和参数范式） ──

_GDAL_SLIM = _ROUTE_HEADER + """\
【GDAL 栅格处理】

- gdal.Open(path).GetRasterBand(1).ReadAsArray() → NumPy ndarray
- gdal.Warp(out, src, dstSRS='EPSG:3857', xRes=30, resampleAlg=gdal.GRA_Bilinear,
    cutlineDSName=mask_path, cropToCutline=True, dstNodata=-9999)
- gdal.Translate(out, src, format='PNG', bandList=[1,2,3], width=2048, height=2048)
- gdal.BuildVRT('merged.vrt', [t1,t2,t3])  # 零拷贝虚拟拼接
- gdal_array.SaveArray(ndarray, 'out.tif', format='GTiff', prototype=ref_ds)
- processing.run('gdal:cliprasterbymasklayer', {INPUT, MASK, SOURCE_CRS, TARGET_CRS,
    NODATA, CROP_TO_CUTLINE, OUTPUT})
- processing.run('gdal:contour', {INPUT, INTERVAL, FIELD_NAME, OUTPUT})
"""

_SHAPELY_SLIM = _ROUTE_HEADER + """\
【Shapely 几何运算】

- 构造: Point(x,y) | LineString([...]) | Polygon([...]) | wkt.loads('POINT(...)')
- 谓词: .contains(b) .within(b) .intersects(b) .touches(b) .crosses(b)
         .overlaps(b) .disjoint(b) .covers(b)
- 集合: .intersection(b) .union(b) .difference(b) .symmetric_difference(b)
- 操作: .buffer(dist, quad_segs=8) .simplify(tol) .convex_hull .centroid
        .area .length .bounds .distance(other)
- STRtree: tree=STRtree(list); tree.query(Point(x,y)) → indices
- 互转: shapely_geom = wkb.loads(bytes(qgs_geom.asWkb()))
        qgs_geom = QgsGeometry.fromWkt(shapely_geom.wkt)
- 批量: from shapely import area,length,buffer,centroid → area(list) 返回浮点数组
"""

_PYQGIS_SLIM = _ROUTE_HEADER + """\
【PyQGIS Processing 基础】

- processing.run('native:xxx', {INPUT:layer, OUTPUT:generate_output_path('pfx',name)})
- 图层引用始终用变量，不用文件路径别名
- QgsCoordinateTransform(src_crs, dst_crs, QgsProject.instance())
- QgsVectorLayer('Point?crs=EPSG:4326&field=id:integer', name, 'memory')
- fieldcalculator: {INPUT, FIELD_NAME, FIELD_TYPE:0, FORMULA:'$area/1000000', OUTPUT}
- reprojectlayer: {INPUT, TARGET_CRS:QgsCoordinateReferenceSystem('EPSG:3857'), OUTPUT}
- fixgeometries: {INPUT, OUTPUT}  # 处理前必调
- dissolve: {INPUT, FIELD:[], DISSOLVE_ALL:True, OUTPUT}
- clip: {INPUT, OVERLAY, OUTPUT}
- buffer: {INPUT, DISTANCE, SEGMENTS:12, DISSOLVE:False, OUTPUT}
"""

# ── 路由函数：按意图 → 返回精简分片 ──

def _match_keywords(query: str, keywords: list) -> bool:
    """简单关键词匹配（大小写不敏感）。"""
    q_lower = query.lower()
    return any(kw.lower() in q_lower for kw in keywords)


def build_reference_injection(user_query: str = "") -> str:
    """动态精细化过滤：根据用户意图仅注入相关 API 参考。

    规则：
    - 命中 GDAL 关键词 → GDAL 分片
    - 命中 Shapely 关键词 → Shapely 分片
    - 均未命中 → PyQGIS 基础分片（默认兜底）
    - 同时命中 → 两者拼接，但总长仍控制在 ~500 tokens 内
    """

    has_gdal = _match_keywords(user_query, _GDAL_KEYWORDS)
    has_shapely = _match_keywords(user_query, _SHAPELY_KEYWORDS)

    parts = []
    if has_gdal:
        parts.append(_GDAL_SLIM)
    if has_shapely:
        parts.append(_SHAPELY_SLIM)
    if not parts:
        parts.append(_PYQGIS_SLIM)

    return "\n".join(parts)