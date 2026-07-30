"""技能管理器 - 启动时自动扫描、加载、注册所有技能模块。"""

import logging
_log = logging.getLogger(__name__)
import importlib
import os
from pathlib import Path
from typing import Dict, Optional

from skills.base_skill import BaseSkill


class SkillManager:
    """技能管理器：自动发现、注册、执行 Skills。"""

    def __init__(self):
        self._skills: Dict[str, BaseSkill] = {}
        self._scan_and_load()

    def _scan_and_load(self) -> None:
        """扫描 skills 目录，动态导入所有有效技能模块。"""
        skills_dir = Path(__file__).parent
        for item in skills_dir.iterdir():
            # 跳过框架文件和非 Python 文件
            if item.name.startswith("_") or item.name.startswith("base"):
                continue
            if item.suffix != ".py" or item.name == "skill_manager.py":
                continue

            module_name = item.stem
            try:
                module = importlib.import_module(f"skills.{module_name}")
                # 查找模块中继承 BaseSkill 的类
                for attr_name in dir(module):
                    attr = getattr(module, attr_name)
                    if (
                        isinstance(attr, type)
                        and issubclass(attr, BaseSkill)
                        and attr is not BaseSkill
                    ):
                        instance = attr()
                        self._skills[instance.get_name()] = instance
            except Exception as e:
                _log.info(f"[SkillManager] 加载 {module_name} 失败: {e}")

    def register(self, skill: BaseSkill) -> None:
        """手动注册一个技能。"""
        self._skills[skill.get_name()] = skill

    def get_skill(self, name: str) -> Optional[BaseSkill]:
        """根据名称获取技能实例。"""
        return self._skills.get(name)

    def get_all_skills(self) -> Dict[str, BaseSkill]:
        """获取所有已注册技能。"""
        return dict(self._skills)

    def get_skill_names(self) -> list:
        """获取所有技能名称列表。"""
        return list(self._skills.keys())

    def build_system_prompt_skills_section(self) -> str:
        """
        动态生成系统提示词中的「可用技能清单」部分。

        Returns
        -------
        str
            格式化的技能清单文本。
        """
        if not self._skills:
            return "（无可用技能）"

        lines = []
        for i, (name, skill) in enumerate(self._skills.items(), 1):
            lines.append(f"### {i}. {name}")
            lines.append(f"{skill.get_description()}")
            lines.append("")
        return "\n".join(lines)

    def execute_skill(
        self,
        name: str,
        canvas=None,
        layer_tree=None,
        arguments: str = "",
        **kwargs,
    ) -> Dict:
        """
        根据名称执行技能。

        Parameters
        ----------
        name : str
            技能名称。
        canvas : QgsMapCanvas, optional
            地图画布。
        layer_tree : QgsLayerTreeView, optional
            图层树视图。
        arguments : str
            参数文本。
        **kwargs
            额外参数。

        Returns
        -------
        dict
            {"success": bool, "message": str, ...}
        """
        skill = self._skills.get(name)
        if not skill:
            return {"success": False, "message": f"未知技能：{name}"}
        try:
            # Phase 5：GeoAgent 空间拦截防御卫士
            # 前置防御：参数修正
            from .geoagent_guard import geoagent_pre_execution_guard
            # 提取技能参数（部分技能内部会解析 arguments）
            # 这里我们假设技能内部会调用 processing.run，卫士在技能内部执行前介入
            # 我们将在 skill.execute 内部调用卫士，因此这里仅传递参数
            
            # 执行技能
            result = skill.execute(
                canvas=canvas,
                layer_tree=layer_tree,
                arguments=arguments,
                **kwargs,
            )
            
            # 后置防御：类型安全
            from .geoagent_guard import geoagent_post_execution_guard
            if "layer" in result:
                result["layer"] = geoagent_post_execution_guard(
                    result["layer"], 
                    display_name=kwargs.get("display_name", "图层结果")
                )
            if "added_layers" in result:
                result["added_layers"] = [
                    geoagent_post_execution_guard(layer, f"图层{i+1}")
                    for i, layer in enumerate(result["added_layers"])
                ]
            if "styled_layers" in result:
                result["styled_layers"] = [
                    geoagent_post_execution_guard(layer, layer.name())
                    for layer in result["styled_layers"]
                ]
            if "loaded_layers" in result:
                result["loaded_layers"] = [
                    geoagent_post_execution_guard(layer, layer.name())
                    for layer in result["loaded_layers"]
                ]
            
            return result
        except Exception as e:
            return {"success": False, "message": f"技能执行异常：{e}"}


# 全局单例
_skill_manager: Optional[SkillManager] = None


def get_skill_manager() -> SkillManager:
    """获取全局技能管理器单例。"""
    global _skill_manager
    if _skill_manager is None:
        _skill_manager = SkillManager()
    return _skill_manager