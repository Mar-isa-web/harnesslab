# HarnessLab

[![CI](https://github.com/Mar-isa-web/harnesslab/actions/workflows/ci.yml/badge.svg)](https://github.com/Mar-isa-web/harnesslab/actions/workflows/ci.yml)

> **一台仪器 + 一块仪表盘**：带全链路埋点的迷你 Agent 运行时，与它的配套观测面板。
> 在仪器上做可量化的改造实验（重试策略 / 上下文压缩 / 双模型 A / B）与故障注入，
> 用自研面板读出数据化结论——面板同样能分析任何 ZCode 兼容遥测库（含真实使用数据）。

## 两个子系统

| 子系统 | 目录 | 是什么 |
|---|---|---|
| **Agent 运行时（仪器）** | `harnesslab/` | 自研主循环 + Provider 抽象 + 工具系统 + 权限门 + SQLite 遥测四表 |
| **观测面板（仪表盘）** | `usagelab/` | 只读遥测管道 + 指标体系 + 模型画像 + 不满信号挖掘 + 周度训练验收仪表盘 |

为什么合在一起：运行时的遥测表结构**刻意对齐 ZCode CLI 生产库**——仪器产出的
实验数据，面板开箱即读（[docs/panel-demo.md](docs/panel-demo.md) 是活例子：
面板读出实验库里 deepseek-chat 23.4% 失败率，那正是注入的限流故障显形）。
反过来，面板分析真实 ZCode 使用库产出的结论（如 Flash 与 5.3 的失败率对比）
又为运行时的 A/B 实验提供方向。**测量 AI 的仪器 × 测量人机协作的仪表盘。**

## 快速开始

```bash
# —— 子系统 1：Agent 运行时 ——

# 离线演示（零配置，不需要 API key）
python cli.py --provider mock "演示任务" --mode yolo

# 端到端测试（主循环 / 权限拒绝 / 失败重试）
python tests/test_loop.py

# 接真实模型（GLM 开放平台 / DeepSeek）
set HL_API_KEY=你的key
python cli.py "列出 tasks 目录并总结里面的任务" --mode yolo

# 跑基准（10 项可判分任务）
python benchmark.py --self-test && python benchmark.py --provider openai --label baseline

# —— 子系统 2：观测面板 ——

# 分析你自己的 ZCode 使用数据（默认自动探测本地库，只读）
python usagelab/cli.py report          # 全量报告（md + HTML + 快照）
python usagelab/cli.py weekly          # 周度训练验收仪表盘（P90 < 3min 目标线）
python usagelab/cli.py diff            # 周间快照对比（🟢变好 / 🔴变差）

# 分析本项目的实验库（招牌闭环；bench.db 由上面 benchmark.py 的真实跑生成，不入库）
python usagelab/cli.py --db data/bench.db report

# 面板测试（46 项断言，合成库，不碰真实数据）
python usagelab/tests/test_all.py
```

## 目录结构

```
harnesslab/
├── cli.py / benchmark.py       # 运行时入口 + 基准 runner
├── harnesslab/                 # 子系统1：Agent 运行时
│   ├── agent.py                #   主循环（每步落库）
│   ├── provider.py             #   Provider 抽象：Mock / OpenAI 兼容
│   ├── context.py              #   上下文策略（朴素截断 / 滑动窗口）
│   ├── tools.py / permissions.py / retry.py / fault.py
│   └── telemetry.py            #   SQLite 四表（对齐 ZCode schema）
├── usagelab/                   # 子系统2：观测面板（原独立仓库，2026-10-24 并入）
│   ├── cli.py                  #   面板入口（report/weekly/models/signals/diff）
│   ├── usagelab/               #   load/metrics/models/sessions/signals/weekly/report
│   ├── tests/ + categories.json + docs/
├── tasks/                      # 基准任务集
├── analysis/ experiments/      # 回放 / 报告生成 / 故障探针
├── reports/ + docs/            # 实验报告 + 架构文档 + study_guide + panel-demo
└── tests/                      # 运行时测试
```

## 路线图

**运行时（仪器）**
- **M0 ✅ 主循环与遥测**：Agent 循环 + 四表全链路埋点，离线测试全绿
- **M1 ✅ 观测与基线**：10 项可判分基准（判分器自检）+ 复盘报告 + 事件流回放
- **M2 ✅ 策略改造与对照**：立即重试 vs 指数退避、朴素截断 vs 滑动窗口；
  注入连续限流下，指数退避使任务存活率 **0% → 100%**
- **M3 ✅ 故障注入与韧性**：种子可复现故障 + 按序号注入 Provider 故障；
  限流恢复 / 工具失败韧性 / 双模型行为对比三类实验完成

**观测面板（仪表盘，原 UsageLab 独立项目）**
- **P0 ✅ 数据管道与指标**：只读装载 + 全指标 + 46 项合成库测试
- **P1 ✅ 周度训练验收**：P90 < 3min 目标线 + 环比 + 信号趋势 + 快照存档
- **P2 ✅ 报告双格式**：markdown + 自包含 HTML（图表内嵌）
- **P3 ✅ 模型画像**：自采 9568 次请求——Flash 失败率 1.6% vs GLM-5.3 9.1%

**合并后（2026-10-24 起）**
- ✅ 面板直读运行时实验库（telemetry 补列 reasoning/cache/ttft/tool_error，
  老库自动迁移；无消息层的数据源信号章节自动降级）
- 待做：多轮均值实验（每配置 ≥3 轮）；交互式会话模式

架构与每个设计决策的"为什么"见 [docs/architecture.md](docs/architecture.md)
（运行时）与 [usagelab/docs/architecture.md](usagelab/docs/architecture.md)（面板）。
