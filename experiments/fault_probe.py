# -*- coding: utf-8 -*-
"""M3 故障注入实验：把故障注入到"真实模型"面前，观测它如何应对。

探针A 限流韧性：FaultyProvider 在第 1 次请求注入 http_429，
        主循环用指数退避重试——验证"注入失败→退避→恢复"全链路在真实模型下成立。
探针B 工具失败韧性：注册 50% 失败率（种子固定）的 flaky 工具，
        任务要求模型反复重试直到成功——观测真实模型面对连续工具失败的行为模式。

运行（需 HL_API_KEY / HL_BASE_URL 已设）：
    python experiments/fault_probe.py
"""
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harnesslab.agent import Agent
from harnesslab.context import NaiveTruncate
from harnesslab.fault import make_flaky_tool, FaultyProvider
from harnesslab.permissions import PermissionPolicy
from harnesslab.provider import OpenAICompatProvider
from harnesslab.retry import ExponentialBackoff
from harnesslab.telemetry import Telemetry
from harnesslab.tools import ToolRegistry


def env(name, default=None):
    return os.environ.get(name, default)


def probe_rate_limit(model_key, base_url, tel):
    """探针A：第 1 次请求注入 429，看退避重试能否恢复。"""
    print("\n[探针A] 注入 http_429 → 指数退避 → 恢复")
    inner = OpenAICompatProvider(model_key, "deepseek-chat", base_url)
    faulty = FaultyProvider(inner, {1: ("http_429", "注入点：模拟限流")})
    ws = tempfile.mkdtemp(prefix="hl_fault_a_")
    try:
        with open(os.path.join(ws, "notes.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(f"line{i}" for i in range(1, 8)) + "\n")
        reg = ToolRegistry(cwd=ws)
        reg.register_builtins()
        agent = Agent(faulty, reg, tel, PermissionPolicy("yolo"),
                      retry=ExponentialBackoff(max_retries=2))
        r = agent.run("读取 notes.txt 统计行数，把数字写入 result.txt",
                      session_title="fault:429")
        got = ""
        p = os.path.join(ws, "result.txt")
        if os.path.exists(p):
            got = open(p, encoding="utf-8").read().strip()
        rows = tel.con.execute(
            "SELECT status, error_type, attempt_index FROM model_usage "
            "WHERE turn_id=? ORDER BY started_at", (r["turn_id"],)).fetchall()
        print(f"  任务状态: {r['status']}  答案: {got}  (期望 7)")
        print(f"  请求轨迹: {rows}")
        return r["status"] == "completed" and got == "7"
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def probe_tool_failure(model_key, base_url, tel):
    """探针B：50% 失败率的工具，看真实模型的重试行为。"""
    print("\n[探针B] 50% 失败率工具 → 观测模型重试行为")
    provider = OpenAICompatProvider(model_key, "deepseek-chat", base_url)
    ws = tempfile.mkdtemp(prefix="hl_fault_b_")
    try:
        reg = ToolRegistry(cwd=ws)
        reg.register_builtins()
        reg.register(make_flaky_tool(failure_rate=0.5, seed=7))
        agent = Agent(provider, reg, tel, PermissionPolicy("yolo"))
        r = agent.run("调用 flaky_task 工具，如果失败就继续调用，直到返回 OK 为止"
                      "（最多尝试 8 次）。成功后把 OK 写入 result.txt",
                      session_title="fault:flaky")
        tool_rows = tel.con.execute(
            "SELECT status, COUNT(*) FROM tool_usage WHERE turn_id=? "
            "AND tool_name='flaky_task' GROUP BY status", (r["turn_id"],)).fetchall()
        got = ""
        p = os.path.join(ws, "result.txt")
        if os.path.exists(p):
            got = open(p, encoding="utf-8").read().strip()
        print(f"  任务状态: {r['status']}  result.txt: {got!r}")
        print(f"  flaky_task 调用统计: {tool_rows}  总模型请求: {r['model_requests']}")
        return r["status"] == "completed" and got == "OK"
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def main():
    model_key = env("HL_API_KEY")
    base_url = env("HL_BASE_URL", "https://api.deepseek.com")
    if not model_key:
        print("缺少 HL_API_KEY")
        sys.exit(1)
    tel = Telemetry(os.path.join("data", "bench.db"))
    ok_a = probe_rate_limit(model_key, base_url, tel)
    ok_b = probe_tool_failure(model_key, base_url, tel)
    print(f"\n[结果] 探针A {'PASS' if ok_a else 'FAIL'} | 探针B {'PASS' if ok_b else 'FAIL'}")
    print("行为细节: python analysis/replay.py --db data/bench.db")
    tel.close()


if __name__ == "__main__":
    main()
