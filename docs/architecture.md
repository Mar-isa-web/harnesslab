# HarnessLab 架构设计（M0）

> 一句话定位：自研一个带全链路埋点的迷你 Agent 运行时（mini harness），
> 在它身上做可量化的改造实验和故障注入，产出数据化的复盘报告。

## 1. 设计目标

1. **每一行代码都能被作者解释**——这是项目对面试的价值底线，宁可朴素可控，不引大框架。
2. **每一步都可追溯**——一次任务里的每次模型请求、每次工具调用都落库，事后能完整复盘。
3. **改造点可 A/B**——重试策略、上下文策略都做成可替换的部件，改造前后跑同一套基准任务对比。

## 2. 总体架构

```
                       ┌──────────────────────────────────────────┐
                       │                cli.py 入口                │
                       │  任务描述 + 配置(provider/model/权限模式)  │
                       └───────────────────┬──────────────────────┘
                                           │
┌──────────────────────────────────────────▼──────────────────────────────┐
│                          Agent 主循环 (agent.py)                         │
│                                                                          │
│   user task ──► Context 上下文组装 ──► Provider 调用(带朴素重试)          │
│                    ▲                        │                            │
│                    │                        ▼                            │
│              工具结果回填 ◄── 工具执行 ◄── 解析 tool_calls ◄── 模型输出   │
│                    │          (过权限门)                                  │
│                    │                                                      │
│        最终文字回答 ◄──────────────── 无 tool_calls 时结束               │
└──────┬──────────────┬────────────────┬──────────────┬────────────────────┘
       │              │                │              │
       ▼              ▼                ▼              ▼
 ┌──────────┐  ┌────────────┐  ┌──────────────┐  ┌───────────────┐
 │provider.py│  │  tools.py  │  │permissions.py│  │ telemetry.py  │
 │ 模型抽象   │  │ 工具注册表  │  │  权限门       │  │ SQLite 落库   │
 │ Mock/真实  │  │ 4个内置工具 │  │ readonly/    │  │ 四张表，对齐  │
 │ OpenAI兼容 │  │            │  │ ask/yolo     │  │ ZCode schema │
 └──────────┘  └────────────┘  └──────────────┘  └───────────────┘
```

## 3. 模块职责

| 模块 | 职责 | 关键接口 |
|---|---|---|
| `agent.py` | 主循环：驱动一次任务到结束，聚合统计 | `Agent.run(task) -> report` |
| `provider.py` | 模型调用抽象；Mock（离线/测试）与 OpenAI 兼容（真实模型）两种实现 | `provider.chat(messages, tools)` |
| `context.py` | 上下文组装与截断（M0 故意用最朴素策略当基线） | `context.assemble() -> messages` |
| `tools.py` | 工具注册表 + 4 个内置工具（read_file / list_dir / write_file / run_shell） | `registry.invoke(name, args)` |
| `permissions.py` | 权限门：readonly / ask / yolo 三种模式 | `policy.check(tool) -> bool` |
| `telemetry.py` | 全链路埋点，写入 SQLite | `session_start / turn_start / model_request / tool_call / turn_end` |

## 4. 数据模型（为什么照抄 ZCode 的表）

四张表 `session / turn_usage / model_usage / tool_usage` 刻意对齐 ZCode CLI
本地库的结构。理由有三：

1. **分析代码直接复用**——`reflect/` 目录那三个分析脚本稍改连接串就能读本项目的库，
   M1 的观测面板几乎零成本。
2. **站在生产设计的肩膀上**——这套 schema 是一个真实商业 Harness 沉淀出来的观测维度
   （重试、取消、上下文超限、首 token 时间），照着学比自己发明少走弯路。
3. **对照实验的素材**——将来可以拿同样的 SQL 同时分析"我的 mini harness"和
   "ZCode 的真实数据"，对比行为差异。

表结构见 `harnesslab/telemetry.py` 顶部 `SCHEMA`（带字段注释）。

## 5. 一次任务的时序（M0 主循环）

```
1. session_start            → session 表插入一行
2. turn_start               → turn_usage 插入一行（status=running）
3. 循环（最多 max_steps 步）:
   a. context.assemble()    → system + 最近 N 条消息（朴素截断基线）
   b. provider.chat()       → 成功/失败都写 model_usage（失败按 attempt_index 记）
      └ M0 失败立即重试 1 次（朴素基线；M2 换成可配置退避策略，收益用数据对比）
   c. 有 tool_calls:
      逐个 → permissions.check → registry.invoke → 写 tool_usage → 结果回填 context
   d. 无 tool_calls: 记录最终回答，跳出循环
4. turn_end                 → 回填聚合统计（耗时/token数/工具数/重试数/状态）
5. session_end
```

## 6. 关键设计决策与理由（面试“为什么”弹药库）

| 决策 | 为什么 |
|---|---|
| Provider 做成抽象层而非直连某个 SDK | ① 同一套循环可接任意 OpenAI 兼容服务；② M2 的 5.3 vs Flash A/B 实验零改造；③ 用 stdlib urllib 手写请求，逼自己看懂线上 JSON 格式，不依赖黑盒 SDK |
| 重试逻辑放在 agent 层而不是 provider 层 | 重试是"策略"，策略要可替换、可观测（每次尝试都单独落库）；埋在 provider 里就测不到了 |
| Mock Provider 按脚本逐条返回 | 让主循环脱离 API 也能被确定性测试；M1 基准任务也需要"同样输入必走同样路径"的离线回放能力 |
| M0 上下文截断故意用最朴素策略 | 它是 M2 压缩改造的对照组——没有朴素基线，就无法证明"滑动窗口+摘要"的收益 |
| 工具输出截断到 8000 字符 | 真实 Harness 的铁律：单次工具结果不能撑爆上下文；截断点本身就是可调参数 |
| 权限门三分 readonly/ask/yolo | 对齐真实产品的权限模式设计；非交互基准跑 readonly/yolo，交互使用走 ask |
| 零第三方依赖（纯 stdlib） | 可移植、可审计；面试时能讲清每一层而不是"SDK 帮我做的" |

## 7. 里程碑进度与实验挂钩点

- **M1 观测面板** ✅代码就绪：`benchmark.py`（10 任务 + 自检判分）、
  `analysis/report.py`（复盘报告）、`analysis/replay.py`（事件流回放）；
  基线数据采集待真实模型
- **M2-a 重试策略** ✅代码就绪：`harnesslab/retry.py`，ImmediateRetry（基线）
  vs ExponentialBackoff；`benchmark.py --retry` 切换，策略名随轮次落库
- **M2-b 上下文压缩** ✅代码就绪：`harnesslab/context.py`，NaiveTruncate（基线）
  vs SlidingWindow（保留头 + 滑窗 + 可选摘要 + 孤儿 tool 修剪）；
  `benchmark.py --context` 切换，裁剪/摘要/孤儿计数器随轮次可查
- **M2-c 双模型 A/B** 待真实 key：同一任务集跑两遍，`model_usage.model_id` 区分
- **M3 故障注入** ✅代码就绪：`harnesslab/fault.py`，FlakyTool（种子可复现的
  可调失败率工具）+ FaultyProvider（按调用序号注入超限/限流等）
- **M3+ 流式与 TTFT**：`model_usage.time_to_first_token_ms` 列已预留（当前 NULL）

已知设计取舍：上下文超限标记在重试成功时也保留（记于 stats 而非仅外层异常）；
SlidingWindow 的摘要是回调注入，真实摘要器接模型、离线测试接假函数。

## 8. M0 验收标准

1. Mock 离线跑通：主循环完成"列目录→读文件→给出回答"任务；
2. 真实模型可接：配置 `HL_API_KEY` 后同一命令直接切到真实模型；
3. 每步落库：任务结束后四张表能完整还原这次任务的全过程；
4. 测试覆盖三条关键路径：正常完成 / 权限拒绝 / 模型失败重试。
