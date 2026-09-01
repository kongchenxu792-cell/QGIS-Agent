# -*- coding: utf-8 -*-
r"""scripts/run_chongqing.py — 重庆跑通：多灾种真实数据（覆盖/洪涝/滑坡）（Solo APPROVED）

Solo 批复「重庆跑通：多灾种真实数据（覆盖/洪涝/滑坡）」：
- 数据：D:\桌面\项目测试数据\中国\重庆\（行政区 1 / 区县 38 / 避难所 1619 点 / 人口 110271 cells / 河流 4364 / 坡度 7139 面，全 valid 双版本）
- 1. 通用覆盖链：避难所双口径（官方 39 点 = source in dazu_longgang/kz_natural；全量 1619 点）
     500m 缓冲对重庆行政区覆盖率 + 盲区 + 人口覆盖 + 报告预警
- 2. 洪涝场景：重庆河流 buffer 300m（Shapely）生成淹没区近似 → 洪涝覆盖（官方 39 点避难所对淹没区）→ 报告
- 3. 滑坡场景：坡度分级矢量 slope_class=3（>30°极陡）→ 滑坡风险区 → 滑坡覆盖（官方 39 点对滑坡风险区）→ 报告
- 验证：各链 success/status=ok、数值合理（0-100% 非 0 非 NaN）、独立抽检 ≤1%、一致性 gap≈100-coverage、
     滑坡风险区几何 valid 100%
- 产出：output/重庆跑通/（淹没区/滑坡风险区 gpkg + 结果表 + 报告副本 + run_record.json + SOURCE.md）
- 红线：无 git 写；不改引擎/guards/模板/CRS/注册表/纠偏/现有代码；零新依赖

运行：qgis-portable\apps\Python312\python.exe scripts\run_chongqing.py
"""
import os
import sys
import json
import time
import contextlib
import traceback
import tempfile as _tmpfile
import shutil

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
sys.path.insert(0, os.path.join(OSGEO4W_ROOT, "apps", "Python312", "Lib", "site-packages"))
sys.path.append(os.path.join(OSGEO4W_ROOT, "apps", "qgis-ltr", "python", "plugins"))
for _d in [os.path.join(OSGEO4W_ROOT, "apps", "qgis-ltr", "bin"), os.path.join(OSGEO4W_ROOT, "bin"), os.path.join(OSGEO4W_ROOT, "apps", "Qt5", "bin")]:
    if hasattr(os, "add_dll_directory"):
        os.add_dll_directory(_d)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

DATA_DIR = r"D:\桌面\项目测试数据\中国\重庆"
OUT_DIR = os.path.join(PROJECT_ROOT, "output", "重庆跑通")
TEMP_DIR = os.path.join(PROJECT_ROOT, "temp", "chongqing")
os.makedirs(OUT_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

SHELTER_ALL = os.path.join(DATA_DIR, "避难所_3857.gpkg")            # 1619 点（官方39 + OSM载体1580）
SHELTER_OFFICIAL = os.path.join(TEMP_DIR, "避难所_官方39_3857.gpkg")  # 官方 39 点（dazu_longgang 24 + kz_natural 15）
BOUNDARY = os.path.join(DATA_DIR, "行政区_3857.gpkg")                # 重庆 1 面
POPULATION = os.path.join(DATA_DIR, "人口_3857.gpkg")                # 110271 cells
RIVER = os.path.join(DATA_DIR, "河流_3857.gpkg")                     # 4364 线
SLOPE = os.path.join(DATA_DIR, "坡度_3857.gpkg")                     # 7139 面（slope_class 1/2/3）
INUND_PATH = os.path.join(OUT_DIR, "淹没区_3857.gpkg")               # 洪涝危险区（河流buffer 300m）
LANDSLIDE_PATH = os.path.join(OUT_DIR, "滑坡风险区_3857.gpkg")        # 滑坡危险区（slope_class=3 >30°）

RADIUS = 500.0
RIVER_BUFFER_M = 300.0
SLOPE_CLASS_RISK = 3  # slope_class=3 → >30° 极陡

LOG_PATH = os.path.join(PROJECT_ROOT, "temp", "run_chongqing_log.txt")
_log_lines = []


def say(msg):
    print(msg, flush=True)
    _log_lines.append(str(msg))


# ────────────────────────────── 数据准备 ──────────────────────────────

def gen_official_shelters():
    """从全量避难所中过滤官方 39 点（source in dazu_longgang/kz_natural）。"""
    from osgeo import ogr
    ds = ogr.Open(SHELTER_ALL)
    lyr = ds.GetLayer(0)
    srs = lyr.GetSpatialRef()
    ldefn = lyr.GetLayerDefn()
    fields = [ldefn.GetFieldDefn(i).GetName() for i in range(ldefn.GetFieldCount())]
    drv = ogr.GetDriverByName("GPKG")
    if os.path.exists(SHELTER_OFFICIAL):
        drv.DeleteDataSource(SHELTER_OFFICIAL)
    out_ds = drv.CreateDataSource(SHELTER_OFFICIAL)
    out_lyr = out_ds.CreateLayer("避难所_官方39", geom_type=lyr.GetGeomType(), srs=srs)
    for f in fields:
        out_lyr.CreateField(ogr.FieldDefn(f, ldefn.GetFieldDefn(fields.index(f)).GetType()))
    n = 0
    for ft in lyr:
        src = str(ft.GetField("source") or "")
        if src in ("dazu_longgang", "kz_natural"):
            out_lyr.CreateFeature(ft.Clone())
            n += 1
    out_ds = None
    ds = None
    assert n == 39, f"官方避难所数量断言失败: {n} != 39"
    say(f"[数据] 官方 39 点 -> {SHELTER_OFFICIAL}（n={n}）")
    return n


def gen_inundation():
    """重庆河流 buffer 300m → 洪涝淹没区近似（Shapely，几何 valid 断言）。"""
    from osgeo import ogr
    from shapely.geometry import shape
    from shapely.ops import unary_union

    ds = ogr.Open(RIVER)
    lyr = ds.GetLayer(0)
    n = lyr.GetFeatureCount()
    srs_ref = lyr.GetSpatialRef()
    geoms = []
    for ft in lyr:
        g = ft.GetGeometryRef()
        if g is None:
            continue
        s = shape(json.loads(g.ExportToJson()))
        if s.is_empty:
            continue
        if s.geom_type not in ("LineString", "MultiLineString", "Polygon", "MultiPolygon"):
            continue
        geoms.append(s)
    ds = None
    say(f"[数据] 河流_3857.gpkg: {n} 要素, 有效几何 {len(geoms)}")

    buf_geoms = []
    for i, g in enumerate(geoms):
        b = g.buffer(RIVER_BUFFER_M, quad_segs=5)
        if not b.is_valid:
            b = b.buffer(0)
        assert b.is_valid, f"淹没区几何 valid 断言失败 idx={i}"
        buf_geoms.append(b)
    union = unary_union(buf_geoms)
    assert union.is_valid, "淹没区 union 结果 invalid"
    say(f"[生成] 淹没区 union: {union.geom_type}, area={union.area:.0f} m², valid={union.is_valid}")

    drv = ogr.GetDriverByName("GPKG")
    if os.path.exists(INUND_PATH):
        drv.DeleteDataSource(INUND_PATH)
    out_ds = drv.CreateDataSource(INUND_PATH)
    out_lyr = out_ds.CreateLayer("淹没区", geom_type=ogr.wkbMultiPolygon, srs=srs_ref)
    fld = ogr.FieldDefn("source", ogr.OFTString)
    out_lyr.CreateField(fld)
    feat = ogr.Feature(out_lyr.GetLayerDefn())
    feat.SetField("source", f"重庆河流buffer近似({RIVER_BUFFER_M}m)")
    feat.SetGeometry(ogr.CreateGeometryFromJson(json.dumps(union.__geo_interface__)))
    out_lyr.CreateFeature(feat)
    out_ds = None
    say(f"[产出] 淹没区 -> {INUND_PATH}")
    return union.area


def gen_landslide_zone():
    """坡度 slope_class=3（>30°极陡）→ 滑坡风险区（几何 valid 断言）。"""
    from osgeo import ogr
    ds = ogr.Open(SLOPE)
    lyr = ds.GetLayer(0)
    srs = lyr.GetSpatialRef()
    ldefn = lyr.GetLayerDefn()
    fields = [ldefn.GetFieldDefn(i).GetName() for i in range(ldefn.GetFieldCount())]
    drv = ogr.GetDriverByName("GPKG")
    if os.path.exists(LANDSLIDE_PATH):
        drv.DeleteDataSource(LANDSLIDE_PATH)
    out_ds = drv.CreateDataSource(LANDSLIDE_PATH)
    out_lyr = out_ds.CreateLayer("滑坡风险区", geom_type=lyr.GetGeomType(), srs=srs)
    for f in fields:
        out_lyr.CreateField(ogr.FieldDefn(f, ldefn.GetFieldDefn(fields.index(f)).GetType()))
    n = 0
    total_area = 0.0
    for ft in lyr:
        if int(ft.GetField("slope_class") or 0) != SLOPE_CLASS_RISK:
            continue
        g = ft.GetGeometryRef()
        assert g is not None and g.IsValid(), f"滑坡风险区几何 invalid (idx={n})"
        total_area += g.GetArea()
        out_lyr.CreateFeature(ft.Clone())
        n += 1
    out_ds = None
    ds = None
    say(f"[数据] 滑坡风险区（slope_class={SLOPE_CLASS_RISK} >30°极陡）: {n} 面, area={total_area:.0f} m²")
    say(f"[产出] 滑坡风险区 -> {LANDSLIDE_PATH}")
    return {"count": n, "area": total_area}


def audit_coverage(shelter_path, boundary_path, radius=500.0, quad_segs=5):
    """独立 OGR+Shapely 复算覆盖面积（与引擎模板 SEGMENTS=5 口径一致）。

    注意：boundary 图层可能为多要素（如滑坡风险区 1156 面），必须全部要素 union。
    """
    from osgeo import ogr
    from shapely.geometry import Point
    from shapely.wkt import loads as wkt_loads
    from shapely.ops import unary_union

    ds = ogr.Open(shelter_path)
    lyr = ds.GetLayer(0)
    pts = [(ft.GetGeometryRef().GetX(), ft.GetGeometryRef().GetY()) for ft in lyr]
    ds = None
    ds2 = ogr.Open(boundary_path)
    lyr2 = ds2.GetLayer(0)
    bnd_geoms = [wkt_loads(ft.GetGeometryRef().ExportToWkt()) for ft in lyr2 if ft.GetGeometryRef() is not None]
    ds2 = None
    boundary = unary_union(bnd_geoms) if len(bnd_geoms) > 1 else bnd_geoms[0]
    union = unary_union([Point(x, y).buffer(radius, quad_segs=quad_segs) for x, y in pts])
    cov = union.intersection(boundary)
    return {"area": cov.area, "points": len(pts)}


# ────────────────────────────── 主流程 ──────────────────────────────

def main():
    say("========================================")
    say("重庆跑通：多灾种真实数据（覆盖/洪涝/滑坡）（Solo APPROVED）")
    say("========================================")

    # ── 预置 QGIS srs.db 临时副本（防 qFatal 崩溃）──
    _srs_src = os.path.join(OSGEO4W_ROOT, "apps", "qgis-ltr", "resources", "srs.db")
    _srs_dst = os.path.join(_tmpfile.gettempdir(), "srs6.db")
    if not os.path.exists(_srs_dst) and os.path.exists(_srs_src):
        try:
            shutil.copy(_srs_src, _srs_dst)
            say(f"[预置] srs6.db -> {_srs_dst}")
        except Exception as _e:
            say(f"[警告] srs.db 预置失败: {_e}")

    # 数据准备（不依赖 QGIS）
    gen_official_shelters()
    inund_area = gen_inundation()
    landslide_info = gen_landslide_zone()

    from qgis.core import QgsApplication, QgsVectorLayer, QgsProject
    from qgis.gui import QgsMapCanvas
    QgsApplication.setPrefixPath(os.path.join(OSGEO4W_ROOT, "apps", "qgis-ltr"), True)
    qgs = QgsApplication([], False)
    qgs.initQgis()

    from core.qgis_env import initialize_processing
    initialize_processing(qgs)
    say("QGIS 引擎初始化完成")

    project = QgsProject.instance()
    canvas = QgsMapCanvas()

    def _find_layer(proj, name):
        for lyr in proj.mapLayers().values():
            if lyr.name() == name:
                return lyr
        return None

    from core.pipeline_executor import PipelineExecutor
    executor = PipelineExecutor()
    TPL = os.path.join(PROJECT_ROOT, "src", "core", "templates")

    def load_layers(project, layer_specs):
        project.removeAllMapLayers()
        loaded = {}
        for path, name in layer_specs:
            lyr = QgsVectorLayer(path, name, "ogr")
            if not lyr.isValid():
                say(f"[数据] 加载失败 {name}: {path}")
                return None
            project.addMapLayer(lyr)
            loaded[name] = {"count": lyr.featureCount(), "geom": lyr.geometryType(), "crs": lyr.crs().authid()}
            say(f"[数据] {name}: {lyr.featureCount()} 要素, geom_type={lyr.geometryType()}, CRS={lyr.crs().authid()}")
        return loaded

    def run_chain(action, template_file, **kwargs):
        say("")
        say(f"── [{action}] 模板 {template_file} ──")
        t0 = time.time()
        res = executor.execute(
            template_path=os.path.join(TPL, template_file),
            project=project,
            canvas=canvas,
            _find_layer_fn=lambda name: _find_layer(project, name),
            **kwargs,
        )
        dt = round(time.time() - t0, 1)
        ok = bool(res.get("success"))
        stats = res.get("stats") or {}
        say(f"[{action}] 成功={ok} status={res.get('status')} {dt}s")
        say(f"[{action}] stats={json.dumps(stats, ensure_ascii=False)}")
        if res.get("message"):
            say(f"[{action}] message={str(res['message'])[:300]}")
        return ok, stats, res

    def make_report(disaster_id, stats, src, bnd, user_text):
        from core.report_generator import generate_report
        report = generate_report(
            disaster_id, stats,
            source_layer=src,
            boundary_layer=bnd,
            lang="zh",
            user_text=user_text,
        )
        say(f"[报告] success={report.get('success')} path={report.get('report_path')} "
            f"disaster={report.get('disaster_name')} 覆盖率={report.get('coverage_rate')} 预警={report.get('warning') or '无'}")
        return report

    results = {"steps": [], "reports": [], "audits": [], "consistency": []}

    def step(chain_name, ok, stats, res, extra=None):
        entry = {"chain": chain_name, "success": ok, "status": res.get("status"),
                 "stats": stats, "message": str(res.get("message") or "")[:300]}
        if extra:
            entry.update(extra)
        results["steps"].append(entry)
        return entry

    # ════════════ 一、通用覆盖链（双口径） ════════════
    for caliber, shelter_path, shelter_name in (
        ("A_官方39", SHELTER_OFFICIAL, "避难所_官方39_3857"),
        ("B_全量1619", SHELTER_ALL, "避难所_3857"),
    ):
        say("")
        say("=" * 40)
        say(f"通用覆盖链 口径 {caliber} 开始")
        say("=" * 40)
        loaded = load_layers(project, [
            (shelter_path, shelter_name),
            (BOUNDARY, "行政区_3857"),
            (POPULATION, "人口_3857"),
        ])
        if loaded is None:
            return 2

        ok1, stats1, res1 = run_chain(
            "coverage", "risk_zone_coverage.json",
            source_layer_name=shelter_name,
            boundary_layer_name="行政区_3857",
            radius_m=RADIUS,
        )
        step(f"通用覆盖_{caliber}_coverage", ok1, stats1, res1)

        ok2, stats2, res2 = run_chain(
            "gap", "gap_analysis.json",
            source_layer_name=shelter_name,
            boundary_layer_name="行政区_3857",
            radius_m=RADIUS,
        )
        step(f"通用覆盖_{caliber}_gap", ok2, stats2, res2)

        ok3, stats3, res3 = run_chain(
            "population_coverage", "population_coverage.json",
            source_layer_name=shelter_name,
            boundary_layer_name="行政区_3857",
            population_layer_name="人口_3857",
            population_field="population",
            radius_m=RADIUS,
        )
        step(f"通用覆盖_{caliber}_population", ok3, stats3, res3)

        # 报告（coverage 链）
        report = None
        if ok1:
            report = make_report(
                "chongqing", stats1,
                shelter_name, "行政区_3857",
                f"重庆跑通 通用覆盖链 口径{caliber}：计算避难所500米缓冲对重庆行政区的覆盖率",
            )
            if report.get("success"):
                dst = os.path.join(OUT_DIR, f"报告_通用覆盖{caliber}.md")
                shutil.copy(report["report_path"], dst)
                say(f"[报告] 副本 -> {dst}")
                results["reports"].append({"chain": f"通用覆盖_{caliber}", "path": dst,
                                           "coverage_rate": report.get("coverage_rate"),
                                           "warning": report.get("warning") or ""})

        # 独立抽检（coverage 链）
        audit = {"done": False, "pass": False, "detail": ""}
        try:
            a = audit_coverage(shelter_path, BOUNDARY, RADIUS)
            engine_area = float(stats1.get("covered_area", 0) or 0) if ok1 else 0
            diff_pct = abs(a["area"] - engine_area) / max(a["area"], 1e-9) * 100.0
            audit["done"] = True
            audit["pass"] = diff_pct <= 1.0
            audit["detail"] = (f"独立复算覆盖面积={a['area']:.0f} m² vs 引擎={engine_area:.0f} m²，"
                               f"相对偏差={diff_pct:.4f}%（容差 ±1%），判定={'PASS' if audit['pass'] else 'FAIL'}")
            say(f"[抽检] {audit['detail']}")
        except Exception as e:
            audit["detail"] = f"抽检异常: {type(e).__name__}: {e}"
            say(f"[抽检] {audit['detail']}")
        results["audits"].append({"chain": f"通用覆盖_{caliber}", **audit})

        # 一致性：gap_rate ≈ 100 - coverage_rate
        if ok1 and ok2:
            cr = float(stats1.get("coverage_rate", 0))
            gr = float(stats2.get("gap_rate", 0))
            diff = abs(gr - (100.0 - cr))
            consist = {"chain": f"通用覆盖_{caliber}_一致性", "success": diff <= 1.0,
                       "detail": f"gap_rate={gr:.4f}% vs 100-coverage_rate={100.0-cr:.4f}%，偏差={diff:.4f}%（容差 ±1%）"}
            say(f"[一致性] {consist['detail']} 判定={'PASS' if consist['success'] else 'FAIL'}")
            results["consistency"].append(consist)
        elif not ok1:
            say("[一致性] coverage 失败，跳过一致性校验")

    # ════════════ 二、洪涝场景 ════════════
    say("")
    say("=" * 40)
    say("洪涝场景（河流 buffer 300m 淹没区近似）开始")
    say("=" * 40)
    loaded = load_layers(project, [
        (SHELTER_OFFICIAL, "避难所_官方39_3857"),
        (INUND_PATH, "淹没区_3857"),
    ])
    if loaded is None:
        return 2

    ok_f, stats_f, res_f = run_chain(
        "flood_coverage", "risk_zone_coverage.json",
        source_layer_name="避难所_官方39_3857",
        boundary_layer_name="淹没区_3857",
        radius_m=RADIUS,
    )
    step("洪涝_coverage", ok_f, stats_f, res_f)
    report_f = None
    if ok_f:
        report_f = make_report(
            "flood", stats_f,
            "避难所_官方39_3857", "淹没区_3857",
            "重庆跑通 洪涝场景：计算避难所500米缓冲对河流buffer淹没区的覆盖率",
        )
        if report_f.get("success"):
            dst = os.path.join(OUT_DIR, "报告_洪涝.md")
            shutil.copy(report_f["report_path"], dst)
            say(f"[报告] 副本 -> {dst}")
            results["reports"].append({"chain": "洪涝", "path": dst,
                                       "coverage_rate": report_f.get("coverage_rate"),
                                       "warning": report_f.get("warning") or ""})
    audit_f = {"done": False, "pass": False, "detail": ""}
    try:
        a = audit_coverage(SHELTER_OFFICIAL, INUND_PATH, RADIUS)
        engine_area = float(stats_f.get("covered_area", 0) or 0) if ok_f else 0
        diff_pct = abs(a["area"] - engine_area) / max(a["area"], 1e-9) * 100.0
        audit_f["done"] = True
        audit_f["pass"] = diff_pct <= 1.0
        audit_f["detail"] = (f"独立复算覆盖面积={a['area']:.0f} m² vs 引擎={engine_area:.0f} m²，"
                             f"相对偏差={diff_pct:.4f}%（容差 ±1%），判定={'PASS' if audit_f['pass'] else 'FAIL'}")
        say(f"[抽检] {audit_f['detail']}")
    except Exception as e:
        audit_f["detail"] = f"抽检异常: {type(e).__name__}: {e}"
        say(f"[抽检] {audit_f['detail']}")
    results["audits"].append({"chain": "洪涝", **audit_f})
    if ok_f and float(stats_f.get("coverage_rate", 0) or 0) <= 0.0:
        say("[裁决] 洪涝覆盖率=0：河流 buffer 淹没区与避难所 500m 缓冲无交集，按批复如实报告不擅改")

    # ════════════ 三、滑坡场景 ════════════
    say("")
    say("=" * 40)
    say("滑坡场景（slope_class=3 >30°极陡）开始")
    say("=" * 40)
    loaded = load_layers(project, [
        (SHELTER_OFFICIAL, "避难所_官方39_3857"),
        (LANDSLIDE_PATH, "滑坡风险区_3857"),
    ])
    if loaded is None:
        return 2

    ok_l, stats_l, res_l = run_chain(
        "landslide_coverage", "risk_zone_coverage.json",
        source_layer_name="避难所_官方39_3857",
        boundary_layer_name="滑坡风险区_3857",
        radius_m=RADIUS,
    )
    step("滑坡_coverage", ok_l, stats_l, res_l)
    report_l = None
    if ok_l:
        report_l = make_report(
            "landslide", stats_l,
            "避难所_官方39_3857", "滑坡风险区_3857",
            "重庆跑通 滑坡场景：计算避难所500米缓冲对>30°极陡滑坡风险区的覆盖率",
        )
        if report_l.get("success"):
            dst = os.path.join(OUT_DIR, "报告_滑坡.md")
            shutil.copy(report_l["report_path"], dst)
            say(f"[报告] 副本 -> {dst}")
            results["reports"].append({"chain": "滑坡", "path": dst,
                                       "coverage_rate": report_l.get("coverage_rate"),
                                       "warning": report_l.get("warning") or ""})
    audit_l = {"done": False, "pass": False, "detail": ""}
    try:
        a = audit_coverage(SHELTER_OFFICIAL, LANDSLIDE_PATH, RADIUS)
        engine_area = float(stats_l.get("covered_area", 0) or 0) if ok_l else 0
        diff_pct = abs(a["area"] - engine_area) / max(a["area"], 1e-9) * 100.0
        audit_l["done"] = True
        audit_l["pass"] = diff_pct <= 1.0
        audit_l["detail"] = (f"独立复算覆盖面积={a['area']:.0f} m² vs 引擎={engine_area:.0f} m²，"
                             f"相对偏差={diff_pct:.4f}%（容差 ±1%），判定={'PASS' if audit_l['pass'] else 'FAIL'}")
        say(f"[抽检] {audit_l['detail']}")
    except Exception as e:
        audit_l["detail"] = f"抽检异常: {type(e).__name__}: {e}"
        say(f"[抽检] {audit_l['detail']}")
    results["audits"].append({"chain": "滑坡", **audit_l})
    if ok_l and float(stats_l.get("coverage_rate", 0) or 0) <= 0.0:
        say("[裁决] 滑坡覆盖率=0：滑坡风险区与避难所 500m 缓冲无交集，按批复如实报告不擅改")

    # ════════════ 汇总落盘 ════════════
    def _num(stats, key, nd=4):
        v = stats.get(key)
        return None if v is None else (f"{float(v):.{nd}f}" if isinstance(v, (int, float)) else str(v))

    st_a = results["steps"][0]["stats"]
    st_g = results["steps"][1]["stats"]
    st_p = results["steps"][2]["stats"]
    st_b = results["steps"][3]["stats"]
    st_bg = results["steps"][4]["stats"]
    st_bp = results["steps"][5]["stats"]

    table_path = os.path.join(OUT_DIR, "重庆跑通结果表.md")
    with open(table_path, "w", encoding="utf-8") as f:
        f.write("# 重庆跑通：多灾种真实数据结果表（Solo 批复）\n\n")
        f.write(f"- 数据目录：`{DATA_DIR}`\n")
        f.write(f"- 半径：{RADIUS}m；引擎：PipelineExecutor（risk_zone_coverage / gap_analysis / population_coverage 模板）\n")
        f.write("- 口径 A：官方 39 点（dazu_longgang 24 + kz_natural 15，开州/大足政府公示表）\n")
        f.write("- 口径 B：全量 1619 点（39 官方 + 1580 OSM 学校/公园/运动场潜在载体）\n")
        f.write("- 洪涝：河流 buffer 300m 淹没区近似（source=重庆河流buffer近似）\n")
        f.write("- 滑坡：坡度 slope_class=3（>30°极陡）风险区\n\n")

        f.write("## 一、通用覆盖链（双口径）\n\n")
        f.write("| 指标 | 口径 A（官方 39 点） | 口径 B（全量 1619 点） | 说明 |\n")
        f.write("|---|---|---|---|\n")
        f.write(f"| 要素数 | 39 | 1619 | +1580 OSM 潜在载体 |\n")
        f.write(f"| 覆盖率 | {_num(st_a,'coverage_rate')}% | {_num(st_b,'coverage_rate')}% | 口径 B 应显著更高 |\n")
        f.write(f"| 覆盖面积 (m²) | {_num(st_a,'covered_area',0)} | {_num(st_b,'covered_area',0)} | |\n")
        f.write(f"| 行政区总面积 (m²) | {_num(st_a,'total_area',0)} | {_num(st_b,'total_area',0)} | |\n")
        f.write(f"| 盲区率 | {_num(st_g,'gap_rate')}% | {_num(st_bg,'gap_rate')}% | |\n")
        f.write(f"| 人口覆盖率 | {_num(st_p,'pop_coverage_rate')}% | {_num(st_bp,'pop_coverage_rate')}% | |\n")
        f.write(f"| 覆盖人口 | {_num(st_p,'covered_population',0)} | {_num(st_bp,'covered_population',0)} | |\n")
        f.write(f"| 总人口 | {_num(st_p,'total_population',0)} | {_num(st_bp,'total_population',0)} | |\n")

        f.write("\n## 二、洪涝场景（官方 39 点对河流 buffer 淹没区）\n\n")
        if ok_f:
            f.write(f"- 淹没区面积（河流buffer {RIVER_BUFFER_M}m 近似）：{inund_area:.0f} m²\n")
            f.write(f"- 洪涝覆盖率：{_num(stats_f,'coverage_rate')}%（covered {_num(stats_f,'covered_area',0)} / total {_num(stats_f,'total_area',0)} m²）\n")
        else:
            f.write("- 洪涝链失败：见 run_record\n")

        f.write("\n## 三、滑坡场景（官方 39 点对 >30°极陡风险区）\n\n")
        if ok_l:
            f.write(f"- 滑坡风险区：{landslide_info['count']} 面（slope_class=3 >30°极陡），面积 {landslide_info['area']:.0f} m²\n")
            f.write(f"- 滑坡覆盖率：{_num(stats_l,'coverage_rate')}%（covered {_num(stats_l,'covered_area',0)} / total {_num(stats_l,'total_area',0)} m²）\n")
        else:
            f.write("- 滑坡链失败：见 run_record\n")

        f.write("\n## 四、独立抽检（OGR+Shapely 复算覆盖面积，容差 ±1%）\n\n")
        for aud in results["audits"]:
            f.write(f"- {aud['chain']}：{aud['detail']}（判定 {'PASS' if aud['pass'] else 'FAIL'}）\n")

        f.write("\n## 五、一致性校验（gap_rate ≈ 100 - coverage_rate，容差 ±1%）\n\n")
        for st in results["consistency"]:
            f.write(f"- {st['chain']}：{st['detail']}\n")

        f.write("\n## 六、报告与预警\n\n")
        for rp in results["reports"]:
            f.write(f"- {rp['chain']}：`{rp['path']}`（覆盖率 {rp['coverage_rate']}%，预警：{rp['warning'] or '未触发'}）\n")

        f.write("\n## 七、最终判定\n\n")
        all_ok = all(s["success"] for s in results["steps"])
        consist_ok = all(c["success"] for c in results["consistency"])
        audit_ok = all(a["pass"] for a in results["audits"])
        f.write(f"- 分析链：{'ALL SUCCESS' if all_ok else '存在 FAIL'}\n")
        f.write(f"- 一致性：{'PASS' if consist_ok else 'FAIL/跳过'}\n")
        f.write(f"- 独立抽检：{'PASS' if audit_ok else 'FAIL'}\n")
        final = all_ok and consist_ok and audit_ok
        f.write(f"- 总体判定：{'ALL PASS' if final else '存在 FAIL'}\n")
    say(f"[产出] 结果表 -> {table_path}")

    # SOURCE.md（淹没区/滑坡风险区说明）
    with open(os.path.join(OUT_DIR, "SOURCE.md"), "w", encoding="utf-8") as f:
        f.write("# 重庆跑通数据说明（Solo 批复）\n\n")
        f.write(f"- 避难所官方39点：`{SHELTER_OFFICIAL}`（source in dazu_longgang/kz_natural，开州/大足政府公示表）\n")
        f.write(f"- 淹没区：`{INUND_PATH}`（重庆河流 buffer {RIVER_BUFFER_M}m 近似，Shapely quad_segs=5，几何 valid 断言）\n")
        f.write(f"- 滑坡风险区：`{LANDSLIDE_PATH}`（坡度 slope_class={SLOPE_CLASS_RISK} >30°极陡，{landslide_info['count']} 面）\n")
        f.write("- 注意：淹没区/滑坡风险区均为近似示意，非水文/地质模型结果，仅用于覆盖分析演示\n")

    run_record = {
        "task": "重庆跑通：多灾种真实数据（覆盖/洪涝/滑坡）",
        "date": time.strftime("%Y-%m-%d %H:%M:%S"),
        "data_dir": DATA_DIR,
        "out_dir": OUT_DIR,
        "radius_m": RADIUS,
        "river_buffer_m": RIVER_BUFFER_M,
        "slope_class_risk": SLOPE_CLASS_RISK,
        "calibers": {
            "A": {"desc": "官方 39 点（dazu_longgang 24 + kz_natural 15）", "path": SHELTER_OFFICIAL},
            "B": {"desc": "全量 1619 点（39 官方 + 1580 OSM 潜在载体）", "path": SHELTER_ALL},
        },
        "inundation_area_m2": inund_area,
        "landslide": landslide_info,
        "results": results,
        "engine_change": "none",
        "registry_change": "none",
        "all_pass": final,
    }
    record_path = os.path.join(OUT_DIR, "run_record.json")
    with open(record_path, "w", encoding="utf-8") as f:
        json.dump(run_record, f, ensure_ascii=False, indent=2)
    say(f"[产出] run 记录 -> {record_path}")

    with open(LOG_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(_log_lines))
    say(f"[产出] 运行日志 -> {LOG_PATH}")

    say("")
    say("========================================")
    say("重庆跑通完成")
    say(f"- 口径 A（官方 39 点）：覆盖率 {_num(st_a,'coverage_rate')}% / 盲区率 {_num(st_g,'gap_rate')}% / 人口覆盖率 {_num(st_p,'pop_coverage_rate')}%")
    say(f"- 口径 B（全量 1619 点）：覆盖率 {_num(st_b,'coverage_rate')}% / 盲区率 {_num(st_bg,'gap_rate')}% / 人口覆盖率 {_num(st_bp,'pop_coverage_rate')}%")
    if ok_f:
        say(f"- 洪涝：覆盖率 {_num(stats_f,'coverage_rate')}%")
    if ok_l:
        say(f"- 滑坡：覆盖率 {_num(stats_l,'coverage_rate')}%")
    say(f"- 独立抽检：{'ALL PASS' if audit_ok else '存在 FAIL'}")
    say(f"- 总体判定：{'ALL PASS' if final else '存在 FAIL'}")
    say("========================================")
    qgs.exitQgis()
    say("RUN_CHONGQING_DONE")
    return 0 if final else 1


if __name__ == "__main__":
    try:
        with open(LOG_PATH, "w", encoding="utf-8") as _f:
            with contextlib.redirect_stdout(_f), contextlib.redirect_stderr(_f):
                rc = main()
        print(f"RC={rc} LOG={LOG_PATH}")
    except Exception:
        with open(LOG_PATH, "a", encoding="utf-8") as _f:
            _f.write("\nFATAL: " + traceback.format_exc())
        print("RC=2 LOG=" + LOG_PATH)
