# QGIS-Agent v2.0.0 — AI 驱动的桌面 GIS 智能助手

> 说一句话，完成 GIS 分析。无需安装 QGIS，双击即用。

[![GitHub](https://img.shields.io/badge/GitHub-QGIS--Agent-blue)](https://github.com/kongchenxu792-cell/QGIS-Agent)

---



## ⚠️ 重要：GitHub 仓库不含 QGIS 引擎

本仓库仅托管源代码（约 2 MB）。QGIS 便携引擎（`qgis-portable/`，约 2.18 GB）因体积超过 GitHub 单文件限制被排除在版本库外。**直接 `git clone` 后双击 `启动.bat` 会报错**——缺的不是代码，是 QGIS 运行环境。

### 如何获得完整可运行版本

**方式一：从已有环境直接复制（最快）**

如果你已经在某台电脑上成功运行过 QGIS-Agent，直接把整个项目文件夹复制到目标电脑即可。`qgis-portable/` 是绿色版，不依赖注册表。

**方式二：手动组装（适合全新环境，约 15 分钟）**

1. `git clone https://github.com/kongchenxu792-cell/QGIS-Agent.git`
2. 下载 [QGIS 3.44 Portable](https://qgis.org/download/) 并解压
3. 将解压后的 QGIS 文件夹重命名为 `qgis-portable`，放在项目根目录
4. 目录结构应为：
   ```
   QGIS-Agent/
   ├── 启动.bat
   ├── src/
   └── qgis-portable/       ← 从这里放进去
       ├── bin/
       ├── apps/
       │   ├── qgis-ltr/
       │   ├── Qt5/
       │   └── Python312/
       └── share/
   ```
5. 双击 `启动.bat`，应能看到 QGIS 界面启动

**方式三：Release 打包版（推荐）**

在 [Releases](https://github.com/kongchenxu792-cell/QGIS-Agent/releases) 页面下载完整打包版本，解压即用。

### 路径注意事项

| 路径示例 | 是否可用 | 原因 |
|----------|---------|------|
| `D:\QGIS-Agent` | ✅ 可用 | 纯英文，无空格 |
| `C:\Users\用户名\桌面\QGIS-Agent` | ✅ 可用 | 中文用户名不影响 |
| `D:\新建文件夹 (17)\QGIS-Agent` | ❌ 不可用 | 中文路径 + 括号 → PROJ/GDAL 初始化失败 |
| `D:\My Projects\QGIS-Agent` | ❌ 不可用 | 空格路径 → Python DLL 加载可能失败 |

**如果出现 `ImportError: DLL load failed`**，说明路径有问题或有多个 Python 版本冲突。检查：
- 项目路径是否含中文或空格
- 本机是否同时安装了系统版 QGIS 或独立 Python（会与便携版冲突）
- 是否缺少 [VC++ Redistributable 2015-2022](https://aka.ms/vs/17/release/vc_redist.x64.exe)
## 快速开始

### 5 分钟上手

1. **下载并解压** 项目文件夹到任意位置（建议纯英文路径）
2. **双击 `启动.bat`**，等待 QGIS 界面加载
3. 在底部对话框中输入指令，回车执行

**无需安装 QGIS**——QGIS 3.44 便携版已随包封装（约 2.18 GB）。

### 在线模式（推荐首次体验）

1. 首次启动会弹出 API Key 输入窗口
2. 填入你的阿里云百炼 API Key（免费申请：https://bailian.aliyun.com）
3. 保存后即可使用

### 离线模式（数据完全本地）

1. 安装 [Ollama](https://ollama.com/download/windows)
2. 拉取模型：`ollama pull qwen2.5:7b`
3. 在 QGIS-Agent 顶部切换到「离线模式」即可

> 离线模式无需联网，无需 API Key，所有数据不出本机。

---

## 核心能力

### 日常 GIS 操作（22 条自然语言指令）

支持中文/日文/英文三语输入，覆盖图层管理、样式设置、要素编辑、属性导出、缓冲区分析等。

| 类别 | 示例指令 |
|------|---------|
| 图层管理 | "加载 D:\data\points.shp"、"移除学校图层"、"列出所有图层" |
| 样式设置 | "按面积字段分级渲染"、"加载 style.qml"、"显示标注" |
| 数据分析 | "导出属性表为 CSV"、"统计面积字段"、"创建 500 米缓冲区" |
| 地图操作 | "放大"、"缩放到学校图层"、"导出地图为 PNG" |
| 要素操作 | "选择面积大于 100 的要素"、"点击查询属性"、"过滤城市字段为北京" |

### 地震灾害自动化分析（5 条专业链路）

这是 QGIS-Agent 区别于通用 GIS 工具的核心能力——将日本内阁府防灾标准业务流自动化。

| 分析链路 | 输入 | 输出 | 应用场景 |
|---------|------|------|---------|
| **空间关联** | 震度分布图 + POI 设施 | 受影响设施清单 | 震后初动——识别影响范围内的关键设施 |
| **覆盖分析** | 避难所点位 + 行政区边界 | 覆盖率 % + 盲区地图 | 验证避难所配置是否满足防灾计划要求 |
| **盲区分析** | 覆盖分析结果 | 未覆盖区域地图 | 定位避难所服务空白区域 |
| **人口覆盖** | 覆盖区域 + 人口分布 | 人口覆盖 %（面积 vs 人口） | 证伪"面积可替代人口"——发现 32.7% 面积覆盖 vs 56.0% 人口覆盖 |
| **建筑风险** | 震度分布 + 人口 + 建筑数据 | 受灾暴露人口 + 建筑风险指数 | 震度 × 人口叠置估算受灾人口规模 |

**政策依据**：日本内阁府令和 8 年 6 月 12 日阁议决定《首都直下地震紧急对策推进基本计划》规定——都心南部直下地震想定死者最大约 1.8 万人、建筑全坏烧失约 40 万栋，今后 10 年间半减为减灾目标。本工具为该计划要求的「膨大な人的・物的被害への対応強化」提供自动化基础分析。

### 架构特色

```
用户输入（自然语言）
    │
    ▼
AI 意图识别（在线 Qwen-Plus / 离线 Qwen2.5 7B）
    │
    ▼
{action, params} JSON ───→ InstructionMapper.match_and_execute()
    │
    ▼
Guard 检查（CRS / 图层类型 / 参数有效性）
    │
    ▼
Pipeline 确定性执行（8 步引擎 + Shapely fallback 全链路）
    │
    ▼
结果输出（覆盖率 / 图层 / CSV / 可追溯日志）
```

- **在线和离线走同一条管道**——AI 只负责理解意图和填参数，不碰实际计算
- **CRS 自动检测和修正**——地理坐标系（度）下自动拦截，避免缓冲区算出错误结果
- **Shapely 全链路 fallback**——QGIS 大坐标处理异常时自动降级到 Shapely 引擎
- **Pipeline 中途失败自动回滚**——不会留下半成品中间图层

---

## 项目结构

```
QGIS-Agent/
├── 启动.bat                    # 一键启动脚本
├── 启动_静默.py                # 无控制台窗口启动（快捷方式用）
├── qgis-portable/              # QGIS 3.44.9 便携版（~2.18 GB）
├── src/
│   ├── main.py                 # 程序入口
│   ├── __init__.py             # 版本号（v2.0.0）
│   ├── core/
│   │   ├── ai_worker.py        # AI 推理调度（在线/离线双模式 + exec 已被移除）
│   │   ├── instruction_mapper.py # 指令映射 + 参数校验 + 图层名修正
│   │   ├── pipeline_executor.py  # Pipeline 确定性执行引擎（6 引擎 + fallback）
│   │   ├── guards.py           # Guard 注册表（CRS/图层类型/参数有效性 9 个守卫）
│   │   ├── handlers_analysis.py  # 分析 Handler（覆盖/盲区/人口/建筑风险）
│   │   ├── handlers_basic.py   # 基础 Handler（加载/导出/缓冲/空间连接等 20 个）
│   │   ├── handlers_seismic.py # 地震 Handler（震度态势图）
│   │   ├── template_registry.py  # Template 注册表 + 图层自动识别
│   │   ├── templates/          # 4 个 JSON Pipeline 模板
│   │   │   ├── coverage_analysis.json
│   │   │   ├── gap_analysis.json
│   │   │   ├── population_coverage.json
│   │   │   └── building_risk.json
│   │   ├── sandbox_worker.py   # 沙箱执行引擎（已封存，保留备审计）
│   │   ├── fallback_utils.py   # Shapely 全链路 fallback（大坐标兜底）
│   │   ├── qgis_env.py         # QGIS 便携环境引导
│   │   ├── config_manager.py   # 配置持久化（aiqgis_config.json）
│   │   ├── ai_config.py        # AI 端点配置
│   │   ├── local_llm.py        # Ollama 推理客户端
│   │   ├── memory_bridge.py    # 对话记忆桥接（mem0 集成）
│   │   └── ...
│   ├── skills/                 # Skill 系统（16 个独立技能模块）
│   ├── prompt_agent/           # 提示词调试工具
│   ├── i18n/                   # 中/日/英三语（各 219 键）
│   └── ui/                     # PyQt5 界面层
├── tests/                      # 88 个单元测试
└── docs/                       # 文档和变更日志
```

---

## 技术栈

| 层级 | 技术 |
|------|------|
| GUI | PyQt5 |
| GIS 引擎 | QGIS 3.44 (PyQGIS) |
| 几何计算 | Shapely 2.x（全链路 fallback） |
| 在线 AI | 阿里云 DashScope Qwen-Plus |
| 离线 AI | Ollama + Qwen2.5:7B |
| 指令解析 | JSON 模板匹配 + keyword 纠偏 + 三层容错 |
| 国际化 | 中文 / 日文 / English（219 键完全对齐） |
| 测试 | pytest（88 用例，0.45s 全量通过） |

---

## 测试

```batch
# 一键运行全部测试（需要 portable QGIS 环境）
tests\run_tests.bat -v

# 预期输出：88 passed in 0.45s
```

---

## 注意事项

- **仅支持 Windows 10/11 64-bit**
- **不建议放在中文路径下**（可能导致 PROJ/GDAL 初始化失败）
- 离线模式需 8 GB 以上内存/显存
- 在线模式首次启动会提示配置 API Key

---

## 许可证

本项目采用 [MIT License](https://opensource.org/licenses/MIT)。

致谢：[QGIS](https://qgis.org/) · [Ollama](https://ollama.com/) · [Qwen](https://github.com/QwenLM/Qwen) · [阿里云百炼](https://bailian.aliyun.com/) · [Shapely](https://shapely.readthedocs.io/)

---

## 联系方式

- **作者**：kongchenxu792
- **邮箱**：kongchenxu792@gmail.com
- **仓库**：[github.com/kongchenxu792-cell/QGIS-Agent](https://github.com/kongchenxu792-cell/QGIS-Agent)


