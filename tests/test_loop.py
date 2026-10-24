# -*- coding: utf-8 -*-
"""HarnessLab M0 端到端测试（纯离线，Mock Provider，不需要 API key）。

覆盖三条关键路径：
1. happy path：主循环完成"列目录→读文件→回答"，每步落库；
2. 权限拒绝：readonly 模式拒绝写工具，被拒也落库且循环继续；
3. 模型失败重试：第一次请求失败立即重试成功，重试计数与两次请求都落库。

运行：python tests/test_loop.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harnesslab.agent import Agent
from harnesslab.permissions import PermissionPolicy
from harnesslab.provider import MockProvider, ModelResponse, ToolCallRequest
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
    tmp = tempfile.mkdtemp(prefix="harnesslab_")
    with open(os.path.join(tmp, "hello.txt"), "w", encoding="utf-8") as f:
        f.write("line1\nline2\nline3\n")
    tel = Telemetry(os.path.join(tmp, "t.db"))
    reg = ToolRegistry(cwd=tmp)
    reg.register_builtins()
    return tmp, tel, reg


def test_happy_path():
    print("\n[测试1] happy path：列目录 → 读文件 → 回答")
    tmp, tel, reg = make_env()
    script = [
        ModelResponse(tool_calls=[ToolCallRequest("c1", "list_dir", {"path": "."})],
                      input_tokens=100, output_tokens=20, finish_reason="tool-calls"),
        ModelResponse(tool_calls=[ToolCallRequest("c2", "read_file",
                                                  {"path": "hello.txt"})],
                      input_tokens=200, output_tokens=20, finish_reason="tool-calls"),
        ModelResponse(text="hello.txt 共 3 行", input_tokens=300, output_tokens=15),
    ]
    agent = Agent(MockProvider(script), reg, tel, PermissionPolicy("yolo"))
    r = agent.run("统计 hello.txt 的行数")
    check(r["status"] == "completed", f"任务完成 (status={r['status']})")
    check(r["tool_calls"] == 2, f"工具调用 2 次 (实际 {r['tool_calls']})")
    check(r["model_requests"] == 3, f"模型请求 3 次 (实际 {r['model_requests']})")
    check(r["in_tok"] == 600, f"输入 token 聚合 600 (实际 {r['in_tok']})")
    tools = tel.con.execute(
        "SELECT tool_name, status FROM tool_usage ORDER BY started_at").fetchall()
    check(tools == [("list_dir", "completed"), ("read_file", "completed")],
          f"tool_usage 两行且都 completed (实际 {tools})")
    st = tel.con.execute(
        "SELECT status FROM turn_usage").fetchone()[0]
    check(st == "completed", f"turn_usage 状态 completed (实际 {st})")
    tel.close()


def test_permission_deny():
    print("\n[测试2] 权限拒绝：readonly 模式拒绝 write_file，循环仍完成")
    tmp, tel, reg = make_env()
    script = [
        ModelResponse(tool_calls=[ToolCallRequest("c1", "write_file",
                                                  {"path": "x.txt", "content": "hi"})],
                      input_tokens=100, output_tokens=20, finish_reason="tool-calls"),
        ModelResponse(text="写权限被拒，任务结束", input_tokens=200, output_tokens=10),
    ]
    agent = Agent(MockProvider(script), reg, tel, PermissionPolicy("readonly"))
    r = agent.run("写一个文件")
    check(r["status"] == "completed", f"循环完成而非崩溃 (status={r['status']})")
    row = tel.con.execute(
        "SELECT tool_name, status FROM tool_usage").fetchone()
    check(row == ("write_file", "denied"),
          f"被拒调用落库为 denied (实际 {row})")
    check(not os.path.exists(os.path.join(tmp, "x.txt")), "文件确实未写入")
    tel.close()


def test_provider_retry():
    print("\n[测试3] 模型失败重试：第1次网络错误，立即重试成功")
    tmp, tel, reg = make_env()
    script = [ModelResponse(text="重试后的回答", input_tokens=150, output_tokens=10)]
    agent = Agent(MockProvider(script, errors_first=1), reg, tel,
                  PermissionPolicy("yolo"))
    r = agent.run("任何任务")
    check(r["status"] == "completed", f"重试后任务完成 (status={r['status']})")
    check(r["model_retries"] == 1, f"重试计数 1 (实际 {r['model_retries']})")
    rows = tel.con.execute(
        "SELECT status, error_type, attempt_index, logical_request_id "
        "FROM model_usage ORDER BY started_at").fetchall()
    check(len(rows) == 2, f"两次尝试都落库 (实际 {len(rows)} 行)")
    check(rows[0][0] == "error" and rows[0][1] == "network_error"
          and rows[0][2] == 0, f"首行是 attempt0 网络错误 (实际 {rows[0]})")
    check(rows[1][0] == "completed" and rows[1][2] == 1,
          f"次行是 attempt1 成功 (实际 {rows[1]})")
    check(rows[0][3] == rows[1][3], "两次尝试共享同一 logical_request_id")
    tel.close()


if __name__ == "__main__":
    test_happy_path()
    test_permission_deny()
    test_provider_retry()
    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    sys.exit(1 if FAIL else 0)
