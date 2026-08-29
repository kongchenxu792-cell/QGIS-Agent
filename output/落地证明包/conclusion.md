---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: a024fff57185fad8e983aecfc0decd09_1471d65ba3c011f1bc17525400826444
    ReservedCode1: BNjygMWqWmpJp7mcZaH8+0FnnuqxqVXpu00n6uICtidJA+zoztct4pvJMMJZX+8sMmdkxQEz0zm+NRDWC40eXDjDfF0N+VQ1JFAuGGzaRXh8aqY0RczHQj/Ml1Ic/pefEI+1UViLK+3Za3EzUKSmAOfMsJ5E+02/ujQStcxybcEGlVYFY8Oco5qz+hM=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: a024fff57185fad8e983aecfc0decd09_1471d65ba3c011f1bc17525400826444
    ReservedCode2: BNjygMWqWmpJp7mcZaH8+0FnnuqxqVXpu00n6uICtidJA+zoztct4pvJMMJZX+8sMmdkxQEz0zm+NRDWC40eXDjDfF0N+VQ1JFAuGGzaRXh8aqY0RczHQj/Ml1Ic/pefEI+1UViLK+3Za3EzUKSmAOfMsJ5E+02/ujQStcxybcEGlVYFY8Oco5qz+hM=
---

# 可运行结论（供对外展示）

本项目已具备可运行、可落地的完整产品链路：用户在自然语言输入框输入中文指令后，本地离线大模型（qwen3.5-4b，Ollama）将指令识别为结构化 action/params，引擎（PipelineExecutor + QGIS 空间分析链）执行真实空间计算，产出覆盖范围图层、盲区图层、盲区地图 PNG 及人口覆盖率统计等可视化与数据结果。

本次落地证明基于合成数据（temp/synth_handcalc/，行政区/避难所/人口三图层，EPSG:3857）执行 4 条自然语言指令：缓冲区创建、覆盖范围分析、盲区分析、人口覆盖率分析，成功 4/4 条。关键指标与 L1550 手算基准 20 边形口径期望一致（覆盖率/盲区率/人口覆盖率 0.7725% / 99.2275% / 0.7725%，容差 ±0.01%），判定 全部 PASS。

本证明全程不依赖 GUI 司机与人工窗口操作，纯脚本端到端可复现（命令见 run_log.txt 头部）。
*（内容由AI生成，仅供参考）*
