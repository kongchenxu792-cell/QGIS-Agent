# -*- coding: utf-8 -*-
r"""scripts/demo_run.py — 本地大模型端到端演示（Solo 指派，真实离线 LLM 不绕过）

流程：加载测试数据 → 逐条中文自然语言指令 → 真实调用 qwen3.5-4b 离线推理
（InstructionMapper.match_and_execute 离线路径）→ 打印 LLM 识别 action/params
与执行结果 → 导出覆盖图层 / 盲区地图 PNG / 汇总到 user_data/demo_output/。

运行：qgis-portable\apps\Python312\python.exe scripts\demo_run.py  （Windows）
"""
import os, sys, json, re, time, traceback

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
say("本地大模型演示启动（qwen3.5-4b 离线端到端）")
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

# ── 1. 加载测试数据（稳妥数据，禁止 12 万面全域） ──
project = QgsProject.instance()
# 注意：避难所完整文件避难所_EPSG3857.gpkg 含 11.5 万点，会触发 create_buffer
# 的 10000 要素保护上限；且全量 11.5 万点做缓冲过重。故按东京行政区范围过滤
# 后采样前 100 个要素生成内存图层「避难所」（EPSG:3857，与行政区/人口图层
# 同 CRS，保证 500 米缓冲区按米计算正确）。
from qgis.core import QgsFeature, QgsFeatureRequest
def load_shelter_subset(path, name, boundary_extent, limit=100):
    """从避难所文件中选取落在东京行政区范围内的前 limit 个点生成内存图层。

    注意：避难所完整文件覆盖全日本（前 100 条位于北海道），若不按边界范围
    过滤，空间筛选（QgsSpatialIndex + bbox）将 0 命中，覆盖率链路全空。
    """
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

# 先加载东京行政区以获取边界范围（用于避难所采样过滤）
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

# ── 2. 逐条中文自然语言指令，真实调用离线 LLM ──
CASES = [
    "对避难所创建 500 米缓冲区",
    "计算避难所的覆盖范围，边界用东京行政区",
    "分析避难所500米服务范围的盲区，边界用东京行政区",
    "计算避难所500米范围内的人口覆盖率，边界用东京行政区，人口字段用population",
]

def extract_json_obj(text):
    m = re.search(r'\{.*\}', text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None

summary = {"total": len(CASES), "success": 0, "fail": 0, "steps": []}
for i, user_text in enumerate(CASES, 1):
    say("")
    say(f"[{i}/{len(CASES)}] 指令：{user_text}")
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
        summary["steps"].append({"index": i, "user_text": user_text, "success": False, "note": f"LLM调用失败: {e}"})
        continue

    try:
        t0 = time.time()
        result = mapper.match_and_execute(resp, canvas=canvas, project=project, user_text=user_text)
        dt = round(time.time() - t0, 1)
        ok = bool(result.get("success"))
        msg = (result.get("message") or "")[:400]
        say(f"        执行结果：{'成功' if ok else '失败'} ({dt}s) — {msg}")
        stats = result.get("stats") or {}
        step = {
            "index": i, "user_text": user_text,
            "llm_action": llm_action, "llm_params": llm_params,
            "final_action": result.get("action"),
            "success": ok, "message": msg, "stats": stats,
        }
        summary["steps"].append(step)
        if ok:
            summary["success"] += 1
        else:
            summary["fail"] += 1
    except Exception as e:
        say(f"        执行异常：{type(e).__name__}: {e}")
        summary["fail"] += 1
        summary["steps"].append({"index": i, "user_text": user_text, "success": False, "note": f"执行异常: {e}"})

# ── 3. 导出结果：覆盖图层 / 盲区地图 PNG / 汇总 ──
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
    else:
        say(f"[导出] 覆盖图层写入失败：{msg}")

gap_layer = find_layer_by_prefix("避难所_gap")
if gap_layer is not None:
    gap_path = os.path.join(DEMO_OUT, "避难所盲区_500m.gpkg")
    err, msg = QgsVectorFileWriter.writeAsVectorFormat(gap_layer, gap_path, "UTF-8", driverName="GPKG")
    if err == QgsVectorFileWriter.NoError:
        say(f"[导出] 盲区图层 -> {gap_path}")
        exported.append(gap_path)

# 盲区地图 PNG：用 QgsMapRendererParallelJob 离屏渲染当前 project 图层
try:
    from qgis.core import QgsMapSettings, QgsMapRendererParallelJob, QgsRectangle
    from qgis.PyQt.QtCore import QSize
    from qgis.PyQt.QtGui import QImage, QColor
    from qgis.PyQt.QtWidgets import QApplication
    _app = QApplication.instance() or QApplication([])
    settings = QgsMapSettings()
    settings.setLayers([l for l in project.mapLayers().values() if l.isValid()])
    # 优先用输出图层 extent（coverage/gap），避免东京行政区含离岛导致全图过小
    out_lyrs = [find_layer_by_prefix(p) for p in ("避难所_gap", "避难所_coverage")]
    out_lyrs = [l for l in out_lyrs if l is not None]
    if out_lyrs:
        ext = QgsRectangle()
        for l in out_lyrs:
            if ext.isEmpty():
                ext = QgsRectangle(l.extent())
            else:
                ext.combineExtentWith(l.extent())
        settings.setExtent(ext)
    else:
        boundary = project.mapLayersByName("东京行政区")
        if boundary:
            settings.setExtent(boundary[0].extent())
        else:
            settings.setExtent(project.mapLayers().values().__iter__().__next__().extent())
    settings.setOutputSize(QSize(1280, 900))
    settings.setBackgroundColor(QColor(255, 255, 255))
    job = QgsMapRendererParallelJob(settings)
    job.start()
    job.waitForFinished()
    img = job.renderedImage()
    png_path = os.path.join(DEMO_OUT, "盲区地图.png")
    if not img.isNull():
        img.save(png_path, "PNG")
        say(f"[导出] 盲区地图 PNG -> {png_path} ({os.path.getsize(png_path)} bytes)")
        exported.append(png_path)
    else:
        say("[导出] 盲区地图 PNG 渲染为空")
except Exception as e:
    say(f"[导出] 盲区地图 PNG 异常：{type(e).__name__}: {e}")

summary["exported"] = exported
sum_path = os.path.join(DEMO_OUT, "demo_summary.json")
with open(sum_path, "w", encoding="utf-8") as f:
    json.dump(summary, f, ensure_ascii=False, indent=2)
say(f"[导出] 汇总 -> {sum_path}")
exported.append(sum_path)

# ── 4. 最终汇总（CEO 一眼看懂） ──
say("")
say("========================================")
say("本地大模型演示完成")
say(f"- 指令总数：{summary['total']} 条，成功 {summary['success']} 条")
for step in summary["steps"]:
    if step.get("stats"):
        st = step["stats"]
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
say("DEMO_DONE")
