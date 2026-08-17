# -*- coding: utf-8 -*-
r"""scripts/benchmark.py — P2-8 一键基准脚本（Solo APPROVED）

一键复跑正式基准，自动完成：
  (a) 25×3 意图识别基准（V5 prompt、/api/chat num_ctx=4096、temperature=0.2、
      expect_json 重试开；25 条中文指令 × 3 轮 = 75 次真实 LLM 调用）
  (b) demo 4 链端到端（真实离线 LLM + InstructionMapper.match_and_execute，
      复用 scripts/demo_run.py 的数据与指令，逐链记录 status/关键统计）
  (c) 输出 JSON 报告（识别率 / nojson / 三语子集 / 每链 status / 关键统计）
      落盘 reports/benchmark_YYYYMMDD_HHMM.json

运行（Windows，须先启动 Ollama 并加载 qwen3.5-4b）：
    qgis-portable\apps\Python312\python.exe scripts\benchmark.py

约束遵守：不改 src/ 生产代码；不改模板/测试数据；不写 git。
"""

import os
import sys
import json
import re
import time
import tempfile
import shutil
import datetime

# ────────────────────────────────────────────────────────────
# 0. 环境初始化（QGIS portable）
# ────────────────────────────────────────────────────────────
PROJECT_ROOT = r"D:\桌面\QGIS-Agent"
OSGEO4W_ROOT = os.path.join(PROJECT_ROOT, "qgis-portable")
REPORTS_DIR = os.path.join(PROJECT_ROOT, "reports")
DATA = r"D:\桌面\项目测试数据"
MODEL = "qwen3.5-4b"
# P2-8 任务书要求：(a) 识别基准 /api/chat num_ctx=4096、temperature=0.2、expect_json 重试开
# demo 端到端沿用 demo_run.py 历史配置（num_ctx=8192，保证与历史 demo 基线可比）
BENCH_NUM_CTX = 4096
BENCH_TEMPERATURE = 0.2
DEMO_NUM_CTX = 8192

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


def say(msg):
    print(msg, flush=True)


say("========================================")
say("P2-8 一键基准启动（qwen3.5-4b 离线）")
say("========================================")

# ── 预置 QGIS srs.db 临时副本（缺失会导致 createFromWkt qFatal 崩溃）──
_srs_src = os.path.join(OSGEO4W_ROOT, "apps", "qgis-ltr", "resources", "srs.db")
_srs_dst = os.path.join(tempfile.gettempdir(), "srs6.db")
if not os.path.exists(_srs_dst) and os.path.exists(_srs_src):
    try:
        shutil.copy(_srs_src, _srs_dst)
        say(f"[预置] srs6.db -> {_srs_dst}")
    except Exception as _e:
        say(f"[警告] srs.db 预置失败: {_e}")

from qgis.core import QgsApplication, QgsVectorLayer, QgsProject, QgsVectorFileWriter, QgsFeature, QgsFeatureRequest
from qgis.gui import QgsMapCanvas
QgsApplication.setPrefixPath(os.path.join(OSGEO4W_ROOT, "apps", "qgis-ltr"), True)
qgs = QgsApplication([], False)
qgs.initQgis()

from core.qgis_env import initialize_processing
initialize_processing(qgs)
say("QGIS 引擎初始化完成")

from core.instruction_mapper import InstructionMapper
from core.local_llm import LocalLLMClient
from core.config_manager import config_manager as _cm

# ────────────────────────────────────────────────────────────
# 1. 25×3 意图识别基准
# ────────────────────────────────────────────────────────────

# 25 条中文指令集（按 V5 prompt 的 30 个 action 重构；原始 25 条脚本已随
# P2-3 清理，temp 无留存。idx13=人口覆盖、idx18=分级渲染 对齐历史顽固项位置，
# 保证与历史基线可比。判定标准与 P1.6/P1.7 一致：LLM 输出的 action == 预期
# action 记为命中；unknown/兜底/错误 action 记为不匹配；无法解析 JSON 记为 nojson。）
CASES_ZH = [
    ("加载图层文件 D:/桌面/项目测试数据/demo_poi.vrt", "load_layer"),          # 1
    ("保存项目", "save_project"),                                               # 2
    ("把当前地图导出为png图片", "export_map"),                                  # 3
    ("缩放到图层 东京行政区", "zoom_to_layer"),                                 # 4
    ("放大地图", "zoom_in"),                                                     # 5
    ("缩小地图", "zoom_out"),                                                    # 6
    ("删除图层 demo_poi", "remove_layer"),                                      # 7
    ("列出当前所有图层", "list_layers"),                                        # 8
    ("识别当前图层要素的属性信息", "identify_feature"),                          # 9
    ("把坐标系设置为EPSG:3857", "set_crs"),                                     # 10
    ("查看当前坐标系", "show_crs"),                                             # 11
    ("把东京震度分布数据重投影到EPSG:3857", "reproject_layer"),                 # 12
    ("计算避难所500米范围内的人口覆盖率，边界用东京行政区，人口字段用population", "population_coverage"),  # 13
    ("开始编辑图层 东京行政区", "toggle_editing"),                               # 14
    ("选择东京行政区内人口大于5000的要素", "select_feature"),                    # 15
    ("重置视图", "reset_view"),                                                  # 16
    ("过滤出人口图层中population大于10000的要素", "filter_layer"),               # 17
    ("对东京震度分布数据做分级渲染", "set_layer_style"),                         # 18
    ("导出东京行政区的属性表为CSV", "export_attribute"),                         # 19
    ("把避难所图层导出为shp格式", "export_layer"),                               # 20
    ("给避难所图层添加标注，标注字段为name", "add_label"),                       # 21
    ("打开人口图层的字段管理器", "open_field_manager"),                          # 22
    ("统计人口图层population字段的平均值", "layer_statistic"),                   # 23
    ("对避难所创建500米缓冲区", "create_buffer"),                                # 24
    ("把震度数据和避难所图层做空间关联", "spatial_join"),                        # 25
]

# 三语子集（P1.7 历史口径：4 条日/英指令）
CASES_TRILINGUAL = [
    ("ja", "レイヤを読み込んでください: D:/桌面/项目测试数据/demo_poi.vrt", "load_layer"),
    ("ja", "東京震度分布データに 500m バッファを作成", "create_buffer"),
    ("en", "export attribute table of 东京行政区 to CSV", "export_attribute"),
    ("en", "list layers", "list_layers"),
]

# 历史基线（P1.6/P1.7 实测，仅作一致性对比参考）
HISTORY_BASELINE = {
    "intent_accuracy_pct": 86.7,     # P1.7 头对头 qwen3.5-4b@Q4，21.67/25 平均
    "nojson": "0/75",
    "trilingual": "4/4",
    "demo": {"coverage_rate_pct": 0.7, "gap_rate_pct": 99.3,
             "pop_coverage_rate_pct": 0.0, "total_population": 14031625},
}


def extract_json_obj(text):
    """与 temp 既有脚本一致的 JSON 提取：取首个 {...} 块并解析。"""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def run_intent_bench(mapper, client, cases, lang, label, rounds=3):
    """跑 N 轮意图识别，返回统计 dict。"""
    sp = mapper.get_system_prompt(lang)
    rows = []
    for rnd in range(1, rounds + 1):
        for idx, case in enumerate(cases, 1):
            if lang == "zh":
                user_text, expected = case
            else:
                user_text, expected = case[1], case[2]
            resp = client.chat(
                [{"role": "system", "content": sp}, {"role": "user", "content": user_text}],
                temperature=BENCH_TEMPERATURE,
                expect_json=True,
                max_retries=2,
                num_ctx=BENCH_NUM_CTX,
                use_native_chat=True,
            )
            obj = extract_json_obj(resp)
            if obj is None:
                rows.append({
                    "case_idx": idx, "round": rnd, "lang": lang,
                    "user": user_text, "expected": expected,
                    "hit": False, "nojson": True, "got": None,
                    "raw": str(resp)[:300],
                })
                continue
            got = str(obj.get("action") or "")
            rows.append({
                "case_idx": idx, "round": rnd, "lang": lang,
                "user": user_text, "expected": expected,
                "hit": (got == expected), "nojson": False, "got": got,
            })

    total = len(rows)
    hit = sum(1 for r in rows if r["hit"])
    nojson = sum(1 for r in rows if r["nojson"])
    per_round = []
    for rnd in range(1, rounds + 1):
        r_rows = [r for r in rows if r["round"] == rnd]
        r_hit = sum(1 for r in r_rows if r["hit"])
        per_round.append({"round": rnd, "hit": r_hit, "total": len(r_rows),
                          "accuracy_pct": round(100.0 * r_hit / len(r_rows), 2)})
    # 每条指令命中次数（跨轮）
    per_case = []
    case_map = {}
    for r in rows:
        case_map.setdefault(r["case_idx"], []).append(r)
    for idx in sorted(case_map):
        rs = case_map[idx]
        misses = [r for r in rs if not r["hit"]]
        per_case.append({
            "idx": idx, "user": rs[0]["user"], "expected": rs[0]["expected"],
            "hits": sum(1 for r in rs if r["hit"]), "total": len(rs),
            "miss_reasons": [{"round": m["round"], "got": m.get("got"), "nojson": m["nojson"]}
                             for m in misses],
        })
    return {
        "label": label, "lang": lang, "cases": len(cases), "rounds": rounds,
        "total_calls": total, "hit": hit, "nojson": nojson,
        "accuracy_pct": round(100.0 * hit / total, 2),
        "per_round": per_round, "per_case": per_case,
    }


say("")
say("── (a) 25×3 意图识别基准（/api/chat num_ctx=4096, temp=0.2, expect_json 开）──")
mapper = InstructionMapper()
client = LocalLLMClient(model=MODEL)
say(f"本地模型：{client.model}")

t0 = time.time()
bench_zh = run_intent_bench(mapper, client, CASES_ZH, "zh", "25x3_zh", rounds=3)
say(f"[基准] 中文 25×3: 命中 {bench_zh['hit']}/{bench_zh['total_calls']} "
    f"识别率 {bench_zh['accuracy_pct']}% nojson {bench_zh['nojson']}")
for pr in bench_zh["per_round"]:
    say(f"        第 {pr['round']} 轮: {pr['hit']}/{pr['total']} = {pr['accuracy_pct']}%")

bench_tri = run_intent_bench(mapper, client, CASES_TRILINGUAL, "trilingual", "trilingual", rounds=1)
say(f"[基准] 三语子集: 命中 {bench_tri['hit']}/{bench_tri['total_calls']} "
    f"识别率 {bench_tri['accuracy_pct']}% nojson {bench_tri['nojson']}")
for r in bench_tri["per_case"]:
    say(f"        [{r['idx']}] {r['user'][:40]} -> expected={r['expected']} "
        f"got={r['miss_reasons'][0]['got'] if r['miss_reasons'] and r['hits']==0 else r['expected']}")
bench_time = round(time.time() - t0, 1)
say(f"[基准] 用时 {bench_time}s")

# ────────────────────────────────────────────────────────────
# 2. demo 4 链端到端（复用 demo_run.py 数据与指令，不改被测代码）
# ────────────────────────────────────────────────────────────
say("")
say("── (b) demo 4 链端到端 ──")

# 沿用 demo_run.py 的 LLM context 限制（8192）
import core.local_llm as _llm_mod
import requests as _requests
_orig_chat = _llm_mod.LocalLLMClient.chat

def _chat_ctx8192(self, messages, temperature=0.7, max_tokens=4096):
    url = f"{self.base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": self.model, "messages": messages, "temperature": temperature,
        "max_tokens": max_tokens, "stream": False,
        "options": {"num_ctx": DEMO_NUM_CTX},
    }
    resp = _requests.post(url, json=payload, timeout=max(self.timeout, 300),
                          headers={"Content-Type": "application/json"},
                          proxies={"http": None, "https": None})
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]

_llm_mod.LocalLLMClient.chat = _chat_ctx8192

project = QgsProject.instance()

# 25×3 识别阶段在 QGIS 项目里没有新建图层；进入 demo 数据加载前清理
# 残留并回收内存，避免长时间运行后 shapely 内存碎片导致分析崩溃
# （复跑实测曾出现 "Unable to allocate ... for an array"）
import gc
project.removeAllMapLayers()
gc.collect()

def load_shelter_subset(path, name, boundary_extent, limit=100):
    src = QgsVectorLayer(path, name + "_src", "ogr")
    if not src.isValid():
        say(f"[数据] 加载失败 {name}: {path}")
        return None
    mem = QgsVectorLayer(f"Point?crs={src.crs().authid()}", name, "memory")
    mem.dataProvider().addAttributes(src.fields())
    mem.updateFields()
    req = QgsFeatureRequest().setFilterRect(boundary_extent)
    feats = []
    for i, feat in enumerate(src.getFeatures(req)):
        if i >= limit:
            break
        feats.append(QgsFeature(feat))
    mem.dataProvider().addFeatures(feats)
    mem.updateExtents()
    return mem

bd_path = os.path.join(DATA, "新数据", "东京行政区_GADM_EPSG3857.shp")
bd_lyr = QgsVectorLayer(bd_path, "东京行政区", "ogr")
if bd_lyr.isValid():
    project.addMapLayer(bd_lyr)
    say(f"[数据] 东京行政区: {bd_lyr.featureCount()} 要素, CRS={bd_lyr.crs().authid()}")
else:
    say(f"[数据] 加载失败 东京行政区: {bd_path}")

loads = [
    (os.path.join(DATA, "测试", "东京震度分布数据_first_EPSG3857.geojson"), "震度", "file"),
    (os.path.join(DATA, "测试", "避难所_EPSG3857.gpkg"), "避难所", "subset"),
    (os.path.join(DATA, "新数据", "tokyo_population_EPSG3857.shp"), "人口", "file"),
]
for path, name, mode in loads:
    if mode == "subset":
        lyr = load_shelter_subset(path, name, bd_lyr.extent(), limit=100)
        if lyr is None:
            continue
        project.addMapLayer(lyr)
        say(f"[数据] {name}: {lyr.featureCount()} 要素（东京范围内采样）")
    else:
        lyr = QgsVectorLayer(path, name, "ogr")
        if lyr.isValid():
            project.addMapLayer(lyr)
            say(f"[数据] {name}: {lyr.featureCount()} 要素")
        else:
            say(f"[数据] 加载失败 {name}: {path}")

canvas = QgsMapCanvas()
DEMO_CASES = [
    "对避难所创建 500 米缓冲区",
    "计算避难所的覆盖范围，边界用东京行政区",
    "分析避难所500米服务范围的盲区，边界用东京行政区",
    "计算避难所500米范围内的人口覆盖率，边界用东京行政区，人口字段用population",
]

def _chat_demo_with_retry(messages, temperature=0.2, retries=3):
    """demo 阶段 LLM 调用：网络/服务瞬时错误自动重试（Ollama 偶发 HTTP 500）。"""
    last_exc = None
    for attempt in range(retries):
        try:
            return client.chat(messages, temperature=temperature)
        except Exception as e:
            last_exc = e
            say(f"        [重试 {attempt + 1}/{retries}] LLM 调用异常：{type(e).__name__}: {e}")
            time.sleep(1.0)
    raise last_exc

demo_summary = {"total": len(DEMO_CASES), "success": 0, "fail": 0, "steps": []}
for i, user_text in enumerate(DEMO_CASES, 1):
    say("")
    say(f"[demo {i}/{len(DEMO_CASES)}] 指令：{user_text}")
    try:
        sp = mapper.get_system_prompt("zh")
        resp = _chat_demo_with_retry([{"role": "system", "content": sp}, {"role": "user", "content": user_text}],
                                     temperature=0.2)
        llm = extract_json_obj(resp) or {}
        llm_action = llm.get("action")
        llm_params = llm.get("params") or {}
        say(f"        本地模型识别：action={llm_action}, params={json.dumps(llm_params, ensure_ascii=False)}")
    except Exception as e:
        say(f"        本地模型调用失败：{type(e).__name__}: {e}")
        demo_summary["fail"] += 1
        demo_summary["steps"].append({"index": i, "user_text": user_text,
                                      "status": "failed", "note": f"LLM调用失败: {e}"})
        continue

    try:
        t0 = time.time()
        result = mapper.match_and_execute(resp, canvas=canvas, project=project, user_text=user_text)
        dt = round(time.time() - t0, 1)
        ok = bool(result.get("success"))
        status = "ok" if ok else "failed"
        msg = (result.get("message") or "")[:400]
        stats = result.get("stats") or {}
        say(f"        执行结果：{status} ({dt}s) — {msg}")
        step = {"index": i, "user_text": user_text,
                "llm_action": llm_action, "final_action": result.get("action"),
                "status": status, "message": msg, "stats": stats}
        demo_summary["steps"].append(step)
        if ok:
            demo_summary["success"] += 1
        else:
            demo_summary["fail"] += 1
    except Exception as e:
        say(f"        执行异常：{type(e).__name__}: {e}")
        demo_summary["fail"] += 1
        demo_summary["steps"].append({"index": i, "user_text": user_text,
                                      "status": "failed", "note": f"执行异常: {e}"})

# demo 关键统计汇总
demo_stats = {}
for step in demo_summary["steps"]:
    st = step.get("stats") or {}
    if "coverage_rate" in st and "gap_rate" not in st and "pop_coverage_rate" not in st:
        demo_stats["coverage_rate_pct"] = round(st["coverage_rate"], 1)
    if "gap_rate" in st:
        demo_stats["gap_rate_pct"] = round(st["gap_rate"], 1)
        demo_stats.setdefault("coverage_rate_pct", round(st.get("coverage_rate", 0), 1))
    if "pop_coverage_rate" in st:
        demo_stats["pop_coverage_rate_pct"] = round(st["pop_coverage_rate"], 1)
        demo_stats["covered_population"] = st.get("covered_population")
        demo_stats["total_population"] = st.get("total_population")
demo_summary["summary"] = demo_stats
say("")
say(f"[demo] 成功 {demo_summary['success']}/{demo_summary['total']}")
say(f"[demo] 关键统计: {json.dumps(demo_stats, ensure_ascii=False)}")

# ────────────────────────────────────────────────────────────
# 3. 汇总 JSON 报告
# ────────────────────────────────────────────────────────────
os.makedirs(REPORTS_DIR, exist_ok=True)
now = datetime.datetime.now()
report_name = f"benchmark_{now.strftime('%Y%m%d_%H%M')}.json"
report_path = os.path.join(REPORTS_DIR, report_name)
report = {
    "meta": {
        "task": "P2-8 基准测试正式化：一键基准脚本",
        "script": "scripts/benchmark.py",
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "model": client.model,
        "config": {
            "prompt": "V5（template_registry._SYSTEM_PROMPT_ZH）",
            "endpoint": "Ollama /api/chat (use_native_chat=True)",
            "num_ctx": BENCH_NUM_CTX,
            "temperature": BENCH_TEMPERATURE,
            "expect_json": True,
            "max_retries": 2,
        },
        "notes": [
            "25×3 中文指令集按 V5 prompt 30 action 重构（原始 25 条脚本已随 P2-3 清理，temp 无留存）；idx13=人口覆盖、idx18=分级渲染对齐历史顽固项",
            "判定标准：LLM 输出 action == 预期 action 记命中；unknown/兜底/错误 action 记不匹配；无法解析 JSON 记 nojson",
            "demo 4 链沿用 demo_run.py 数据与指令（num_ctx=8192），不改被测代码",
        ],
    },
    "intent_recognition": {
        "zh_25x3": bench_zh,
        "trilingual": bench_tri,
        "benchmark_time_s": bench_time,
    },
    "demo": demo_summary,
    "history_baseline": HISTORY_BASELINE,
    "consistency": {
        "intent": {
            "current_pct": bench_zh["accuracy_pct"],
            "baseline_pct": HISTORY_BASELINE["intent_accuracy_pct"],
            "comment": "量级对比：与历史基线 86.7% 差异需结合指令集重构说明（见 notes）",
        },
        "demo": {
            "current": demo_stats,
            "baseline": HISTORY_BASELINE["demo"],
            "comment": "覆盖率/盲区率/人口覆盖率为确定性几何计算，历史基线 0.7%/99.3%/0.0%",
        },
    },
}
with open(report_path, "w", encoding="utf-8") as f:
    json.dump(report, f, ensure_ascii=False, indent=2)
say(f"[报告] -> {report_path}")

# ── 最终汇总 ──
say("")
say("========================================")
say("P2-8 一键基准完成")
say(f"- 25×3 识别率：{bench_zh['accuracy_pct']}% （命中 {bench_zh['hit']}/{bench_zh['total_calls']}）")
say(f"- nojson：{bench_zh['nojson']}/{bench_zh['total_calls']}")
say(f"- 三语子集：{bench_tri['hit']}/{bench_tri['total_calls']} = {bench_tri['accuracy_pct']}%")
say(f"- demo 4 链：成功 {demo_summary['success']}/{demo_summary['total']}")
say(f"- demo 统计：{json.dumps(demo_stats, ensure_ascii=False)}")
say(f"- 报告：{report_path}")
say("========================================")

qgs.exitQgis()
say("BENCH_DONE")
