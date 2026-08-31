# 洪涝淹没区数据说明（Solo 批复）

- 数据源：`D:\桌面\项目测试数据\中国\成都\河流_3857.gpkg`（成都河流水系，5122 条，EPSG:3857）
- 生成方式：河流 buffer 300.0m（Shapely，quad_segs=5，几何 valid 断言）→ unary_union
- 输出：`D:\桌面\QGIS-Agent\output\成都洪涝\淹没区_3857.gpkg`（source=河流buffer近似）
- 用途：作为洪涝危险区图层（flood risk zone），评估避难所 500m 缓冲对淹没区覆盖率
- 注意：该淹没区为近似示意，非水文模型结果，仅用于覆盖分析演示
