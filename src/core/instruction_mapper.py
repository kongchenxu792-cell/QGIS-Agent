"""
instruction_mapper.py — 多语言 GIS 指令映射层

将本地大模型解析出的自然语言指令对接底层 QGIS/GDAL 接口。
支持中/日/英三语指令模板匹配，提升小模型准确率。

架构（REFACTOR-3 拆分后）：
    template_registry.py  — 模板数据 + 关键词匹配 + 图层自动检测
    handlers_basic.py     — HandlersBasicMixin（24 个基础 handler）
    handlers_analysis.py  — HandlersAnalysisMixin（空间关联 + 覆盖率分析）
    instruction_mapper.py — InstructionMapper 类（Mixin 继承 + 路由核心）
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Optional

from core.handlers_basic import HandlersBasicMixin
from core.handlers_analysis import HandlersAnalysisMixin
from core.handlers_seismic import HandlersSeismicMixin
from core.template_registry import (
    _INSTRUCTION_TEMPLATES,
    _SYSTEM_PROMPT_ZH,
    _SYSTEM_PROMPT_JA,
    _SYSTEM_PROMPT_EN,
    find_template,
    keyword_pre_match,
    detect_lang,
    auto_detect_layers_from_text,
)

_log = logging.getLogger("instruction_mapper")

# ── 图层参数键名列表（所有分析操作涉及到的 layer 参数）──
_LAYER_PARAM_KEYS = [
    "target_layer", "join_layer", "layer_name",
    "source_layer", "boundary_layer",
    "population_layer", "population_field",
    "intensity_layer", "intensity_field",
]

# 字段类参数键（占位符应清空后交由字段自动探测，而非按图层名匹配）
_FIELD_PARAM_KEYS = {"population_field", "intensity_field"}

# 占位文案模式：小模型常照抄 system prompt 示例中的占位符
_PLACEHOLDER_PATTERNS = [
    r"图层名", r"字段名", r"图层\d+", r"字段\d+",
    r"field\d+", r"layer\s*name", r"参数名", r"xxx", r"\.\.\.",
]


def _is_placeholder(val: str) -> bool:
    """判断 LLM 输出值是否为占位文案（如「图层名」「字段1」「field2」）。"""
    if not val:
        return False
    v = val.strip().lower()
    for pat in _PLACEHOLDER_PATTERNS:
        if re.search(pat, v):
            return True
    return False


def _find_layer_by_name(project, name: str):
    """按图层名在项目中查找 QgsMapLayer（找不到返回 None）。"""
    if not project or not name:
        return None
    for layer in project.mapLayers().values():
        if layer.name() == name:
            return layer
    return None


def _auto_detect_field_params(project, params: Dict[str, Any]) -> None:
    """对清空/占位后的字段参数做自动探测兜底（就地修改 params）。

    - population_field：取 population_layer 首个数值字段
    - intensity_field：优先 J-SHIS 震度概率字段（T\\d+_I\\d+），否则首个数值字段
    """
    if not params.get("population_field") and params.get("population_layer"):
        pop_layer = _find_layer_by_name(project, params["population_layer"])
        if pop_layer is not None and hasattr(pop_layer, "fields"):
            for f in pop_layer.fields():
                if f.isNumeric():
                    params["population_field"] = f.name()
                    _log.info("自动探测人口字段：%s", f.name())
                    break
    if not params.get("intensity_field") and params.get("intensity_layer"):
        int_layer = _find_layer_by_name(project, params["intensity_layer"])
        if int_layer is not None and hasattr(int_layer, "fields"):
            try:
                from core.seismic_situation_map import JMA_INTENSITY_FIELD_PATTERN
            except Exception:
                JMA_INTENSITY_FIELD_PATTERN = None
            for f in int_layer.fields():
                if JMA_INTENSITY_FIELD_PATTERN and JMA_INTENSITY_FIELD_PATTERN.match(f.name()):
                    params["intensity_field"] = f.name()
                    _log.info("自动探测震度字段：%s", f.name())
                    break
            if not params.get("intensity_field"):
                for f in int_layer.fields():
                    if f.isNumeric():
                        params["intensity_field"] = f.name()
                        _log.info("兜底震度字段（首个数值型）：%s", f.name())
                        break


def _correct_layer_params(project, params: Dict[str, Any], user_text: str) -> Dict[str, Any]:
    """校验并修正 LLM 输出的图层参数。

    离线小模型（7B）经常幻想不存在的图层名（如 source_points），
    或照抄 system prompt 占位文案（如「图层名」「字段1」）。
    本函数在校验失败时：清除无效值 → auto_detect → 字段自动探测 → 单图层兜底。
    """
    loaded_layers = list(project.mapLayers().values())
    if not loaded_layers:
        return params

    loaded_names = [l.name() for l in loaded_layers]
    need_correction = False

    for key in _LAYER_PARAM_KEYS:
        val = params.get(key, "")
        if not val:
            continue
        if key in _FIELD_PARAM_KEYS:
            # 字段参数：占位文案直接清空，交由字段自动探测接管
            if _is_placeholder(val):
                _log.warning(
                    "LLM 占位字段 '%s' (参数 %s)，已清空待自动探测",
                    val, key,
                )
                params[key] = ""
                need_correction = True
            continue
        if val not in loaded_names:
            _log.warning(
                "LLM 幻想的图层名 '%s' (参数 %s) 不在项目中，可用图层：%s",
                val, key, loaded_names,
            )
            params[key] = ""
            need_correction = True

    if need_correction:
        params = auto_detect_layers_from_text(user_text, params, project)

    # 字段参数自动探测（占位符清空后触发）
    _auto_detect_field_params(project, params)

    # 单图层兜底：只加载了一个图层时，用它填充所有仍为空的 layer 参数
    if len(loaded_layers) == 1:
        only_name = loaded_names[0]
        for key in _LAYER_PARAM_KEYS:
            if key in params and (not params[key] or params[key] not in loaded_names):
                params[key] = only_name

    return params


class InstructionMapper(HandlersBasicMixin, HandlersAnalysisMixin, HandlersSeismicMixin):
    """多语言 GIS 指令映射器。

    将大模型输出的自然语言指令匹配到预定义操作模板，并调用对应 QGIS API。

    Handler 方法通过 HandlersBasicMixin / HandlersAnalysisMixin 继承注入，
    match_and_execute 通过 getattr(self, handler_name) 动态路由。
    """

    def __init__(self, iface: Any = None) -> None:
        self._iface = iface  # QgisInterface 引用（可选）
        self._templates = _INSTRUCTION_TEMPLATES

    @staticmethod
    def get_system_prompt(lang: str = "zh") -> str:
        """获取离线模式系统提示词。"""
        prompts = {"zh": _SYSTEM_PROMPT_ZH, "ja": _SYSTEM_PROMPT_JA, "en": _SYSTEM_PROMPT_EN}
        return prompts.get(lang, _SYSTEM_PROMPT_EN)

    def match_and_execute(
        self,
        llm_response: str,
        canvas: Any = None,
        project: Any = None,
        user_text: str = "",
    ) -> Dict[str, Any]:
        """解析 LLM 响应并执行匹配的指令。

        Parameters
        ----------
        llm_response : str
            本地大模型的原始响应文本。
        canvas : QgsMapCanvas or None
            当前地图画布。
        project : QgsProject or None
            当前 QGIS 项目。
        user_text : str
            用户原始输入文本，用于关键词兜底匹配和图层名自动检测。

        Returns
        -------
        dict
            {"success": bool, "message": str, "action": str or None}
        """
        # 1. 尝试从 LLM 响应中提取 JSON
        instruction = self._extract_json(llm_response)
        if instruction is None:
            return {"success": False, "message": llm_response.strip()[:500], "action": None}

        action = instruction.get("action", "")
        params = instruction.get("params", {})
        _log.info("LLM extracted: action=%s params_keys=%s", action, sorted(params.keys()) if params else [])

        # 2. 关键词兜底：LLM 返回 unknown 时，用触发词做最后一次匹配
        if action == "unknown" and user_text:
            keyword_match = keyword_pre_match(user_text, lang=detect_lang(user_text))
            if keyword_match:
                action = keyword_match["action"]
                kw_params = keyword_match.get("params", {})
                for k, v in kw_params.items():
                    if k not in params or not params[k]:
                        params[k] = v
                params = auto_detect_layers_from_text(user_text, params, project)

        # 2b. 关键词纠偏：LLM 返回了具体动作但可能与用户意图不符，用关键词做交叉校验
        if user_text and action not in ("unknown", "answer"):
            keyword_match = keyword_pre_match(user_text, lang=detect_lang(user_text))
            if keyword_match and keyword_match["action"] != action:
                _log.info(
                    "关键词纠偏：LLM=%s → keyword=%s (user=%.60s)",
                    action, keyword_match["action"], user_text,
                )
                action = keyword_match["action"]
                kw_params = keyword_match.get("params", {})
                for k, v in kw_params.items():
                    if k not in params or not params[k]:
                        params[k] = v
                params = auto_detect_layers_from_text(user_text, params, project)

        if action == "unknown":
            return {"success": False, "message": instruction.get("message", "无法识别指令"), "action": "unknown"}
        if action == "answer":
            return {"success": True, "message": instruction.get("message", ""), "action": "answer"}

        # 3. 匹配模板
        template = find_template(action)
        if template is None:
            return {"success": False, "message": f"不支持的操作：{action}", "action": action}

        # 3.5 图层名校验与自动修正：LLM 可能幻想不存在的图层名
        if project:
            params = _correct_layer_params(project, params, user_text)

        # 3.6 参数键别名：LLM 可能输出 layer 而非 layer_name（export_attribute/zoom_to_layer 等）
        if "layer" in params and "layer_name" not in params:
            params["layer_name"] = params["layer"]
            _log.info("参数键别名：layer → layer_name (%s)", params["layer_name"])

        # 4. 执行处理函数
        handler_name = template["handler"]
        handler = getattr(self, handler_name, None)
        if handler is None:
            return {"success": False, "message": f"处理函数未实现：{handler_name}", "action": action}

        try:
            _log.info("Calling handler=%s with params=%s", handler_name, {k: v for k, v in params.items() if v})
            result = handler(canvas=canvas, project=project, **params)
            result["action"] = action
            return result
        except Exception as e:
            _log.exception(f"执行指令 {action} 失败")
            return {"success": False, "message": f"执行失败：{e}", "action": action}

    def _extract_json(self, text: str) -> Optional[Dict[str, Any]]:
        """从文本中提取 JSON 对象。增强 7B 模型输出容错。"""
        text = text.strip()

        # 1. 直接解析
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 2. Markdown 代码块
        match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass

        # 3. 找到第一个 { 到最后一个 }，尝试解析（容错兜底）
        start = text.find('{')
        end = text.rfind('}')
        if start != -1 and end != -1 and end > start:
            candidate = text[start:end + 1]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass

        return None
