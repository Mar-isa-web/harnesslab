# HarnessLab

> 带全链路埋点的迷你 Agent 运行时：主循环 + Provider 抽象 + 工具系统 + SQLite 遥测。
> 在它身上做可量化的改造实验（重试策略/上下文压缩/双模型 A/B）与故障注入，产出数据化复盘报告。

架构设计见 [docs/architecture.md](docs/architecture.md)（含每个设计决策的"为什么"）。

## 快速开始

```bash
# 1. 离线演示（零配置，不需要 API key）
python cli.py --provider mock "演示任务" --mode yolo

# 2. 跑端到端测试（三条关键路径：正常完成 / 权限拒绝 / 失败重试）
python tests/test_loop.py

# 3. 接真实模型（GLM 开放平台）
set HL_API_KEY=你的key
python cli.py "列出 tasks 目录并总结里面的任务" --mode yolo

#    或 DeepSeek
set HL_API_KEY=你的key
set HL_BASE_URL=https://api.deepseek.com
python cli.py "..." --model deepseek-chat --mode yolo
```

任务结束后自动打印遥测自检：四张表（session / turn_usage / model_usage /
tool_usage）的行数与本次工具调用明细。库文件默认在 `data/harnesslab.db`。

## 目录结构

```
harnesslab/
├── cli.py                 # 命令行入口
├── docs/architecture.md   # 架构设计文档（含设计决策理由）
├── harnesslab/
│   ├── agent.py           # 主循环（每步落库）
│   ├── provider.py        # Provider 抽象：Mock / OpenAI 兼容
│   ├── context.py         # 上下文管理（M0 朴素截断 = M2 改造基线）
│   ├── tools.py           # 工具注册表 + 4 个内置工具
│   ├── permissions.py     # 权限门 readonly/ask/yolo
│   └── telemetry.py       # SQLite 四表，对齐 ZCode schema
├── tasks/                 # 基准任务集（M1 扩充）
├── tests/test_loop.py     # 离线端到端测试
└── data/                  # 遥测库（gitignore）
```

## 路线图

- **M0 ✅ 主循环 + 全链路落库**
- **M1 ✅ 代码就绪** 观测面板：benchmark.py（10 任务+自检）、report.py、replay.py；
  基线数据待真实模型采集
- **M2 ✅ 代码就绪** 改造实验：retry.py（立即重试 vs 指数退避）、context.py
  （朴素截断 vs 滑动窗口+摘要+孤儿修剪）、benchmark.py --retry/--context 一键切换；
  A/B 数据待真实模型采集
- **M3 ✅ 代码就绪** 故障注入：fault.py（种子可复现的 FlakyTool + 按序号注入的
  FaultyProvider）；注入实验待真实模型采集

**待办（最后一批，需 API key 额度）**：基线跑分 → 策略 A/B → 双模型对比 →
故障注入实验 → 基线复盘报告。测试现状：M0 15/15，M2/M3 17/17，判分器 10/10。

对应《Agent 实习邪修大法》四式：M0=解剖并重写主循环，M1=自建观测，
M2=改造点量化，M3=主动暴露失败。
