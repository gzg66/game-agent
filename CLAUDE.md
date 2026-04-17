# CLAUDE.md

此文件为 Claude Code (claude.ai/code) 在处理本仓库代码时提供指导。

## 项目概览

基于 AI 驱动的手游自动化测试系统。Agent 通过 Poco UI 自动化工具（Airtest + PocoUI）自主进行游戏，构建游戏状态的世界模型，检测异常情况（崩溃、卡死、数值错误、资源缺失、日志错误），并生成结构化的测试报告。

**语言：** Python 3.12 (通过 `uv` 管理)。虚拟环境位于 `.venv/` (提示符：`ai-npc-validator`)。

## 环境搭建

```bash
# 激活虚拟环境
source .venv/Scripts/activate   # Linux/Mac 或 Windows git bash
# 或在 Windows cmd 中执行：.venv\Scripts\activate

# 核心依赖 (已安装在 .venv 中):
# airtest, pocoui, jinja2, pyyaml (可选，用于 YAML 配置文件)
```

`LLM_API_KEY` 环境变量从 `.env` 文件加载（已加入 gitignore）。

## 运行方式

```bash
# 使用项目配置运行完整流程 (需要连接设备 + 集成了 Poco SDK 的游戏):
python examples/poco_xingtu_tiancheng_mumu_runner.py --profile examples/xingtu_tiancheng_poco_profile.yaml

# 仅预览模式 (验证 Poco UI 树连接性，不运行探索逻辑):
python examples/poco_xingtu_tiancheng_mumu_runner.py --preview-only

# 通用 DFS 运行器 (通过 UnityPoco 测试 Unity3D 游戏):
python examples/poco_generic_game_runner.py --mode discover --device-uri Android:///emulator-5554

# 两阶段套件 (先探索，后定时结束并回放):
python examples/poco_generic_run_suite.py
```

输出结果保存在 `outputs/` 目录，包含 `result.json`、`summary.html`、`ui_graph.mmd` 以及 JSONL 格式的执行追踪记录。

## 系统架构

### 核心库：`poco_ui_automation/`

* **models.py** — 所有数据结构（使用 `slots=True` 的 dataclasses）：`UiNode`（UI节点）、`PageSnapshot`（页面快照）、`ActionCandidate`（待选动作）、`ExecutedAction`（已执行动作）、`StateNode`/`StateEdge`（世界模型图节点/边）、`IssueRecord`（问题记录）、`RunSummary`（运行摘要）、`CoverageStats`（覆盖率统计）、`PerformanceSample`（性能样本）。
* **framework.py** — `AutomationSession` 编排核心 BFS 爬取循环 (`crawl_ui_graph`)。定义了 `DriverProtocol`（驱动程序必须实现的接口：`freeze_nodes`, `click`, `back`, `get_text`, `get_attr`）。通过对可点击名称和关键文本进行 SHA-1 哈希来构建页面签名。
* **ai_strategy.py** — `HybridPlanner` 使用基于规则的启发式评分对 UI 节点进行排序（中英文关键词加分、危险操作减分、未见转移的探索加分）。`StateGraphMemory` 维护已访问状态和转移的有向图。`RuleScenario` 允许调用者增强或屏蔽特定关键词。
* **drivers.py** — `AirtestPocoDriver` 通过 Airtest 连接真实设备，并根据 `EngineType`（Unity3D 端口 5001, Cocos-JS 端口 5003, Cocos2dx-Lua 端口 15004, Android 原生 UIAutomation 备选）分发到正确的 Poco 驱动。`MockDriver` 模拟状态图用于本地测试。
* **cache.py** — `UiStateCache` 缓存带有 TTL（默认 3 秒）的 `PageSnapshot`，并跟踪选择器的成功/失败统计。
* **integration.py** — `ProjectProfile` 从 JSON/YAML 加载游戏配置。`EngineType` 枚举包括：`unity3d`, `cocos_creator`, `cocos2dx_js`, `cocos2dx_lua`。`IntegrationRegistry` 保存每个引擎的 SDK 集成指南。
* **metrics.py** — `AndroidFrameParsers` 解析 `gfxinfo` 和 SurfaceFlinger 延迟输出，计算帧耗时百分位数。`MetricSampler` 收集业务和性能指标。
* **reporting.py** — `ReportBuilder` 产生三种输出：JSON 数据、HTML 报告（基于 Jinja2 的深色主题中文模板）和状态图的 Mermaid 流程图。

### 示例目录：`examples/`

* **poco_generic_game_runner.py** — 针对 Unity3D 游戏的独立 DFS UI 图探索器。具有两种模式：`discover`（从零构建 UI 地图）和 `replay`（从保存的 `map.json` 重新执行）。处理 Poco 重连、返回导航、路径恢复和崩溃检测。直接通过 Airtest 内置的 adb 使用 ADB 命令。
* **poco_generic_run_suite.py** — 两阶段包装器：先运行探索，然后带 `--kill-after-seconds` 参数运行回放，以测试崩溃恢复。
* **poco_xingtu_tiancheng_mumu_runner.py** — 使用 `poco_ui_automation` 库的功能完备运行器。包含多模块巡航循环及运行时防护（轮次限制、巡航定时器、停滞检测）、问题检测（数值异常、资源缺失、logcat 错误过滤）、聚合报告和崩溃重启逻辑。

### 决策流程

1.  `AutomationSession.crawl_ui_graph` 对游戏页面进行 BFS 遍历。
2.  在每个页面：执行 `driver.freeze_nodes()` → 构建带有 SHA-1 签名的 `PageSnapshot`。
3.  `HybridPlanner.plan()` 对所有可见/启用/可点击的节点进行评分，生成排序后的 `ActionCandidate`。
4.  通过 `driver.click()` 执行评分最高的候选动作，并在 `StateGraphMemory` 中记录状态转移。
5.  遍历结束后：聚合覆盖率，检测问题，生成报告。

### 项目配置文件 (Project Profiles)

游戏特定的配置存储在 JSON 或 YAML 中（参见 `examples/*.yaml`）。关键字段包括：`engine_type`、`package_name`、`page_signatures`（关键词到页面的映射）、`module_scenarios`（每个模块的目标/关键词）、`dangerous_actions`（危险操作）、`critical_pages`（关键页面）、`runtime_guard`（运行时防护）、`issue_detection`（问题检测）。

## 设计文档

* `AI手游自主游玩测试整体技术方案.md` — 主设计文档：认知优先（而非遍历优先）、世界模型持续更新、先候选后决策模式、决策循环中的异常检测。
* `Agent单步决策协议.md` — Agent 单步决策协议。
* `世界模型数据结构设计.md` — 世界模型数据结构设计。
* `冷启动探索流程设计.md` — 冷启动探索流程设计。
* `异常检测器设计.md` — 异常检测器设计。

## 关键约定

* `models.py` 中所有数据模型均使用 `@dataclass(slots=True)`。
* UI 字符串和注释使用 **中文**；代码标识符使用 **英文**。
* 页面唯一性由排序后的可点击节点名称 + 关键文本的 SHA-1 哈希决定。
* **注意：** `HybridPlanner` 目前使用基于关键词的启发式评分，而非 LLM 调用。设计文档中描述的 LLM 集成是目标架构，目前代码中尚未完全实现。
* 危险操作（支付、注销账号）在规划器中会被扣除 100 分。
* 驱动程序必须实现 `DriverProtocol`；离线测试请使用 `MockDriver`。