# -*- coding: utf-8 -*-
r"""scripts/gen_synth_data.py — 手算基准合成数据生成器（Solo L1550 授权入库）

背景：东京真实数据反复失败（bbox 错 / r2ka13 union 几何无效 / 3857 大坐标坑），
本脚本用 Shapely 生成**几何绝对正确、答案可手算**的合成数据，用于验证
「引擎 + 数据 + 结果」全链可信。

场景（L1550 手算期望值）：
- 一个正方形行政边界 10000m x 10000m（EPSG:3857，以东京都中心附近为原点）
- 避难所：正方形中心 1 个点，radius=500m -> 缓冲区圆面积 = pi*500^2 ≈ 785398 m²
- coverage 期望 = 785398 / 1e8 = 0.785%（±0.01%）
- gap 期望 = 100 - 0.785 = 99.215%（±0.01%）
- 人口：4 个 5000x5000 分区（各含 population），被圆覆盖部分按面积比例加权
  -> 人口覆盖率期望 = 圆面积 / 总面积 = 0.785%（面积均匀时）

输出：temp/synth_handcalc/ 下 3 个 GPKG，全部 EPSG:3857，
几何有效性 is_valid 全部 True（脚本断言），make_valid 兜底。

运行：qgis-portable\apps\Python312\python.exe scripts\gen_synth_data.py
"""
import os
import sys
import math

PROJECT_ROOT = r"D:\桌面\QGIS-Agent"
OSGEO4W_ROOT = os.path.join(PROJECT_ROOT, "qgis-portable")
os.environ["OSGEO4W_ROOT"] = OSGEO4W_ROOT
os.environ["QGIS_PREFIX_PATH"] = os.path.join(OSGEO4W_ROOT, "apps", "qgis-ltr")
os.environ["PROJ_LIB"] = os.path.join(OSGEO4W_ROOT, "share", "proj")
os.environ["PROJ_DATA"] = os.path.join(OSGEO4W_ROOT, "share", "proj")
os.environ["GDAL_DATA"] = os.path.join(OSGEO4W_ROOT, "apps", "gdal", "share", "gdal")
os.environ["PATH"] = ";".join([
    os.path.join(OSGEO4W_ROOT, "apps", "Qt5", "bin"),
    os.path.join(OSGEO4W_ROOT, "bin"),
    os.path.join(OSGEO4W_ROOT, "apps", "Python312"),
    os.path.join(OSGEO4W_ROOT, "apps", "qgis-ltr", "bin"),
    r"C:\WINDOWS\system32", r"C:\WINDOWS",
])

from shapely.geometry import Polygon, Point, box
from shapely.validation import make_valid
from osgeo import ogr, osr

OUT_DIR = os.path.join(PROJECT_ROOT, "temp", "synth_handcalc")

# ── 原点：东京都厅附近（EPSG:3857）──────────────────────────────
TOKYO_LON, TOKYO_LAT = 139.6917, 35.6895  # 东京都厅

def lonlat_to_3857(lon: float, lat: float):
    """Web Mercator 投影（EPSG:3857）经纬度 -> 米。"""
    x = lon * 20037508.34 / 180.0
    y = math.log(math.tan((90.0 + lat) * math.pi / 360.0)) / (math.pi / 180.0)
    y = y * 20037508.34 / 180.0
    return x, y

OX, OY = lonlat_to_3857(TOKYO_LON, TOKYO_LAT)
HALF = 5000.0  # 半边长 5000m -> 总边长 10000m
RADIUS_M = 500.0

# ── 1. 几何构造（全部用 Shapely，保证确定性）──────────────────
boundary_poly = box(OX - HALF, OY - HALF, OX + HALF, OY + HALF)  # 正方形边界
shelter_pt = Point(OX, OY)                                       # 中心避难所点
# 4 个 5000x5000 人口分区（各含 population 字段）
pop_zones = [
    (box(OX - HALF, OY - HALF, OX, OY), "zone_bl", 10000),
    (box(OX, OY - HALF, OX + HALF, OY), "zone_br", 10000),
    (box(OX - HALF, OY, OX, OY + HALF), "zone_tl", 10000),
    (box(OX, OY, OX + HALF, OY + HALF), "zone_tr", 10000),
]

# ── 2. 几何有效性校验（is_valid 全部 True 断言 + make_valid 兜底）──
def check_valid(geom, name):
    if not geom.is_valid:
        fixed = make_valid(geom)
        print(f"[WARN] {name} is_valid=False -> make_valid 修复")
        geom = fixed
    assert geom.is_valid, f"{name} 几何无效（make_valid 兜底后仍无效）"
    assert not geom.is_empty, f"{name} 几何为空"
    return geom

boundary_poly = check_valid(boundary_poly, "东京行政区(正方形边界)")
shelter_pt = check_valid(shelter_pt, "避难所(中心点)")
pop_zones = [(check_valid(g, n), n, p) for g, n, p in pop_zones]

# ── 3. 写入 GPKG（osgeo.ogr，EPSG:3857）────────────────────────
os.makedirs(OUT_DIR, exist_ok=True)

def _create_ds(path, geom_type):
    if os.path.exists(path):
        os.remove(path)
    drv = ogr.GetDriverByName("GPKG")
    ds = drv.CreateDataSource(path)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(3857)
    lyr = ds.CreateLayer("layer", srs, geom_type)
    return ds, lyr

def _wkt(geom):
    return geom.wkt

# 3a. 东京行政区（正方形边界）
ds, lyr = _create_ds(os.path.join(OUT_DIR, "行政区_handcalc.gpkg"), ogr.wkbPolygon)
f = ogr.Feature(lyr.GetLayerDefn())
f.SetGeometry(ogr.CreateGeometryFromWkt(_wkt(boundary_poly)))
f.SetField("name", "东京行政区")
lyr.CreateFeature(f)
ds = None
print(f"[OK] 行政区_handcalc.gpkg: 1 要素, 面积={boundary_poly.area:.0f} m²")

# 3b. 避难所（中心点）
ds, lyr = _create_ds(os.path.join(OUT_DIR, "避难所_handcalc.gpkg"), ogr.wkbPoint)
f = ogr.Feature(lyr.GetLayerDefn())
f.SetGeometry(ogr.CreateGeometryFromWkt(_wkt(shelter_pt)))
f.SetField("name", "避难所中心")
lyr.CreateFeature(f)
ds = None
print(f"[OK] 避难所_handcalc.gpkg: 1 要素, 坐标=({OX:.1f}, {OY:.1f})")

# 3c. 人口（4 分区，含 population 字段）
ds, lyr = _create_ds(os.path.join(OUT_DIR, "人口_handcalc.gpkg"), ogr.wkbPolygon)
fld = ogr.FieldDefn("population", ogr.OFTInteger)
lyr.CreateField(fld)
fld2 = ogr.FieldDefn("zone_name", ogr.OFTString)
lyr.CreateField(fld2)
for geom, name, pop in pop_zones:
    f = ogr.Feature(lyr.GetLayerDefn())
    f.SetGeometry(ogr.CreateGeometryFromWkt(_wkt(geom)))
    f.SetField("population", pop)
    f.SetField("zone_name", name)
    lyr.CreateFeature(f)
ds = None
print(f"[OK] 人口_handcalc.gpkg: {len(pop_zones)} 要素, 各 population=10000")

# ── 4. 汇总与几何校验输出 ─────────────────────────────────────
print("")
print("========================================")
print("合成数据生成完成（L1550 手算基准）")
print(f"输出目录: {OUT_DIR}")
print(f"原点(EPSG:3857): ({OX:.2f}, {OY:.2f})")
print(f"正方形边长: 10000m, 面积: {boundary_poly.area:.0f} m²")
print(f"中心避难所半径: {RADIUS_M}m -> 圆面积: {math.pi*RADIUS_M**2:.0f} m²")
print(f"coverage 期望: {math.pi*RADIUS_M**2/1e8*100:.3f}%")
print(f"gap 期望: {100 - math.pi*RADIUS_M**2/1e8*100:.3f}%")
print("几何校验: 全部 is_valid=True（断言通过）")
print("========================================")
print("GEN_SYNTH_DONE")
