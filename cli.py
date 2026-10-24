# -*- coding: utf-8 -*-
"""HarnessLab CLI 入口。

零配置体验（不需要任何 API key）：
    python cli.py --provider mock "演示任务"

接真实模型（OpenAI 兼容，GLM 开放平台示例）：
    set HL_API_KEY=你的key
    python cli.py "列出 tasks 目录并总结里面的任务" --model glm-5.3-flash

DeepSeek 示例：
    set HL_API_KEY=你的key
    set HL_BASE_URL=https://api.deepseek.com
    python cli.py "..." --model deepseek-chat
"""
import argparse
import os
import sqlite3
import sys

from harnesslab.agent import Agent
from harnesslab.permissions import PermissionPolicy
from harnesslab.provider import MockProvider, OpenAICompatProvider, \
    ModelResponse, ToolCallRequest
from harnesslab.retry import ImmediateRetry, ExponentialBackoff
from harnesslab.telemetry import Telemetry
from harnesslab.tools import ToolRegistry

# 内置演示脚本：列出 tasks 目录 → 读取示例任务 → 汇报（全离线）
DEMO_SCRIPT = [
    ModelResponse(tool_calls=[ToolCallRequest("c1", "list_dir", {"path": "tasks"})],
                  input_tokens=520, output_tokens=40, finish_reason="tool-calls"),
    ModelResponse(tool_calls=[ToolCallRequest("c2", "read_file",
                                              {"path": "tasks/sample_task.md"})],
                  input_tokens=610, output_tokens=35, finish_reason="tool-calls"),
    ModelResponse(text="[Mock 演示] 已列出 tasks 目录并读取示例任务文件。"
                       "主循环两次工具调用与三次模型请求均已落库。",
                  input_tokens=700, output_tokens=60, finish_reason="stop"),
]


def build_provider(args):
    if args.provider == "mock":
        return MockProvider(DEMO_SCRIPT)
    api_key = args.api_key or os.environ.get("HL_API_KEY")
    if not api_key:
        print("缺少 API key：请设置环境变量 HL_API_KEY，或改用 --provider mock 离线体验。")
        sys.exit(1)
    return OpenAICompatProvider(api_key, args.model, args.base_url)


def main():
    ap = argparse.ArgumentParser(description="HarnessLab - 带全链路埋点的迷你 Agent")
    ap.add_argument("task", help="任务描述")
    ap.add_argument("--provider", default="mock", choices=["mock", "openai"])
    ap.add_argument("--model", default=os.environ.get("HL_MODEL", "glm-5.3-flash"))
    ap.add_argument("--base-url", default=os.environ.get(
        "HL_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"))
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--mode", default="ask", choices=["readonly", "ask", "yolo"])
    ap.add_argument("--retry", default="immediate", choices=["immediate", "backoff"])
    ap.add_argument("--db", default=os.path.join("data", "harnesslab.db"))
    ap.add_argument("--max-steps", type=int, default=20)
    args = ap.parse_args()

    tel = Telemetry(args.db)
    registry = ToolRegistry(cwd=os.getcwd())
    registry.register_builtins()
    provider = build_provider(args)
    retry = ExponentialBackoff() if args.retry == "backoff" else ImmediateRetry()
    agent = Agent(provider, registry, tel, PermissionPolicy(args.mode),
                  retry=retry, max_steps=args.max_steps)
    print(f"[HarnessLab] provider={provider.name} model={provider.model_name} "
          f"mode={args.mode} cwd={registry.cwd}")
    report = agent.run(args.task)
    tel.close()

    print("\n========== 任务报告 ==========")
    print(f"状态: {report['status']}   耗时: {report['duration_ms']/1000:.1f}s")
    print(f"模型请求 {report['model_requests']} 次（重试 {report['model_retries']}）"
          f" | 工具调用 {report['tool_calls']} 次"
          f" | tokens: 输入 {report['in_tok']} / 输出 {report['out_tok']}")
    if report["answer"]:
        print(f"回答: {report['answer'][:500]}")

    # 落库自检：任务结束后四张表应能完整还原这次任务
    con = sqlite3.connect(args.db)
    print(f"\n---------- 遥测落库自检 ({args.db}) ----------")
    for table in ("session", "turn_usage", "model_usage", "tool_usage"):
        n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table:<12} {n} 行")
    print("  本次工具调用明细:")
    for name, status, dur, err in con.execute(
            "SELECT tool_name, status, duration_ms, error_message FROM tool_usage "
            "WHERE turn_id=? ORDER BY started_at", (report["turn_id"],)):
        line = f"    {name:<12} {status:<10} {dur}ms"
        if err:
            line += f"  {err[:60]}"
        print(line)
    con.close()


if __name__ == "__main__":
    main()
