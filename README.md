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

- **M0 ✅ 主循环与遥测**：Agent 循环 + SQLite 四表全链路埋点，42 项离线测试全绿
- **M1 ✅ 观测面板**：10 项可判分基准（判分器自检）+ 复盘报告生成器 + 事件流回放；
  deepseek-chat 基线 10/10
- **M2 ✅ 策略改造与对照**：立即重试 vs 指数退避、朴素截断 vs 滑动窗口（含孤儿
  tool 修剪）；注入连续限流下，指数退避使任务存活率 0%→100%
- **M3 ✅ 故障注入与韧性实验**：种子可复现的可调失败率工具 + 按调用序号注入
  Provider 故障；已完成限流恢复、工具失败韧性、双模型行为对比三类实验

**后续方向（按项目自身需求排序，做完一个再评估下一个）**：
- 多轮均值实验：实测任务通过率存在 ~10% 的模型行为波动，正式对照结论需
  每配置 ≥3 轮取均值；
- 交互式会话模式：当前是单任务批处理架构，若引入交互模式，再评估跨任务
  记忆等能力是否具有真实使用场景。
