"""handlers_seismic — 震度态势图处理器 Mixin。

从 instruction_mapper.py 抽离的 seismic_situation_map handler，
通过 Mixin 继承注入到 InstructionMapper 中。
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Dict

_log = logging.getLogger("instruction_mapper")

# 默认 PNG 导出目录
_DEFAULT_PNG_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "user_data", "exports", "png",
)


class HandlersSeismicMixin:
    """震度态势图处理器。"""

    def _handle_seismic_situation_map(
        self,
        canvas=None,
        project=None,
        output_path: str = "",
        dpi: int = 300,
        **kwargs,
    ) -> Dict[str, Any]:
        """震度态势图全流程：识别图层 → 套用JMA配色 → 缩放画布 → 导出PNG。

        Parameters
        ----------
        canvas : QgsMapCanvas or None
            当前地图画布。
        project : QgsProject or None
            当前 QGIS 项目。
        output_path : str
            PNG 输出路径。空字符串时自动生成默认文件名。
        dpi : int
            导出 DPI，默认 300。

        Returns
        -------
        {"success": bool, "message": str, "stats": dict, "output_path": str}
        """
        from core.seismic_situation_map import SeismicSituationMap, LAYER_TYPE_LABELS

        ssm = SeismicSituationMap(project=project, canvas=canvas)

        # ── Step 1: 图层语义识别 ──
        recognized = ssm.recognize_layers()

        total_recognized = sum(
            len(v) for k, v in recognized.items() if k != "unmatched"
        )
        if total_recognized == 0:
            unmatched_names = [lyr.name() for lyr in recognized.get("unmatched", [])]
            return {
                "success": False,
                "message": (
                    f"未识别到任何地震相关图层。"
                    f"当前图层: {unmatched_names if unmatched_names else '无'}"
                ),
                "stats": {},
                "output_path": "",
            }

        # ── Step 2: 批量应用样式 ──
        style_results = ssm.apply_all_styles(recognized)
        _log.info("_handle_seismic_situation_map: style_results=%s", style_results)

        # ── Step 3: 画布缩放 ──
        all_styled = []
        for ltype in ("intensity", "shelter", "coverage", "gap", "population"):
            all_styled.extend(recognized.get(ltype, []))
        zoom_msg = ssm.zoom_to_extent(all_styled)

        # ── Step 4: PNG 导出 ──
        actual_output = output_path
        if not actual_output:
            os.makedirs(_DEFAULT_PNG_DIR, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            actual_output = os.path.join(_DEFAULT_PNG_DIR, f"seismic_map_{ts}.png")

        export_msg = ssm.export_png(actual_output, dpi=dpi)

        # ── 组装结果 ──
        recognized_summary = {}
        for ltype, layers in recognized.items():
            if layers:
                recognized_summary[LAYER_TYPE_LABELS.get(ltype, ltype)] = [
                    lyr.name() for lyr in layers
                ]

        return {
            "success": True,
            "message": (
                f"震度态势图生成完成。识别到 {total_recognized} 个相关图层，"
                f"已套用 JMA 配色并导出 PNG。\n"
                f"图层识别: {recognized_summary}\n"
                f"样式: {style_results}\n"
                f"缩放: {zoom_msg}\n"
                f"导出: {export_msg}"
            ),
            "stats": {
                "recognized": recognized_summary,
                "style_results": style_results,
                "zoom": zoom_msg,
                "export": export_msg,
            },
            "output_path": actual_output,
        }
