"""template_registry — 指令模板注册表与关键词匹配。

从 instruction_mapper.py 抽离的模块级数据定义：
- 22 条三语指令模板（_INSTRUCTION_TEMPLATES）
- 三语系统提示词（_SYSTEM_PROMPT_ZH/JA/EN）
- 关键词兜底匹配（keyword_pre_match）
- 图层名自动检测（auto_detect_layers_from_text）
- 模板查找（find_template）
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

_log = logging.getLogger("instruction_mapper")

# ── 指令模板定义 ────────────────────────────────────────
# 格式: { "action": str, "zh": [...], "ja": [...], "en": [...], "handler": str, "params": {} }

_INSTRUCTION_TEMPLATES: List[Dict[str, Any]] = [
    # ── 文件操作 ──
    {
        "action": "load_layer",
        "zh": ["加载图层", "打开文件", "导入图层", "添加图层", "加载.*文件"],
        "ja": ["レイヤを読み込む", "ファイルを開く", "レイヤをインポート", "レイヤを追加"],
        "en": ["load layer", "open file", "import layer", "add layer", "load.*file"],
        "handler": "_handle_load_layer",
        "params": {"file_path": ""},
    },
    {
        "action": "save_project",
        "zh": ["保存项目", "保存工程"],
        "ja": ["プロジェクトを保存", "プロジェクト保存"],
        "en": ["save project"],
        "handler": "_handle_save_project",
        "params": {},
    },
    {
        "action": "save_as_project",
        "zh": ["另存为", "保存副本", "保存为新文件", "导出为新项目"],
        "ja": ["名前を付けて保存", "別名で保存"],
        "en": ["save as", "save as new", "save copy"],
        "handler": "_handle_save_as_project",
        "params": {},
    },
    {
        "action": "export_map",
        "zh": ["导出地图", "导出为图片", "保存为图片", "截图"],
        "ja": ["地図をエクスポート", "画像として保存", "マップをエクスポート"],
        "en": ["export map", "save as image", "export as image", "screenshot"],
        "handler": "_handle_export_map",
        "params": {"format": "png"},
    },
    # ── 视图操作 ──
    {
        "action": "zoom_to_layer",
        "zh": ["缩放到图层", "缩放到.*层", "全图显示"],
        "ja": ["レイヤにズーム", "全体表示", "ズーム.*レイヤ"],
        "en": ["zoom to layer", "zoom to full extent", "zoom full"],
        "handler": "_handle_zoom_to_layer",
        "params": {"layer_name": ""},
    },
    {
        "action": "zoom_in",
        "zh": ["放大", "拉近"],
        "ja": ["拡大", "ズームイン"],
        "en": ["zoom in"],
        "handler": "_handle_zoom_in",
        "params": {},
    },
    {
        "action": "zoom_out",
        "zh": ["缩小", "拉远"],
        "ja": ["縮小", "ズームアウト"],
        "en": ["zoom out"],
        "handler": "_handle_zoom_out",
        "params": {},
    },
    # ── 图层操作 ──
    {
        "action": "remove_layer",
        "zh": ["删除图层", "移除图层", "去掉.*层"],
        "ja": ["レイヤを削除", "レイヤを除去"],
        "en": ["remove layer", "delete layer"],
        "handler": "_handle_remove_layer",
        "params": {"layer_name": ""},
    },
    {
        "action": "list_layers",
        "zh": ["列出图层", "显示图层", "有哪些图层", "图层列表"],
        "ja": ["レイヤ一覧", "レイヤを表示", "レイヤリスト"],
        "en": ["list layers", "show layers", "what layers"],
        "handler": "_handle_list_layers",
        "params": {},
    },
    # ── 查询操作 ──
    {
        "action": "identify_feature",
        "zh": ["识别要素", "点击查询", "要素信息", "查询.*属性"],
        "ja": ["地物を識別", "クリック照会", "属性を照会"],
        "en": ["identify feature", "query feature", "feature info"],
        "handler": "_handle_identify_feature",
        "params": {},
    },
    # ── 坐标系 ──
    {
        "action": "set_crs",
        "zh": ["设置坐标系", "切换投影", "坐标系.*EPSG", "CRS.*4326"],
        "ja": ["座標系を設定", "投影法を変更", "CRS.*EPSG"],
        "en": ["set CRS", "set projection", "change coordinate system"],
        "handler": "_handle_set_crs",
        "params": {"epsg": 4326},
    },
    {
        "action": "show_crs",
        "zh": ["查看坐标系", "当前投影", "是什么坐标系"],
        "ja": ["座標系を確認", "現在の投影法"],
        "en": ["show CRS", "what projection", "current CRS"],
        "handler": "_handle_show_crs",
        "params": {},
    },
    {
        "action": "reproject_layer",
        "zh": ["重投影", "投影转换", "转换坐标系", "换个坐标系", "改投影",
               "坐标.*转换", "重投"],
        "ja": ["再投影", "投影変換", "座標変換", "投影.*変更"],
        "en": ["reproject", "change projection", "convert CRS", "transform layer"],
        "handler": "_handle_reproject_layer",
        "params": {"layer_name": "", "target_epsg": 3857},
    },
    # ── P0 新增：编辑/选择/视图 ──
    {
        "action": "toggle_editing",
        "zh": ["切换编辑", "开启编辑", "关闭编辑", "停止编辑", "编辑状态", "开始编辑"],
        "ja": ["編集切替", "編集開始", "編集停止", "編集状態"],
        "en": ["toggle editing", "start editing", "stop editing", "edit state"],
        "handler": "_handle_toggle_editing",
        "params": {"layer_name": ""},
    },
    {
        "action": "select_feature",
        "zh": ["选择要素", "框选要素", "点选要素", "条件选择", "清除选择", "选中要素"],
        "ja": ["地物選択", "矩形選択", "ポイント選択", "条件選択", "選択解除"],
        "en": ["select feature", "select by rectangle", "select by point", "select by expression", "clear selection"],
        "handler": "_handle_select_feature",
        "params": {"method": "rect"},
    },
    {
        "action": "reset_view",
        "zh": ["重置视图", "全图显示", "显示全部", "回到全图", "全景"],
        "ja": ["ビューをリセット", "全体表示", "全図表示", "全景"],
        "en": ["reset view", "zoom to full", "show all", "full extent", "panorama"],
        "handler": "_handle_reset_view",
        "params": {},
    },
    # ── P1 新增：样式/过滤/导出 ──
    {
        "action": "set_layer_style",
        "zh": ["设置样式", "图层样式", "渲染样式", "单一样式", "分类样式", "分级样式"],
        "ja": ["スタイル設定", "レイヤスタイル", "単一スタイル", "分類スタイル", "段階スタイル"],
        "en": ["set style", "layer style", "render style", "single style", "categorized style", "graduated style"],
        "handler": "_handle_set_layer_style",
        "params": {"layer_name": "", "render_type": "single", "color": "#FF0000"},
    },
    {
        "action": "load_layer_style",
        "zh": ["加载样式", "导入样式", "QML样式", "样式文件"],
        "ja": ["スタイル読込", "QMLスタイル", "スタイルファイル"],
        "en": ["load style", "import style", "QML style", "style file"],
        "handler": "_handle_load_layer_style",
        "params": {"layer_name": "", "qml_path": ""},
    },
    {
        "action": "filter_layer",
        "zh": ["过滤图层", "属性过滤", "条件筛选", "设置过滤", "清除过滤"],
        "ja": ["レイヤフィルタ", "属性フィルタ", "条件抽出", "フィルタ解除"],
        "en": ["filter layer", "attribute filter", "set filter", "clear filter", "filter by expression"],
        "handler": "_handle_filter_layer",
        "params": {"layer_name": "", "expression": ""},
    },
    {
        "action": "export_attribute",
        "zh": ["导出属性表", "导出表格", "导出CSV", "属性导出", "导出属性"],
        "ja": ["属性テーブルエクスポート", "CSV出力", "属性エクスポート"],
        "en": ["export attribute table", "export table", "export CSV", "attribute export"],
        "handler": "_handle_export_attribute",
        "params": {"layer_name": "", "output_path": ""},
    },
    {
        "action": "export_layer",
        "zh": ["导出图层", "保存图层", "输出图层", "导出为文件"],
        "ja": ["レイヤをエクスポート", "レイヤを保存", "レイヤを出力"],
        "en": ["export layer", "save layer", "output layer", "export as file"],
        "handler": "_handle_export_layer",
        "params": {"layer_name": "", "output_path": "", "format": "shp"},
    },
    # ── P2 新增：标注/字段/统计/缓冲区 ──
    {
        "action": "add_label",
        "zh": ["添加标注", "显示标注", "关闭标注", "要素标注", "标注字段"],
        "ja": ["ラベル追加", "ラベル表示", "ラベル非表示", "ラベルフィールド"],
        "en": ["add label", "show label", "hide label", "feature label", "label field"],
        "handler": "_handle_add_label",
        "params": {"layer_name": "", "field": ""},
    },
    {
        "action": "open_field_manager",
        "zh": ["字段管理", "打开字段管理器", "管理字段", "属性字段"],
        "ja": ["フィールド管理", "フィールドマネージャ", "属性フィールド"],
        "en": ["field manager", "open field manager", "manage fields", "attribute fields"],
        "handler": "_handle_open_field_manager",
        "params": {"layer_name": ""},
    },
    {
        "action": "layer_statistic",
        "zh": ["图层统计", "数据统计", "要素统计", "字段统计", "最大值", "最小值", "平均值", "求和"],
        "ja": ["レイヤ統計", "データ統計", "最大値", "最小値", "平均値", "合計"],
        "en": ["layer statistic", "data statistics", "feature count", "field statistics", "max", "min", "mean", "sum"],
        "handler": "_handle_layer_statistic",
        "params": {"layer_name": "", "method": "count"},
    },
    {
        "action": "create_buffer",
        "zh": ["缓冲区分析", "创建缓冲区", "缓冲距离", "缓冲区"],
        "ja": ["バッファ分析", "バッファ作成", "バッファ距離"],
        "en": ["buffer analysis", "create buffer", "buffer distance"],
        "handler": "_handle_create_buffer",
        "params": {"layer_name": "", "distance": 100.0},
    },
    # ── P3 新增：空间关联 ──
    {
        "action": "spatial_join",
        "zh": ["空间关联", "空间连接", "叠加到据点",
               "按位置关联", "按位置连接", "空间叠加", "关联.*图层"],
        "ja": ["空間結合", "空間ジョイン", "拠点に重ねる",
               "位置による結合", "空間オーバーレイ"],
        "en": ["spatial join", "join by location", "spatial overlay",
               "overlay to site"],
        "handler": "_handle_spatial_join",
        "params": {"target_layer": "", "join_layer": "", "predicate": "intersects",
                   "join_fields": [], "summary_mode": "first"},
    },
    # ── P4 新增：覆盖率分析 ──
    {
        "action": "coverage_analysis",
        "zh": ["覆盖率", "覆盖区域", "缓冲区覆盖", "覆盖分析",
               "\\d+米覆盖", "覆盖范围", "点覆盖", "要素覆盖分析"],
        "ja": ["カバー率", "ポイントカバー", "カバー区域", "バッファカバー", "バッファカバー率", "カバー分析",
               "カバレッジ分析"],
        "en": ["coverage", "point coverage", "buffer coverage", "feature coverage", "coverage analysis",
               "coverage rate"],
        "handler": "_handle_coverage_analysis",
        "params": {"source_layer": "", "boundary_layer": "", "radius_m": 500.0, "selected_only": False},
    },
    # ── P5 新增：盲区分析 ──
    {
        "action": "gap_analysis",
        "zh": ["盲区", "覆盖盲区", "空白区域", "未覆盖", "服务盲区", "盲区分析"],
        "ja": ["サービス盲域", "カバーされていないエリア", "空白地域", "未カバー", "サービスギャップ"],
        "en": ["gap", "gap analysis", "uncovered", "coverage gap", "service gap"],
        "handler": "_handle_gap_analysis",
        "params": {"source_layer": "", "boundary_layer": "", "radius_m": 500.0, "selected_only": False},
    },
    # ── P6 新增：人口覆盖率分析 ──
    {
        "action": "population_coverage",
        "zh": ["人口覆盖", "人口覆盖率", "人口カバー率", "人口覆盖分析"],
        "ja": ["人口カバー率", "ポピュレーションカバレッジ", "人口カバー", "人口カバー分析"],
        "en": ["population coverage", "population coverage rate", "population coverage analysis"],
        "handler": "_handle_population_coverage",
        "params": {"source_layer": "", "boundary_layer": "", "population_layer": "", "population_field": "", "radius_m": 500.0, "selected_only": False},
    },
    # ── Phase 3 地震专攻：震度态势图 ──
    {
        "action": "seismic_situation_map",
        "zh": ["震度态势图", "震度图", "地震态势图", "地震.*态势",
               "地震.*样式", "JMA.*配色", "震度.*配色", "套用震度",
               "震度.*渲染", "地震.*可视化", "震度.*视觉化"],
        "ja": ["震度態勢図", "震度マップ", "地震態勢図", "JMA.*配色",
               "震度.*配色", "震度.*可視化", "震度.*レンダリング"],
        "en": ["seismic situation map", "intensity map", "JMA style",
               "seismic.*map", "intensity.*colors", "seismic.*visual"],
        "handler": "_handle_seismic_situation_map",
        "params": {"output_path": "", "dpi": 300},
    },
    # ── Phase 4 地震专攻：建筑倒塌风险分析 ──
    {
        "action": "building_risk_analysis",
        "zh": ["建筑倒塌风险", "震度人口风险", "受灾人口估算", "建筑风险分析"],
        "ja": ["建物倒壊リスク", "震度人口リスク", "被災人口推定", "建物リスク分析"],
        "en": ["building collapse risk", "seismic population risk", "exposed population estimation", "building risk analysis"],
        "handler": "_handle_building_risk_analysis",
        "params": {"intensity_layer": "", "population_layer": "", "population_field": "", "intensity_field": "", "boundary_layer": ""},
    },
]

# ── 系统提示词（离线模式用）─────────────────────────────

_SYSTEM_PROMPT_ZH = """你是一个 GIS 桌面助手，运行在离线模式下。你必须严格遵循以下规则：

【硬性规则】
- 你只能输出 JSON，不要输出任何解释、分析、步骤描述或 Markdown 格式。
- 即使你认为指令无法执行，也必须用 JSON 回复，不要用自然语言解释原因。
- 不要输出 ```json 代码块，直接输出纯 JSON。

【输出格式】
- 能匹配到操作时：{"action": "<action_name>", "params": {"<key>": "<value>"}}
- 无法匹配时：{"action": "unknown", "message": "<简短原因，不超过30字>"}
- 回答 GIS 知识问题时：{"action": "answer", "message": "<回答内容>"}

【可用操作列表（严格从以下选择，不能自创）】

load_layer       — 加载图层文件 {"file_path": "文件完整路径"}
save_project     — 保存当前项目（无参数）
export_map       — 导出地图为图片 {"format": "png"}
zoom_to_layer    — 缩放到指定图层 {"layer_name": "图层名称"}
zoom_in          — 放大（无参数）
zoom_out         — 缩小（无参数）
remove_layer     — 删除图层 {"layer_name": "图层名称"}
list_layers      — 列出所有图层（无参数）
identify_feature — 识别要素属性（无参数）
set_crs          — 设置坐标系 {"epsg": 4326}
show_crs         — 查看当前坐标系（无参数）
reproject_layer  — 图层重投影 {"layer_name": "图层名称", "target_epsg": 3857}
toggle_editing   — 切换矢量图层编辑状态 {"layer_name": "图层名称", "target": "all(可选，关闭所有)"}
select_feature   — 要素选择 {"method": "point/rect/expression/clear", "layer_name": "图层名称(可选)", "expression": "SQL表达式(method=expression时)"}
reset_view       — 重置视图为全图范围（无参数）
set_layer_style  — 设置图层样式 {"layer_name": "图层名称", "render_type": "single/categorized/graduated", "color": "#FF0000", "field_name": "字段名"}
load_layer_style — 加载QML样式文件 {"layer_name": "图层名称", "qml_path": "QML文件路径"}
filter_layer     — 图层属性过滤 {"layer_name": "图层名称", "expression": "SQL表达式（空字符串清除过滤）"}
export_attribute — 导出属性表为CSV {"layer_name": "图层名称", "output_path": "CSV输出路径"}
export_layer     — 导出图层为文件 {"layer_name": "图层名称", "output_path": "输出路径(可选)", "format": "shp/geojson/gpkg"}
add_label        — 添加/隐藏要素标注 {"layer_name": "图层名称", "field": "标注字段名（不传或空关闭）"}
open_field_manager — 打开字段管理器 {"layer_name": "图层名称"}
layer_statistic  — 图层数据统计 {"layer_name": "图层名称", "method": "count/min/max/sum/mean/all", "field": "字段名(可选)"}
create_buffer    — 缓冲区分析 {"layer_name": "图层名称", "distance": 100.0, "selected_only": false}
spatial_join     — 空间关联分析 {"target_layer": "目标图层名", "join_layer": "关联图层名", "predicate": "intersects/within/contains", "join_fields": [], "summary_mode": "first/max/min"}
coverage_analysis — 覆盖率分析（源点要素→缓冲区→溶解→裁剪→面积覆盖率统计） {"source_layer": "源点图层名", "boundary_layer": "边界多边形图层名", "radius_m": 500.0, "selected_only": false}
gap_analysis — 盲区分析（输入点图层+边界面图层+缓冲区半径，计算覆盖盲区面积和盲区率，输出盲区图层） {"source_layer": "源点图层名", "boundary_layer": "边界多边形图层名", "radius_m": 500.0, "selected_only": false}
population_coverage — 人口覆盖率分析（输入点图层+边界面图层+人口面图层+人口字段名+缓冲区半径，计算面积和人口双重覆盖率，输出人口覆盖交集图层） {"source_layer": "源点图层名", "boundary_layer": "边界多边形图层名", "population_layer": "人口面图层名", "population_field": "人口字段名", "radius_m": 500.0, "selected_only": false}
seismic_situation_map — 震度态势图（自动识别震度/避难所/覆盖/盲区/人口图层，套用JMA震度配色，缩放画布并导出PNG） {"output_path": "PNG输出路径(可选)", "dpi": 300}
building_risk_analysis — 建筑倒塌风险分析（震度图层×人口图层，估算受灾人口和建筑风险指数） {"intensity_layer": "震度图层名", "population_layer": "人口图层名", "population_field": "人口字段名", "intensity_field": "震度概率字段名(可选)", "boundary_layer": "边界图层名(可选)"}

【示例】
用户："加载 D:/data/roads.shp"
你：{"action": "load_layer", "params": {"file_path": "D:/data/roads.shp"}}

用户："删除名为'临时图层'的图层"
你：{"action": "remove_layer", "params": {"layer_name": "临时图层"}}

用户："什么是空间索引"
你：{"action": "answer", "message": "空间索引是一种用于加速空间查询的数据结构，常用 R-tree 实现。"}

用户："帮我做个缓冲区分析"
你：{"action": "unknown", "message": "当前不支持缓冲区分析操作"}

记住：无论什么情况，只输出 JSON。"""

_SYSTEM_PROMPT_JA = """あなたは GIS デスクトップアシスタントで、オフラインモードで動作しています。以下のことができます：
1. GIS 関連の質問に回答
2. GIS 操作コマンドの実行

操作指示がある場合は、次の JSON 形式で返信してください：
{"action": "操作名", "params": {"パラメータ名": "値"}}

対応操作：load_layer, save_project, export_map, zoom_to_layer, zoom_in, zoom_out,
remove_layer, list_layers, identify_feature, set_crs, show_crs, reproject_layer,
toggle_editing, select_feature, reset_view, set_layer_style, load_layer_style,
filter_layer, export_attribute, export_layer, add_label, open_field_manager, layer_statistic, create_buffer, spatial_join, coverage_analysis, gap_analysis, population_coverage, seismic_situation_map, building_risk_analysis

不明な場合は次を返信：
{"action": "unknown", "message": "指示を認識できませんでした。より明確な説明をお試しください。"}"""

_SYSTEM_PROMPT_EN = """You are a GIS desktop assistant running in offline mode. You can:
1. Answer GIS-related questions
2. Execute GIS operation commands

When the user issues an operation command, reply with a JSON object:
{"action": "operation_name", "params": {"param_name": "value"}}

Supported actions: load_layer, save_project, export_map, zoom_to_layer, zoom_in, zoom_out,
remove_layer, list_layers, identify_feature, set_crs, show_crs, reproject_layer,
toggle_editing, select_feature, reset_view, set_layer_style, load_layer_style,
filter_layer, export_attribute, export_layer, add_label, open_field_manager, layer_statistic, create_buffer, spatial_join, coverage_analysis, gap_analysis, population_coverage, seismic_situation_map, building_risk_analysis

If unrecognized, reply:
{"action": "unknown", "message": "Unable to recognize the instruction. Please try a clearer description."}"""


# ── 关键词兜底匹配 ──────────────────────────────────────

def keyword_pre_match(user_text: str, lang: str = "zh") -> Optional[Dict[str, Any]]:
    """关键词兜底匹配：用模板触发词在用户原文中做关键词/正则匹配。

    当 LLM 返回 unknown 时，用此方法做最后一次兜底匹配，
    弥补 7B 模型语义映射能力不足的问题。

    Returns JSON-compatible instruction dict (action + params) if matched, None otherwise.
    """
    best_action = None
    best_score = 0
    best_params = {}  # noqa: F841

    for template in _INSTRUCTION_TEMPLATES:
        triggers = template.get(lang, [])
        if not triggers:
            continue

        score = 0
        for trigger in triggers:
            try:
                if re.search(trigger, user_text):
                    score += 1
            except re.error:
                # 非正则文本，降级为包含匹配
                if trigger in user_text:
                    score += 1

        if score > best_score:
            best_score = score
            best_action = template["action"]
            best_params = template.get("params", {}).copy()

    if best_action and best_score >= 1:
        _log.info(
            "关键词兜底匹配成功：action=%s score=%d user_text=%.80s",
            best_action, best_score, user_text,
        )
        return {"action": best_action, "params": best_params}
    return None


def auto_detect_layers_from_text(
    user_text: str,
    params: Dict[str, Any],
    project,
) -> Dict[str, Any]:
    """从用户文本中匹配已加载图层名称，自动填充 layer 参数。

    对 params 中值为空字符串的 layer 相关参数（target_layer, join_layer,
    layer_name 等），尝试从 project 中找出名称出现在 user_text 中的图层。
    """
    if not project:
        return params

    layer_params = ["target_layer", "join_layer", "layer_name",
                    "source_layer", "boundary_layer",
                    "population_layer", "population_field",
                    "intensity_layer", "intensity_field"]
    from qgis.core import QgsProject  # noqa: F811

    layers = list(project.mapLayers().values())
    layer_names = [layer.name() for layer in layers]

    # 收集 user_text 中出现的图层名（精确 + 前缀模糊匹配）
    found_names = []
    for n in layer_names:
        if n in user_text:
            found_names.append(n)
        else:
            # 模糊匹配：图层名以 user_text 中的某个词开头（如"东京行政区"匹配"东京行政区_GADM EPSG3857"）
            # 取图层名中第一个 _ 或空格之前的前缀，检查是否出现在 user_text 中
            prefix = n
            for sep in ("_", " "):
                idx = n.find(sep)
                if idx > 0:
                    prefix = n[:idx]
                    break
            if prefix and len(prefix) >= 2 and prefix in user_text:
                found_names.append(n)
                _log.info("模糊匹配图层：'%s' ← 前缀 '%s' 出现在用户文本中", n, prefix)

    if not found_names:
        return params

    filled = dict(params)

    # 多图层歧义消除：按 layer name 中的关键字 hint 分配角色
    intensity_hints = ["震度", "intensity", "jma", "seismic"]
    population_hints = ["人口", "pop", "population"]

    def _match_hint(layer_name, hints):
        lower = layer_name.lower()
        return any(h.lower() in lower for h in hints)

    for key in layer_params:
        if key in filled and not filled[key]:
            if key == "target_layer" and len(found_names) >= 2:
                filled[key] = found_names[1]
            elif key == "join_layer" and found_names:
                filled[key] = found_names[0]
            elif key == "layer_name" and found_names:
                filled[key] = found_names[0]
            # ── source_layer / boundary_layer：覆盖分析专用 ──
            elif key == "source_layer":
                if len(found_names) >= 2:
                    filled[key] = found_names[0]   # 用户先提到的 → source
                elif found_names:
                    filled[key] = found_names[0]
            elif key == "boundary_layer":
                if len(found_names) >= 2:
                    filled[key] = found_names[1]   # 用户后提到的 → boundary
                elif found_names:
                    filled[key] = found_names[0]
            elif key == "intensity_layer":
                # 优先匹配含"震度"/"intensity"的图层名
                candidates = [n for n in found_names if _match_hint(n, intensity_hints)]
                if candidates:
                    filled[key] = candidates[0]
                elif found_names:
                    filled[key] = found_names[0]
            elif key == "population_layer":
                # 优先匹配含"人口"/"pop"的图层名
                candidates = [n for n in found_names if _match_hint(n, population_hints)]
                if candidates:
                    filled[key] = candidates[0]
                elif len(found_names) >= 2 and "intensity_layer" in filled and filled["intensity_layer"]:
                    # 排除已分配给 intensity 的图层
                    others = [n for n in found_names if n != filled.get("intensity_layer")]
                    if others:
                        filled[key] = others[0]

    return filled


def find_template(action: str) -> Optional[Dict[str, Any]]:
    """按 action 名称查找模板。"""
    for t in _INSTRUCTION_TEMPLATES:
        if t["action"] == action:
            return t
    return None
