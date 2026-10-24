# UsageLab

[![CI](https://github.com/Mar-isa-web/usagelab/actions/workflows/ci.yml/badge.svg)](https://github.com/Mar-isa-web/usagelab/actions/workflows/ci.yml)

> AI 协作使用观测系统：把「我怎么用 AI」变成可度量的指标。
> 数据源是 ZCode CLI 的本地遥测库（**只读**），零 API 成本，全程离线。

一次命令，产出：等待分位 / 模型画像（限流率·TTFT·耗时）/ 工具画像 /
不满信号挖掘 / 会话卫生 / **周度训练验收仪表盘**（P90 < 3 分钟目标线），
渲染成 markdown + 自包含 HTML（图表内嵌），并存指标快照供周间对比。

```
# 全量报告（md + html + 图表 + 快照）
python cli.py report

# 只看训练验收仪表盘
python cli.py weekly

# 只看模型画像 / 不满信号
python cli.py models
python cli.py signals

# 限定时间窗（例如复盘启动之后）
python cli.py report --since 2026-10-12
```

测试（合成库，数字全部已知可硬断言，不碰真实数据）：

```
python tests/test_all.py   # 40 项全绿
```

## 它回答什么问题

| 问题 | 看哪一节 | 本机真实读数（2026-10-24） |
|---|---|---|
| 我每周等 AI 等多久？训练达标了吗？ | 周度仪表盘 | P90 连续 5 周 10-13 分钟，0/2 周达标 |
| 模型该用哪个（Flash 还是 5.3）？ | 模型画像 | Flash 9267 次请求失败率 1.6%；5.3 330 次失败率 9.1%（约 6 倍） |
| 为什么这么慢？ | 上下文×耗时 | 单轮输入 >2M 的轮次中位 10.8 分钟，<200k 的只要 66 秒（10 倍差） |
| 会话卫生做得怎么样？ | 会话卫生 | 75.3% 的轮次发生在曾超 1M token 的巨型会话里 |
| 我的不满在变少吗？ | 不满信号 | 周信号数 71 → 25（-65%），取消率 12% → 4-8% |

## 目录结构

```
usagelab/
├── cli.py                  # 命令行入口（report/weekly/models/signals）
├── categories.json         # 项目归类词典（可自行编辑，改完即生效）
├── usagelab/
│   ├── load.py             # 只读连接 + 时间窗 + 周分桶（本地时区）
│   ├── metrics.py          # 总览/分位/工具画像/上下文×耗时
│   ├── models.py           # 模型画像（失败率/限流/TTFT/周迁移轨迹）
│   ├── sessions.py         # 项目归类 + 会话卫生（巨型会话占比）
│   ├── signals.py          # 不满信号挖掘（加权情绪分）+ 连发检测
│   ├── weekly.py           # 周度聚合 + 训练验收仪表盘
│   ├── report.py           # 采集 -> markdown + 指标快照
│   └── report_html.py      # 自包含 HTML（图表 base64 内嵌）
├── tests/
│   ├── build_fixture.py    # 合成遥测库构造器（schema 与真库同构）
│   └── test_all.py         # 40 项断言测试
├── reports/                # 报告输出（gitignore）
└── history/                # 每次运行的指标快照（gitignore）
```

## 路线图

- **M0 ✅ 数据管道与指标**：只读装载 + 全指标 + 40 项合成库测试
- **M1 ✅ 周度训练验收**：P90<3min 目标线 + 环比 + 信号趋势 + 快照存档
- **M2 ✅ 报告双格式**：markdown + 自包含 HTML（matplotlib 图表可选，缺失自动降级）
- **M3 ✅ 模型画像**：model_usage 逐请求统计——用自己的数据回答 Flash vs 5.3
- **后续（按需）**：快照间自动 diff 命令；信号词典外置；新 ZCode schema 兼容层

## 设计原则

1. **只读戒律**：观测工具永远以 `mode=ro` 打开生产库，测试用合成库；
2. **零 API 成本**：纯本地分析，核心功能纯标准库（matplotlib 仅图表可选）；
3. **口径一致性**：分位数沿用 `int(n·p)` 最近邻口径，与历史复盘脚本可比；
4. **采集与渲染分离**：`build_report()` 只产 dict，md/html 各自消费。

架构与每个设计决策的"为什么"见 [docs/architecture.md](docs/architecture.md)。

## 背景

这是作者的第二条项目线：HarnessLab 测量「AI 干活的行为」，UsageLab 测量
「人指挥 AI 的行为」。前身是 reflect 目录下三个一次性复盘脚本
（analyze_step1/2/3.py，2026-10-12 数据复盘的产物），本项目把它们升级为
可长期运行的观测系统。
