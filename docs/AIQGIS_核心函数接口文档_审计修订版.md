# AIQGIS 核心函数接口文档（审计修订版）

日期：2026-06-16  
基准：以本地代码实现为准（`d:\桌面\AIQGIS_APP\src\`）

## 修订要点（用于对齐 Mavis / 接口标准）

### 1) `generate_output_path()`

- 真实输出根目录：`user_data/exports/shapefiles/`
- 参考实现：[output_persistence.py](file:///d:/%E6%A1%8C%E9%9D%A2/AIQGIS_APP/src/core/output_persistence.py#L13-L58)

### 2) `_safe_create_output()` 与 5 个水文降级函数的输出契约

- 当前 5 个水文降级函数均通过 `_safe_create_output()` 写 GeoTIFF，并统一返回 `actual_path`：
  - `safe_fill_sinks()`
  - `safe_d8_flow_direction()`
  - `safe_flow_accumulation()`
  - `safe_stream_network()`
  - `safe_basin()`
- 契约要求：调用方必须使用函数返回值作为“真实输出路径”（Windows 下文件被占用时可能自动改名）。
- 参考实现：[fallback_utils.py](file:///d:/%E6%A1%8C%E9%9D%A2/AIQGIS_APP/src/core/fallback_utils.py)

### 3) `sandbox_worker` 的降级路由返回值

- `processing.run()` 的降级路由会返回 `{OUTPUT: actual_output}`，其中 `actual_output` 来自 fallback 函数返回值（若为空则回退到传入的 `output_path`）。
- 参考实现：[sandbox_worker.py](file:///d:/%E6%A1%8C%E9%9D%A2/AIQGIS_APP/src/core/sandbox_worker.py#L381-L406)

### 4) `_resolve_paths()` 的空字符串行为

- `_resolve_paths()` 使用 `if not input_path:` / `if not output_path:` 判断缺失参数；因此空字符串 `""` 会被视为缺失并继续兜底匹配。
- 契约要求：调用方不要传入空字符串，缺省请传 `None` 或直接不传该 key。
- 参考实现：[fallback_utils.py:L58-L87](file:///d:/%E6%A1%8C%E9%9D%A2/AIQGIS_APP/src/core/fallback_utils.py#L58-L87)

