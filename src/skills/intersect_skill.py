"""
相交技能 — 封装 QGIS native:intersection 算法。

计算两个矢量图层之间的几何交集，保留两个图层的属性字段。
结果持久化到 user_data/exports/shapefiles/，应用重启后数据不丢失。

参数格式（arguments 字符串）：
  - JSON: {"input_layer": "roads", "overlay_layer": "boundary"}
  - KV:   input_layer=roads overlay_layer=boundary
"""

import json
from typing import Any, Dict, List

from qgis.core import QgsProject, QgsVectorLayer, QgsMapLayer

from skills.base_skill import BaseSkill
from core.output_persistence import generate_output_path


class IntersectSkill(BaseSkill):
    """相交技能：计算两个矢量的几何交集。"""

    def get_name(self) -> str:
        return "intersect"

    def get_description(self) -> str:
        return (
            "用于计算两个矢量图层的几何交集（例如：提取落在某个区域内的道路、"
            "计算土地利用与行政边界的重叠区域）。"
            "参数：input_layer（输入图层名）、overlay_layer（叠加图层名）"
        )

    # ------------------------------------------------------------------
    # 参数解析
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_arguments(arguments: str) -> Dict[str, Any]:
        """解析 arguments 字符串，支持 JSON 和 key=value 两种格式。"""
        if not arguments or not arguments.strip():
            return {}

        s = arguments.strip()
        if s.startswith("{"):
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                pass

        params: Dict[str, Any] = {}
        for token in s.split():
            if "=" in token:
                key, _, val = token.partition("=")
                params[key.strip()] = val.strip()
        return params

    # ------------------------------------------------------------------
    # 图层查找
    # ------------------------------------------------------------------
    @staticmethod
    def _find_layer_by_name(name: str) -> QgsVectorLayer:
        """按名称查找矢量图层，找不到返回 None。"""
        for lyr in QgsProject.instance().mapLayers().values():
            if isinstance(lyr, QgsVectorLayer) and lyr.name() == name:
                return lyr
        return None

    @staticmethod
    def _list_vector_layer_names() -> List[str]:
        """返回当前工程中所有矢量图层的名称列表。"""
        return [
            lyr.name()
            for lyr in QgsProject.instance().mapLayers().values()
            if isinstance(lyr, QgsVectorLayer)
        ]

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------
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

        # ---------- 1. 解析参数 ----------
        try:
            params = self._parse_arguments(arguments)
        except Exception as e:
            return {"success": False, "message": f"参数解析失败：{e}"}

        input_name = params.get("input_layer", "")
        overlay_name = params.get("overlay_layer", "")

        # ---------- 2. 定位输入图层 ----------
        input_layer: QgsVectorLayer = None
        overlay_layer: QgsVectorLayer = None

        available = self._list_vector_layer_names()

        if input_name:
            input_layer = self._find_layer_by_name(input_name)
            if input_layer is None:
                return {
                    "success": False,
                    "message": (
                        f"未找到输入图层「{input_name}」。"
                        f"当前可用矢量图层：{available}"
                    ),
                }
        elif active_layer is not None and isinstance(active_layer, QgsVectorLayer):
            input_layer = active_layer
        else:
            for lyr in QgsProject.instance().mapLayers().values():
                if isinstance(lyr, QgsVectorLayer):
                    input_layer = lyr
                    break

        if input_layer is None:
            return {"success": False, "message": "未找到任何矢量图层作为输入，请先加载数据"}

        # ---------- 3. 定位叠加图层 ----------
        if overlay_name:
            overlay_layer = self._find_layer_by_name(overlay_name)
            if overlay_layer is None:
                return {
                    "success": False,
                    "message": (
                        f"未找到叠加图层「{overlay_name}」。"
                        f"当前可用矢量图层：{available}"
                    ),
                }
        else:
            for lyr in QgsProject.instance().mapLayers().values():
                if isinstance(lyr, QgsVectorLayer) and lyr != input_layer:
                    overlay_layer = lyr
                    break

        if overlay_layer is None:
            return {
                "success": False,
                "message": "未找到叠加矢量图层（需要至少两个矢量图层）",
            }

        if input_layer is overlay_layer:
            return {
                "success": False,
                "message": "输入图层与叠加图层不能相同",
            }

        # ---------- 4. 执行算法 ----------
        output_path = generate_output_path("intersect", input_layer.name())

        alg_params = {
            "INPUT": input_layer,
            "OVERLAY": overlay_layer,
            "OUTPUT": output_path,
        }

        # Phase 5：GeoAgent 前置防御卫士（CRS 对齐）
        from .geoagent_guard import geoagent_pre_execution_guard
        alg_params = geoagent_pre_execution_guard("intersect", alg_params)

        try:
            result = processing.run("native:intersection", alg_params)
        except Exception as e:
            return {
                "success": False,
                "message": f"native:intersection 执行失败：{e}",
            }

        # ---------- 5. 加载结果 ----------
        # result["OUTPUT"] 是文件路径字符串（非图层对象），需从磁盘加载
        new_name = f"[Intersect] {input_layer.name()}"
        intersect_layer = QgsVectorLayer(output_path, new_name, "ogr")
        if not intersect_layer.isValid():
            return {
                "success": False,
                "message": f"相交结果加载失败：{output_path}",
            }

        QgsProject.instance().addMapLayer(intersect_layer)

        if canvas and hasattr(canvas, "refresh"):
            canvas.refresh()

        return {
            "success": True,
            "message": (
                f"相交完成：{input_layer.name()} ∩ {overlay_layer.name()} → {new_name}"
            ),
            "added_layers": [intersect_layer],
            "output_path": output_path,
            "output_layer_name": new_name,
        }
