"""
融合技能 — 封装 QGIS native:dissolve 算法。

将具有相同字段值的相邻多边形合并为一个多边形。
结果持久化到 user_data/exports/shapefiles/，应用重启后数据不丢失。
"""

import os
from typing import Any, Dict, List

from qgis.core import QgsProject, QgsVectorLayer, QgsMapLayer

from skills.base_skill import BaseSkill
from core.output_persistence import generate_output_path


class DissolveSkill(BaseSkill):
    """融合技能：合并相邻多边形。"""

    def get_name(self) -> str:
        return "dissolve"

    def get_description(self) -> str:
        return "用于融合/合并矢量图层的多边形（例如：把各区的边界融合成整个城市的边界）"

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

        input_layer = active_layer
        if input_layer is None or not isinstance(input_layer, QgsVectorLayer):
            layers = list(QgsProject.instance().mapLayers().values())
            for lyr in layers:
                if isinstance(lyr, QgsVectorLayer):
                    input_layer = lyr
                    break

        if input_layer is None:
            return {"success": False, "message": "未找到可融合的矢量图层"}

        # 如果用户指定了字段名，使用该字段分组融合
        dissolve_field = None
        if arguments and arguments.strip():
            field_name = arguments.strip()
            idx = input_layer.fields().indexFromName(field_name)
            if idx >= 0:
                dissolve_field = field_name

        # 持久化输出路径
        output_path = generate_output_path("dissolve", input_layer.name())

        params = {
            "INPUT": input_layer,
            "OUTPUT": output_path,
        }
        if dissolve_field is not None:
            params["FIELD"] = [dissolve_field]

        result = processing.run("native:dissolve", params)
        dissolved_layer: QgsVectorLayer = result["OUTPUT"]

        new_name = f"[融合] {input_layer.name()}"
        dissolved_layer.setName(new_name)

        QgsProject.instance().addMapLayer(dissolved_layer)
        added: List[QgsMapLayer] = [dissolved_layer]

        if canvas and hasattr(canvas, "refresh"):
            canvas.refresh()

        return {
            "success": True,
            "message": f"融合完成：{input_layer.name()} → {new_name}",
            "added_layers": added,
            "output_path": output_path,
            "output_layer_name": new_name,
        }