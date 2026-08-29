---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: a024fff57185fad8e983aecfc0decd09_119057eba3c011f1abe1525400e6dd8f
    ReservedCode1: jcvbRvnuPYS0UWW1nan4QF3LKoQjBXYzMl68D2gvrwwk8Sgk1so0nejuWPvMaRdHUGBlis8G9IUbbi3GRY5w4FNdfC12Y+Cwt0g++bFxWVgmXhtl3eh2+DEGAafXdqOR0Ry1serCHXO+VEkZrl9Fu5KYquoFfHM+V7DvYMszB/Mk498GOICWd1jUDO4=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: a024fff57185fad8e983aecfc0decd09_119057eba3c011f1abe1525400e6dd8f
    ReservedCode2: jcvbRvnuPYS0UWW1nan4QF3LKoQjBXYzMl68D2gvrwwk8Sgk1so0nejuWPvMaRdHUGBlis8G9IUbbi3GRY5w4FNdfC12Y+Cwt0g++bFxWVgmXhtl3eh2+DEGAafXdqOR0Ry1serCHXO+VEkZrl9Fu5KYquoFfHM+V7DvYMszB/Mk498GOICWd1jUDO4=
---

# 4 链结果表（真实 LLM 识别 → 引擎执行）

- 指令总数：4，成功：4，失败：0
- 合成数据：`D:\桌面\QGIS-Agent\temp\synth_handcalc`
- 期望口径：L1599 裁决 20 边形近似（SEGMENTS=5 → n=20）

| # | 指令 | LLM识别action | LLM参数 | 最终action | 状态 | 关键stats |
|---|------|--------------|---------|-----------|------|-----------|
| 1 | 对避难所创建 500 米缓冲区 | create_buffer | {"layer_name": "避难所", "distance": 500.0, "selected_only": false} | create_buffer | 成功 | {} |
| 2 | 计算避难所的覆盖范围，边界用东京行政区 | coverage_analysis | {"source_layer": "避难所", "boundary_layer": "东京行政区", "radius_m": 500.0} | coverage_analysis | 成功 | coverage_rate=0.7725% |
| 3 | 分析避难所500米服务范围的盲区，边界用东京行政区 | gap_analysis | {"source_layer": "避难所", "boundary_layer": "东京行政区", "radius_m": 500.0} | gap_analysis | 成功 | gap_rate=99.2275% coverage_rate=0.7725% |
| 4 | 计算避难所500米范围内的人口覆盖率，边界用东京行政区，人口字段用population | population_coverage | {"source_layer": "避难所", "boundary_layer": "东京行政区", "population_layer": "人口", "population_field": "population", "radius_m": 500.0} | population_coverage | 成功 | pop_coverage_rate=0.7725% covered=309.01699437486945/total=40000.0 |
*（内容由AI生成，仅供参考）*
