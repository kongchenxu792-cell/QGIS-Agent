---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: a024fff57185fad8e983aecfc0decd09_ec4218d5a46811f1bc17525400826444
    ReservedCode1: yuzj+Mg9B+I+393B8OM+EvrLdUwXBpT+PkdN+0RYOs0zxDEX3z/Z1d3TsUjifOCP0mdjBeD28CKcKkX366xn0IQs1RHcC4qI5WKR5PIJZvtdmMzeIlIfpGYemPoyO7uuhQsCTUmRV3Gx5XcJUp/FqJJDxvJfoMVKyIeGiN3FWH42TTXUkcUHqHh/zm4=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: a024fff57185fad8e983aecfc0decd09_ec4218d5a46811f1bc17525400826444
    ReservedCode2: yuzj+Mg9B+I+393B8OM+EvrLdUwXBpT+PkdN+0RYOs0zxDEX3z/Z1d3TsUjifOCP0mdjBeD28CKcKkX366xn0IQs1RHcC4qI5WKR5PIJZvtdmMzeIlIfpGYemPoyO7uuhQsCTUmRV3Gx5XcJUp/FqJJDxvJfoMVKyIeGiN3FWH42TTXUkcUHqHh/zm4=
---

# 中国成都数据正式跑通结果表（Solo 批复）

- 数据：`D:\桌面\项目测试数据\中国\成都`（避难所_3857 215 点版 / 行政区 / 人口格网 19365，EPSG:3857）
- 半径：500.0m；引擎：PipelineExecutor（risk_zone_coverage / gap_analysis / population_coverage 模板）
- 注册表：新增 `chengdu` 条目（country=CN，data_dir 条目级覆盖；现有 4 灾种零改动）

## 三链结果

| 链路 | 指标 | 详情 | 状态 |
|---|---|---|---|
| coverage | 覆盖率 0.47396577851106203% | covered=92163042.58373527 / total=19445083751.248985 m², src=202 | 成功 |
| gap | 盲区率 99.52603422148894% | gap=19352920708.66525 / total=19445083751.248985 m², 覆盖率=0.47396577851106203% | 成功 |
| population_coverage | 人口覆盖率 2.7356752876237045% | 覆盖人口 511563.96377764136 / 总人口 18699732.60686221 | 成功 |

## 新旧对比（39 点参考版 → 215 点版）

| 指标 | 39 点参考版 | 215 点版 | 变化 |
|---|---|---|---|
| 覆盖率 | 0.148% | 0.474% | ↑ 3.2× |
| 盲区率 | 99.85% | 99.526% | ↓ 0.324pp |
| 人口覆盖率 | 2.53% | 2.736% | ↑ 0.206pp |

> 39 点参考版数值来源：README 当前产品叙事（0.148%/99.85%/2.53%）；215 点版为本轮重跑实测。
> 注：避难所_3857 共 215 点，其中 183 点位于成都行政区内（QGIS contains 核验），32 点在边界外；引擎 source_count=202 为其自身过滤口径，不影响三链结果正确性（抽检面积偏差 0.0000%）。

## 抽检

- 独立复算覆盖面积=92163043 m² vs 引擎=92163043 m²，相对偏差=0.0000%（容差 ±1%），判定=PASS

## 一致性校验

- gap_rate=99.5260% vs 100-coverage_rate=99.5260%，偏差=0.0000%（容差 ±1%）

## 报告

- 报告路径：`D:\桌面\QGIS-Agent\user_data\reports\risk_report_20260830_194918_chengdu.md`
- 覆盖率：0.47396577851106203%
- 预警：「成都」危险区覆盖率仅为 0.47%，低于预警阈值 50.00%，建议核查避难所布点。

*（内容由AI生成，仅供参考）*
