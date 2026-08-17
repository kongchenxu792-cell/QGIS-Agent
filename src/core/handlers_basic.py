"""handlers_basic — 基础指令处理器 Mixin。

从 instruction_mapper.py 抽离的 24 个基础 handler 方法，
通过 Mixin 继承注入到 InstructionMapper 中。
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
from typing import Any, Dict

_log = logging.getLogger("instruction_mapper")


class HandlersBasicMixin:
    """基础指令处理器（24 handlers + 共享工具函数）。

    以 Mixin 形式被 InstructionMapper 继承，所有 handler 方法签名
    保持 self.xxx() 不变，通过 getattr 在 match_and_execute 中动态路由。
    """

    # ── 共享工具函数 ─────────────────────────────────────

    @staticmethod
    def _find_layer(project, layer_name: str):
        """按名称匹配图层：精确匹配优先，模糊匹配兜底。

        模糊匹配若命中派生图层（如源图层名是"避难所"，派生图层叫
        "避难所_buffer_500.0m"），会把 Polygon 误判为源图层，导致
        source_is_point 等守卫误报。因此先做大小写不敏感的精确匹配。
        """
        from qgis.core import QgsProject
        proj = project or QgsProject.instance()
        # 精确匹配优先（大小写不敏感）
        for _lid, layer in proj.mapLayers().items():
            if layer.name().lower() == layer_name.lower():
                return layer
        # 模糊匹配兜底
        for _lid, layer in proj.mapLayers().items():
            if layer_name.lower() in layer.name().lower():
                return layer
        return None

    @staticmethod
    def _check_vector(layer) -> Dict[str, Any] | None:
        """检查图层是否为矢量图层，不是则返回错误字典。"""
        from qgis.core import QgsVectorLayer
        if not isinstance(layer, QgsVectorLayer):
            return {"success": False, "message": "编辑操作仅支持矢量图层"}
        return None

    @staticmethod
    def _validate_shapefile_sidecars(shp_path: str):
        """防回归守护线：shapefile 配套文件完整性校验。

        Shapefile 是一组同名不同扩展名的文件。缺少 .dbf / .shx 会导致
        QGIS 读取字段数为 0 或加载失败（历史案例：RECORD #14）。

        Returns
        -------
        (is_complete : bool, missing : list[str])
        """
        base = os.path.splitext(shp_path)[0]
        required = {".shp", ".shx", ".dbf"}
        optional = {".prj"}

        missing_required = []
        missing_optional = []

        for ext in required:
            path = base + ext
            if not os.path.exists(path):
                missing_required.append(path)

        for ext in optional:
            path = base + ext
            if not os.path.exists(path):
                missing_optional.append(path)

        if missing_required:
            _log.warning(
                "shapefile 配套文件缺失（必须）：%s → %s", base, missing_required,
            )
        if missing_optional:
            _log.warning(
                "shapefile 配套文件缺失（可选）：%s → %s", base, missing_optional,
            )

        return (len(missing_required) == 0, missing_required + missing_optional)

    @staticmethod
    def _export_attribute_table(layer, output_path: str) -> Dict[str, Any]:
        """导出矢量图层属性表为 CSV（共享工具函数）。"""
        from qgis.core import QgsVectorFileWriter
        error = QgsVectorFileWriter.writeAsVectorFormat(
            layer, output_path, "UTF-8", layer.crs(), "CSV",
            layerOptions=["GEOMETRY=AS_WKT"]
        )
        if error[0] == QgsVectorFileWriter.NoError:
            size_kb = os.path.getsize(output_path) / 1024 if os.path.exists(output_path) else 0
            return {"success": True, "message": f"属性表已导出：{output_path}（{size_kb:.1f} KB）"}
        return {"success": False, "message": f"导出失败：{error}"}

    # ── 文件操作 handler ─────────────────────────────────

    def _handle_load_layer(self, canvas=None, project=None, file_path: str = "", **kwargs) -> Dict[str, Any]:
        from qgis.core import QgsVectorLayer, QgsRasterLayer, QgsProject
        proj = project or QgsProject.instance()

        # ── 参数键别名兼容：LLM 可能输出 filename / path ──
        file_path = file_path or kwargs.get("filename") or kwargs.get("path") or ""

        if not file_path or not os.path.exists(file_path):
            return {"success": False, "message": f"文件不存在：{file_path}"}

        name = os.path.splitext(os.path.basename(file_path))[0]
        ext = os.path.splitext(file_path)[1].lower()
        raster_exts = {'.tif', '.tiff', '.png', '.jpg', '.jpeg', '.bmp'}

        if ext in raster_exts:
            layer = QgsRasterLayer(file_path, name)
        else:
            # ── 防回归守护线：shapefile 配套文件完整性校验 ──
            if file_path.lower().endswith(".shp"):
                complete, missing = self._validate_shapefile_sidecars(file_path)
                if not complete:
                    return {
                        "success": False,
                        "message": (
                            f"Shapefile 配套文件不完整：{name}。"
                            f"缺失：{', '.join(os.path.basename(p) for p in missing[:3])}"
                            f"{' 等' if len(missing) > 3 else ''}。"
                            f"请确保 .shp / .shx / .dbf 三件套齐备。"
                        ),
                    }
            layer = QgsVectorLayer(file_path, name, "ogr")

        if not layer.isValid():
            return {"success": False, "message": f"无法加载图层：{file_path}"}

        proj.addMapLayer(layer)
        if canvas:
            canvas.setExtent(layer.extent())
            canvas.refresh()
        return {"success": True, "message": f"已加载图层：{name}"}

    def _handle_save_project(self, canvas=None, project=None, **kwargs) -> Dict[str, Any]:
        from qgis.core import QgsProject
        proj = project or QgsProject.instance()
        path = proj.fileName()
        if not path:
            return {"success": False, "message": "项目尚未保存。请使用「文件 → 另存为」先指定路径。"}
        if proj.write():
            return {"success": True, "message": f"项目已保存：{path}"}
        return {"success": False, "message": "保存失败"}

    def _handle_save_as_project(self, canvas=None, project=None, main_window=None, **kwargs) -> Dict[str, Any]:
        """通过 SkillManager 执行另存为技能。"""
        from skills.skill_manager import get_skill_manager
        mgr = get_skill_manager()
        return mgr.execute_skill("save_as_project", canvas=canvas, main_window=main_window)

    def _handle_export_map(self, canvas=None, project=None, format: str = "png", **kwargs) -> Dict[str, Any]:
        if canvas is None:
            return {"success": False, "message": "地图画布未初始化"}

        from PyQt5.QtGui import QImage, QPainter
        from PyQt5.QtCore import Qt
        from qgis.core import QgsMapRendererCustomPainterJob

        path = os.path.join(tempfile.gettempdir(), f"aiqgis_export_{int(time.time())}.{format}")

        settings = canvas.mapSettings()
        size = settings.outputSize()
        image = QImage(size, QImage.Format_ARGB32_Premultiplied)
        image.fill(Qt.transparent)

        painter = QPainter(image)
        job = QgsMapRendererCustomPainterJob(settings, painter)
        job.start()
        job.waitForFinished()
        painter.end()

        if image.save(path):
            return {"success": True, "message": f"地图已导出：{path}", "output_path": path}
        return {"success": False, "message": "导出失败"}

    # ── 视图操作 handler ─────────────────────────────────

    def _handle_zoom_to_layer(self, canvas=None, project=None, layer_name: str = "", **kwargs) -> Dict[str, Any]:
        if canvas is None:
            return {"success": False, "message": "地图画布未初始化"}

        from qgis.core import QgsProject
        proj = project or QgsProject.instance()

        for layer_id, layer in proj.mapLayers().items():
            if layer_name.lower() in layer.name().lower():
                canvas.setExtent(layer.extent())
                canvas.refresh()
                return {"success": True, "message": f"已缩放至图层：{layer.name()}"}

        if not layer_name:
            canvas.zoomToFullExtent()
            canvas.refresh()
            return {"success": True, "message": "已缩放至全图范围"}

        return {"success": False, "message": f"未找到图层：{layer_name}"}

    def _handle_zoom_in(self, canvas=None, project=None, **kwargs) -> Dict[str, Any]:
        if canvas is None:
            return {"success": False, "message": "地图画布未初始化"}
        canvas.zoomIn()
        canvas.refresh()
        return {"success": True, "message": "已放大"}

    def _handle_zoom_out(self, canvas=None, project=None, **kwargs) -> Dict[str, Any]:
        if canvas is None:
            return {"success": False, "message": "地图画布未初始化"}
        canvas.zoomOut()
        canvas.refresh()
        return {"success": True, "message": "已缩小"}

    def _handle_reset_view(self, canvas=None, project=None, **kwargs) -> Dict[str, Any]:
        if canvas is None:
            return {"success": False, "message": "地图画布未初始化"}
        canvas.zoomToFullExtent()
        canvas.refresh()
        return {"success": True, "message": "已重置为全图范围", "action": "reset_view"}

    # ── 图层操作 handler ─────────────────────────────────

    def _handle_remove_layer(self, canvas=None, project=None, layer_name: str = "", **kwargs) -> Dict[str, Any]:
        from qgis.core import QgsProject
        proj = project or QgsProject.instance()

        for layer_id, layer in list(proj.mapLayers().items()):
            if layer_name.lower() in layer.name().lower():
                layer_display_name = layer.name()
                proj.removeMapLayer(layer_id)
                if canvas:
                    canvas.refresh()
                return {"success": True, "message": f"已移除图层：{layer_display_name}"}

        return {"success": False, "message": f"未找到图层：{layer_name}"}

    def _handle_list_layers(self, canvas=None, project=None, **kwargs) -> Dict[str, Any]:
        from qgis.core import QgsProject, QgsVectorLayer
        proj = project or QgsProject.instance()
        layers = []
        for layer in proj.mapLayers().values():
            ltype = "矢量" if isinstance(layer, QgsVectorLayer) else "栅格"
            layers.append(f"  - [{ltype}] {layer.name()}")

        if not layers:
            return {"success": True, "message": "当前项目没有图层。", "layers": []}

        return {"success": True, "message": "当前图层：\n" + "\n".join(layers), "layers": layers}

    # ── 查询操作 handler ─────────────────────────────────

    def _handle_identify_feature(self, canvas=None, project=None, **kwargs) -> Dict[str, Any]:
        try:
            from qgis.utils import iface
            if iface:
                iface.actionIdentify().trigger()
                return {"success": True, "message": "已激活要素识别工具，请点击地图上的要素查看属性。", "action": "identify_feature"}
        except Exception:
            pass
        return {"success": False, "message": "离线模式下要素识别请使用工具栏的「识别」工具点击地图。", "action": "identify_feature"}

    # ── 坐标系 handler ───────────────────────────────────

    def _handle_set_crs(self, canvas=None, project=None, epsg: int = 4326, **kwargs) -> Dict[str, Any]:
        from qgis.core import QgsCoordinateReferenceSystem, QgsProject
        proj = project or QgsProject.instance()

        crs = QgsCoordinateReferenceSystem(f"EPSG:{epsg}")
        if not crs.isValid():
            return {"success": False, "message": f"无效的坐标系：EPSG:{epsg}"}

        proj.setCrs(crs)
        if canvas:
            canvas.refresh()
        return {"success": True, "message": f"项目坐标系已设置为 EPSG:{epsg} — {crs.description()}"}

    def _handle_show_crs(self, canvas=None, project=None, **kwargs) -> Dict[str, Any]:
        from qgis.core import QgsProject
        proj = project or QgsProject.instance()
        crs = proj.crs()
        return {"success": True, "message": f"当前坐标系：{crs.authid()} — {crs.description()}"}

    def _handle_reproject_layer(self, canvas=None, project=None,
                                 layer_name: str = "", target_epsg: int = 3857,
                                 **kwargs) -> Dict[str, Any]:
        import processing
        from qgis.core import QgsCoordinateReferenceSystem, QgsProject

        if not layer_name:
            return {"success": False, "message": "请指定要重投影的图层名称"}

        layer = self._find_layer(project, layer_name)
        if layer is None:
            return {"success": False, "message": f"未找到图层：{layer_name}"}

        crs = QgsCoordinateReferenceSystem(f"EPSG:{target_epsg}")
        if not crs.isValid():
            return {"success": False, "message": f"无效的目标坐标系：EPSG:{target_epsg}"}

        current_crs = layer.crs()
        if current_crs.isValid() and current_crs.authid() == f"EPSG:{target_epsg}":
            return {"success": True, "message": f"图层 {layer_name} 已是 EPSG:{target_epsg}，无需转换"}

        output_name = f"{layer_name}_EPSG{target_epsg}"
        result = processing.run("native:reprojectlayer", {
            "INPUT": layer,
            "TARGET_CRS": crs,
            "OUTPUT": "memory:",
        })
        reprojected = result.get("OUTPUT")
        if reprojected is None:
            return {"success": False, "message": "重投影失败"}

        reprojected.setName(output_name)
        proj = project or QgsProject.instance()
        proj.addMapLayer(reprojected)

        if canvas:
            canvas.refresh()

        return {
            "success": True,
            "message": f"图层 {layer_name} 已重投影为 EPSG:{target_epsg}，新图层名：{output_name}",
            "reprojected_layer": output_name,
        }

    # ── P0 编辑/选择 handler ─────────────────────────────

    def _handle_toggle_editing(self, canvas=None, project=None, layer_name: str = "",
                                target: str = "", **kwargs) -> Dict[str, Any]:
        from qgis.core import QgsProject, QgsVectorLayer
        proj = project or QgsProject.instance()

        if target == "all":
            closed = []
            for _lid, layer in proj.mapLayers().items():
                if isinstance(layer, QgsVectorLayer) and layer.isEditable():
                    try:
                        layer.commitChanges()
                        closed.append(layer.name())
                    except Exception:
                        layer.rollBack()
            msg = f"已关闭所有编辑图层：{', '.join(closed)}" if closed else "当前没有正在编辑的图层"
            return {"success": True, "message": msg, "action": "toggle_editing", "closed": closed}

        layer = self._find_layer(project, layer_name) if layer_name else None
        if layer is None:
            return {"success": False, "message": f"未找到图层：{layer_name}"}

        err = self._check_vector(layer)
        if err:
            return err

        try:
            if layer.isEditable():
                layer.commitChanges()
                return {"success": True, "message": f"已保存编辑并关闭：{layer.name()}", "action": "toggle_editing"}
            else:
                layer.startEditing()
                return {"success": True, "message": f"已开启编辑：{layer.name()}", "action": "toggle_editing"}
        except Exception as e:
            try:
                layer.rollBack()
            except Exception:
                pass
            return {"success": False, "message": f"编辑切换失败：{e}", "action": "toggle_editing"}

    def _handle_select_feature(self, canvas=None, project=None, method: str = "rect",
                                layer_name: str = "", expression: str = "", **kwargs) -> Dict[str, Any]:
        from qgis.core import QgsProject, QgsVectorLayer
        proj = project or QgsProject.instance()

        if method == "clear":
            for _lid, layer in proj.mapLayers().items():
                if isinstance(layer, QgsVectorLayer):
                    layer.removeSelection()
            if canvas:
                canvas.refresh()
            return {"success": True, "message": "已清除所有图层选择", "action": "select_feature"}

        if method == "point":
            try:
                from qgis.utils import iface
                if iface:
                    iface.actionSelect().trigger()
                    return {"success": True, "message": "已激活点选工具，请在地图上点击要素", "action": "select_feature"}
            except Exception:
                pass
            return {"success": False, "message": "点选工具仅在 QGIS 桌面环境下可用"}

        if method == "rect":
            try:
                from qgis.utils import iface
                if iface:
                    iface.actionSelectRectangle().trigger()
                    return {"success": True, "message": "已激活框选工具，请在地图上拖拽矩形区域", "action": "select_feature"}
            except Exception:
                pass
            return {"success": False, "message": "框选工具仅在 QGIS 桌面环境下可用"}

        if method == "expression":
            layer = self._find_layer(project, layer_name) if layer_name else None
            if layer is None:
                return {"success": False, "message": f"未找到图层：{layer_name}"}
            err = self._check_vector(layer)
            if err:
                return err
            if not expression:
                return {"success": False, "message": "expression 模式下必须提供 SQL 表达式"}
            layer.selectByExpression(expression)
            count = layer.selectedFeatureCount()
            return {"success": True, "message": f"已选中 {count} 个要素", "selected_count": count, "action": "select_feature"}

        return {"success": False, "message": f"不支持的选择方式：{method}"}

    # ── P1 样式/过滤/导出 handler ────────────────────────

    def _handle_set_layer_style(self, canvas=None, project=None, layer_name: str = "",
                                 render_type: str = "single", color: str = "#FF0000",
                                 field_name: str = "", **kwargs) -> Dict[str, Any]:
        from qgis.core import (
            QgsProject, QgsVectorLayer, QgsRasterLayer,
            QgsSingleSymbolRenderer, QgsCategorizedSymbolRenderer,
            QgsGraduatedSymbolRenderer, QgsRendererCategory, QgsRendererRange,
            QgsFillSymbol, QgsLineSymbol, QgsMarkerSymbol,
            QgsSingleBandPseudoColorRenderer, QgsColorRampShader,
            QgsRasterShader, QgsStyle, QgsGraduatedSymbolRenderer,
        )
        from qgis.PyQt.QtGui import QColor
        from qgis.utils import iface as qgis_iface
        import random

        proj = project or QgsProject.instance()
        layer = self._find_layer(project, layer_name)
        if layer is None:
            return {"success": False, "message": f"未找到图层：{layer_name}"}

        if isinstance(layer, QgsRasterLayer):
            if render_type != "single":
                return {"success": False, "message": "栅格图层仅支持 single 样式"}
            try:
                shader_func = QgsColorRampShader(0, 255)
                import numpy as np
                stats = layer.dataProvider().bandStatistics(1)
                vmin = stats.minimumValue
                vmax = stats.maximumValue
                color_ramp_items = [
                    QgsColorRampShader.ColorRampItem(vmin, QColor("#808080")),
                    QgsColorRampShader.ColorRampItem(vmax, QColor("#FF0000")),
                ]
                shader_func.setColorRampItemList(color_ramp_items)
                shader_func.setColorRampType(QgsColorRampShader.Interpolated)
                raster_shader = QgsRasterShader()
                raster_shader.setRasterShaderFunction(shader_func)
                renderer = QgsSingleBandPseudoColorRenderer(
                    layer.dataProvider(), 1, raster_shader
                )
                layer.setRenderer(renderer)
                layer.triggerRepaint()
                return {"success": True, "message": f"栅格图层 {layer.name()} 样式已设置"}
            except Exception as e:
                return {"success": False, "message": f"栅格样式设置失败：{e}"}

        err = self._check_vector(layer)
        if err:
            return err

        qcolor = QColor(color)

        try:
            if render_type == "single":
                geom_type = layer.geometryType()
                if geom_type == 0:
                    symbol = QgsMarkerSymbol.createSimple({})
                elif geom_type == 1:
                    symbol = QgsLineSymbol.createSimple({})
                else:
                    symbol = QgsFillSymbol.createSimple({})
                symbol.setColor(qcolor)
                renderer = QgsSingleSymbolRenderer(symbol)

            elif render_type == "categorized":
                if not field_name:
                    field_name = layer.fields().at(0).name() if layer.fields().count() > 0 else ""
                if not field_name:
                    return {"success": False, "message": "分类样式需要指定 field_name 参数"}
                idx = layer.fields().indexOf(field_name)
                unique_values = list(layer.uniqueValues(idx))
                categories = []
                for i, val in enumerate(unique_values):
                    hue = (i * 137) % 360
                    cat_color = QColor.fromHsv(hue, 200, 220)
                    cat_symbol = QgsFillSymbol.createSimple({})
                    cat_symbol.setColor(cat_color)
                    category = QgsRendererCategory(val, cat_symbol, str(val))
                    categories.append(category)
                renderer = QgsCategorizedSymbolRenderer(field_name, categories)

            elif render_type == "graduated":
                if not field_name:
                    field_name = layer.fields().at(0).name() if layer.fields().count() > 0 else ""
                if not field_name:
                    return {"success": False, "message": "分级样式需要指定 field_name 参数"}
                idx = layer.fields().indexOf(field_name)
                values = []
                for feat in layer.getFeatures():
                    val = feat.attribute(field_name)
                    if val is None:
                        continue
                    try:
                        values.append(float(val))
                    except (TypeError, ValueError):
                        # QVariant 包装值：先解包再转换
                        qv = val.value() if hasattr(val, "value") else None
                        if qv is not None:
                            try:
                                values.append(float(qv))
                            except (TypeError, ValueError):
                                pass
                if not values:
                    return {"success": False, "message": f"字段 {field_name} 没有有效数值"}
                vmin, vmax = min(values), max(values)
                if vmin == vmax:
                    vmax = vmin + 1
                step = (vmax - vmin) / 5
                ranges = []
                for i in range(5):
                    lo = vmin + i * step
                    hi = vmin + (i + 1) * step
                    r = int(255 * i / 4)
                    g = int(255 * (4 - i) / 4)
                    rcolor = QColor(r, g, 128)
                    rsymbol = QgsFillSymbol.createSimple({})
                    rsymbol.setColor(rcolor)
                    rrange = QgsRendererRange(lo, hi, rsymbol, f"{lo:.1f} - {hi:.1f}")
                    ranges.append(rrange)
                renderer = QgsGraduatedSymbolRenderer(field_name, ranges)
            else:
                return {"success": False, "message": f"不支持的渲染类型：{render_type}"}

            layer.setRenderer(renderer)
            layer.triggerRepaint()
            return {"success": True, "message": f"图层 {layer.name()} 样式已设置为 {render_type}"}

        except Exception as e:
            _log.exception("set_layer_style 失败")
            return {"success": False, "message": f"样式设置失败：{e}"}

    def _handle_load_layer_style(self, canvas=None, project=None, layer_name: str = "",
                                  qml_path: str = "", **kwargs) -> Dict[str, Any]:
        layer = self._find_layer(project, layer_name)
        if layer is None:
            return {"success": False, "message": f"未找到图层：{layer_name}"}

        if not qml_path or not os.path.exists(qml_path):
            return {"success": False, "message": f"QML 文件不存在：{qml_path}"}

        result = layer.loadNamedStyle(qml_path)
        if result[0]:
            layer.triggerRepaint()
            return {"success": True, "message": f"已加载样式：{os.path.basename(qml_path)}"}
        return {"success": False, "message": f"样式加载失败：{result[1]}"}

    def _handle_filter_layer(self, canvas=None, project=None, layer_name: str = "",
                              expression: str = "", **kwargs) -> Dict[str, Any]:
        layer = self._find_layer(project, layer_name)
        if layer is None:
            return {"success": False, "message": f"未找到图层：{layer_name}"}

        err = self._check_vector(layer)
        if err:
            return err

        if expression.strip() == "":
            layer.setSubsetString("")
            return {"success": True, "message": f"图层 {layer.name()} 过滤已清除，共 {layer.featureCount()} 个要素"}

        layer.setSubsetString(expression)
        return {"success": True, "message": f"图层 {layer.name()} 过滤已应用，当前显示 {layer.featureCount()} 个要素"}

    def _handle_export_attribute(self, canvas=None, project=None, layer_name: str = "",
                                  output_path: str = "", **kwargs) -> Dict[str, Any]:
        # ── 参数键别名兼容：LLM 可能输出 file_path / file / filename ──
        output_path = (output_path or kwargs.get("file_path")
                       or kwargs.get("file") or kwargs.get("filename") or "")
        layer = self._find_layer(project, layer_name)
        if layer is None:
            return {"success": False, "message": f"未找到图层：{layer_name}"}
        err = self._check_vector(layer)
        if err:
            return err
        # ── 默认输出路径兜底：用户未指定时生成带时间戳的 CSV ──
        if not output_path:
            from core.output_persistence import generate_output_path
            output_path = generate_output_path("export", layer.name(), extension=".csv")
        return self._export_attribute_table(layer, output_path)

    def _handle_export_layer(self, canvas=None, project=None, layer_name: str = "",
                              output_path: str = "", format: str = "shp", **kwargs) -> Dict[str, Any]:
        from qgis.core import (
            QgsProject, QgsVectorLayer, QgsRasterLayer,
            QgsVectorFileWriter, QgsRasterFileWriter, QgsRasterPipe,
        )
        from core.output_persistence import generate_output_path

        layer = self._find_layer(project, layer_name)
        if layer is None:
            return {"success": False, "message": f"未找到图层：{layer_name}"}

        FORMAT_MAP = {
            "shp": {"driver": "ESRI Shapefile", "ext": ".shp"},
            "geojson": {"driver": "GeoJSON", "ext": ".geojson"},
            "gpkg": {"driver": "GPKG", "ext": ".gpkg"},
        }
        fmt_cfg = FORMAT_MAP.get(format, FORMAT_MAP["shp"])

        if not output_path:
            ext = fmt_cfg["ext"] if isinstance(layer, QgsVectorLayer) else ".tif"
            output_path = generate_output_path("export", layer.name(), extension=ext)

        if isinstance(layer, QgsVectorLayer):
            error = QgsVectorFileWriter.writeAsVectorFormat(
                layer, output_path, "UTF-8", layer.crs(), fmt_cfg["driver"]
            )
            if error[0] == QgsVectorFileWriter.NoError:
                size_kb = os.path.getsize(output_path) / 1024 if os.path.exists(output_path) else 0
                return {"success": True, "message": f"图层已导出：{output_path}（{size_kb:.1f} KB）",
                        "output_path": output_path}
            return {"success": False, "message": f"导出失败：{error}"}

        elif isinstance(layer, QgsRasterLayer):
            provider = layer.dataProvider()
            pipe = QgsRasterPipe()
            if pipe.set(provider.clone()):
                writer = QgsRasterFileWriter(output_path)
                writer.setOutputFormat("GTiff")
                writer.writeRaster(pipe, provider.xSize(), provider.ySize(),
                                   provider.extent(), layer.crs())
                return {"success": True, "message": f"图层已导出：{output_path}",
                        "output_path": output_path}
            return {"success": False, "message": "无法创建栅格数据管道"}

        return {"success": False, "message": "不支持的图层类型"}

    # ── P2 标注/字段/统计/缓冲区 handler ─────────────────

    def _handle_add_label(self, canvas=None, project=None, layer_name: str = "",
                           field: str = "", **kwargs) -> Dict[str, Any]:
        from qgis.core import (
            QgsProject, QgsPalLayerSettings, QgsVectorLayerSimpleLabeling,
            QgsTextFormat,
        )

        layer = self._find_layer(project, layer_name)
        if layer is None:
            return {"success": False, "message": f"未找到图层：{layer_name}"}
        err = self._check_vector(layer)
        if err:
            return err

        if not field:
            layer.setLabeling(None)
            layer.triggerRepaint()
            return {"success": True, "message": f"已关闭图层 {layer.name()} 的标注"}

        settings = QgsPalLayerSettings()
        settings.fieldName = field
        settings.isExpression = False
        fmt = QgsTextFormat()
        fmt.setSize(10)
        settings.setFormat(fmt)
        labeling = QgsVectorLayerSimpleLabeling(settings)
        layer.setLabeling(labeling)
        layer.triggerRepaint()
        return {"success": True, "message": f"已为图层 {layer.name()} 开启标注，字段：{field}"}

    def _handle_open_field_manager(self, canvas=None, project=None, layer_name: str = "",
                                    **kwargs) -> Dict[str, Any]:
        layer = self._find_layer(project, layer_name)
        if layer is None:
            return {"success": False, "message": f"未找到图层：{layer_name}"}

        err = self._check_vector(layer)
        if err:
            return err

        try:
            from qgis.utils import iface
            if iface:
                iface.setActiveLayer(layer)
                iface.actionManageFields().trigger()
                return {"success": True, "message": f"已打开字段管理器：{layer.name()}"}
        except Exception:
            pass
        return {"success": False, "message": f"请在 QGIS 桌面环境中手动打开 {layer.name()} 的字段管理器"}

    def _handle_layer_statistic(self, canvas=None, project=None, layer_name: str = "",
                                 method: str = "count", field: str = "", **kwargs) -> Dict[str, Any]:
        layer = self._find_layer(project, layer_name)
        if layer is None:
            return {"success": False, "message": f"未找到图层：{layer_name}"}
        err = self._check_vector(layer)
        if err:
            return err

        if method == "count":
            return {"success": True, "message": f"图层 {layer.name()} 共 {layer.featureCount()} 个要素",
                    "count": layer.featureCount()}

        if method == "all" and not field:
            return {"success": True, "message": f"图层 {layer.name()} 共 {layer.featureCount()} 个要素",
                    "count": layer.featureCount()}

        if not field and method in ("min", "max", "sum", "mean", "all"):
            return {"success": False, "message": f"{method} 统计需要指定 field 参数"}

        idx = layer.fields().indexOf(field)
        if idx < 0:
            return {"success": False, "message": f"字段不存在：{field}"}

        values = []
        for feat in layer.getFeatures():
            val = feat.attribute(field)
            if val is not None:
                try:
                    values.append(float(val))
                except (ValueError, TypeError):
                    pass

        if not values:
            return {"success": False, "message": f"字段 {field} 没有有效数值"}

        if method == "min":
            result = min(values)
            return {"success": True, "message": f"{field} 最小值：{result}", "value": result}
        elif method == "max":
            result = max(values)
            return {"success": True, "message": f"{field} 最大值：{result}", "value": result}
        elif method == "sum":
            result = sum(values)
            return {"success": True, "message": f"{field} 合计：{result}", "value": result}
        elif method == "mean":
            result = sum(values) / len(values)
            return {"success": True, "message": f"{field} 平均值：{result:.4f}", "value": result}
        elif method == "all":
            cnt = len(values)
            mn = min(values)
            mx = max(values)
            sm = sum(values)
            avg = sm / cnt
            return {"success": True,
                    "message": f"{field} 统计：count={cnt}, min={mn}, max={mx}, sum={sm}, mean={avg:.4f}",
                    "count": cnt, "min": mn, "max": mx, "sum": sm, "mean": avg}

        return {"success": False, "message": f"不支持的统计方法：{method}"}

    def _handle_create_buffer(self, canvas=None, project=None, layer_name: str = "",
                               distance: float = 100.0, selected_only: bool = False,
                               **kwargs) -> Dict[str, Any]:
        from qgis.core import (
            QgsProject, QgsVectorLayer, QgsFeature, QgsGeometry,
            QgsField, QgsFields,
        )
        from qgis.PyQt.QtCore import QVariant

        layer = self._find_layer(project, layer_name)
        if layer is None:
            return {"success": False, "message": f"未找到图层：{layer_name}"}
        err = self._check_vector(layer)
        if err:
            return err

        if layer.featureCount() > 10000 and not selected_only:
            return {"success": False,
                    "message": f"图层要素数量（{layer.featureCount()}）超过 10000，请使用 selected_only=true 或先筛选数据"}

        crs = layer.crs().authid()
        geom_type_str = "Polygon"
        uri = f"{geom_type_str}?crs={crs}"
        buff_layer = QgsVectorLayer(uri, f"{layer.name()}_buffer_{distance}m", "memory")
        provider = buff_layer.dataProvider()

        fields = QgsFields()
        fields.append(QgsField("original_id", QVariant.Int))
        provider.addAttributes(fields)
        buff_layer.updateFields()

        if selected_only:
            features = layer.selectedFeatures()
        else:
            features = layer.getFeatures()

        new_features = []
        for feat in features:
            geom = feat.geometry()
            if geom and not geom.isNull():
                buff_geom = geom.buffer(distance, 5)
                if buff_geom and not buff_geom.isNull():
                    new_feat = QgsFeature()
                    new_feat.setGeometry(buff_geom)
                    new_feat.setAttributes([feat.id()])
                    new_features.append(new_feat)

        if not new_features:
            return {"success": False, "message": "没有要素可用于缓冲区分析"}

        provider.addFeatures(new_features)
        buff_layer.updateExtents()

        proj = project or QgsProject.instance()
        proj.addMapLayer(buff_layer)

        if canvas:
            canvas.setExtent(buff_layer.extent())
            canvas.refresh()

        return {"success": True,
                "message": f"缓冲区分析完成，新增图层：{buff_layer.name()}（{len(new_features)} 个要素）"}
