# -*- coding: utf-8 -*-
r"""scripts/gen_multi_disaster_data.py — 多灾种合成数据 + 4 灾种引擎验证（Solo 片A）

背景：产品定位「多灾种快速评估，灾种可插拔」。引擎不认灾种，只认
「危险区集 + 边界 + 避难所」；灾种差异由 disaster_registry.py 注册表 +
模板实例（risk_zone_coverage.json）+ 危险区图层承载，引擎零改动。

本脚本：
1. 用 Shapely 合成 4 灾种危险区面（地震震度分布/洪涝淹没区/滑坡风险区/
   火灾风险区），几何 100% valid 断言（make_valid 兜底），EPSG:3857，
   输出 temp/multi_disaster/
2. 复用 L1550 手算基准同款行政区（正方形 10000x10000）与中心避难所点
3. 通过 disaster_registry 读取注册表，4 灾种各执行 1 条覆盖分析
   （PipelineExecutor + risk_zone_coverage 模板实例，source=避难所，
   boundary=该灾种危险区面）
4. 落 run 记录 temp/multi_disaster/run_records.json（4 条可查）

运行：qgis-portable\apps\Python312\python.exe scripts\gen_multi_disaster_data.py
红线遵守：无 git 写；不动 pipeline_executor/guards/现有 4 模板/CRS/引擎链；
新建模板实例 risk_zone_coverage.json 仅复制 coverage_analysis.json 改触发词。

预期覆盖率（buffer=500m 20边形近似，面积=772542.5 m²；风险区为同圆心圆）：
- 地震 r=800 : 772542.5 / (pi*800^2)   ≈ 38.4%
- 洪涝 r=400 : 100%（400m 圆整体落在 500m 缓冲区内）
- 滑坡 r=1200: 772542.5 / (pi*1200^2)  ≈ 17.1%
- 火灾 r=600 : 772542.5 / (pi*600^2)   ≈ 68.3%
"""
import os
import sys
import json
import time
import math

PROJECT_ROOT = r"D:\桌面\QGIS-Agent"
OSGEO4W_ROOT = os.path.join(PROJECT_ROOT, "qgis-portable")
os.environ["OSGEO4W_ROOT"] = OSGEO4W_ROOT
os.environ["QGIS_PREFIX_PATH"] = os.path.join(OSGEO4W_ROOT, "apps", "qgis-ltr")
os.environ["GDAL_FILENAME_IS_UTF8"] = "YES"
os.environ["PROJ_LIB"] = os.path.join(OSGEO4W_ROOT, "share", "proj")
os.environ["PROJ_DATA"] = os.path.join(OSGEO4W_ROOT, "share", "proj")
os.environ["GDAL_DATA"] = os.path.join(OSGEO4W_ROOT, "apps", "gdal", "share", "gdal")
os.environ["QT_PLUGIN_PATH"] = os.path.join(OSGEO4W_ROOT, "apps", "qgis-ltr", "qtplugins") + ";" + os.path.join(OSGEO4W_ROOT, "apps", "Qt5", "plugins")
os.environ["GDAL_DRIVER_PATH"] = os.path.join(OSGEO4W_ROOT, "apps", "gdal", "lib", "gdalplugins")
os.environ["PYTHONHOME"] = os.path.join(OSGEO4W_ROOT, "apps", "Python312")
os.environ["PYTHONUTF8"] = "1"
os.environ["PATH"] = ";".join([os.path.join(OSGEO4W_ROOT, "apps", "Qt5", "bin"), os.path.join(OSGEO4W_ROOT, "bin"), os.path.join(OSGEO4W_ROOT, "apps", "Python312"), os.path.join(OSGEO4W_ROOT, "apps", "qgis-ltr", "bin"), r"C:\WINDOWS\system32", r"C:\WINDOWS"])
os.environ["PYTHONPATH"] = os.path.join(OSGEO4W_ROOT, "apps", "qgis-ltr", "python") + ";" + os.path.join(OSGEO4W_ROOT, "apps", "Python312", "Lib", "site-packages")
sys.path.insert(0, os.path.join(OSGEO4W_ROOT, "apps", "qgis-ltr", "python"))
sys.path.append(os.path.join(OSGEO4W_ROOT, "apps", "qgis-ltr", "python", "plugins"))
sys.path.insert(0, os.path.join(OSGEO4W_ROOT, "apps", "Python312", "Lib", "site-packages"))
for _d in [os.path.join(OSGEO4W_ROOT, "apps", "qgis-ltr", "bin"), os.path.join(OSGEO4W_ROOT, "bin"), os.path.join(OSGEO4W_ROOT, "apps", "Qt5", "bin")]:
    if hasattr(os, "add_dll_directory"):
        os.add_dll_directory(_d)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from shapely.geometry import Point, box
from shapely.validation import make_valid
from osgeo import ogr, osr

from core.disaster_registry import list_disasters, get_risk_zone_path, get_template_path, get_template_name, get_disaster_name

OUT_DIR = os.path.join(PROJECT_ROOT, "temp", "multi_disaster")
RUN_RECORD = os.path.join(OUT_DIR, "run_records.json")
RADIUS_M = 500.0

# 风险区半径（与中心避难所同圆心，单位米）—— 见模块 docstring 的预期值
RISK_ZONES = {
    "earthquake": {"r": 800.0, "fields": {"intensity": "6弱", "name": "震度6弱区域"}},
    "flood":      {"r": 400.0, "fields": {"depth": 1.5, "name": "淹没区"}},
    "landslide":  {"r": 1200.0, "fields": {"risk_level": "高", "name": "滑坡高风险区"}},
    "wildfire":   {"r": 600.0, "fields": {"risk_level": "高", "name": "火灾高风险区"}},
}


def say(msg):
    print(msg, flush=True)


say("========================================")
say("多灾种合成数据生成 + 4 灾种引擎验证")
say("========================================")

# ── 1. Shapely 合成几何（L1550 同款原点/边界）────────────────
TOKYO_LON, TOKYO_LAT = 139.6917, 35.6895  # 东京都厅

def lonlat_to_3857(lon, lat):
    x = lon * 20037508.34 / 180.0
    y = math.log(math.tan((90.0 + lat) * math.pi / 360.0)) / (math.pi / 180.0)
    y = y * 20037508.34 / 180.0
    return x, y

OX, OY = lonlat_to_3857(TOKYO_LON, TOKYO_LAT)
HALF = 5000.0  # 正方形边界半边长 -> 总面积 1e8 m²

boundary_poly = box(OX - HALF, OY - HALF, OX + HALF, OY + HALF)
shelter_pt = Point(OX, OY)


def check_valid(geom, name):
    if not geom.is_valid:
        fixed = make_valid(geom)
        say(f"[WARN] {name} is_valid=False -> make_valid 修复")
        geom = fixed
    assert geom.is_valid, f"{name} 几何无效（make_valid 兜底后仍无效）"
    assert not geom.is_empty, f"{name} 几何为空"
    return geom


boundary_poly = check_valid(boundary_poly, "行政区(正方形边界)")
shelter_pt = check_valid(shelter_pt, "避难所(中心点)")

risk_geoms = {}
for did, spec in RISK_ZONES.items():
    g = Point(OX, OY).buffer(spec["r"], quad_segs=64)
    risk_geoms[did] = check_valid(g, f"{did} 危险区(半径{spec['r']}m圆)")

# ── 2. 写入 GPKG（全部 EPSG:3857）─────────────────────────────
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


def _write_polygon(path, geom, fields):
    ds, lyr = _create_ds(path, ogr.wkbPolygon)
    for fname, fval in fields.items():
        ftype = ogr.OFTReal if isinstance(fval, float) else ogr.OFTString
        lyr.CreateField(ogr.FieldDefn(fname, ftype))
    f = ogr.Feature(lyr.GetLayerDefn())
    f.SetGeometry(ogr.CreateGeometryFromWkt(_wkt(geom)))
    for fname, fval in fields.items():
        f.SetField(fname, fval)
    lyr.CreateFeature(f)
    ds = None


# 行政区（正方形边界）
_write_polygon(os.path.join(OUT_DIR, "行政区.gpkg"), boundary_poly, {"name": "东京行政区"})
say(f"[OK] 行政区.gpkg: 1 要素, 面积={boundary_poly.area:.0f} m²")

# 避难所（中心点）
ds, lyr = _create_ds(os.path.join(OUT_DIR, "避难所.gpkg"), ogr.wkbPoint)
f = ogr.Feature(lyr.GetLayerDefn())
f.SetGeometry(ogr.CreateGeometryFromWkt(_wkt(shelter_pt)))
f.SetField("name", "避难所中心")
lyr.CreateFeature(f)
ds = None
say(f"[OK] 避难所.gpkg: 1 要素, 坐标=({OX:.1f}, {OY:.1f})")

# 4 灾种危险区面
for did, spec in RISK_ZONES.items():
    path = get_risk_zone_path(did)
    _write_polygon(path, risk_geoms[did], spec["fields"])
    say(f"[OK] {os.path.basename(path)}: 1 要素, 半径={spec['r']}m, "
        f"面积={risk_geoms[did].area:.0f} m², is_valid=True")

say("")
say("几何校验：全部 is_valid=True（断言通过）")

# ── 3. QGIS 引擎初始化 ─────────────────────────────────────────
import tempfile, shutil
_srs_src = os.path.join(OSGEO4W_ROOT, "apps", "qgis-ltr", "resources", "srs.db")
_srs_dst = os.path.join(tempfile.gettempdir(), "srs6.db")
if not os.path.exists(_srs_dst) and os.path.exists(_srs_src):
    try:
        shutil.copy(_srs_src, _srs_dst)
        say(f"[预置] srs6.db -> {_srs_dst}")
    except Exception as _e:
        say(f"[警告] srs.db 预置失败: {_e}")

from qgis.core import QgsApplication, QgsVectorLayer, QgsProject
from qgis.gui import QgsMapCanvas
QgsApplication.setPrefixPath(os.path.join(OSGEO4W_ROOT, "apps", "qgis-ltr"), True)
qgs = QgsApplication([], False)
qgs.initQgis()

from core.qgis_env import initialize_processing
initialize_processing(qgs)
say("QGIS 引擎初始化完成")

# ── 4. 加载数据（行政区/避难所 + 4 灾种危险区）────────────────
project = QgsProject.instance()
BASE_LAYERS = [
    (os.path.join(OUT_DIR, "行政区.gpkg"), "东京行政区"),
    (os.path.join(OUT_DIR, "避难所.gpkg"), "避难所"),
]
for path, name in BASE_LAYERS:
    lyr = QgsVectorLayer(path, name, "ogr")
    if not lyr.isValid():
        say(f"[数据] 加载失败 {name}: {path}")
        sys.exit(1)
    project.addMapLayer(lyr)
    say(f"[数据] {name}: {lyr.featureCount()} 要素, CRS={lyr.crs().authid()}")

# 危险区图层名 = 注册表 risk_zone_layer 去扩展名
disaster_layer_names = {}
for info in list_disasters():
    layer_file = info["risk_zone_layer"]
    layer_name = os.path.splitext(layer_file)[0]
    path = get_risk_zone_path(info["disaster_id"])
    lyr = QgsVectorLayer(path, layer_name, "ogr")
    if not lyr.isValid():
        say(f"[数据] 加载失败 {layer_name}: {path}")
        sys.exit(1)
    project.addMapLayer(lyr)
    disaster_layer_names[info["disaster_id"]] = layer_name
    say(f"[数据] {layer_name}: {lyr.featureCount()} 要素, CRS={lyr.crs().authid()}")

canvas = QgsMapCanvas()
from core.pipeline_executor import PipelineExecutor


def run_chain(disaster_id, boundary_layer_name):
    say("")
    say(f"[{disaster_id}] 开始执行覆盖分析...")
    t0 = time.time()
    executor = PipelineExecutor()
    result = executor.execute(
        template_path=get_template_path(disaster_id),
        source_layer_name="避难所",
        boundary_layer_name=boundary_layer_name,
        radius_m=RADIUS_M,
        project=project,
        canvas=canvas,
    )
    dt = round(time.time() - t0, 1)
    ok = bool(result.get("success"))
    stats = result.get("stats") or {}
    say(f"[{disaster_id}] 结果: {'成功' if ok else '失败'} ({dt}s)")
    say(f"[{disaster_id}] status={result.get('status')} message={str(result.get('message'))[:300]}")
    if stats:
        say(f"[{disaster_id}] stats={json.dumps(stats, ensure_ascii=False)}")
    return {
        "disaster_id": disaster_id,
        "disaster_name": get_disaster_name(disaster_id, "zh"),
        "template": get_template_name(disaster_id),
        "risk_zone_layer": disaster_layer_names[disaster_id],
        "success": ok,
        "status": result.get("status"),
        "message": str(result.get("message"))[:500],
        "stats": stats,
        "dt_s": dt,
    }


records = []
for info in list_disasters():
    records.append(run_chain(info["disaster_id"], disaster_layer_names[info["disaster_id"]]))

# ── 5. 结果判定 + 落 run 记录 ─────────────────────────────────
say("")
say("========== 4 灾种结果表 ==========")
all_ok = True
for rec in records:
    stats = rec["stats"]
    coverage = stats.get("coverage_rate") if stats else None
    ok_chain = rec["success"] and rec["status"] == "ok" and coverage is not None and -1e-6 <= coverage <= 100 + 1e-6
    rec["pass"] = ok_chain
    all_ok = all_ok and ok_chain
    say(f"{rec['disaster_name']}({rec['disaster_id']}): 模板={rec['template']} | "
        f"危险区图层={rec['risk_zone_layer']} | status={rec['status']} | "
        f"覆盖率={coverage:.2f}% | {'PASS' if ok_chain else 'FAIL'}")

run_record = {
    "task": "片A 多灾种注册表 + 合成数据 4 灾种验证",
    "date": time.strftime("%Y-%m-%d %H:%M:%S"),
    "data_dir": OUT_DIR,
    "radius_m": RADIUS_M,
    "template": "risk_zone_coverage",
    "engine_change": "none（引擎零改动，灾种由注册表+模板实例+危险区图层承载）",
    "records": records,
    "all_pass": all_ok,
}
with open(RUN_RECORD, "w", encoding="utf-8") as f:
    json.dump(run_record, f, ensure_ascii=False, indent=2)
say(f"[记录] run_records.json -> {RUN_RECORD}（{len(records)} 条）")

say("")
say(f"========== 最终判定: {'ALL PASS' if all_ok else '存在 FAIL，需停下上报'} ==========")
say("MULTI_DISASTER_DONE")
qgs.exitQgis()
