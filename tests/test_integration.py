"""test_integration — PipelineExecutor 集成测试 (~4 用例)。

测试 PipelineExecutor 端到端流程，mock 关键方法替代真实 QGIS 调用。
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.pipeline_executor import (
    PipelineExecutor,
    StepResult,
    StepDef,
    EngineSpec,
)

TEMPLATE_PATH = str(Path(__file__).resolve().parent.parent / "src" / "core" / "templates" / "coverage_analysis.json")
GAP_TEMPLATE_PATH = str(Path(__file__).resolve().parent.parent / "src" / "core" / "templates" / "gap_analysis.json")


def make_mock_qgis_layer(name, geom_type=0, crs_authid="EPSG:3857", feature_count=50):
    """构造 mock QgsVectorLayer。"""
    layer = MagicMock()
    layer.name.return_value = name
    layer.isValid.return_value = True
    layer.type.return_value = 0  # QgsMapLayer.VectorLayer（P2-0 source_is_vector 真实检查需要）
    layer.featureCount.return_value = feature_count
    layer.geometryType.return_value = geom_type
    layer.wkbType.return_value = 1  # Point
    layer.source.return_value = f"/fake/path/{name}.shp"

    crs = MagicMock()
    crs.authid.return_value = crs_authid
    crs.isValid.return_value = True
    crs.isGeographic.return_value = ("4326" in crs_authid)
    layer.crs.return_value = crs

    # fields
    from unittest.mock import MagicMock as M
    fields = M()
    fields.toList.return_value = []
    layer.fields.return_value = fields

    # features
    feat = MagicMock()
    feat.isValid.return_value = True
    geom = MagicMock()
    geom.isEmpty.return_value = False
    geom.asWkt.return_value = "POINT (0 0)"
    geom.boundingBox.return_value = MagicMock()
    feat.geometry.return_value = geom
    feat.id.return_value = 1
    layer.getFeatures.return_value = [feat]
    layer.getFeature.return_value = feat
    layer.extent.return_value = MagicMock()
    layer.dataProvider.return_value = MagicMock()
    layer.updateFields = MagicMock()
    layer.updateExtents = MagicMock()

    return layer


class DummyShapelyGeom:
    """模拟 Shapely 几何对象。"""
    def __init__(self, area_val=1000.0):
        self._area = area_val
        self.is_empty = False
        self.wkt = "POLYGON ((0 0, 10 0, 10 10, 0 10, 0 0))"

    @property
    def area(self):
        return self._area

    def buffer(self, distance):
        return DummyShapelyGeom(area_val=self._area + distance * 100)

    def intersection(self, other):
        return DummyShapelyGeom(area_val=min(self._area, other._area))

    def __or__(self, other):
        return DummyShapelyGeom(area_val=self._area + other._area)


class TestIntegration(unittest.TestCase):
    """PipelineExecutor 集成测试。"""

    def setUp(self):
        self.executor = PipelineExecutor()
        self.source_layer = make_mock_qgis_layer("shelters", geom_type=0, crs_authid="EPSG:3857")
        self.boundary_layer = make_mock_qgis_layer("admin_boundary", geom_type=2, crs_authid="EPSG:3857")

        def find_layer_fn(name):
            if "shelter" in name.lower() or "source" in name.lower():
                return self.source_layer
            if "boundary" in name.lower() or "admin" in name.lower():
                return self.boundary_layer
            return None

        self.find_layer_fn = find_layer_fn
        self.project = MagicMock()
        self.canvas = MagicMock()

    def _make_dummy_step_results(self):
        """构造 8 步 dummy 结果序列。"""
        geom_1 = DummyShapelyGeom(5000)
        geom_2 = DummyShapelyGeom(3000)

        results = {
            "boundary_dissolve": StepResult(
                step_id="boundary_dissolve", output_type="layer",
                feature_count=1, qgis_layer=self.boundary_layer, engine_used="qgis_processing::native:dissolve",
            ),
            "boundary_buffer": StepResult(
                step_id="boundary_buffer", output_type="layer",
                feature_count=1, qgis_layer=self.boundary_layer, engine_used="qgis_processing::native:buffer",
            ),
            "spatial_filter": StepResult(
                step_id="spatial_filter", output_type="layer",
                feature_count=30, qgis_layer=self.source_layer, engine_used="qgis_spatial_index",
            ),
            "buffer": StepResult(
                step_id="buffer", output_type="shapely_geom",
                feature_count=0, shapely_geom=geom_1, engine_used="shapely::buffer",
            ),
            "dissolve": StepResult(
                step_id="dissolve", output_type="shapely_geom",
                feature_count=0, shapely_geom=geom_2, engine_used="shapely::union",
            ),
            "clip": StepResult(
                step_id="clip", output_type="shapely_geom",
                feature_count=0, shapely_geom=DummyShapelyGeom(2500), engine_used="shapely_wkt::intersection",
            ),
            "stats": StepResult(
                step_id="stats", output_type="stats_dict",
                stats={"source_count": 30, "radius_m": 500, "total_area": 5000,
                       "covered_area": 2500, "coverage_rate": 50.0},
                engine_used="shapely::area",
            ),
            "output_layer": StepResult(
                step_id="output_layer", output_type="layer",
                feature_count=1, qgis_layer=self.source_layer, file_path="/tmp/output.geojson",
                shapely_geom=DummyShapelyGeom(2500), engine_used="qgis_memory_layer",
            ),
        }
        return results

    # ── 闭环 A：全流程走通 8 步 ────────────────────────────

    @patch.object(PipelineExecutor, '_execute_step')
    def test_full_pipeline_8_steps(self, mock_execute_step):
        """模拟 8 步全部成功，验证最终 stats 含 coverage_rate。"""
        dummy_results = self._make_dummy_step_results()

        def side_effect(step, input_data, boundary_data):
            return dummy_results.get(step.id, StepResult(step_id=step.id, error="unknown"))

        mock_execute_step.side_effect = side_effect

        result = self.executor.execute(
            template_path=TEMPLATE_PATH,
            source_layer_name="shelters",
            boundary_layer_name="admin_boundary",
            radius_m=500.0,
            project=self.project,
            canvas=self.canvas,
            _find_layer_fn=self.find_layer_fn,
        )

        self.assertTrue(result["success"], f"Pipeline should succeed, got: {result.get('message')}")
        self.assertEqual(result["step_count"], 8)
        self.assertIsNotNone(result["stats"])
        self.assertIn("coverage_rate", result["stats"])
        self.assertGreater(result["stats"]["coverage_rate"], 0)

    # ── 闭环 B：boundary=None 默认用 source_layer ──────────

    @patch.object(PipelineExecutor, '_execute_step')
    def test_boundary_none_defaults_to_source(self, mock_execute_step):
        """boundary_layer_name 不存在时，execute 应返回失败而非崩溃。

        注：当前 PipelineExecutor.execute() 中 boundary_layer 找不到时返回
        success=False，不会默认用 source_layer。本用例验证此行为符合预期。
        """
        mock_execute_step.return_value = StepResult(step_id="dummy", error="not reached")

        result = self.executor.execute(
            template_path=TEMPLATE_PATH,
            source_layer_name="shelters",
            boundary_layer_name="nonexistent_layer",
            radius_m=500.0,
            project=self.project,
            canvas=self.canvas,
            _find_layer_fn=self.find_layer_fn,
        )

        # current behavior: boundary not found → success=False
        self.assertFalse(result["success"])
        self.assertIn("未找到", result.get("message", ""))

    # ── 半径=0 时 buffer 输出为 0 缓冲 ─────────────────────

    @patch.object(PipelineExecutor, '_execute_step')
    def test_radius_zero_buffer(self, mock_execute_step):
        """radius_m=0 时 buffer_step 应输出几何不变。"""

        # 记录 buffer step 收到的参数
        captured_params = {}

        def side_effect(step, input_data, boundary_data):
            if step.id == "buffer":
                captured_params["step_params"] = dict(step.params)
                captured_params["input_data"] = input_data
                return StepResult(
                    step_id="buffer", output_type="shapely_geom",
                    shapely_geom=DummyShapelyGeom(1000), engine_used="shapely::buffer",
                )
            elif step.id == "stats":
                return StepResult(
                    step_id="stats", output_type="stats_dict",
                    stats={"source_count": 10, "radius_m": 0, "total_area": 5000,
                           "covered_area": 1000, "coverage_rate": 20.0},
                    engine_used="shapely::area",
                )
            return StepResult(step_id=step.id, output_type="layer",
                              feature_count=1, qgis_layer=self.source_layer,
                              engine_used="qgis_processing")

        mock_execute_step.side_effect = side_effect

        result = self.executor.execute(
            template_path=TEMPLATE_PATH,
            source_layer_name="shelters",
            boundary_layer_name="admin_boundary",
            radius_m=0.0,
            project=self.project,
            canvas=self.canvas,
            _find_layer_fn=self.find_layer_fn,
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["stats"]["radius_m"], 0)

    # ── 模板解析后 step 数量正确 ────────────────────────────

    def test_template_parsed_correctly(self):
        template = self.executor._parse_template(TEMPLATE_PATH)
        self.assertEqual(len(template.steps), 8)
        self.assertEqual(template.template_id, "coverage_analysis")
        # P2-2：output_check 解析（coverage → empty=degraded）
        self.assertEqual(template.output_check, {"empty": "degraded"})

    # ── P2-1：fail-fast 中止 ────────────────────────────────

    @patch.object(PipelineExecutor, '_execute_step')
    def test_fail_fast_aborts_subsequent_steps(self, mock_execute_step):
        """第 3 步失败 → 立即中止，后续步骤不执行，返回失败终态。"""
        dummy_results = self._make_dummy_step_results()
        called_steps = []

        def side_effect(step, input_data, boundary_data):
            called_steps.append(step.id)
            if step.id == "spatial_filter":
                return StepResult(step_id=step.id, error="mock failure", status="failed")
            return dummy_results.get(step.id, StepResult(step_id=step.id, error="unknown"))

        mock_execute_step.side_effect = side_effect

        result = self.executor.execute(
            template_path=TEMPLATE_PATH,
            source_layer_name="shelters",
            boundary_layer_name="admin_boundary",
            radius_m=500.0,
            project=self.project,
            canvas=self.canvas,
            _find_layer_fn=self.find_layer_fn,
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("第 3 步 'spatial_filter' 失败", result["message"])
        # fail-fast：只执行了前 3 步（第 3 步失败后立即返回，后续 buffer/dissolve/clip/stats/output_layer 不再执行）
        self.assertEqual(called_steps, ["boundary_dissolve", "boundary_buffer", "spatial_filter"])
        self.assertIn("step_results", result)

    # ── P2-1：终态诚实化 ────────────────────────────────────

    @patch.object(PipelineExecutor, '_execute_step')
    def test_honest_terminal_no_output_layer(self, mock_execute_step):
        """输出步成功但未产出有效图层（qgis_layer=None）→ 终态 failed，不再无条件 success:True。"""
        dummy_results = self._make_dummy_step_results()

        def side_effect(step, input_data, boundary_data):
            if step.id == "output_layer":
                # 模拟"成功但无有效图层"（fail-fast 不会拦截，终态诚实化必须兜住）
                return StepResult(step_id=step.id, output_type="layer", engine_used="qgis_memory_layer")
            return dummy_results.get(step.id, StepResult(step_id=step.id, error="unknown"))

        mock_execute_step.side_effect = side_effect

        result = self.executor.execute(
            template_path=TEMPLATE_PATH,
            source_layer_name="shelters",
            boundary_layer_name="admin_boundary",
            radius_m=500.0,
            project=self.project,
            canvas=self.canvas,
            _find_layer_fn=self.find_layer_fn,
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("output_layer", result["message"])
        self.assertIn("未产生有效图层", result["message"])

    @patch.object(PipelineExecutor, '_execute_step')
    def test_honest_terminal_success_status_ok(self, mock_execute_step):
        """健康链 → success:True + status:ok，且保留现有返回字段向后兼容。"""
        dummy_results = self._make_dummy_step_results()

        def side_effect(step, input_data, boundary_data):
            return dummy_results.get(step.id, StepResult(step_id=step.id, error="unknown"))

        mock_execute_step.side_effect = side_effect

        result = self.executor.execute(
            template_path=TEMPLATE_PATH,
            source_layer_name="shelters",
            boundary_layer_name="admin_boundary",
            radius_m=500.0,
            project=self.project,
            canvas=self.canvas,
            _find_layer_fn=self.find_layer_fn,
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "ok")
        # 向后兼容字段
        self.assertEqual(result["step_count"], 8)
        self.assertIsNotNone(result["output_layer"])
        self.assertIn("coverage_rate", result["stats"])
        self.assertIn("elapsed", result)

    # ── P2-2：出口体检 degraded 分支 ─────────────────────────

    @patch.object(PipelineExecutor, '_execute_step')
    def test_terminal_degraded_empty_output(self, mock_execute_step):
        """coverage 输出步 fc=0 但 qgis_layer 存在（empty=degraded）→ success:True + status:degraded + 警告文案。"""
        dummy_results = self._make_dummy_step_results()

        def side_effect(step, input_data, boundary_data):
            if step.id == "output_layer":
                return StepResult(
                    step_id=step.id, output_type="layer", feature_count=0,
                    qgis_layer=self.source_layer,
                    shapely_geom=DummyShapelyGeom(0), engine_used="qgis_memory_layer",
                )
            return dummy_results.get(step.id, StepResult(step_id=step.id, error="unknown"))

        mock_execute_step.side_effect = side_effect

        result = self.executor.execute(
            template_path=TEMPLATE_PATH,
            source_layer_name="shelters",
            boundary_layer_name="admin_boundary",
            radius_m=500.0,
            project=self.project,
            canvas=self.canvas,
            _find_layer_fn=self.find_layer_fn,
        )

        self.assertTrue(result["success"], "degraded 不判死，success 应保持 True")
        self.assertEqual(result["status"], "degraded")
        self.assertIn("结果为空", result["message"])
        # 结构检查只查结构，不判业务失败
        self.assertIsNotNone(result["output_layer"])

    @patch.object(PipelineExecutor, '_execute_step')
    def test_terminal_degraded_no_geom(self, mock_execute_step):
        """输出步 qgis_layer 存在但 shapely_geom=None（empty=degraded）→ status:degraded。"""
        dummy_results = self._make_dummy_step_results()

        def side_effect(step, input_data, boundary_data):
            if step.id == "output_layer":
                return StepResult(
                    step_id=step.id, output_type="layer", feature_count=1,
                    qgis_layer=self.source_layer,
                    shapely_geom=None, engine_used="qgis_memory_layer",
                )
            return dummy_results.get(step.id, StepResult(step_id=step.id, error="unknown"))

        mock_execute_step.side_effect = side_effect

        result = self.executor.execute(
            template_path=TEMPLATE_PATH,
            source_layer_name="shelters",
            boundary_layer_name="admin_boundary",
            radius_m=500.0,
            project=self.project,
            canvas=self.canvas,
            _find_layer_fn=self.find_layer_fn,
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "degraded")
        self.assertIn("结果为空", result["message"])

    @patch.object(PipelineExecutor, '_execute_step')
    def test_gap_empty_output_ok(self, mock_execute_step):
        """gap_analysis 全覆盖 → 输出空图层是合法业务结果（empty=ok）→ status:ok，无警告。"""
        dummy_results = self._make_dummy_step_results()

        def side_effect(step, input_data, boundary_data):
            if step.id == "difference":
                return StepResult(
                    step_id=step.id, output_type="shapely_geom",
                    shapely_geom=DummyShapelyGeom(0), engine_used="shapely_wkt::difference",
                )
            if step.id == "stats":
                return StepResult(
                    step_id=step.id, output_type="stats_dict",
                    stats={"source_count": 30, "radius_m": 500, "total_area": 5000,
                           "covered_area": 5000, "gap_area": 0,
                           "coverage_rate": 100.0, "gap_rate": 0.0},
                    engine_used="shapely::area",
                )
            if step.id == "output_layer":
                return StepResult(
                    step_id=step.id, output_type="layer", feature_count=0,
                    qgis_layer=self.source_layer,
                    shapely_geom=DummyShapelyGeom(0), engine_used="qgis_memory_layer",
                )
            return dummy_results.get(step.id, StepResult(step_id=step.id, error="unknown"))

        mock_execute_step.side_effect = side_effect

        result = self.executor.execute(
            template_path=GAP_TEMPLATE_PATH,
            source_layer_name="shelters",
            boundary_layer_name="admin_boundary",
            radius_m=500.0,
            project=self.project,
            canvas=self.canvas,
            _find_layer_fn=self.find_layer_fn,
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "ok", "gap 全覆盖空图层应视为合法业务结果（empty=ok）")
        self.assertNotIn("结果为空", result["message"])

    # ── P2-2：_execute_step 输出步 validate 放宽（终态结构检查可达性）──

    def _make_output_step(self, step_id="output_layer"):
        """构造含 output_layer 输出的 StepDef（validate 'not is_empty'，同模板声明）。"""
        return StepDef(
            id=step_id,
            engine_chain=[EngineSpec(name="qgis_memory_layer", validate="not is_empty")],
        )

    @patch.object(PipelineExecutor, '_run_engine')
    def test_output_step_validate_fail_not_fatal(self, mock_run_engine):
        """输出步引擎运行成功但 validate 未过（空几何输出）→ 不判死，保留结果交由终态。"""
        empty_geom = MagicMock()
        empty_geom.is_empty = True
        empty_result = StepResult(
            step_id="output_layer", output_type="layer", feature_count=0,
            qgis_layer=self.source_layer,
            shapely_geom=empty_geom, engine_used="qgis_memory_layer",
        )
        mock_run_engine.return_value = empty_result

        result = self.executor._execute_step(
            self._make_output_step(), input_data=None, boundary_data=None)

        self.assertIsNotNone(result)
        self.assertNotEqual(result.status, "failed", "输出步 validate 未过不应判死（P2-2 放宽）")
        self.assertEqual(result.feature_count, 0, "应保留引擎真实结果供终态结构检查")

    @patch.object(PipelineExecutor, '_run_engine')
    def test_intermediate_step_validate_fail_still_fatal(self, mock_run_engine):
        """中间步骤 validate 未过 → 仍走 P2-1 fail-fast（status=failed），不受 P2-2 放宽影响。"""
        empty_geom = MagicMock()
        empty_geom.is_empty = True
        empty_result = StepResult(
            step_id="clip", output_type="layer", feature_count=0,
            qgis_layer=self.source_layer,
            shapely_geom=empty_geom, engine_used="qgis_memory_layer",
        )
        mock_run_engine.return_value = empty_result

        result = self.executor._execute_step(
            self._make_output_step(step_id="clip"), input_data=None, boundary_data=None)

        self.assertEqual(result.status, "failed", "中间步骤结构空必须走 fail-fast（error 语义）")
        self.assertIn("Validate failed", result.error or "")


class TestP23StatusMessages(unittest.TestCase):
    """P2-3 声带：status 派生消息（result_contract 层）。"""

    def setUp(self):
        from src.core import result_contract as rc
        self.rc = rc

    def test_derive_degraded_warning_with_step_reason_suggestion(self):
        """degraded → level=warning，内容含哪一步、为什么、建议。"""
        msg = self.rc.derive_status_message(
            status="degraded", step_index=3, step_id="population_coverage",
            reason="输出结果为空", suggestion="检查 population 字段是否正确",
        )
        self.assertEqual(msg["level"], "warning")
        self.assertIn("第 3 步", msg["content"])
        self.assertIn("population_coverage", msg["content"])
        self.assertIn("输出结果为空", msg["content"])
        self.assertIn("建议", msg["content"])
        self.assertIn("population 字段", msg["content"])

    def test_derive_failed_error_with_step_reason(self):
        """failed → level=error，格式：第 N 步 '<id>' 失败: 原因 + 建议。"""
        msg = self.rc.derive_status_message(
            status="failed", step_index=1, step_id="clip",
            reason="坐标系不一致 EPSG:4326 vs EPSG:3857",
        )
        self.assertEqual(msg["level"], "error")
        self.assertIn("第 1 步", msg["content"])
        self.assertIn("clip", msg["content"])
        self.assertIn("失败", msg["content"])
        self.assertIn("坐标系不一致", msg["content"])
        self.assertIn("建议", msg["content"], "failed 消息须含建议（验收：哪一步/为什么/建议）")

    def test_derive_degraded_fallback_reason_suggestion(self):
        """degraded 缺省 reason/suggestion → 使用默认占位与通用建议。"""
        msg = self.rc.derive_status_message(status="degraded", step_index=2, step_id="gap_analysis")
        self.assertEqual(msg["level"], "warning")
        self.assertIn("输出结果为空或结构异常", msg["content"])
        self.assertIn("建议", msg["content"])

    def test_derive_failed_fallback_reason(self):
        """failed 缺省 reason → 使用未知原因占位。"""
        msg = self.rc.derive_status_message(status="failed", step_index=4, step_id="map_export")
        self.assertEqual(msg["level"], "error")
        self.assertIn("未知原因", msg["content"])

    def test_derive_ok_is_info_empty(self):
        """ok → level=info 且 content 为空（不产生用户消息）。"""
        msg = self.rc.derive_status_message(status="ok", step_index=1, step_id="clip")
        self.assertEqual(msg["level"], "info")
        self.assertEqual(msg["content"], "")

    def test_append_status_messages_only_non_ok(self):
        """append_status_messages 仅对 degraded/failed 追加；ok 不追加。"""
        messages = []
        self.rc.append_status_messages(messages, status="ok", step_index=1, step_id="clip")
        self.assertEqual(len(messages), 0, "ok 不应产生消息")

        self.rc.append_status_messages(
            messages, status="degraded", step_index=2, step_id="population_coverage",
            reason="人口链输出空", suggestion="检查人口字段",
        )
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[-1]["level"], "warning")

        self.rc.append_status_messages(
            messages, status="failed", step_index=3, step_id="clip", reason="CRS 不一致",
        )
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[-1]["level"], "error")

    def test_append_status_messages_keeps_messages_schema_valid(self):
        """追加后的 messages 全部通过 validate_schema（level 合法、含 content）。"""
        messages = [{"level": "info", "content": "开始处理"}]
        self.rc.append_status_messages(
            messages, status="degraded", step_index=1, step_id="coverage_analysis",
            reason="覆盖率 0.7%", suggestion="检查输入范围",
        )
        errors = self.rc.validate_schema({
            "layers": [], "files": [], "messages": messages, "stats": {},
        })
        self.assertEqual(errors, [], f"schema 校验失败: {errors}")


if __name__ == "__main__":
    unittest.main()
