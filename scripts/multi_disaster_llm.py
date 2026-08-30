# -*- coding: utf-8 -*-
r"""scripts/multi_disaster_llm.py — 片A补充：LLM 全链验证 4 灾种（Solo APPROVED）

Solo 批复「片A补充：LLM 全链验证 4 灾种」：
4 条自然语言指令（地震/洪涝/滑坡/火灾）→ 离线 LLM（qwen3.5-4b）识别 →
InstructionMapper 映射 risk_zone_coverage 模板语义 → 引擎执行；
4 条全部 success 且数值与引擎直连一致（±0.01% 内）即 PASS。
产出 LLM+引擎 4 灾种结果表与 run 记录。

说明：LLM system prompt 为静态模板（不含危险区图层清单），
boundary 图层依赖 auto_detect_layers_from_text 从指令文本兜底匹配，
因此指令按 Solo 示例（如「分析地震震度分布覆盖情况」）扩展为包含
source（避难所）与危险区图层名的自然语言表述，更贴近真实用户用法。

红线遵守：无 git 写；不改引擎/guards/现有模板/CRS/注册表；零新依赖。
运行：qgis-portable\apps\Python312\python.exe scripts\multi_disaster_llm.py
"""
import os
import sys
import json
import re
import time

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

DATA_DIR = os.path.join(PROJECT_ROOT, "temp", "multi_disaster")
OUT_DIR = os.path.join(PROJECT_ROOT, "output", "片A补充_LLM全链验证")
os.makedirs(OUT_DIR, exist_ok=True)

MODEL = "qwen3.5-4b"
RADIUS_M = 500.0
TOLERANCE = 0.01  # ±0.01%（Solo 要求）

# 引擎直连基准（temp/multi_disaster/run_records.json，risk_zone_coverage 模板）
EXPECTED = {
    "震度分布": 38.426968963612445,   # 地震
    "淹没区": 100.00000000000013,     # 洪涝
    "滑坡风险区": 17.078652872713416,  # 滑坡
    "火灾风险区": 68.31461149086756,   # 火灾
}

# 4 灾种自然语言指令（含 source 与危险区图层名，供 auto_detect 兜底）
CASES = [
    {"disaster": "地震", "risk_zone": "震度分布",
     "text": "计算避难所对震度分布区域的覆盖率"},
    {"disaster": "洪涝", "risk_zone": "淹没区",
     "text": "计算避难所对淹没区域的覆盖率"},
    {"disaster": "滑坡", "risk_zone": "滑坡风险区",
     "text": "计算避难所对滑坡风险区的覆盖率"},
    {"disaster": "火灾", "risk_zone": "火灾风险区",
     "text": "计算避难所对火灾风险区的覆盖率"},
]

_log_lines = []


def say(msg):
    print(msg, flush=True)
    _log_lines.append(str(msg))


def extract_json_obj(text):
    m = re.search(r'\{.*\}', text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


say("========================================")
say("片A补充：LLM 全链验证 4 灾种（Solo APPROVED）")
say("========================================")

# ── 预置 QGIS srs.db 临时副本（防 qFatal 崩溃）──
import tempfile, shutil
_srs_src = os.path.join(OSGEO4W_ROOT, "apps", "qgis-ltr", "resources", "srs.db")
_srs_dst = os.path.join(tempfile.gettempdir(), "srs6.db")
if not os.path.exists(_srs_dst) and os.path.exists(_srs_src):
    try:
        shutil.copy(_srs_src, _srs_dst)
        say(f"[预置] srs6.db -> {_srs_dst}")
    except Exception as _e:
        say(f"[警告] srs.db 预置失败: {_e}")

from qgis.core import QgsApplication, QgsVectorLayer, QgsProject, QgsVectorFileWriter
from qgis.gui import QgsMapCanvas
QgsApplication.setPrefixPath(os.path.join(OSGEO4W_ROOT, "apps", "qgis-ltr"), True)
qgs = QgsApplication([], False)
qgs.initQgis()

from core.qgis_env import initialize_processing
initialize_processing(qgs)
say("QGIS 引擎初始化完成")

# ── 测试专用：限制 Ollama context 长度，避免显存压力 ──
import core.local_llm as _llm_mod
import requests as _requests
_orig_chat = _llm_mod.LocalLLMClient.chat


def _chat_ctx8192(self, messages, temperature=0.7, max_tokens=4096):
    url = f"{self.base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": self.model, "messages": messages, "temperature": temperature,
        "max_tokens": max_tokens, "stream": False,
        "options": {"num_ctx": 8192},
    }
    resp = _requests.post(url, json=payload, timeout=max(self.timeout, 300),
                          headers={"Content-Type": "application/json"},
                          proxies={"http": None, "https": None})
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


_llm_mod.LocalLLMClient.chat = _chat_ctx8192
say("Ollama context 已限制为 8192（显存稳妥）")

from core.instruction_mapper import InstructionMapper
from core.local_llm import LocalLLMClient

# ── 1. 加载多灾种合成数据（temp/multi_disaster/）──
project = QgsProject.instance()
LAYERS = [
    (os.path.join(DATA_DIR, "行政区.gpkg"), "行政区"),
    (os.path.join(DATA_DIR, "避难所.gpkg"), "避难所"),
    (os.path.join(DATA_DIR, "震度分布.gpkg"), "震度分布"),
    (os.path.join(DATA_DIR, "淹没区.gpkg"), "淹没区"),
    (os.path.join(DATA_DIR, "滑坡风险区.gpkg"), "滑坡风险区"),
    (os.path.join(DATA_DIR, "火灾风险区.gpkg"), "火灾风险区"),
]
for path, name in LAYERS:
    lyr = QgsVectorLayer(path, name, "ogr")
    if not lyr.isValid():
        say(f"[数据] 加载失败 {name}: {path}")
        sys.exit(1)
    project.addMapLayer(lyr)
    say(f"[数据] {name}: {lyr.featureCount()} 要素, CRS={lyr.crs().authid()}")

canvas = QgsMapCanvas()
mapper = InstructionMapper()
client = LocalLLMClient(model=MODEL)
say(f"本地模型：{client.model}")

# ── 2. 逐条自然语言指令：真实 LLM 识别 → InstructionMapper → 引擎执行 ──
summary = {"total": len(CASES), "success": 0, "fail": 0, "steps": []}
for i, case in enumerate(CASES, 1):
    user_text = case["text"]
    expected = EXPECTED[case["risk_zone"]]
    say("")
    say(f"[{i}/{len(CASES)}] 指令：{user_text}（灾种={case['disaster']}，期望边界={case['risk_zone']}）")
    try:
        sp = mapper.get_system_prompt("zh")
        messages = [{"role": "system", "content": sp}, {"role": "user", "content": user_text}]
        resp = client.chat(messages, temperature=0.2)
        llm = extract_json_obj(resp) or {}
        llm_action = llm.get("action")
        llm_params = llm.get("params") or {}
        say(f"        本地模型识别：action={llm_action}, params={json.dumps(llm_params, ensure_ascii=False)}")
    except Exception as e:
        say(f"        本地模型调用失败：{type(e).__name__}: {e}")
        summary["fail"] += 1
        summary["steps"].append({"index": i, **case, "success": False, "note": f"LLM调用失败: {e}"})
        continue

    try:
        t0 = time.time()
        result = mapper.match_and_execute(resp, canvas=canvas, project=project, user_text=user_text)
        dt = round(time.time() - t0, 1)
        ok = bool(result.get("success"))
        status = result.get("status")
        msg = (result.get("message") or "")[:400]
        stats = result.get("stats") or {}
        actual = stats.get("coverage_rate")
        if ok and actual is not None:
            diff = abs(actual - expected)
            pass_ok = diff <= TOLERANCE
        else:
            diff = None
            pass_ok = False
        say(f"        执行结果：{'成功' if ok else '失败'} (status={status}, {dt}s) — {msg}")
        if actual is not None:
            say(f"        覆盖率：实际={round(actual, 6)}% 期望={round(expected, 6)}% 差值={None if diff is None else round(diff, 6)}% 判定={'PASS' if pass_ok else 'FAIL'}")
        step = {
            "index": i, **case,
            "llm_action": llm_action, "llm_params": llm_params,
            "final_action": result.get("action"),
            "status": status,
            "success": ok, "message": msg, "stats": stats,
            "expected_coverage_rate": expected,
            "coverage_rate": actual,
            "diff": diff, "pass": pass_ok,
            "dt_s": dt,
        }
        summary["steps"].append(step)
        if pass_ok:
            summary["success"] += 1
        else:
            summary["fail"] += 1
    except Exception as e:
        say(f"        执行异常：{type(e).__name__}: {e}")
        summary["fail"] += 1
        summary["steps"].append({"index": i, **case, "success": False, "note": f"执行异常: {e}"})

all_pass = summary["fail"] == 0

# ── 3. 落交付物 ──
# 3a. 运行日志原文
log_path = os.path.join(OUT_DIR, "run_log.txt")
with open(log_path, "w", encoding="utf-8") as f:
    f.write("\n".join(_log_lines))
say(f"[产出] 运行日志 -> {log_path}")

# 3b. LLM+引擎 4 灾种结果表
table_md = ["| # | 灾种 | 指令 | LLM识别action | LLM参数 | 最终action | 状态 | 覆盖率实际(%) | 期望(%) | 差值(%) | 判定 |",
            "|---|------|------|--------------|---------|-----------|------|--------------|---------|---------|------|"]
for st in summary["steps"]:
    actual = st.get("coverage_rate")
    diff = st.get("diff")
    table_md.append(
        f"| {st['index']} | {st.get('disaster', '-')} | {st.get('user_text', st.get('text', '-'))} "
        f"| {st.get('llm_action', '-')} | {json.dumps(st.get('llm_params') or {}, ensure_ascii=False)} "
        f"| {st.get('final_action', '-')} | {'成功' if st.get('success') else '失败'} "
        f"| {'-' if actual is None else round(actual, 6)} "
        f"| {st.get('expected_coverage_rate', '-')} "
        f"| {'-' if diff is None else round(diff, 6)} "
        f"| {'PASS' if st.get('pass') else 'FAIL'} |"
    )
table_path = os.path.join(OUT_DIR, "results_table.md")
with open(table_path, "w", encoding="utf-8") as f:
    f.write("# LLM+引擎 4 灾种结果表（Solo 片A补充批复）\n\n")
    f.write(f"- 指令总数：{summary['total']}，PASS：{summary['success']}，FAIL：{summary['fail']}\n")
    f.write(f"- 数据：`{DATA_DIR}`（行政区/避难所/震度分布/淹没区/滑坡风险区/火灾风险区，EPSG:3857）\n")
    f.write(f"- 链路：自然语言指令 → qwen3.5-4b（Ollama 离线）→ InstructionMapper（关键词纠偏+图层自动检测）→ PipelineExecutor 引擎\n")
    f.write(f"- 期望口径：引擎直连基准（temp/multi_disaster/run_records.json），容差 ±{TOLERANCE}%\n\n")
    f.write("\n".join(table_md))
    f.write("\n")
say(f"[产出] 4 灾种结果表 -> {table_path}")

# 3c. run 记录
run_record = {
    "task": "片A补充：LLM 全链验证 4 灾种",
    "date": time.strftime("%Y-%m-%d %H:%M:%S"),
    "model": MODEL,
    "data_dir": DATA_DIR,
    "out_dir": OUT_DIR,
    "radius_m": RADIUS_M,
    "tolerance": TOLERANCE,
    "expected": EXPECTED,
    "chain": "自然语言 → qwen3.5-4b → InstructionMapper → PipelineExecutor",
    "engine_change": "none（引擎零改动）",
    "summary": summary,
    "all_pass": all_pass,
}
record_path = os.path.join(OUT_DIR, "run_record.json")
with open(record_path, "w", encoding="utf-8") as f:
    json.dump(run_record, f, ensure_ascii=False, indent=2)
say(f"[产出] run 记录 -> {record_path}")

# ── 4. 最终汇总 ──
say("")
say("========================================")
say("LLM 全链验证完成")
say(f"- 指令总数：{summary['total']} 条，PASS {summary['success']} 条，FAIL {summary['fail']} 条")
for st in summary["steps"]:
    actual = st.get("coverage_rate")
    if actual is not None:
        say(f"- {st['disaster']}（{st['risk_zone']}）：覆盖率 {round(actual, 6)}%（期望 {round(st['expected_coverage_rate'], 6)}%，判定 {'PASS' if st.get('pass') else 'FAIL'}）")
say("- 交付物：")
for f in [log_path, table_path, record_path]:
    say(f"  {f}")
say(f"- 最终判定：{'ALL PASS' if all_pass else '存在 FAIL'}")
say("========================================")

qgs.exitQgis()
say("MULTI_DISASTER_LLM_DONE")
