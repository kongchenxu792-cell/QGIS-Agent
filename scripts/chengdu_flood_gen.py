# -*- coding: utf-8 -*-
r"""temp/chengdu_flood_gen.py — 成都真实洪涝场景（Solo APPROVED）

Solo 批复「成都真实洪涝场景 + README 多灾种如实标注」：
- 用成都河流水系（河流_3857.gpkg，5122 条）生成洪涝淹没区近似：
  河流 buffer（300m，Shapely，几何 valid 断言）→ 作为 flood 危险区图层
- 输出 temp/chengdu_flood/淹没区_3857.gpkg（source=河流buffer近似）
- 跑洪涝覆盖：避难所（37 点官方口径A）500m 缓冲对淹没区覆盖率 + 报告预警
- 验证：success/status=ok、数值合理、独立抽检 ≤1%、报告生成
- 若覆盖率 0：如实报告并报 Solo 裁决，不擅自改（本脚本仅在覆盖率>0时出 PASS）

红线遵守：无 git 写；不改引擎/guards/模板/CRS/注册表/纠偏逻辑/现有代码；零新依赖；
仅新增脚本 + 数据 + 文档（Solo 授权范围内）。

运行：qgis-portable\apps\Python312\python.exe temp\chengdu_flood_gen.py
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

DATA_DIR = r"D:\桌面\项目测试数据\中国\成都"
FLOOD_DIR = os.path.join(PROJECT_ROOT, "output", "成都洪涝")
os.makedirs(FLOOD_DIR, exist_ok=True)
RIVER_PATH = os.path.join(DATA_DIR, "河流_3857.gpkg")
INUND_PATH = os.path.join(FLOOD_DIR, "淹没区_3857.gpkg")
SHELTER_A = os.path.join(DATA_DIR, "避难所_3857_gpkg_37pt_backup.gpkg")  # 37 点官方口径 A
BOUNDARY_A = os.path.join(DATA_DIR, "行政区_3857.gpkg")
RADIUS = 500.0
BUFFER_M = 300.0  # 河流 buffer 近似淹没区宽度（批复 200-500m 范围取中间值）

LOG_PATH = os.path.join(PROJECT_ROOT, "temp", "run_chengdu_flood_log.txt")
_log_lines = []


def say(msg):
    print(msg, flush=True)
    _log_lines.append(str(msg))


def gen_inundation():
    """河流 buffer → 淹没区面图层（Shapely，几何 valid 断言）。"""
    from osgeo import ogr
    from shapely.geometry import shape
    from shapely.ops import unary_union

    ds = ogr.Open(RIVER_PATH)
    lyr = ds.GetLayer(0)
    n = lyr.GetFeatureCount()
    srs_ref = lyr.GetSpatialRef()
    say(f"[数据] 河流_3857.gpkg: {n} 要素, geom_type={lyr.GetGeomType()}, CRS={srs_ref.ExportToWkt()[:60]}")
    geoms = []
    for ft in lyr:
        g = ft.GetGeometryRef()
        if g is None:
            continue
        s = shape(json.loads(g.ExportToJson()))
        if s.is_empty:
            continue
        if s.geom_type not in ("LineString", "MultiLineString", "Polygon", "MultiPolygon"):
            # 点状河流要素（极少）跳过
            continue
        geoms.append(s)
    ds = None
    say(f"[生成] 有效几何 {len(geoms)}/{n} 条")

    # 河流 buffer 近似淹没区
    buf_geoms = []
    for i, g in enumerate(geoms):
        b = g.buffer(BUFFER_M, quad_segs=5)
        if not b.is_valid:
            b = b.buffer(0)  # 自相交修复
        assert b.is_valid, f"几何 valid 断言失败 idx={i}"
        buf_geoms.append(b)
    union = unary_union(buf_geoms)
    assert union.is_valid, "union 结果 invalid"
    say(f"[生成] 淹没区 union: {union.geom_type}, area={union.area:.0f} m², valid={union.is_valid}")

    # 写 gpkg
    drv = ogr.GetDriverByName("GPKG")
    if os.path.exists(INUND_PATH):
        drv.DeleteDataSource(INUND_PATH)
    out_ds = drv.CreateDataSource(INUND_PATH)
    out_lyr = out_ds.CreateLayer("淹没区", geom_type=ogr.wkbMultiPolygon, srs=srs_ref)
    fld = ogr.FieldDefn("source", ogr.OFTString)
    out_lyr.CreateField(fld)
    feat = ogr.Feature(out_lyr.GetLayerDefn())
    feat.SetField("source", "河流buffer近似(300m)")
    feat.SetGeometry(ogr.CreateGeometryFromJson(json.dumps(union.__geo_interface__)))
    out_lyr.CreateFeature(feat)
    out_ds = None
    say(f"[产出] 淹没区 -> {INUND_PATH} (source=河流buffer近似)")

    # source 说明
    with open(os.path.join(FLOOD_DIR, "SOURCE.md"), "w", encoding="utf-8") as f:
        f.write("# 洪涝淹没区数据说明（Solo 批复）\n\n")
        f.write(f"- 数据源：`{RIVER_PATH}`（成都河流水系，{n} 条，EPSG:3857）\n")
        f.write(f"- 生成方式：河流 buffer {BUFFER_M}m（Shapely，quad_segs=5，几何 valid 断言）→ unary_union\n")
        f.write(f"- 输出：`{INUND_PATH}`（source=河流buffer近似）\n")
        f.write("- 用途：作为洪涝危险区图层（flood risk zone），评估避难所 500m 缓冲对淹没区覆盖率\n")
        f.write("- 注意：该淹没区为近似示意，非水文模型结果，仅用于覆盖分析演示\n")
    say(f"[产出] SOURCE.md -> {os.path.join(FLOOD_DIR, 'SOURCE.md')}")
    return union.area


def audit_flood_coverage(shelter_path, inund_path, radius=500.0, quad_segs=5):
    """独立 OGR+Shapely 复算覆盖面积（与引擎模板口径一致）。"""
    from osgeo import ogr
    from shapely.geometry import Point
    from shapely.wkt import loads as wkt_loads
    from shapely.ops import unary_union

    ds = ogr.Open(shelter_path)
    lyr = ds.GetLayer(0)
    pts = [(ft.GetGeometryRef().GetX(), ft.GetGeometryRef().GetY()) for ft in lyr]
    ds = None
    ds2 = ogr.Open(inund_path)
    lyr2 = ds2.GetLayer(0)
    ft2 = lyr2.GetNextFeature()
    inund = wkt_loads(ft2.GetGeometryRef().ExportToWkt())
    ds2 = None
    union = unary_union([Point(x, y).buffer(radius, quad_segs=quad_segs) for x, y in pts])
    cov = union.intersection(inund)
    return {"area": cov.area, "inund_area": inund.area, "points": len(pts)}


def main():
    say("========================================")
    say("成都真实洪涝场景（Solo APPROVED）")
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

    from qgis.core import QgsApplication, QgsVectorLayer, QgsProject
    from qgis.gui import QgsMapCanvas
    QgsApplication.setPrefixPath(os.path.join(OSGEO4W_ROOT, "apps", "qgis-ltr"), True)
    qgs = QgsApplication([], False)
    qgs.initQgis()

    from core.qgis_env import initialize_processing
    initialize_processing(qgs)
    say("QGIS 引擎初始化完成")

    # 1) 生成淹没区
    inund_area = gen_inundation()

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

    project.removeAllMapLayers()
    LAYERS = [
        (SHELTER_A, "避难所_3857"),
        (INUND_PATH, "淹没区_3857"),
    ]
    for path, name in LAYERS:
        lyr = QgsVectorLayer(path, name, "ogr")
        if not lyr.isValid():
            say(f"[数据] 加载失败 {name}: {path}")
            return 2
        project.addMapLayer(lyr)
        say(f"[数据] {name}: {lyr.featureCount()} 要素, geom_type={lyr.geometryType()}, CRS={lyr.crs().authid()}")

    # 2) 洪涝覆盖链（risk_zone_coverage：源=避难所点，boundary=淹没区面）
    say("")
    say(f"── [洪涝覆盖] risk_zone_coverage 模板，radius={RADIUS}m ──")
    t0 = time.time()
    res = executor.execute(
        template_path=os.path.join(TPL, "risk_zone_coverage.json"),
        project=project,
        canvas=canvas,
        _find_layer_fn=lambda name: _find_layer(project, name),
        source_layer_name="避难所_3857",
        boundary_layer_name="淹没区_3857",
        radius_m=RADIUS,
    )
    dt = round(time.time() - t0, 1)
    ok = bool(res.get("success"))
    stats = res.get("stats") or {}
    say(f"[洪涝覆盖] 成功={ok} status={res.get('status')} {dt}s")
    say(f"[洪涝覆盖] stats={json.dumps(stats, ensure_ascii=False)}")
    if res.get("message"):
        say(f"[洪涝覆盖] message={str(res['message'])[:300]}")
    if not ok:
        say(f"[洪涝覆盖] 失败：{res.get('error') or res.get('message')}")
        return 2

    coverage_rate = float(stats.get("coverage_rate", 0) or 0)
    covered_area = float(stats.get("covered_area", 0) or 0)
    total_area = float(stats.get("total_area", 0) or 0)
    say(f"[洪涝覆盖] 覆盖率={coverage_rate}%（covered={covered_area:.0f} / total={total_area:.0f} m²）")

    # 3) 独立抽检
    audit = {"done": False, "pass": False, "detail": ""}
    try:
        a = audit_flood_coverage(SHELTER_A, INUND_PATH, RADIUS)
        diff_pct = abs(a["area"] - covered_area) / max(a["area"], 1e-9) * 100.0
        audit["done"] = True
        audit["pass"] = diff_pct <= 1.0
        audit["detail"] = (f"独立复算覆盖面积={a['area']:.0f} m² vs 引擎={covered_area:.0f} m²，"
                           f"相对偏差={diff_pct:.4f}%（容差 ±1%），判定={'PASS' if audit['pass'] else 'FAIL'}")
        say(f"[抽检] {audit['detail']}")
    except Exception as e:
        audit["detail"] = f"抽检异常: {type(e).__name__}: {e}"
        say(f"[抽检] {audit['detail']}")

    # 4) 报告预警（coverage 链）
    report = {"success": False}
    if ok:
        from core.report_generator import generate_report
        report = generate_report(
            "chengdu", stats,
            source_layer="避难所_3857",
            boundary_layer="淹没区_3857",
            lang="zh",
            user_text="成都真实洪涝场景：计算避难所500米范围内对淹没区的覆盖率",
        )
        say(f"[报告] success={report.get('success')} path={report.get('report_path')}")
        say(f"[报告] 覆盖率={report.get('coverage_rate')} 预警={report.get('warning') or '无'}")
        if report.get("success"):
            dst = os.path.join(FLOOD_DIR, "报告_洪涝.md")
            shutil.copy(report["report_path"], dst)
            say(f"[报告] 副本 -> {dst}")

    # 5) 覆盖率 0 判定（批复：如实报告，不擅自改）
    zero_coverage = coverage_rate <= 0.0
    if zero_coverage:
        say("[裁决] 覆盖率=0：河流 buffer 淹没区与避难所 500m 缓冲无交集。")
        say("[裁决] 按 Solo 批复如实报告，未擅自扩大 buffer/调整策略。")

    all_pass = ok and audit.get("pass") and bool(report and report.get("success")) and (not zero_coverage)

    # 6) 落盘记录
    run_record = {
        "task": "成都真实洪涝场景（河流 buffer 近似淹没区）",
        "date": time.strftime("%Y-%m-%d %H:%M:%S"),
        "data_dir": DATA_DIR,
        "river_path": RIVER_PATH,
        "inundation_path": INUND_PATH,
        "inundation_source": "河流buffer近似(300m)",
        "inundation_area_m2": inund_area,
        "shelter_path": SHELTER_A,
        "radius_m": RADIUS,
        "coverage_rate": coverage_rate,
        "covered_area": covered_area,
        "total_area": total_area,
        "audit": audit,
        "report": report,
        "zero_coverage": zero_coverage,
        "engine_change": "none",
        "registry_change": "none",
        "all_pass": all_pass,
    }
    record_path = os.path.join(FLOOD_DIR, "run_record.json")
    with open(record_path, "w", encoding="utf-8") as f:
        json.dump(run_record, f, ensure_ascii=False, indent=2)
    say(f"[产出] run 记录 -> {record_path}")

    with open(LOG_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(_log_lines))
    say(f"[产出] 运行日志 -> {LOG_PATH}")

    say("")
    say("========================================")
    say("成都真实洪涝场景完成")
    say(f"- 淹没区面积（河流buffer {BUFFER_M}m 近似）：{inund_area:.0f} m²")
    say(f"- 洪涝覆盖：覆盖率 {coverage_rate}%（covered={covered_area:.0f} / total={total_area:.0f} m²）")
    say(f"- 独立抽检：{'PASS' if audit.get('pass') else 'FAIL'} {audit.get('detail','')}")
    say(f"- 报告预警：{'生成' if report.get('success') else '失败'}（{report.get('warning') or '未触发'}）")
    say(f"- 总体判定：{'ALL PASS' if all_pass else '存在 FAIL'}")
    say("========================================")
    qgs.exitQgis()
    say("RUN_CHENGDU_FLOOD_DONE")
    return 0 if all_pass else 1


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
