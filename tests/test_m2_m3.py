# -*- coding: utf-8 -*-
"""M2/M3 离线测试：重试策略、滑动窗口压缩、故障注入。全部不依赖 API key。

运行：python tests/test_m2_m3.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harnesslab.agent import Agent
from harnesslab.context import Context, NaiveTruncate, SlidingWindow
from harnesslab.fault import make_flaky_tool, FaultyProvider
from harnesslab.permissions import PermissionPolicy
from harnesslab.provider import MockProvider, ModelResponse, ToolCallRequest
from harnesslab.retry import ImmediateRetry, ExponentialBackoff
from harnesslab.telemetry import Telemetry
from harnesslab.tools import ToolRegistry

PASS, FAIL = 0, 0


def check(cond, label):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}")


def make_env():
    tmp = tempfile.mkdtemp(prefix="hl_m2_")
    tel = Telemetry(os.path.join(tmp, "t.db"))
    reg = ToolRegistry(cwd=tmp)
    reg.register_builtins()
    return tmp, tel, reg


def test_exponential_backoff():
    print("\n[测试1] 指数退避：注入两次网络错误，断言退避序列与落库")
    tmp, tel, reg = make_env()
    sleeps = []
    policy = ExponentialBackoff(max_retries=2, base_delay=0.5, factor=2.0,
                                sleeper=sleeps.append)
    script = [ModelResponse(text="done", input_tokens=100, output_tokens=10)]
    agent = Agent(MockProvider(script, errors_first=2), reg, tel,
                  PermissionPolicy("yolo"), retry=policy)
    r = agent.run("任务")
    check(r["status"] == "completed", f"两次失败后退避重试成功 ({r['status']})")
    check(sleeps == [0.5, 1.0], f"退避序列 [0.5, 1.0]s (实际 {sleeps})")
    check(r["model_retries"] == 2, f"重试计数 2 (实际 {r['model_retries']})")
    rows = tel.con.execute(
        "SELECT COUNT(*) FROM model_usage WHERE status='error'").fetchone()[0]
    check(rows == 2, f"两次失败尝试均落库 (实际 {rows})")
    pol = tel.con.execute(
        "SELECT retry_policy FROM turn_usage").fetchone()[0]
    check(pol == "exp-backoff", f"策略名随轮次落库 (实际 {pol})")
    tel.close()


def test_sliding_window():
    print("\n[测试2] 滑动窗口：长历史裁剪、摘要注入、孤儿 tool 修剪")
    # 构造：head 2 条 + 30 组 (assistant(tool_calls) + tool) + 尾部对话
    msgs = [{"role": "user", "content": "任务描述"},
            {"role": "assistant", "content": "开始执行"}]
    for i in range(30):
        msgs.append({"role": "assistant", "content": "",
                     "tool_calls": [{"id": f"c{i}", "type": "function",
                                     "function": {"name": "read_file",
                                                  "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}",
                     "content": f"结果{i}"})
    # 场景A：窗口边界对齐（偶数），无孤儿
    ctx = Context("sys", SlidingWindow(window=10, keep_head=2,
                                       summarizer=lambda d: f"共{len(d)}条"))
    ctx.messages = list(msgs)
    kept = ctx.assemble()
    c = ctx.counters
    check(c["truncated"] == 50, f"裁掉 50 条 (实际 {c['truncated']})")
    check(c["summarized"] == 1, "摘要注入 1 次")
    check(any("[历史摘要] 共50条" in m.get("content", "") for m in kept),
          "摘要内容出现在上下文")
    orphan_at_head = kept[3].get("role") == "tool"
    check(not orphan_at_head, "对齐窗口下无孤儿 tool 开头")
    # 场景B：窗口错位 1 条，尾部以孤儿 tool 开头，应被修剪
    ctx2 = Context("sys", SlidingWindow(window=11, keep_head=2))
    ctx2.messages = list(msgs)
    kept2 = ctx2.assemble()
    check(ctx2.counters["orphan_trimmed"] == 1,
          f"错位窗口修剪 1 条孤儿 (实际 {ctx2.counters['orphan_trimmed']})")
    check(kept2[2].get("role") != "tool", "修剪后首条不是孤儿 tool")
    # 场景C：未超限则原样返回
    ctx3 = Context("sys", SlidingWindow(window=100, keep_head=2))
    ctx3.messages = list(msgs)
    n3 = len(ctx3.assemble())
    check(n3 == len(msgs) + 1 and ctx3.counters["truncated"] == 0,
          "未超限不裁剪")
    tel_dummy_close = None  # 本测试纯内存，无需库


def test_flaky_tool():
    print("\n[测试3] 故障注入工具：失败率可控且可复现")
    tmp, tel, reg = make_env()
    reg.register(make_flaky_tool(failure_rate=0.3, seed=42))
    fails = sum(1 for _ in range(300)
                if not reg.invoke("flaky_task", {}).ok)
    check(0.2 <= fails / 300 <= 0.4,
          f"300 次调用失败率≈30% (实际 {fails/300:.2f})")
    # 同种子可复现：两个全新实例（避开已被频率测试消耗过状态的旧实例）
    ra, rb = ToolRegistry(cwd=tmp), ToolRegistry(cwd=tmp)
    ra.register(make_flaky_tool(failure_rate=0.3, seed=42))
    rb.register(make_flaky_tool(failure_rate=0.3, seed=42))
    seq1 = [ra.invoke("flaky_task", {}).ok for _ in range(50)]
    seq2 = [rb.invoke("flaky_task", {}).ok for _ in range(50)]
    check(seq1 == seq2, "同种子失败序列完全一致（实验可复现）")
    tel.close()


def test_context_exceeded_injection():
    print("\n[测试4] 上下文超限注入：context_exceeded 标记落库")
    tmp, tel, reg = make_env()
    script = [ModelResponse(text="恢复后的回答", input_tokens=50, output_tokens=5)]
    faulty = FaultyProvider(MockProvider(script),
                            {1: ("context_length_exceeded", "模拟上下文超限")})
    agent = Agent(faulty, reg, tel, PermissionPolicy("yolo"),
                  retry=ImmediateRetry(1))
    r = agent.run("长任务")
    check(r["status"] == "completed", f"重试后恢复 ({r['status']})")
    flag = tel.con.execute(
        "SELECT context_exceeded FROM turn_usage").fetchone()[0]
    check(flag == 1, f"超限标记已落库 (实际 {flag})")
    tel.close()


def test_strategy_logged_end_to_end():
    print("\n[测试5] 端到端：策略标注随轮次写入")
    tmp, tel, reg = make_env()
    script = [
        ModelResponse(tool_calls=[ToolCallRequest("c1", "list_dir", {"path": "."})],
                      input_tokens=80, output_tokens=15, finish_reason="tool-calls"),
        ModelResponse(text="完成", input_tokens=90, output_tokens=8),
    ]
    agent = Agent(MockProvider(script), reg, tel, PermissionPolicy("yolo"),
                  context_strategy=SlidingWindow(window=50))
    r = agent.run("小任务")
    row = tel.con.execute(
        "SELECT retry_policy, context_strategy FROM turn_usage").fetchone()
    check(r["status"] == "completed" and row == ("immediate", "sliding-window"),
          f"标注正确 (实际 {row})")
    tel.close()


if __name__ == "__main__":
    test_exponential_backoff()
    test_sliding_window()
    test_flaky_tool()
    test_context_exceeded_injection()
    test_strategy_logged_end_to_end()
    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    sys.exit(1 if FAIL else 0)
