"""
MultimodalPromptBuilder — 多模态消息构建器

将文本指令 + 图片（含视口元数据）+ 图层上下文 构建为
OpenAI Vision API / DeepSeek Vision API 兼容的 messages 列表。

防御契约 (v1.1)：
    自动从 CanvasCapture 返回的 spatial_context 中提取视口元数据，
    以结构化文本形式注入到 Prompt 中，让大模型获得精确的空间尺度感知。

输出格式 (OpenAI Vision 兼容)：
    [
      {"role": "system", "content": "系统提示词"},
      {"role": "user", "content": [
          {"type": "text", "text": "文本指令 + 视口元数据 + 图层上下文 + 技能清单"},
          {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
      ]}
    ]
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

_log = logging.getLogger("multimodal.prompt_builder")


class MultimodalPromptBuilder:
    """多模态 Vision API 消息构建器。"""

    @staticmethod
    def build_system_prompt(
        skills_section: str,
        layer_metadata: Optional[List[Dict[str, Any]]] = None,
        history_text: str = "",
        pipeline_context: Optional[Dict[str, Any]] = None,
    ) -> str:
        """构建多模态场景下的系统提示词。

        与纯文本管线的 build_system_prompt 保持语义一致，
        但额外强调「空间尺度感知」要求。
        """
        # 图层状态
        if layer_metadata:
            layer_lines = []
            for i, meta in enumerate(layer_metadata, 1):
                active_mark = " [当前活动]" if meta.get("is_active") else ""
                layer_lines.append(
                    f"  {i}. {meta['name']} ({meta['type']}, "
                    f"provider: {meta.get('provider', 'unknown')}){active_mark}"
                )
            layer_state = "\n".join(layer_lines)
        else:
            layer_state = "（当前无已加载图层）"

        # 流水线上下文
        context_section = ""
        if pipeline_context:
            context_lines = []
            for key, value in pipeline_context.items():
                if key.startswith("last_output"):
                    context_lines.append(f"  - {key}: {value}")
            if context_lines:
                context_section = (
                    "## 流水线上下文（前序步骤的输出）\n\n"
                    + "\n".join(context_lines)
                    + "\n\n"
                )

        return (
            "你是 AIQGIS 的 GIS 智能体调度中心（Agent Coordinator），具备视觉分析能力。\n"
            "你的职责是：根据用户的自然语言指令 + 截图中的地理内容 + 视口空间元数据，"
            "规划并输出一个有序的技能执行流水线。\n\n"
            "## 空间尺度感知规则\n\n"
            "1. 当你看到的截图附带了「视口空间元数据」时，请严格按照其中的 CRS 和比例尺进行空间推理。\n"
            "2. 若用户在图片上指出某个区域并要求进行 buffer/clip 等空间操作，\n"
            "   必须基于比例尺将屏幕像素距离换算为实际地理距离（米/度）。\n"
            "3. 若 CRS 为度坐标系（如 EPSG:4326），buffer 距离使用度；\n"
            "   若 CRS 为米坐标（如 EPSG:3857），buffer 距离使用米。\n\n"
            "## 当前图层树状态\n\n"
            f"{layer_state}\n\n"
            f"{context_section}"
            "## 对话历史\n\n"
            f"{history_text}\n\n"
            "## 可用技能清单\n\n"
            f"{skills_section}\n"
            "## 输出格式要求（极其重要）\n\n"
            "你必须**只输出**一个严格的 JSON 数组，不要输出任何其他内容。\n"
            "数组中每个元素代表流水线中的一个步骤，按执行顺序排列：\n\n"
            "[\n"
            "  {\n"
            '    "skill": "技能名称",\n'
            '    "arguments": "{\\"param1\\": value1, \\"param2\\": value2}",\n'
            '    "reasoning": "该步骤的简短理由（中文）"\n'
            "  },\n"
            "  ...\n"
            "]\n\n"
            "### arguments 字段强制规则（违反将导致执行失败）\n\n"
            "arguments 必须是**严格的 JSON 对象字符串**，包含该技能所需的全部参数。\n"
            "**绝对禁止**在 arguments 中放入自然语言、中文句子、描述性文本或散文。\n"
            "错误示例：\"对 roads 图层生成 500 米缓冲区\" ← 这是自然语言，禁止！\n"
            "正确示例：\"{\\\"input_layer\\\": \\\"roads\\\", \\\"distance\\\": 500.0}\" ← 这是 JSON，正确！\n\n"
            "### 各技能 arguments 格式速查\n\n"
            "- buffer:       {{\"input_layer\": \"图层名\", \"distance\": 数值, \"segments\": 整数}}\n"
            "  示例: {{\"input_layer\": \"roads\", \"distance\": 500.0, \"segments\": 8}}\n"
            "- clip:         {{\"input_layer\": \"图层名\", \"overlay_layer\": \"边界图层名\"}}\n"
            "  示例: {{\"input_layer\": \"roads\", \"overlay_layer\": \"boundary\"}}\n"
            "- centroid:     {{\"input_layer\": \"图层名\"}}\n"
            "  示例: {{\"input_layer\": \"buildings\"}}\n"
            "- dissolve:     {{\"input_layer\": \"图层名\", \"field\": \"字段名\"}}\n"
            "  示例: {{\"input_layer\": \"parcels\", \"field\": \"district\"}}\n"
            "- intersect:    {{\"input_layer\": \"图层名\", \"overlay_layer\": \"叠加图层名\"}}\n"
            "  示例: {{\"input_layer\": \"roads\", \"overlay_layer\": \"admin_boundary\"}}\n"
            "- navigation:   {{\"action\": \"zoom_layer\"|\"zoom_extent\"|\"center\"|\"scale\"|\"refresh\""
            "|\"zoom_in\"|\"zoom_out\", ...}}\n"
            "  示例: {{\"action\": \"zoom_layer\", \"layer_name\": \"成都市_市\"}}\n"
            "  示例: {{\"action\": \"zoom_extent\", \"west\": 103.0, \"south\": 30.0, \"east\": 105.0, \"north\": 31.5}}\n"
            "  示例: {{\"action\": \"center\", \"lat\": 30.57, \"lon\": 104.07, \"scale\": 50000}}\n"
            "- inspect:      {{\"action\": \"list_layers\"|\"fields\"|\"summary\"|\"selected\"|\"select\""
            "|\"clear_selection\", \"layer_name\": \"...\", \"expression\": \"...\"}}\n"
            "  示例: {{\"action\": \"fields\", \"layer_name\": \"成都市_市\"}}\n"
            "  示例: {{\"action\": \"summary\", \"layer_name\": \"points\"}}\n"
            "- raster_style: {{\"layer_name\": \"图层名\", \"palette\": \"terrain\"|\"viridis\""
            "|\"grayscale\", \"min_value\": 数值, \"max_value\": 数值}}\n"
            "  示例: {{\"layer_name\": \"DEM\", \"palette\": \"terrain\"}}\n"
            "- layer_control: {{\"action\": \"add_vector\"|\"add_raster\"|\"add_xyz\"|\"set_visibility\""
            "|\"set_opacity\"|\"remove\"|\"hillshade\", ...}}\n"
            "  示例: {{\"action\": \"add_xyz\", \"source\": \"osm\"}}\n"
            "  示例: {{\"action\": \"set_visibility\", \"layer_name\": \"边界\", \"visible\": false}}\n"
            "  示例: {{\"action\": \"remove\", \"layer_name\": \"临时图层\"}}\n\n"
            "## 流水线规划规则\n\n"
            "1. 如果用户请求只需一步完成 → 输出单元素数组\n"
            "2. 如果用户请求需要多步链式处理（如裁剪→导出、缓冲区→样式→导出）→ 按依赖顺序排列\n"
            "3. 后续步骤的 arguments 中引用前序步骤输出时，使用图层名前缀如 [Buffer]、[Clip] 等\n"
            "4. 无法匹配任何技能 → 输出 [{\"skill\": \"unknown\", \"arguments\": \"{}\", "
            '"reasoning": "解释原因"}]\n'
            "5. 禁止输出单元素数组以外的任何 JSON 对象格式"
        )

    @staticmethod
    def build_user_content(
        user_text: str,
        viewport_snapshots: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """构建多模态 user message 的 content 数组。

        结构：文本块（含用户指令 + 视口元数据）→ 图片块。

        Parameters
        ----------
        user_text : str
            用户的自然语言指令。
        viewport_snapshots : list of dict
            CanvasCapture 返回的复合 Dict 列表，每个包含 image_base64 和 spatial_context。

        Returns
        -------
        list of dict
            [{"type": "text", "text": "..."}, {"type": "image_url", "image_url": {"url": "..."}}, ...]
        """
        content: List[Dict[str, Any]] = []

        # ── 文本块：用户指令 + 视口空间元数据 ──
        text_segments = [user_text]

        if viewport_snapshots:
            for i, snapshot in enumerate(viewport_snapshots, 1):
                sc = snapshot.get("spatial_context", {})
                if not sc:
                    continue

                crs = sc.get("crs", "UNKNOWN")
                extent = sc.get("extent", {})
                scale = sc.get("scale", 0.0)

                # 计算视口的近似地理尺寸
                width_deg = abs(extent.get("xmax", 0) - extent.get("xmin", 0))
                height_deg = abs(extent.get("ymax", 0) - extent.get("ymin", 0))

                metadata_block = (
                    f"\n\n[视口 {i} 空间元数据]\n"
                    f"- 画布坐标系: {crs}\n"
                    f"- 当前比例尺: 1:{scale:.0f}\n"
                    f"- 视口范围: xmin={extent.get('xmin', '?'):.6f}, "
                    f"xmax={extent.get('xmax', '?'):.6f}, "
                    f"ymin={extent.get('ymin', '?'):.6f}, "
                    f"ymax={extent.get('ymax', '?'):.6f}\n"
                    f"- 视口宽度: ~{width_deg:.4f}°, 视口高度: ~{height_deg:.4f}°"
                )

                # 尺度换算提示
                md_hint = MultimodalPromptBuilder._scale_hint(crs, scale, width_deg)
                if md_hint:
                    metadata_block += f"\n（{md_hint}）"

                text_segments.append(metadata_block)

        # 追加通用尺度换算提醒
        text_segments.append(
            "\n\n（提示：在 buffer / clip 等空间操作中，请基于上述尺度将用户标注的像素区域换算为地理距离）"
        )

        content.append({"type": "text", "text": "\n".join(text_segments)})

        # ── 图片块 ──
        for snapshot in viewport_snapshots:
            img_b64 = snapshot.get("image_base64", "")
            if img_b64:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": img_b64},
                })

        return content

    @staticmethod
    def build_messages(
        system_prompt: str,
        user_content: List[Dict[str, Any]],
        history_messages: Optional[List[Dict[str, str]]] = None,
    ) -> List[Dict[str, Any]]:
        """组装完整的多模态 messages 列表。

        Parameters
        ----------
        system_prompt : str
            系统提示词。
        user_content : list of dict
            build_user_content() 的输出。
        history_messages : list of dict, optional
            对话历史（纯文本格式，{"role":"user","content":"..."}）。

        Returns
        -------
        list of dict
            OpenAI Vision API 兼容的 messages 列表。
        """
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
        ]

        # 注入历史对话（纯文本格式保持兼容）
        if history_messages:
            messages.extend(history_messages)

        # 当前多模态消息
        messages.append({"role": "user", "content": user_content})

        return messages

    # ── 内部辅助 ──

    @staticmethod
    def _scale_hint(crs: str, scale: float, width_deg: float) -> str:
        """根据 CRS 类型和比例尺生成尺度换算提示。"""
        if not scale or scale <= 0:
            return ""

        # 粗略估计：1 像素在地面上的距离
        # 在 QGIS 中 scale = map_units_per_pixel * dpi_adjustment
        # 简化：1px ≈ scale / 72 * 0.0254 米（假设 72 DPI 屏幕）
        px_meters = scale / 72.0 * 0.0254

        if "4326" in crs or crs.upper() == "EPSG:4326":
            # 度坐标系：只能用近似换算
            # 1° ≈ 111,320m（赤道处），此处使用近似
            px_deg = px_meters / 111320.0
            return (
                f"比例尺 1:{scale:.0f}，约 {px_meters:.1f}m/像素"
                f"（~{px_deg:.8f}°/像素）"
            )
        else:
            return f"比例尺 1:{scale:.0f}，约 {px_meters:.2f}m/像素"
