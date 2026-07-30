"""seismic_situation_map — 震度态势图（缩小版）。

Phase 3 地震专攻第五步：
- JMA 震度配色自动套用
- 图层语义自动识别（震度/避难所/覆盖/盲区/人口）
- 画布缩放 + PNG 导出

不碰 Print Layout。图面装饰留给 QGIS GUI。

Author: Marvis | Review: Trea Solo | 2026-06-23
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from qgis.core import (
    QgsCategorizedSymbolRenderer,
    QgsFillSymbol,
    QgsGraduatedSymbolRenderer,
    QgsMarkerSymbol,
    QgsProject,
    QgsRendererCategory,
    QgsRendererRange,
    QgsSingleSymbolRenderer,
    QgsVectorLayer,
)
from qgis.PyQt.QtGui import QColor, QImage, QPainter

_log = logging.getLogger("seismic_situation_map")

# ═══════════════════════════════════════════════════════════
# JMA 震度配色常量
# ═══════════════════════════════════════════════════════════

JMA_INTENSITY_COLORS: Dict[str, str] = {
    "7":    "#800080",   # 紫紅
    "6強":  "#FF0000",   # 赤
    "6弱":  "#FF8800",   # 橙
    "5強":  "#FFCC00",   # 黄
    "5弱":  "#FFFF00",   # 黄色
    "4":    "#AAFF55",   # 淡緑
    "3":    "#55AAFF",   # 青
    "2以下": "#CCCCCC",  # 灰
}

JMA_INTENSITY_ORDER: List[str] = [
    "7", "6強", "6弱", "5強", "5弱", "4", "3", "2以下"
]

JMA_FIELD_CANDIDATES: List[str] = [
    "震度", "intensity", "JMA_INT", "SI", "震度階級", "seismic_intensity"
]

# J-SHIS 震度字段正则：T30_I50_PS → 30年周期 震度5.0 PS值
JMA_INTENSITY_FIELD_PATTERN = re.compile(r'^T\d+_I(\d+)_\w+$')

# ═══════════════════════════════════════════════════════════
# 图层识别规则（按优先级排序，首次命中即归属）
# ═══════════════════════════════════════════════════════════
# 格式: (name_keywords, geom_constraint, layer_type, extra_rule)
# geom_constraint: None=any, "point"/"polygon"/"line"
# extra_rule: {"exclude": [...]}  命中但含排除词则跳过

# 注：population 规则置于 coverage 之前，因 population_coverage 模板产出的
# 图层名含 "_coverage_"（如 避难所_EPSG3857_coverage_500m），不含 "population" 关键词。
# 若 coverage_analysis 产出同名 _coverage_ 图层，会被误判为人口——此系命名冲突，
# 彻底修复需在 population_coverage 模板中改变 output 图层命名规则。
LAYER_RECOGNITION_RULES: List[Tuple[List[str], Optional[str], str, Optional[Dict]]] = [
    (["spatial_join", "震度", "join"],  None,      "intensity",  None),
    (["shelter", "避難所", "避难所"],              "point",   "shelter",    None),
    (["population", "pop_intersect", "_coverage_"], "polygon", "population", None),
    (["coverage"],                       None,      "coverage",   {"exclude": ["gap", "population", "pop"]}),
    (["gap"],                            None,      "gap",        None),
]

# 图层类型 → 中文标签
LAYER_TYPE_LABELS: Dict[str, str] = {
    "intensity":  "震度图层",
    "shelter":    "避难所图层",
    "coverage":   "覆盖区图层",
    "gap":        "盲区图层",
    "population": "人口图层",
}

# ═══════════════════════════════════════════════════════════
# SeismicSituationMap 核心类
# ═══════════════════════════════════════════════════════════


class SeismicSituationMap:
    """震度态势图（缩小版）。

    职责：JMA震度配色 + 图层语义识别 + 画布缩放 + PNG导出。
    不碰 Print Layout，图面装饰留给 QGIS GUI。

    Parameters
    ----------
    project : QgsProject | None
        当前 QGIS 项目实例，None 时取 QgsProject.instance()。
    canvas : QgsMapCanvas | None
        当前画布，None 时取 iface.mapCanvas() 兜底。
    """

    def __init__(self, project=None, canvas=None) -> None:
        self._project = project or QgsProject.instance()
        self._canvas = canvas

    # ── 图层语义识别 ──────────────────────────────────

    def recognize_layers(self) -> Dict[str, List[QgsVectorLayer]]:
        """对当前所有图层按规则分类。

        Returns
        -------
        {
            "intensity":  [...],
            "shelter":    [...],
            "coverage":   [...],
            "gap":        [...],
            "population": [...],
            "unmatched":  [...],
        }
        """
        result: Dict[str, List[QgsVectorLayer]] = {
            "intensity": [], "shelter": [], "coverage": [],
            "gap": [], "population": [], "unmatched": [],
        }

        layers = list(self._project.mapLayers().values())

        for layer in layers:
            if not isinstance(layer, QgsVectorLayer):
                continue

            layer_name = layer.name()
            geom_type = self._geometry_type_name(layer)
            matched = self._match_layer_type(layer_name, geom_type)

            if matched in result:
                result[matched].append(layer)
            else:
                result["unmatched"].append(layer)

        # 日志输出识别结果
        for ltype, llist in result.items():
            if llist:
                names = [lyr.name() for lyr in llist]
                _log.info("recognize_layers: %s → %s", ltype, names)

        return result

    def _match_layer_type(self, layer_name: str, geometry_type: str) -> Optional[str]:
        """单图层→类别匹配。不命中返回 None。"""
        name_lower = layer_name.lower()
        for keywords, geom_constraint, layer_type, extra_rule in LAYER_RECOGNITION_RULES:
            # 1. 关键词匹配
            if not any(kw.lower() in name_lower for kw in keywords):
                continue

            # 2. 几何约束
            if geom_constraint and geometry_type != geom_constraint:
                continue

            # 3. 排除规则
            if extra_rule and "exclude" in extra_rule:
                exclude_words = extra_rule["exclude"]
                if any(ex.lower() in name_lower for ex in exclude_words):
                    continue

            return layer_type

        return None

    @staticmethod
    def _geometry_type_name(layer: QgsVectorLayer) -> str:
        """返回图层几何类型的字符串：point / polygon / line / unknown。"""
        try:
            geom_type = layer.geometryType()
            mapping = {0: "point", 1: "line", 2: "polygon"}
            return mapping.get(geom_type, "unknown")
        except Exception:
            return "unknown"

    # ── 批量样式 ──────────────────────────────────────

    def apply_all_styles(self, recognized: Dict[str, List[QgsVectorLayer]]) -> Dict[str, str]:
        """批量调用各样式方法。

        Returns
        -------
        {"intensity": "OK (1个图层)", "shelter": "跳过(无图层)", ...}
        """
        style_methods = {
            "intensity":  self.apply_jma_style,
            "shelter":    self.apply_shelter_style,
            "coverage":   self.apply_coverage_style,
            "gap":        self.apply_gap_style,
            "population": self.apply_population_density_style,
        }

        results: Dict[str, str] = {}
        for ltype, layers in recognized.items():
            if ltype == "unmatched":
                results[ltype] = f"跳过({len(layers)}个未识别图层)"
                continue

            if not layers:
                results[ltype] = "跳过(无图层)"
                continue

            messages = []
            method = style_methods.get(ltype)
            if method:
                for layer in layers:
                    msg = method(layer)
                    messages.append(f"{layer.name()}: {msg}")
            results[ltype] = "; ".join(messages) if messages else "无操作"

        return results

    # ── JMA 震度配色 ──────────────────────────────────

    def apply_jma_style(self, layer: QgsVectorLayer) -> str:
        """JMA 震度配色。读取震度字段 → 分类着色。

        字段识别回退链：JMA_FIELD_CANDIDATES 依次尝试 → 首个匹配字段。
        QgsCategorizedSymbolRenderer，按 JMA_INTENSITY_ORDER 排序输出。

        Returns
        -------
        结果描述字符串，如 "OK (字段: 震度, 8 类)" 或 "震度字段未找到"
        """
        # 1. 查找震度字段
        intensity_field = self._find_intensity_field(layer)
        if intensity_field is None:
            available = [f.name() for f in layer.fields()]
            _log.warning(
                "apply_jma_style: %s 中未找到震度字段，候选=%s 实际字段=%s",
                layer.name(), JMA_FIELD_CANDIDATES, available,
            )
            return f"震度字段未找到 (实际字段: {available[:6]})"

        # 2. 读取字段中实际出现的震度值
        field_idx = layer.fields().indexOf(intensity_field)
        raw_values = set()
        for feat in layer.getFeatures():
            val = feat.attribute(field_idx)
            if val is not None and str(val).strip():
                raw_values.add(str(val).strip())

        # 3. 归一化震度值 → JMA key
        value_map: Dict[str, str] = {}
        for raw in raw_values:
            jma_key = self._normalize_intensity(raw)
            if jma_key in JMA_INTENSITY_COLORS:
                value_map[raw] = jma_key

        if not value_map:
            # Fallback: 检测是否为概率面（全部为 0-1 浮点数）→ Graduated
            raw_list = list(raw_values)
            try:
                numeric_vals = [float(v) for v in raw_list]
                if all(0 <= v <= 1 for v in numeric_vals):
                    return self._apply_probability_graduated(
                        layer, intensity_field, numeric_vals)
            except ValueError:
                pass
            _log.warning(
                "apply_jma_style: %s 字段值无法匹配 JMA 常量，raw=%s",
                layer.name(), raw_list[:10],
            )
            return f"震度值无法匹配 (raw: {raw_list[:5]})"

        # 4. 构建分类渲染器
        categories = []
        # 按 JMA_INTENSITY_ORDER 排序输出
        seen_keys = set()
        for jma_key in JMA_INTENSITY_ORDER:
            for raw_val, mapped_key in value_map.items():
                if mapped_key == jma_key and mapped_key not in seen_keys:
                    seen_keys.add(mapped_key)
                    color = QColor(JMA_INTENSITY_COLORS[mapped_key])
                    symbol = QgsFillSymbol.createSimple({
                        "color": color.name(),
                        "outline_color": "#333333",
                        "outline_width": "0.3",
                    })
                    category = QgsRendererCategory(raw_val, symbol, raw_val)
                    categories.append(category)
                    break

        renderer = QgsCategorizedSymbolRenderer(intensity_field, categories)
        layer.setRenderer(renderer)
        layer.triggerRepaint()

        _log.info(
            "apply_jma_style: %s 完成，字段=%s 分类数=%d",
            layer.name(), intensity_field, len(categories),
        )
        return f"OK (字段: {intensity_field}, {len(categories)} 类)"


    def _apply_probability_graduated(
        self, layer, field_name,
        values
    ):
        """Probability surface fallback: 5-class quantile breaks with JMA gradient.

        When intensity field values are all 0-1 probabilities (cannot map to
        discrete JMA keys), use QgsGraduatedSymbolRenderer with 5-class
        quantile breaks. Color ramp: purple(high) -> red -> orange -> yellow -> gray(low).
        """
        jma_ramp = ["#800080", "#FF0000", "#FF8800", "#FFCC00", "#CCCCCC"]

        # Quantile breaks (manual, no external classification class)
        sorted_vals = sorted(values)
        n = len(sorted_vals)
        if n < 5:
            # Too few unique values, use as-is
            actual_classes = max(1, len(set(values)))
            step = max(1, n // actual_classes) if actual_classes > 1 else n
            breaks = []
            for i in range(actual_classes):
                idx = min(i * step, n - 1)
                breaks.append(sorted_vals[idx])
            breaks.append(max(values) + 0.001)
        else:
            actual_classes = 5
            breaks = []
            for i in range(actual_classes + 1):
                idx = min(i * n // actual_classes, n - 1)
                breaks.append(sorted_vals[idx])
            breaks[-1] = max(values) + 0.001

        ranges = []
        for i in range(actual_classes):
            lower = breaks[i]
            upper = breaks[i + 1]
            label = "%.3f - %.3f" % (lower, upper)
            color = QColor(jma_ramp[i % len(jma_ramp)])
            symbol = QgsFillSymbol.createSimple({
                "color": color.name(),
                "outline_color": "#333333",
                "outline_width": "0.3",
            })
            rng = QgsRendererRange(lower, upper, symbol, label)
            ranges.append(rng)

        renderer = QgsGraduatedSymbolRenderer(field_name, ranges)
        layer.setRenderer(renderer)
        layer.triggerRepaint()

        _log.info(
            "_apply_probability_graduated: %s field=%s %d-class quantile",
            layer.name(), field_name, actual_classes,
        )
        return "OK (prob surface Graduated, field: %s, %d classes)" % (
            field_name, actual_classes)

    @staticmethod
    def _find_intensity_field(layer: QgsVectorLayer) -> Optional[str]:
        """在图层字段中查找震度字段名。精确匹配→正则回退。"""
        field_names = [f.name() for f in layer.fields()]
        # 1. 精确匹配候选列表
        for candidate in JMA_FIELD_CANDIDATES:
            if candidate in field_names:
                return candidate
        # 2. 正则回退：T30_I50_PS 等 J-SHIS 格式
        for fname in field_names:
            if JMA_INTENSITY_FIELD_PATTERN.match(fname):
                return fname
        return None

    @staticmethod
    def _normalize_intensity(raw: str) -> str:
        """将各种震度值写法归一化为 JMA_INTENSITY_COLORS 的 key。

        例如: "震度7"→"7", "6強"→"6強", "7"→"7", "震度6強"→"6強"
        """
        raw = raw.replace("震度", "").replace("階級", "").replace("阶级", "").strip()

        # 直接匹配
        if raw in JMA_INTENSITY_COLORS:
            return raw

        # 模糊匹配
        raw_normalized = raw.replace("-", "").replace("～", "").replace("以上", "").replace("以下", "")
        for key in JMA_INTENSITY_COLORS:
            key_norm = key.replace("強", "强").replace("弱", "弱")
            raw_norm = raw_normalized.replace("強", "强").replace("弱", "弱")
            if raw_norm == key_norm:
                return key

        return raw

    # ── 避难所样式 ────────────────────────────────────

    def apply_shelter_style(self, layer: QgsVectorLayer) -> str:
        """避难所图层：QgsSingleSymbolRenderer，绿色圆点。"""
        symbol = QgsMarkerSymbol.createSimple({
            "name": "circle",
            "color": "#00AA00",
            "size": "4",
            "outline_color": "#006600",
            "outline_width": "0.5",
        })
        layer.setRenderer(QgsSingleSymbolRenderer(symbol))
        layer.triggerRepaint()
        _log.info("apply_shelter_style: %s → 绿色圆点", layer.name())
        return "OK (绿色圆点)"

    # ── 覆盖区样式 ────────────────────────────────────

    def apply_coverage_style(self, layer: QgsVectorLayer) -> str:
        """覆盖区图层：蓝色半透明 #3388FF 40%。"""
        symbol = QgsFillSymbol.createSimple({
            "color": "#3388FF",
            "color_opacity": "0.4",
            "outline_color": "#2266CC",
            "outline_width": "0.5",
        })
        layer.setRenderer(QgsSingleSymbolRenderer(symbol))
        layer.triggerRepaint()
        _log.info("apply_coverage_style: %s → 蓝40%%", layer.name())
        return "OK (蓝色半透明 40%)"

    # ── 盲区样式 ──────────────────────────────────────

    def apply_gap_style(self, layer: QgsVectorLayer) -> str:
        """盲区图层：红色半透明 #FF4444 20%。"""
        symbol = QgsFillSymbol.createSimple({
            "color": "#FF4444",
            "color_opacity": "0.2",
            "outline_color": "#CC3333",
            "outline_width": "0.5",
        })
        layer.setRenderer(QgsSingleSymbolRenderer(symbol))
        layer.triggerRepaint()
        _log.info("apply_gap_style: %s → 红20%%", layer.name())
        return "OK (红色半透明 20%)"

    # ── 人口密度样式 ──────────────────────────────────

    def apply_population_density_style(self, layer: QgsVectorLayer) -> str:
        """人口密度图层：QgsGraduatedSymbolRenderer, 5级 natural breaks (Jenks)。
        颜色渐变：黄→红。人口字段回退：population → JINKO → 首个数值字段。
        """
        # 1. 查找人口字段
        pop_field = self._find_population_field(layer)
        if pop_field is None:
            available = [f.name() for f in layer.fields()]
            _log.warning(
                "apply_population_density_style: %s 中未找到人口字段，实际字段=%s",
                layer.name(), available,
            )
            return f"人口字段未找到 (实际字段: {available[:6]})"

        # 2. 分级渲染
        from qgis.core import QgsClassificationJenks

        renderer = QgsGraduatedSymbolRenderer()
        renderer.setClassAttribute(pop_field)

        classification = QgsClassificationJenks()
        renderer.setClassificationMethod(classification)
        renderer.updateClasses(layer, 5)

        # 3. 黄→红渐变
        color_start = QColor(255, 255, 128)  # 黄
        color_end = QColor(255, 0, 0)        # 红
        ramp = renderer.sourceColorRamp()
        if ramp is not None:
            ramp.setColor1(color_start)
            ramp.setColor2(color_end)
        renderer.setSourceColorRamp(ramp)

        layer.setRenderer(renderer)
        layer.triggerRepaint()

        _log.info(
            "apply_population_density_style: %s → 5级Jenks 字段=%s",
            layer.name(), pop_field,
        )
        return f"OK (5级 Jenks, 字段: {pop_field})"

    @staticmethod
    def _find_population_field(layer: QgsVectorLayer) -> Optional[str]:
        """人口字段名回退链：population → JINKO → 首个数值字段。"""
        from PyQt5.QtCore import QVariant

        field_names = [f.name() for f in layer.fields()]

        # 优先级 1: population
        if "population" in field_names:
            return "population"

        # 优先级 2: JINKO
        if "JINKO" in field_names:
            return "JINKO"

        # 优先级 3: 首个数值类型字段
        for f in layer.fields():
            if f.type() in (QVariant.Int, QVariant.Double, QVariant.LongLong):
                return f.name()

        return None

    # ── 画布缩放 ──────────────────────────────────────

    def zoom_to_extent(
        self,
        layers: List[QgsVectorLayer],
        margin_ratio: float = 0.05,
    ) -> str:
        """缩放到给定图层的组合范围。

        Parameters
        ----------
        layers : list of QgsVectorLayer
            用于计算组合范围的图层。
        margin_ratio : float
            四周边距比例，默认 5%。

        Returns
        -------
        结果描述，如 "已缩放到 4 个图层范围" 或 "无有效图层"
        """
        if not self._canvas:
            return "跳过 (无画布)"

        valid_layers = [lyr for lyr in layers if lyr.isValid() and lyr.featureCount() > 0]
        if not valid_layers:
            return "跳过 (无有效图层)"

        # 组合 extent
        from qgis.core import QgsRectangle

        combined = QgsRectangle()
        first = True
        for lyr in valid_layers:
            extent = lyr.extent()
            if first:
                combined = QgsRectangle(extent)
                first = False
            else:
                combined.combineExtentWith(extent)

        # 加 margin
        w = combined.width()
        h = combined.height()
        combined.setXMinimum(combined.xMinimum() - w * margin_ratio)
        combined.setXMaximum(combined.xMaximum() + w * margin_ratio)
        combined.setYMinimum(combined.yMinimum() - h * margin_ratio)
        combined.setYMaximum(combined.yMaximum() + h * margin_ratio)

        self._canvas.setExtent(combined)
        self._canvas.refresh()

        layer_names = [lyr.name() for lyr in valid_layers]
        _log.info("zoom_to_extent: %d 个图层 → %s", len(valid_layers), layer_names)
        return f"已缩放到 {len(valid_layers)} 个图层范围"

    # ── PNG 导出 ──────────────────────────────────────

    def export_png(self, output_path: str, dpi: int = 300) -> str:
        """QgsMapRendererCustomPainterJob → QImage → save PNG。

        Parameters
        ----------
        output_path : str
            PNG 输出绝对路径。
        dpi : int
            导出 DPI，默认 300。

        Returns
        -------
        结果描述，如 "PNG 已导出: D:/xxx/seismic_map.png (1920×1080)"
        """
        if not self._canvas:
            return "错误 (无画布)"

        # 确保输出目录存在
        output_dir = os.path.dirname(output_path)
        if output_dir and not os.path.exists(output_dir):
            os.makedirs(output_dir, exist_ok=True)

        from qgis.core import (
            QgsMapRendererCustomPainterJob,
            QgsMapSettings,
        )
        from qgis.PyQt.QtCore import QSize

        settings: QgsMapSettings = self._canvas.mapSettings()
        extent = settings.extent()

        # DPI 比例因子
        scale_factor = dpi / settings.outputDpi()
        pixel_width = int(settings.outputSize().width() * scale_factor)
        pixel_height = int(settings.outputSize().height() * scale_factor)

        image = QImage(
            QSize(pixel_width, pixel_height),
            QImage.Format_ARGB32_Premultiplied,
        )
        image.setDotsPerMeterX(int(dpi / 0.0254))
        image.setDotsPerMeterY(int(dpi / 0.0254))
        image.fill(QColor(255, 255, 255))

        painter = QPainter(image)

        render_settings = QgsMapSettings(settings)
        render_settings.setOutputSize(QSize(pixel_width, pixel_height))
        render_settings.setExtent(extent)
        render_settings.setOutputDpi(dpi)

        job = QgsMapRendererCustomPainterJob(render_settings, painter)
        job.start()
        job.waitForFinished()
        painter.end()

        image.save(output_path, "PNG")

        _log.info(
            "export_png: %s (%d×%d, %d dpi)",
            output_path, pixel_width, pixel_height, dpi,
        )
        return f"PNG 已导出: {output_path} ({pixel_width}×{pixel_height}, {dpi} dpi)"
