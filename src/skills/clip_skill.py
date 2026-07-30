"""
裁剪技能 — 封装 QGIS native:clip 算法。

用叠加图层的边界裁剪输入图层，保留重叠区域内的要素。
结果持久化到 user_data/exports/shapefiles/，应用重启后数据不丢失。
"""

import logging
import os

_log = logging.getLogger(__name__)
from typing import Any, Dict, List

from qgis.core import QgsProject, QgsVectorLayer, QgsMapLayer

from skills.base_skill import BaseSkill
from core.output_persistence import generate_output_path


class ClipSkill(BaseSkill):
    """裁剪技能：用边界图层裁剪输入图层。"""

    def get_name(self) -> str:
        return "clip"

    def get_description(self) -> str:
        return "用于裁剪矢量图层（例如：用边界裁剪道路，提取某个区域内的要素）"

    def execute(
        self,
        canvas=None,
        layer_tree=None,
        arguments: str = "",
        active_layer=None,
        main_window=None,
        **kwargs,
    ) -> Dict[str, Any]:
        import processing

        layers = list(QgsProject.instance().mapLayers().values())
        if len(layers) < 2:
            return {
                "success": False,
                "message": "裁剪需要至少两个矢量图层（输入图层 + 边界图层）",
            }

        input_layer = active_layer
        if input_layer is None or not isinstance(input_layer, QgsVectorLayer):
            for lyr in layers:
                if isinstance(lyr, QgsVectorLayer):
                    input_layer = lyr
                    break

        if input_layer is None:
            return {"success": False, "message": "未找到输入矢量图层"}

        overlay_layer = None
        for lyr in layers:
            if lyr != input_layer and isinstance(lyr, QgsVectorLayer):
                overlay_layer = lyr
                break

        if overlay_layer is None:
            return {"success": False, "message": "未找到可作为裁剪边界的矢量图层"}

        # 持久化输出路径
        output_path = generate_output_path("clip", input_layer.name())

        # CRS 对齐：若输入图层与边界图层坐标系不一致，先将边界重投影至与输入一致
        _log.info(f"[CRS对齐拦截] 蛋糕坐标系: {input_layer.crs().authid()}, 切刀坐标系: {overlay_layer.crs().authid()}")

        final_overlay = overlay_layer
        if input_layer.crs().authid() != overlay_layer.crs().authid():
            _log.info(f"[CRS对齐] 坐标系不一致！正在将切刀动态对齐到: {input_layer.crs().authid()}")
            reproject_res = processing.run("native:reprojectlayer", {
                'INPUT': overlay_layer,
                'TARGET_CRS': input_layer.crs(),
                'OUTPUT': 'memory:temp_aligned_overlay',
            })
            final_overlay = reproject_res.get('OUTPUT')

        params = {
            "INPUT": input_layer,
            "OVERLAY": final_overlay,
            "OUTPUT": output_path,
        }

        # Phase 5：GeoAgent 前置防御卫士（参数对调 + CRS 对齐）
        from .geoagent_guard import geoagent_pre_execution_guard
        params = geoagent_pre_execution_guard("clip", params)

        _log.info("[CRS对齐] 正在执行同坐标系裁剪算子...")
        result = processing.run("native:clip", params)

        # FIXED: native:clip 返回的是文件路径字符串，不是 QgsVectorLayer 对象
        output_result = result.get('OUTPUT')
        clipped_layer = None

        new_name = f"[裁剪] {input_layer.name()}"

        if isinstance(output_result, str):
            clipped_layer = QgsVectorLayer(output_result, new_name, "ogr")
        elif isinstance(output_result, QgsVectorLayer) and output_result.isValid():
            clipped_layer = output_result
            clipped_layer.setName(new_name)
        else:
            return {
                "success": False,
                "message": f"裁剪输出图层无效或无法加载: {type(output_result)}",
            }

        if not clipped_layer or not clipped_layer.isValid():
            return {
                "success": False,
                "message": "裁剪输出图层加载失败",
            }

        # 继承输入图层的渲染器（保留热力图 Magma 色带等）
        if input_layer.renderer():
            clipped_layer.setRenderer(input_layer.renderer().clone())

        # 去重同名图层
        existing = QgsProject.instance().mapLayersByName(new_name)
        for ly in existing:
            QgsProject.instance().removeMapLayer(ly.id())

        QgsProject.instance().addMapLayer(clipped_layer)
        added: List[QgsMapLayer] = [clipped_layer]

        if canvas and hasattr(canvas, "refresh"):
            canvas.refresh()

        _log.info(f"[成功通关] 裁剪后的图层已完美加载：{new_name}")
        return {
            "success": True,
            "message": f"裁剪完成：{input_layer.name()} × {overlay_layer.name()} → {new_name}",
            "added_layers": added,
            "output_path": output_path,
            "output_layer_name": new_name,
        }