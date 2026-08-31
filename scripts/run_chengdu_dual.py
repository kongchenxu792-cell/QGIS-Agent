# -*- coding: utf-8 -*-
r"""scripts/run_chengdu_dual.py — 成都双口径跑通 + CEO 实测复现（Solo APPROVED）

Solo 批复「成都双口径跑通 + CEO 实测复现」：
- 口径 A（挂牌可信）：37 点官方（避难所_3857_gpkg_37pt_backup.gpkg）
- 口径 B（含潜在载体）：1284 点（避难所_3857.gpkg = 37 官方 + 1247 OSM 潜在载体）
- 每口径三链：coverage / gap_analysis / population_coverage（radius=500m）
  + 报告预警（coverage 链）+ 独立抽检（OGR+Shapely 复算，±1%）
- 字段缺失统计：1284 点 OSM 候选无 address/area，报告标注缺失率
- CEO 实测复现（关键）：加载成都 3 图层 → 发「计算避难所500米范围内的人口覆盖率」
  → 应正确识别 源=避难所(点)/边界=行政区(面)/人口=人口(面)，链成功（纠偏生效实证）
  场景1：LLM 正确识别 → 全链成功
  场景2：LLM 源填行政区(面)/边界填避难所(点)（CEO 报错原文场景）→ 几何纠偏修正 → 全链成功

红线遵守：无 git 写；不改引擎/guards/模板/CRS/注册表/纠偏逻辑/现有代码；零新依赖；
仅新增脚本（脚本/验证层，Solo 授权范围内）。

运行：qgis-portable\apps\Python312\python.exe scripts\run_chengdu_dual.py
"""
import os
import sys
import json
import time
import contextlib
import traceback
import tempfile
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
OUT_DIR = os.path.join(PROJECT_ROOT, "output", "成都双口径")
os.makedirs(OUT_DIR, exist_ok=True)

LOG_PATH = os.path.join(PROJECT_ROOT, "temp", "run_chengdu_dual_log.txt")
_log_lines = []


def say(msg):
    print(msg, flush=True)
    _log_lines.append(str(msg))


def field_missing_stats(gpkg_path):
    """统计 name/address/area 字段缺失率（OGR 只读）。"""
    from osgeo import ogr
    ds = ogr.Open(gpkg_path)
    lyr = ds.GetLayer(0)
    total = lyr.GetFeatureCount()
    cnt = {"name": 0, "address": 0, "area": 0}
    for ft in lyr:
        for f in cnt:
            v = ft.GetField(f)
            if v is None or str(v).strip() == "":
                cnt[f] += 1
    ds = None
    return {f: {"missing": cnt[f], "total": total, "rate": round(cnt[f] / total * 100.0, 2)} for f in cnt}


def audit_coverage(shelter_path, boundary_path, radius=500.0, quad_segs=5):
    """独立 OGR+Shapely 复算覆盖面积（与引擎模板 SEGMENTS=5 口径一致）。"""
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
    ft2 = lyr2.GetNextFeature()
    boundary = wkt_loads(ft2.GetGeometryRef().ExportToWkt())
    ds2 = None
    union = unary_union([Point(x, y).buffer(radius, quad_segs=quad_segs) for x, y in pts])
    cov = union.intersection(boundary)
    return {"area": cov.area, "points": len(pts)}


def main():
    say("========================================")
    say("成都双口径跑通 + CEO 实测复现（Solo APPROVED）")
    say("========================================")

    # ── 预置 QGIS srs.db 临时副本（防 qFatal 崩溃）──
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
    RADIUS = 500.0

    def load_layers(proj, shelter_path, shelter_name):
        """清空项目并加载 避难所/行政区/人口 三图层。"""
        proj.removeAllMapLayers()
        LAYERS = [
            (shelter_path, shelter_name),
            (os.path.join(DATA_DIR, "行政区_3857.gpkg"), "行政区_3857"),
            (os.path.join(DATA_DIR, "人口_3857.gpkg"), "人口_3857"),
        ]
        loaded = {}
        for path, name in LAYERS:
            lyr = QgsVectorLayer(path, name, "ogr")
            if not lyr.isValid():
                say(f"[数据] 加载失败 {name}: {path}")
                return None
            proj.addMapLayer(lyr)
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

    def run_caliber(caliber_name, shelter_path, shelter_layer_name):
        """单口径三链 + 报告 + 抽检 + 缺失统计。"""
        say("")
        say("=" * 40)
        say(f"口径 {caliber_name} 开始")
        say("=" * 40)
        loaded = load_layers(project, shelter_path, shelter_layer_name)
        if loaded is None:
            return None

        summary = {"total": 3, "success": 0, "fail": 0, "steps": []}

        def _res_summary(res):
            return {
                "status": res.get("status"),
                "action": res.get("action"),
                "message": str(res.get("message") or "")[:400],
                "info": str(res.get("info") or "")[:200],
            }

        ok1, stats1, res1 = run_chain(
            "coverage", "risk_zone_coverage.json",
            source_layer_name=shelter_layer_name,
            boundary_layer_name="行政区_3857",
            radius_m=RADIUS,
        )
        summary["steps"].append({"chain": "coverage", "success": ok1, "stats": stats1, "res": _res_summary(res1)})
        summary["success"] += 1 if ok1 else 0
        summary["fail"] += 0 if ok1 else 1

        ok2, stats2, res2 = run_chain(
            "gap", "gap_analysis.json",
            source_layer_name=shelter_layer_name,
            boundary_layer_name="行政区_3857",
            radius_m=RADIUS,
        )
        summary["steps"].append({"chain": "gap", "success": ok2, "stats": stats2, "res": _res_summary(res2)})
        summary["success"] += 1 if ok2 else 0
        summary["fail"] += 0 if ok2 else 1

        ok3, stats3, res3 = run_chain(
            "population_coverage", "population_coverage.json",
            source_layer_name=shelter_layer_name,
            boundary_layer_name="行政区_3857",
            population_layer_name="人口_3857",
            population_field="population",
            radius_m=RADIUS,
        )
        summary["steps"].append({"chain": "population_coverage", "success": ok3, "stats": stats3, "res": _res_summary(res3)})
        summary["success"] += 1 if ok3 else 0
        summary["fail"] += 0 if ok3 else 1

        # 报告（coverage 链）
        report = None
        if ok1:
            from core.report_generator import generate_report
            report = generate_report(
                "chengdu", stats1,
                source_layer=shelter_layer_name,
                boundary_layer="行政区_3857",
                lang="zh",
                user_text=f"成都双口径跑通（口径{caliber_name}）：计算避难所对成都行政区的覆盖率",
            )
            say(f"[报告] success={report.get('success')} path={report.get('report_path')}")
            say(f"[报告] 覆盖率={report.get('coverage_rate')} 预警={report.get('warning') or '无'}")
            if report.get("success"):
                dst = os.path.join(OUT_DIR, f"报告_口径{caliber_name}.md")
                shutil.copy(report["report_path"], dst)
                say(f"[报告] 副本 -> {dst}")

        # 独立抽检：coverage 链
        audit = {"done": False, "pass": False, "detail": ""}
        try:
            a = audit_coverage(shelter_path, os.path.join(DATA_DIR, "行政区_3857.gpkg"), RADIUS)
            engine_area = float(stats1.get("covered_area", 0) or 0)
            diff_pct = abs(a["area"] - engine_area) / max(a["area"], 1e-9) * 100.0
            audit["done"] = True
            audit["pass"] = diff_pct <= 1.0
            audit["detail"] = (f"独立复算覆盖面积={a['area']:.0f} m² vs 引擎={engine_area:.0f} m²，"
                               f"相对偏差={diff_pct:.4f}%（容差 ±1%），判定={'PASS' if audit['pass'] else 'FAIL'}")
            say(f"[抽检] {audit['detail']}")
        except Exception as e:
            audit["detail"] = f"抽检异常: {type(e).__name__}: {e}"
            say(f"[抽检] {audit['detail']}")

        # 一致性：gap_rate ≈ 100 - coverage_rate
        consist = {"done": False, "pass": False, "detail": ""}
        if ok1 and ok2:
            cr = float(stats1.get("coverage_rate", 0))
            gr = float(stats2.get("gap_rate", 0))
            diff = abs(gr - (100.0 - cr))
            consist["done"] = True
            consist["pass"] = diff <= 1.0
            consist["detail"] = f"gap_rate={gr:.4f}% vs 100-coverage_rate={100.0-cr:.4f}%，偏差={diff:.4f}%（容差 ±1%）"
            say(f"[一致性] {consist['detail']} 判定={'PASS' if consist['pass'] else 'FAIL'}")

        # 字段缺失统计
        missing = field_missing_stats(shelter_path)
        say(f"[缺失率] name={missing['name']['rate']}% address={missing['address']['rate']}% area={missing['area']['rate']}% (total={missing['name']['total']})")

        return {
            "caliber": caliber_name,
            "loaded": loaded,
            "summary": summary,
            "report": report,
            "audit": audit,
            "consistency": consist,
            "missing": missing,
            "all_pass": summary["fail"] == 0 and audit.get("pass") and consist.get("pass") and bool(report and report.get("success")),
        }

    # ══ 口径 A：37 点官方 ══
    shelter_a = os.path.join(DATA_DIR, "避难所_3857_gpkg_37pt_backup.gpkg")
    res_a = run_caliber("A", shelter_a, "避难所_3857")

    # ══ 口径 B：1284 点（含潜在载体）══
    shelter_b = os.path.join(DATA_DIR, "避难所_3857.gpkg")
    res_b = run_caliber("B", shelter_b, "避难所_3857")

    # ══ CEO 实测复现（关键）══
    say("")
    say("=" * 40)
    say("CEO 实测复现：加载成都 3 图层 →「计算避难所500米范围内的人口覆盖率」")
    say("=" * 40)
    load_layers(project, shelter_b, "避难所_3857")

    from core.instruction_mapper import InstructionMapper
    mapper = InstructionMapper()
    CEO_TEXT = "计算避难所500米范围内的人口覆盖率"

    def ceo_scenario(name, params):
        say("")
        say(f"── CEO 场景 {name} ──")
        llm_response = json.dumps({"action": "population_coverage", "params": params}, ensure_ascii=False)
        say(f"[CEO] 指令原文: {CEO_TEXT}")
        say(f"[CEO] LLM 响应: {llm_response}")
        try:
            res = mapper.match_and_execute(
                llm_response=llm_response,
                canvas=canvas,
                project=project,
                user_text=CEO_TEXT,
            )
        except Exception as e:
            say(f"[CEO] 异常: {type(e).__name__}: {e}")
            return {"name": name, "success": False, "message": f"异常 {e}", "action": None, "res": None}
        say(f"[CEO] 成功={res.get('success')} action={res.get('action')} status={res.get('status')}")
        say(f"[CEO] message={str(res.get('message') or '')[:300]}")
        stats = res.get("stats") or {}
        say(f"[CEO] stats={json.dumps(stats, ensure_ascii=False)}")
        return {
            "name": name,
            "success": bool(res.get("success")),
            "action": res.get("action"),
            "status": res.get("status"),
            "message": str(res.get("message") or "")[:500],
            "stats_keys": sorted(stats.keys()) if stats else [],
            "pop_coverage_rate": stats.get("pop_coverage_rate"),
            "covered_population": stats.get("covered_population"),
            "total_population": stats.get("total_population"),
        }

    ceo_ok = ceo_scenario("1_正确识别", {
        "source_layer": "避难所_3857",
        "boundary_layer": "行政区_3857",
        "population_layer": "人口_3857",
        "population_field": "population",
        "radius_m": RADIUS,
    })
    ceo_correct = ceo_scenario("2_纠偏实证", {
        "source_layer": "行政区_3857",      # 错误：面
        "boundary_layer": "避难所_3857",    # 错误：点
        "population_layer": "人口_3857",
        "population_field": "population",
        "radius_m": RADIUS,
    })

    # ══ 汇总落盘 ══
    rows = []
    for r in (res_a, res_b):
        if not r:
            continue
        s1 = r["summary"]["steps"][0]["stats"]
        s2 = r["summary"]["steps"][1]["stats"]
        s3 = r["summary"]["steps"][2]["stats"]
        rows.append({
            "caliber": r["caliber"],
            "count": r["loaded"]["避难所_3857"]["count"],
            "coverage_rate": s1.get("coverage_rate"),
            "covered_area": s1.get("covered_area"),
            "total_area": s1.get("total_area"),
            "gap_rate": s2.get("gap_rate"),
            "pop_coverage_rate": s3.get("pop_coverage_rate"),
            "covered_population": s3.get("covered_population"),
            "total_population": s3.get("total_population"),
            "audit": r["audit"],
            "consistency": r["consistency"],
            "missing": r["missing"],
            "report": r["report"],
            "summary": r["summary"],
            "all_pass": r["all_pass"],
        })

    table_path = os.path.join(OUT_DIR, "双口径对照表.md")
    with open(table_path, "w", encoding="utf-8") as f:
        f.write("# 成都双口径跑通对照表（Solo 批复）\n\n")
        f.write(f"- 数据目录：`{DATA_DIR}`\n")
        f.write(f"- 半径：{RADIUS}m；引擎：PipelineExecutor（risk_zone_coverage / gap_analysis / population_coverage 模板）\n")
        f.write("- 口径 A：37 点官方挂牌（scdata 应急厅，最高可信度）\n")
        f.write("- 口径 B：1284 点（37 官方 + 1247 OSM 学校/公园/运动场潜在载体）\n\n")
        f.write("| 指标 | 口径 A（37 点官方） | 口径 B（1284 点含潜在载体） | 说明 |\n")
        f.write("|---|---|---|---|\n")
        a = rows[0]
        b = rows[1]
        f.write(f"| 要素数 | {a['count']} | {b['count']} | +{b['count'] - a['count']} OSM 候选 |\n")
        f.write(f"| 覆盖率 | {a['coverage_rate']}% | {b['coverage_rate']}% | 口径 B 应显著更高 |\n")
        f.write(f"| 覆盖面积 (m²) | {a['covered_area']} | {b['covered_area']} | |\n")
        f.write(f"| 行政区总面积 (m²) | {a['total_area']} | {b['total_area']} | |\n")
        f.write(f"| 盲区率 | {a['gap_rate']}% | {b['gap_rate']}% | |\n")
        f.write(f"| 人口覆盖率 | {a['pop_coverage_rate']}% | {b['pop_coverage_rate']}% | |\n")
        f.write(f"| 覆盖人口 | {a['covered_population']} | {b['covered_population']} | |\n")
        f.write(f"| 总人口 | {a['total_population']} | {b['total_population']} | |\n")
        f.write("\n## 独立抽检（OGR+Shapely 复算覆盖面积，容差 ±1%）\n\n")
        f.write(f"- 口径 A：{a['audit']['detail']}（判定 {'PASS' if a['audit']['pass'] else 'FAIL'}）\n")
        f.write(f"- 口径 B：{b['audit']['detail']}（判定 {'PASS' if b['audit']['pass'] else 'FAIL'}）\n")
        f.write("\n## 一致性校验（gap_rate ≈ 100 - coverage_rate，容差 ±1%）\n\n")
        f.write(f"- 口径 A：{a['consistency']['detail']}（判定 {'PASS' if a['consistency']['pass'] else 'FAIL'}）\n")
        f.write(f"- 口径 B：{b['consistency']['detail']}（判定 {'PASS' if b['consistency']['pass'] else 'FAIL'}）\n")
        f.write("\n## 字段缺失率（OSM 候选无 address/area）\n\n")
        f.write("| 口径 | name 缺失率 | address 缺失率 | area 缺失率 |\n")
        f.write("|---|---|---|---|\n")
        for r in rows:
            f.write(f"| {r['caliber']}（{r['count']} 点） | {r['missing']['name']['rate']}% | {r['missing']['address']['rate']}% | {r['missing']['area']['rate']}% |\n")
        f.write("\n> 说明：OSM 潜在载体仅含 name 字段（学校/公园/运动场名称），无官方挂牌的 address/area；"
                "引擎三链统计口径（几何覆盖）不受字段缺失影响，报告已标注。\n\n")
        f.write("## 报告（coverage 链，含阈值预警）\n\n")
        for r in rows:
            rp = r["report"]
            if rp and rp.get("success"):
                f.write(f"- 口径 {r['caliber']}：`{rp['report_path']}`（覆盖率 {rp['coverage_rate']}%，预警：{rp.get('warning') or '未触发'}）\n")
            else:
                f.write(f"- 口径 {r['caliber']}：报告生成失败\n")
        f.write("\n## CEO 实测复现证据\n\n")
        f.write(f"- 指令原文：`{CEO_TEXT}`\n\n")
        for s in (ceo_ok, ceo_correct):
            f.write(f"### 场景 {s['name']}\n")
            f.write(f"- 识别结果：action={s['action']} status={s['status']} success={s['success']}\n")
            f.write(f"- message：{s['message']}\n")
            f.write(f"- 人口覆盖率：{s['pop_coverage_rate']}%（覆盖人口 {s['covered_population']} / 总人口 {s['total_population']}）\n")
            f.write(f"- 链状态：{'成功（无 Guard 报错）' if s['success'] else '失败'}\n\n")
        f.write("\n## 最终判定\n\n")
        all_pass = (rows[0]["all_pass"] and rows[1]["all_pass"]
                    and ceo_ok["success"] and ceo_correct["success"])
        f.write(f"- 双口径 6 条链：{'ALL SUCCESS' if (rows[0]['summary']['fail'] == 0 and rows[1]['summary']['fail'] == 0) else '存在 FAIL'}\n")
        f.write(f"- 独立抽检：{'PASS' if (a['audit']['pass'] and b['audit']['pass']) else 'FAIL'}\n")
        f.write(f"- 一致性校验：{'PASS' if (a['consistency']['pass'] and b['consistency']['pass']) else 'FAIL'}\n")
        f.write(f"- CEO 实测复现（正确 + 纠偏）：{'PASS' if (ceo_ok['success'] and ceo_correct['success']) else 'FAIL'}\n")
        f.write(f"- 总体判定：{'ALL PASS' if all_pass else '存在 FAIL'}\n")
    say(f"[产出] 双口径对照表 -> {table_path}")

    run_record = {
        "task": "成都双口径跑通 + CEO 实测复现",
        "date": time.strftime("%Y-%m-%d %H:%M:%S"),
        "data_dir": DATA_DIR,
        "out_dir": OUT_DIR,
        "radius_m": RADIUS,
        "calibers": {
            "A": {"desc": "37 点官方挂牌（scdata 应急厅）", "path": shelter_a},
            "B": {"desc": "1284 点 = 37 官方 + 1247 OSM 潜在载体", "path": shelter_b},
        },
        "results": rows,
        "ceo_repro": {"text": CEO_TEXT, "scenario_ok": ceo_ok, "scenario_correct": ceo_correct},
        "engine_change": "none",
        "registry_change": "none",
        "all_pass": all_pass,
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
    say("成都双口径跑通完成")
    for r in rows:
        say(f"- 口径 {r['caliber']}（{r['count']} 点）：覆盖率 {r['coverage_rate']}% / 盲区率 {r['gap_rate']}% / 人口覆盖率 {r['pop_coverage_rate']}%")
    say(f"- CEO 复现：场景1（正确）={ceo_ok['success']}，场景2（纠偏）={ceo_correct['success']}")
    say(f"- 总体判定：{'ALL PASS' if all_pass else '存在 FAIL'}")
    say("- 交付物：")
    for p in [LOG_PATH, table_path, record_path]:
        say(f"  {p}")
    for r in rows:
        rp = r["report"]
        if rp and rp.get("success"):
            say(f"  {os.path.join(OUT_DIR, '报告_口径' + r['caliber'] + '.md')}")
    say("========================================")
    qgs.exitQgis()
    say("RUN_CHENGDU_DUAL_DONE")
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
