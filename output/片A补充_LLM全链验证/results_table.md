---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: a024fff57185fad8e983aecfc0decd09_c4d83c90a43011f1abe1525400e6dd8f
    ReservedCode1: EETXCgMKXXfgDs9Wgz4liOB7bJTaqx7pWFDEvNWtJOsnTxksG3jSMl8o7PrNiV1FAZECD++4IOXgiRyjJswRXO1QWiz8kfETDpDAfUnFPG3Q0+DtgJyx9Dn4Y+9rcLaqOz0/npBAsGUR3ZDtq8GGCHMTVQVvXPuk7ApU/iK/QSN2kAgrFGw9aV5BhoU=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: a024fff57185fad8e983aecfc0decd09_c4d83c90a43011f1abe1525400e6dd8f
    ReservedCode2: EETXCgMKXXfgDs9Wgz4liOB7bJTaqx7pWFDEvNWtJOsnTxksG3jSMl8o7PrNiV1FAZECD++4IOXgiRyjJswRXO1QWiz8kfETDpDAfUnFPG3Q0+DtgJyx9Dn4Y+9rcLaqOz0/npBAsGUR3ZDtq8GGCHMTVQVvXPuk7ApU/iK/QSN2kAgrFGw9aV5BhoU=
---

# LLM+引擎 4 灾种结果表（Solo 片A补充批复）

- 指令总数：4，PASS：4，FAIL：0
- 数据：`D:\桌面\QGIS-Agent\temp\multi_disaster`（行政区/避难所/震度分布/淹没区/滑坡风险区/火灾风险区，EPSG:3857）
- 链路：自然语言指令 → qwen3.5-4b（Ollama 离线）→ InstructionMapper（关键词纠偏+图层自动检测）→ PipelineExecutor 引擎
- 期望口径：引擎直连基准（temp/multi_disaster/run_records.json），容差 ±0.01%

| # | 灾种 | 指令 | LLM识别action | LLM参数 | 最终action | 状态 | 覆盖率实际(%) | 期望(%) | 差值(%) | 判定 |
|---|------|------|--------------|---------|-----------|------|--------------|---------|---------|------|
| 1 | 地震 | 计算避难所对震度分布区域的覆盖率 | coverage_analysis | {"source_layer": "避难所", "boundary_layer": "震度分布区域", "radius_m": 500.0} | coverage_analysis | 成功 | 38.426969 | 38.426968963612445 | 0.0 | PASS |
| 2 | 洪涝 | 计算避难所对淹没区域的覆盖率 | coverage_analysis | {"source_layer": "避难所", "boundary_layer": "淹没区域", "radius_m": 500.0} | coverage_analysis | 成功 | 100.0 | 100.00000000000013 | 0.0 | PASS |
| 3 | 滑坡 | 计算避难所对滑坡风险区的覆盖率 | coverage_analysis | {"source_layer": "避难所", "boundary_layer": "滑坡风险区", "radius_m": 500.0} | coverage_analysis | 成功 | 17.078653 | 17.078652872713416 | 0.0 | PASS |
| 4 | 火灾 | 计算避难所对火灾风险区的覆盖率 | coverage_analysis | {"source_layer": "避难所", "boundary_layer": "火灾风险区", "radius_m": 500.0} | coverage_analysis | 成功 | 68.314611 | 68.31461149086756 | 0.0 | PASS |
*（内容由AI生成，仅供参考）*
