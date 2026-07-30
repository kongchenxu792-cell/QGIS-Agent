"""
Tier0 离线闭环技能（统一包装）

将 loop_a / loop_b 包装为 BaseSkill 子类，供 SkillManager 自动发现。
"""

import os
from typing import Any, Dict

from skills.base_skill import BaseSkill
from skills.tier0_templates import match_loop, execute_loop


class Tier0LoopSkill(BaseSkill):
    """Tier0 离线闭环技能。"""

    def get_name(self) -> str:
        return "tier0_loop"

    def get_description(self) -> str:
        return (
            "Tier0 离线闭环技能（无 LLM 依赖）。\n"
            "触发词：震度影响区态势图、避难所覆盖盲区、地震应急分析。\n"
            "输入：图层路径或图层名 + 参数（如半径）。\n"
            "输出：统一结果契约（layers/files/messages/stats）。\n"
            "优先级：离线模式最高，优先于自由代码生成。"
        )

    def execute(
        self,
        canvas=None,
        layer_tree=None,
        arguments: str = "",
        **kwargs,
    ) -> Dict[str, Any]:
        # 1. 匹配闭环
        loop_id = match_loop(arguments)
        if not loop_id:
            return {
                "success": False,
                "message": f"未识别到 Tier0 闭环关键词。用户输入: {arguments}",
            }

        # 2. 从 kwargs 提取图层参数
        #    约定：kwargs 中可能包含 active_layer、layers_by_name
        #    但 Tier0 模板要求显式传入图层路径或图层名
        #    这里简单地将 arguments 作为参数传递给 execute_loop
        #    实际参数解析由 Tier1 层完成，此处仅做演示
        from src.core.output_persistence import generate_output_path
        output_dir = os.path.dirname(generate_output_path("tier0", "_"))

        # 3. 执行闭环（参数由 Tier1 解析后填充）
        #    这里仅演示，实际应传入解析后的参数
        result = {
            "layers": [],
            "files": [],
            "messages": [
                {"level": "info", "content": f"Tier0 闭环 '{loop_id}' 已匹配，但缺少参数解析。"},
                {"level": "info", "content": "请通过 Tier1 参数解析层传入图层路径。"},
            ],
            "stats": {},
        }

        # 4. 转换为 BaseSkill 返回格式
        return {
            "success": True,
            "message": f"Tier0 闭环 '{loop_id}' 已就绪，等待参数解析。",
            "result": result,
        }
