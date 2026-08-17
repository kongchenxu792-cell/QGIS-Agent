# -*- coding: utf-8 -*-
r"""scripts/demo_queue_run.py — P3-3 demo：4 链端到端经运行队列执行（不回归）

与 scripts/demo_run.py 同一数据/LLM/执行链，区别：
- 4 条指令（LLM 识别 + 执行）各自封装为队列任务，经 RunQueue 后台串行执行
- enqueue 前资源预检（sources 内存粗估 + llm_model 显存粗估，4b≈3GB 基线）
- 队列状态查询（pending/running/queue_length/completed）与 state_changed 声带

运行：qgis-portable\apps\Python312\python.exe scripts\demo_queue_run.py  （Windows）
"""
import os, sys, json, re, time

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

MODEL = "qwen3.5-4b"
DEMO_OUT = os.path.join(PROJECT_ROOT, "user_data", "demo_output")
DATA = r"D:\桌面\项目测试数据"

def say(msg):
    print(msg, flush=True)

say("========================================")
say("P3-3 demo：4 链经运行队列执行（qwen3.5-4b 离线端到端）")
say("========================================")

# ── 预置 QGIS srs.db 临时副本（缺失会导致 createFromWkt qFatal 崩溃 0xC0000409）──
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
from core.run_queue import get_run_queue

# ── 1. 加载测试数据（与 demo_run.py 一致：稳妥数据，禁止 12 万面全域） ──
project = QgsProject.instance()
from qgis.core import QgsFeature, QgsFeatureRequest
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
        say(f"[数据] {name}: {lyr.featureCount()} 要素（东京范围内采样, 源 {os.path.basename(path)}）, CRS={lyr.crs().authid()}")
    else:
        lyr = QgsVectorLayer(path, name, "ogr")
        if lyr.isValid():
            project.addMapLayer(lyr)
            say(f"[数据] {name}: {lyr.featureCount()} 要素, CRS={lyr.crs().authid()}")
        else:
            say(f"[数据] 加载失败 {name}: {path}")

canvas = QgsMapCanvas()
mapper = InstructionMapper()
client = LocalLLMClient(model=MODEL)
say(f"本地模型：{client.model}")

# ── 2. 构造 4 条队列任务（LLM 识别 + 执行封装为 fn；入队前资源预检） ──
CASES = [
    "对避难所创建 500 米缓冲区",
    "计算避难所的覆盖范围，边界用东京行政区",
    "分析避难所500米服务范围的盲区，边界用东京行政区",
    "计算避难所500米范围内的人口覆盖率，边界用东京行政区，人口字段用population",
]
SOURCES = [bd_path] + [p for p, _, _ in loads]

def extract_json_obj(text):
    m = re.search(r'\{.*\}', text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None

def make_task(i, user_text):
    """队列任务：内部完成 LLM 识别 + 分析链执行，返回统一 result dict。"""
    def fn():
        try:
            sp = mapper.get_system_prompt("zh")
            messages = [{"role": "system", "content": sp}, {"role": "user", "content": user_text}]
            resp = client.chat(messages, temperature=0.2)
            llm = extract_json_obj(resp) or {}
        except Exception as e:
            return {"success": False, "message": f"LLM调用失败: {type(e).__name__}: {e}", "action": ""}
        try:
            t0 = time.time()
            result = mapper.match_and_execute(resp, canvas=canvas, project=project, user_text=user_text)
            dt = round(time.time() - t0, 1)
            result = dict(result)
            result["_elapsed_s"] = dt
            return result
        except Exception as e:
            return {"success": False, "message": f"执行异常: {type(e).__name__}: {e}", "action": ""}
    return {
        "name": f"case{i}",
        "fn": fn,
        "sources": SOURCES,
        "llm_model": MODEL,  # 显存预检：4b ≈ 3GB 基线
    }

queue = get_run_queue()
queue_msgs = []
queue.state_changed.connect(queue_msgs.append)
for i, user_text in enumerate(CASES, 1):
    r = queue.enqueue(make_task(i, user_text))
    say(f"[入队] case{i}：queued={r['queued']} queue_length={r['queue_length']} "
        f"资源预检 mem={r['resource']['memory_mb']:.0f}MB vram={r['resource']['vram_mb']:.0f}MB "
        f"warnings={len(r['warnings'])}")

# ── 3. 主线程事件循环等待队列全部完成（跨线程信号由 processEvents 驱动） ──
def wait_queue_done(expected, timeout_s=900):
    from PyQt5.QtCore import QCoreApplication
    deadline = time.time() + timeout_s
    last_len = -1
    while time.time() < deadline:
        QCoreApplication.processEvents()
        st = queue.status()
        if len(st["completed"]) != last_len:
            last_len = len(st["completed"])
            say(f"[队列] 完成 {last_len}/{expected} 当前running={st['running']} 排队={len(st['pending'])}")
        if len(st["completed"]) >= expected:
            return True
        time.sleep(0.2)
    return False

ok_all = wait_queue_done(len(CASES))
say(f"[队列] 全部完成：{ok_all}；状态={json.dumps(queue.status(), ensure_ascii=False, default=str)[:400]}")
if queue_msgs:
    say(f"[声带] 队列状态消息示例：{queue_msgs[0]} | {queue_msgs[-1]}")

# ── 4. 汇总 4 链结果 ──
summary = {"total": len(CASES), "success": 0, "fail": 0, "steps": [], "queue": queue.status()}
completed_map = {c["name"]: c for c in queue.status()["completed"]}
for i, user_text in enumerate(CASES, 1):
    name = f"case{i}"
    c = completed_map.get(name, {})
    result = c.get("result") or {}
    ok = bool(result.get("success"))
    msg = (result.get("message") or "")[:400]
    stats = result.get("stats") or {}
    say(f"[{i}/{len(CASES)}] 指令：{user_text}")
    say(f"        队列完成：ok={c.get('ok')} 耗时={result.get('_elapsed_s')}s")
    say(f"        执行结果：{'成功' if ok else '失败'} — {msg}")
    step = {"index": i, "user_text": user_text, "success": ok, "message": msg, "stats": stats}
    summary["steps"].append(step)
    summary["success"] += 1 if ok else 0
    summary["fail"] += 0 if ok else 1

# ── 5. 导出结果（与 demo_run.py 一致） ──
os.makedirs(DEMO_OUT, exist_ok=True)
exported = []

def find_layer_by_prefix(prefix):
    for lyr in project.mapLayers().values():
        if lyr.name().startswith(prefix):
            return lyr
    return None

cov_layer = find_layer_by_prefix("避难所_coverage")
if cov_layer is not None:
    cov_path = os.path.join(DEMO_OUT, "避难所覆盖_500m.gpkg")
    err, msg = QgsVectorFileWriter.writeAsVectorFormat(cov_layer, cov_path, "UTF-8", driverName="GPKG")
    if err == QgsVectorFileWriter.NoError:
        say(f"[导出] 覆盖图层 -> {cov_path}")
        exported.append(cov_path)

gap_layer = find_layer_by_prefix("避难所_gap")
if gap_layer is not None:
    gap_path = os.path.join(DEMO_OUT, "避难所盲区_500m.gpkg")
    err, msg = QgsVectorFileWriter.writeAsVectorFormat(gap_layer, gap_path, "UTF-8", driverName="GPKG")
    if err == QgsVectorFileWriter.NoError:
        say(f"[导出] 盲区图层 -> {gap_path}")
        exported.append(gap_path)

summary["exported"] = exported
sum_path = os.path.join(DEMO_OUT, "demo_queue_summary.json")
with open(sum_path, "w", encoding="utf-8") as f:
    json.dump(summary, f, ensure_ascii=False, indent=2, default=str)
say(f"[导出] 汇总 -> {sum_path}")

# ── 6. 最终汇总 ──
say("")
say("========================================")
say("P3-3 demo（队列执行）完成")
say(f"- 指令总数：{summary['total']} 条，成功 {summary['success']} 条，失败 {summary['fail']} 条")
for step in summary["steps"]:
    st = step.get("stats") or {}
    if "coverage_rate" in st and "gap_rate" not in st and "pop_coverage_rate" not in st:
        say(f"- 避难所覆盖率：{st['coverage_rate']:.1f}%")
    if "gap_rate" in st:
        say(f"- 盲区率：{st['gap_rate']:.1f}%（覆盖率 {st.get('coverage_rate', 0):.1f}%）")
    if "pop_coverage_rate" in st:
        say(f"- 人口覆盖率：{st['pop_coverage_rate']:.1f}%（覆盖人口 {st.get('covered_population', 0):.0f} / 总人口 {st.get('total_population', 0):.0f}）")
say("- 输出文件：")
for p in exported:
    say(f"  {p}")
say("========================================")

qgs.exitQgis()
say("DEMO_QUEUE_DONE")
