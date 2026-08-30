---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: a024fff57185fad8e983aecfc0decd09_fa41cb54a45311f1bc17525400826444
    ReservedCode1: nCWajieDI6IhLHq3u8M/1JCeUNs8yJmmVwrFxnhkmgBhCgJ5pddQ+T1Z6LGQSYPuasxYfyiZcqAroXfGP6b2jUpZodWpQ93bg0lz2f6s8X9CB7dNaIkmxBcX9+y7Vk5/jT+tn6j/fUm/xhlckU/7JvNNII3dWuiFDzE0mju9Uvk9zdEozMYH2vz0v8I=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: a024fff57185fad8e983aecfc0decd09_fa41cb54a45311f1bc17525400826444
    ReservedCode2: nCWajieDI6IhLHq3u8M/1JCeUNs8yJmmVwrFxnhkmgBhCgJ5pddQ+T1Z6LGQSYPuasxYfyiZcqAroXfGP6b2jUpZodWpQ93bg0lz2f6s8X9CB7dNaIkmxBcX9+y7Vk5/jT+tn6j/fUm/xhlckU/7JvNNII3dWuiFDzE0mju9Uvk9zdEozMYH2vz0v8I=
---

# 中国成都数据正式跑通结果表（Solo 批复）

- 数据：`D:\桌面\项目测试数据\中国\成都`（参考版避难所 39 点 / 行政区 / 人口格网 19365，EPSG:3857）
- 半径：500.0m；引擎：PipelineExecutor（risk_zone_coverage / gap_analysis / population_coverage 模板）
- 注册表：新增 `chengdu` 条目（country=CN，data_dir 条目级覆盖；现有 4 灾种零改动）

## 三链结果

| 链路 | 指标 | 详情 | 状态 |
|---|---|---|---|
| coverage | 覆盖率 0.14813964334691312% | covered=28805877.717608806 / total=19445083751.248985 m², src=39 | 成功 |
| gap | 盲区率 99.85186035665309% | gap=19416277873.531376 / total=19445083751.248985 m², 覆盖率=0.14813964334691312% | 成功 |
| population_coverage | 人口覆盖率 2.5298336820896963% | 覆盖人口 473072.1339491098 / 总人口 18699732.60686221 | 成功 |

## 抽检

- 独立复算覆盖面积=28805878 m² vs 引擎=28805878 m²，相对偏差=0.0000%（容差 ±1%），判定=PASS

## 一致性校验

- gap_rate=99.8519% vs 100-coverage_rate=99.8519%，偏差=0.0000%（容差 ±1%）

## 报告

- 报告路径：`D:\桌面\QGIS-Agent\user_data\reports\risk_report_20260830_171938_chengdu.md`
- 覆盖率：0.14813964334691312%
- 预警：「成都」危险区覆盖率仅为 0.15%，低于预警阈值 50.00%，建议核查避难所布点。

*（内容由AI生成，仅供参考）*
