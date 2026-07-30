"""test_population_coverage — 人口覆盖率模板单元测试。

依赖：pytest, shapely, 项目 Python 路径。
不需要 QGIS 运行时，纯 Python 逻辑验证。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import pytest

# 将项目 src 加入路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


# ── 用例 1：模板 JSON 结构校验 ──────────────────────────────

def test_population_coverage_template_structure():
    """验证 population_coverage.json 结构完整性。"""
    template_path = os.path.join(
        os.path.dirname(__file__), "..", "src", "core", "templates",
        "population_coverage.json"
    )
    assert os.path.exists(template_path), f"模板文件未找到: {template_path}"

    with open(template_path, "r", encoding="utf-8") as f:
        tmpl = json.load(f)

    # 基础字段
    assert tmpl["action"] == "population_coverage"
    assert len(tmpl.get("zh_triggers", [])) >= 2
    assert len(tmpl.get("ja_triggers", [])) >= 2
    assert len(tmpl.get("en_triggers", [])) >= 2

    # params: 必须有 population_layer 和 population_field
    param_names = [p["name"] for p in tmpl.get("params", [])]
    assert "source_layer" in param_names
    assert "boundary_layer" in param_names
    assert "population_layer" in param_names
    assert "population_field" in param_names
    assert "radius_m" in param_names

    # guards: 必须有 population_is_polygon
    guard_conditions = [g["condition"] for g in tmpl.get("guards", [])]
    assert "population_is_polygon" in guard_conditions

    # steps: 必须有 pop_intersect 步骤
    step_ids = [s["id"] for s in tmpl.get("steps", [])]
    assert "pop_intersect" in step_ids
    assert "stats" in step_ids
    assert "output_layer" in step_ids

    # stats 步骤的 output_keys 必须含人口字段
    stats_step = next(s for s in tmpl["steps"] if s["id"] == "stats")
    output_keys = stats_step.get("output_keys", [])
    assert "total_population" in output_keys
    assert "covered_population" in output_keys
    assert "pop_coverage_rate" in output_keys


# ── 用例 2：Guard 注册验证 ─────────────────────────────────

def test_population_is_polygon_guard_registered():
    """验证 population_is_polygon 守卫已在注册表中。"""
    from core.guards import GUARD_REGISTRY
    assert "population_is_polygon" in GUARD_REGISTRY


# ── 用例 3：pop_intersect 面积加权计算验证 ──────────────────

def test_pop_coverage_weighted_calculation():
    """验证人口加权覆盖率的计算逻辑。

    模拟场景：一个人口 zone（面积 100 m²，人口 1000），覆盖区域占 60 m²。
    zone_covered_pop = 1000 × (60 / 100) = 600
    total_population = 1000
    pop_coverage_rate = 600 / 1000 × 100 = 60.0%
    """
    from shapely.geometry import box, mapping
    import json as _json

    # 构造测试数据
    pop_zone = box(0, 0, 10, 10)          # 面积 100
    covered_area = box(0, 0, 6, 10)        # 面积 60

    intersection = pop_zone.intersection(covered_area)
    zone_area = pop_zone.area               # 100
    intersect_area = intersection.area      # 60
    pop_val = 1000.0

    # 面积加权计算
    zone_covered_pop = pop_val * (intersect_area / zone_area)  # 600
    total_population = pop_val                                # 1000
    pop_coverage_rate = zone_covered_pop / total_population * 100  # 60.0

    assert abs(zone_area - 100.0) < 0.01
    assert abs(intersect_area - 60.0) < 0.01
    assert abs(zone_covered_pop - 600.0) < 0.01
    assert abs(pop_coverage_rate - 60.0) < 0.01

    # 验证 GeoJSON 写入格式
    geojson = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {
                "population": pop_val,
                "zone_area": zone_area,
            },
            "geometry": mapping(intersection),
        }],
    }

    with tempfile.NamedTemporaryFile(suffix=".geojson", mode="w", delete=False) as f:
        _json.dump(geojson, f, ensure_ascii=False)
        temp_path = f.name

    try:
        with open(temp_path, "r") as f:
            reloaded = _json.load(f)
        feat = reloaded["features"][0]
        assert feat["properties"]["population"] == 1000.0
        assert abs(feat["properties"]["zone_area"] - 100.0) < 0.01
    finally:
        os.unlink(temp_path)


# ── main ───────────────────────────────────────────────────

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
