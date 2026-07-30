"""
GeoAgent 空间拦截防御卫士 — 技能层前置/后置智能防御

在 SkillManager 执行入口织入，无论文本路径还是多模态路径返回的
JSON 技能流，在调用 processing.run 或执行具体 GIS 算子前，
必须无感通过此卫士。

防御契约：
    A. 图层位置对调防御（防大模型将切刀与蛋糕参数传反）
    B. 时空绝对对齐防御（防 CRS 错位导致裁剪空白/偏移）
    C. 类型防崩防御（彻底根除 'str' object has no attribute 'setName'）

设计原则：
    - 零侵入：不修改任何现有技能代码
    - 无感知：防御失败不抛异常，仅打印警告并原样放行
    - 可观测：所有防御动作均带 [GeoAgent 防御卫士] 日志前缀
"""

from __future__ import annotations

import logging
from typing import Any, Dict

_log = logging.getLogger("geoagent_guard")

# ─── 需要 CRS 对齐的地理空间操作技能白名单 ───
_CRS_SENSITIVE_SKILLS = frozenset({"clip", "intersection", "difference", "union"})


def geoagent_pre_execution_guard(skill_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """技能执行前的前置空间契约校验。

    防止大模型因幻觉将参数传反、忽略 CRS 差异导致结果异常。

    Parameters
    ----------
    skill_name : str
        即将执行的技能名称
    params : dict
        技能参数字典（会被原地修改）

    Returns
    -------
    dict
        经过校验（可能被修正）的参数
    """
    if not params:
        return params

    _log.info(f"[GeoAgent 防御卫士] 正在对技能 '{skill_name}' 进行空间契约校验...")

    # ── 加载 QGIS API ──
    try:
        from qgis.core import QgsProject, QgsVectorLayer, QgsRasterLayer
    except ImportError as e:
        _log.warning("QGIS 环境不可用，跳过 GeoAgent 防御：%s", e)
        return params

    # ── 防御 A：图层位置对调防御（防传反） ──
    _guard_param_swap(skill_name, params, QgsProject)

    # ── 防御 B：时空绝对对齐防御（防 CRS 错位） ──
    _guard_crs_alignment(skill_name, params, QgsProject)

    _log.info(f"[GeoAgent 防御卫士] 技能 '{skill_name}' 前置防御完成。")
    return params


def geoagent_post_execution_guard(
    output_result: Any, display_name: str = "图层结果"
) -> Any:
    """技能执行后的类型安全防御。

    确保吐给画布和图层列表的是合法的 QgsVectorLayer / QgsRasterLayer 对象，
    而非裸字符串路径。

    Parameters
    ----------
    output_result : Any
        技能执行返回的原始结果
    display_name : str
        图层显示名称

    Returns
    -------
    Any
        类型安全的图层对象或原始结果
    """
    if output_result is None:
        return None

    # ── 防御 C：类型防崩（字符串路径 → QgsVectorLayer） ──
    if isinstance(output_result, str):
        _log.info(
            f"[类型安全纠回] 拦截到字符串路径: {output_result}，"
            f"正在实例化为合法的 QgsVectorLayer 对象..."
        )
        try:
            from qgis.core import QgsVectorLayer, QgsRasterLayer

            # 根据扩展名判断图层类型
            lower = output_result.lower()
            if lower.endswith(('.tif', '.tiff', '.img', '.jp2')):
                final_layer = QgsRasterLayer(output_result, display_name)
            else:
                final_layer = QgsVectorLayer(output_result, display_name, "ogr")

            if final_layer.isValid():
                _log.info(f"[类型安全纠回] 成功实例化为: {type(final_layer).__name__}")
                return final_layer
            else:
                _log.info(
                    f"[类型安全纠回] 实例化失败，保留原始路径: {output_result}"
                )
                return output_result
        except Exception as e:
            _log.info(f"[类型安全纠回] 异常，保留原始结果: {e}")
            return output_result

    return output_result


# ═══════════════════════════════════════════════════════════════
# 内部防御子函数
# ═══════════════════════════════════════════════════════════════


def _resolve_layer(value: Any, QgsProject: Any) -> Any:
    """将参数值统一解析为 QgsVectorLayer 对象。

    兼容两种输入：
    - QgsVectorLayer 对象：直接返回
    - str（图层名称）：通过 QgsProject.mapLayersByName 查找
    """
    if value is None:
        return None
    # 已经是图层对象
    if hasattr(value, "geometryType") and hasattr(value, "crs"):
        return value
    # 字符串名称 → 查找
    if isinstance(value, str):
        try:
            layers = QgsProject.instance().mapLayersByName(value)
            return layers[0] if layers else None
        except Exception:
            return None
    return None


def _guard_param_swap(
    skill_name: str, params: Dict[str, Any], QgsProject: Any
) -> None:
    """防御 A：检测并将切刀与蛋糕参数对调纠正。

    铁律：点图层（geometryType==0）绝对不能作为裁剪边界去裁剪
    面图层（geometryType==2）。如果大模型传反了（用面去切点），
    强制对调 INPUT 和 OVERLAY。
    """
    if skill_name != "clip":
        return

    input_val = params.get("INPUT")
    overlay_val = params.get("OVERLAY")
    if input_val is None or overlay_val is None:
        return

    input_layer = _resolve_layer(input_val, QgsProject)
    overlay_layer = _resolve_layer(overlay_val, QgsProject)

    if input_layer is None or overlay_layer is None:
        return

    input_gt = input_layer.geometryType()
    overlay_gt = overlay_layer.geometryType()

    # 面(2)裁剪点(0) → 逻辑正常
    if input_gt == 0 and overlay_gt == 2:
        return
    # 面(2)裁剪面(2) / 线(1)裁剪面(2) → 正常
    if overlay_gt in (2, 1) and input_gt in (2, 1):
        return

    # 异常：点(0)作为 OVERLAY 去裁剪面(2) → 对调
    if input_gt == 2 and overlay_gt == 0:
        input_name = getattr(input_val, "name", lambda: str(input_val))() if hasattr(input_val, "name") else str(input_val)
        overlay_name = getattr(overlay_val, "name", lambda: str(overlay_val))() if hasattr(overlay_val, "name") else str(overlay_val)
        _log.info(
            "[防御预警] 发现大模型将切刀与蛋糕参数传反！"
            f"（INPUT={input_name} 是面，OVERLAY={overlay_name} 是点）"
            " 已自动启动矢量对调纠错机制。"
        )
        params["INPUT"] = overlay_val
        params["OVERLAY"] = input_val
        return

    # 其他异常组合：警告但不强制修改
    input_name = input_layer.name()
    overlay_name = overlay_layer.name()
    _log.info(
        f"[防御注意] 裁剪参数几何类型组合可疑 "
        f"（INPUT={input_name} gt={input_gt}，"
        f"OVERLAY={overlay_name} gt={overlay_gt}），跳过自动对调。"
    )


def _guard_crs_alignment(
    skill_name: str, params: Dict[str, Any], QgsProject: Any
) -> None:
    """防御 B：自动、无感地将 OVERLAY 图层重投影对齐到 INPUT 图层坐标系。

    针对 clip / intersection / difference 等需要两图层 CRS 一致的算子。
    兼容 QgsVectorLayer 对象和 str（图层名称）两种参数格式。
    """
    if skill_name not in _CRS_SENSITIVE_SKILLS:
        return

    input_val = params.get("INPUT")
    overlay_val = params.get("OVERLAY")
    if input_val is None or overlay_val is None:
        return

    input_layer = _resolve_layer(input_val, QgsProject)
    overlay_layer = _resolve_layer(overlay_val, QgsProject)

    if input_layer is None or overlay_layer is None:
        return

    input_crs = input_layer.crs().authid()
    overlay_crs = overlay_layer.crs().authid()

    if input_crs == overlay_crs:
        return

    _log.info(
        f"[CRS 强行对齐] 检测到坐标系冲突 "
        f"（INPUT={input_crs} vs OVERLAY={overlay_crs}）。"
    )
    _log.info("正在后台自动、无感调用 native:reprojectlayer 将切刀对齐至主图层坐标系...")

    try:
        import processing
        reproject_res = processing.run(
            "native:reprojectlayer",
            {
                "INPUT": overlay_layer,
                "TARGET_CRS": input_layer.crs(),
                "OUTPUT": "memory:geoagent_aligned_overlay",
            },
        )
        aligned = reproject_res.get("OUTPUT")
        if aligned is not None:
            params["OVERLAY"] = aligned
            _log.info(
                f"[CRS 强行对齐] 切刀已重投影至 {input_crs}，"
                f"掉包 OVERLAY 为内存图层对象。"
            )
        else:
            _log.info("[CRS 强行对齐] 重投影返回空，保留原 OVERLAY 参数。")
    except Exception as e:
        _log.info(f"[CRS 强行对齐] 重投影异常，保留原参数继续：{e}")
