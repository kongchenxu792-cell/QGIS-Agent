"""
缓冲技能 — 封装 QGIS native:buffer 算法。

对输入矢量图层生成指定距离的缓冲区多边形。
结果持久化到 user_data/exports/shapefiles/，应用重启后数据不丢失。

参数格式（arguments 字符串）：
  - JSON: {"input_layer": "roads", "distance": 100.0, "segments": 8}
  - KV:   input_layer=roads distance=100 segments=8
"""

import json
from typing import Any, Dict, List

from qgis.core import QgsProject, QgsVectorLayer, QgsMapLayer

from skills.base_skill import BaseSkill
from core.output_persistence import generate_output_path


class BufferSkill(BaseSkill):
    """缓冲技能：为矢量图层生成缓冲区。"""

    def get_name(self) -> str:
        return "buffer"

    def get_description(self) -> str:
        return (
            "用于为矢量图层生成缓冲区多边形（例如：沿道路生成 50 米缓冲区、"
            "为点要素生成服务半径）。参数：input_layer（图层名）、distance（距离）、"
            "segments（分段数，默认 5）"
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
        # 尝试 JSON
        if s.startswith("{"):
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                pass

        # 回退到 key=value 解析
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
        distance_str = params.get("distance", "")
        segments_str = params.get("segments", "5")

        # ---------- 2. 定位输入图层 ----------
        input_layer: QgsVectorLayer = None

        if input_name:
            input_layer = self._find_layer_by_name(input_name)
            if input_layer is None:
                return {
                    "success": False,
                    "message": (
                        f"未找到名为「{input_name}」的矢量图层。"
                        f"当前可用图层："
                        f"{[lyr.name() for lyr in QgsProject.instance().mapLayers().values() if isinstance(lyr, QgsVectorLayer)]}"
                    ),
                }
        elif active_layer is not None and isinstance(active_layer, QgsVectorLayer):
            input_layer = active_layer
        else:
            # 自动选取第一个矢量图层
            for lyr in QgsProject.instance().mapLayers().values():
                if isinstance(lyr, QgsVectorLayer):
                    input_layer = lyr
                    break

        if input_layer is None:
            return {"success": False, "message": "未找到任何矢量图层，请先加载数据"}

        # ---------- 3. 解析距离 ----------
        try:
            distance = float(distance_str)
        except (ValueError, TypeError):
            return {
                "success": False,
                "message": f"缓冲区距离「{distance_str}」无法解析为数值",
            }

        if distance <= 0:
            return {
                "success": False,
                "message": f"缓冲区距离必须大于 0，当前值：{distance}",
            }

        # ---------- 4. 解析分段数 ----------
        try:
            segments = int(segments_str)
        except (ValueError, TypeError):
            segments = 5

        if segments < 1:
            segments = 5

        # ---------- 5. CRS 单位守卫：禁止地理坐标系（度）直接缓冲 ----------
        if input_layer.crs().isGeographic():
            return {
                "success": False,
                "message": (
                    f"图层「{input_layer.name()}」使用地理坐标系 "
                    f"（{input_layer.crs().authid()}，单位：度）。"
                    "直接缓冲会导致画布爆炸（例如 300→300°），请先使用 "
                    "「重投影」将图层转换到投影坐标系（如 UTM）后再执行缓冲。"
                ),
            }

        # ---------- 6. 执行算法 ----------
        output_path = generate_output_path("buffer", input_layer.name())

        alg_params = {
            "INPUT": input_layer,
            "DISTANCE": distance,
            "SEGMENTS": segments,
            "OUTPUT": output_path,
        }

        try:
            result = processing.run("native:buffer", alg_params)
        except Exception as e:
            return {
                "success": False,
                "message": f"native:buffer 执行失败：{e}",
            }

        # ---------- 7. 加载结果 ----------
        # result["OUTPUT"] 是文件路径字符串（非图层对象），需从磁盘加载
        new_name = f"[Buffer] {input_layer.name()}"
        buffer_layer = QgsVectorLayer(output_path, new_name, "ogr")
        if not buffer_layer.isValid():
            return {
                "success": False,
                "message": f"缓冲区结果加载失败：{output_path}",
            }

        QgsProject.instance().addMapLayer(buffer_layer)

        if canvas and hasattr(canvas, "refresh"):
            canvas.refresh()

        return {
            "success": True,
            "message": (
                f"缓冲完成：{input_layer.name()} "
                f"（距离={distance}，分段={segments}）→ {new_name}"
            ),
            "added_layers": [buffer_layer],
            "output_path": output_path,
            "output_layer_name": new_name,
        }
