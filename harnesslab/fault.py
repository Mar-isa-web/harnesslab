# -*- coding: utf-8 -*-
"""故障注入（M3）：让失败可控、可复现、可统计地发生。

- make_flaky_tool：可调失败率的工具，带随机种子——同一个种子下失败序列
  完全一致，实验可复现（"30% 失败率"就是字面意义的 30%，不是玄学）；
- FaultyProvider：按调用序号注入 ProviderError 的包装器——模拟限流、
  服务端错误、上下文超限等模型侧故障，测主循环的应对。

这套东西的价值在面试里就是那句话：我的项目不只有 happy path，
失败是被我当作实验变量来设计和测量的。
"""
import random

from .provider import BaseProvider, ProviderError
from .tools import Tool


def make_flaky_tool(failure_rate: float = 0.5, seed: int = None,
                    name: str = "flaky_task") -> Tool:
    rng = random.Random(seed)

    def flaky(args, cwd):
        if rng.random() < failure_rate:
            raise RuntimeError(f"注入失败 (flaky rate={failure_rate})")
        return "OK"

    return Tool(name, f"故障注入测试工具：约 {failure_rate:.0%} 概率失败",
                {"type": "object", "properties": {}}, flaky, write=False)


class FaultyProvider(BaseProvider):
    """按调用序号注入错误的 Provider 包装器。

    plan 形如 {1: ("context_length_exceeded", "模拟超限"), 3: ("http_429", "限流")}，
    key 是第几次调用（从 1 起）。序号注入 = 场景可精确重放。
    """

    name = "faulty-wrapper"

    def __init__(self, inner: BaseProvider, plan: dict):
        self.inner = inner
        self.plan = plan
        self.calls = 0
        self.model_name = inner.model_name

    def chat(self, messages, tools=None):
        self.calls += 1
        if self.calls in self.plan:
            err_type, msg = self.plan[self.calls]
            raise ProviderError(err_type, msg)
        return self.inner.chat(messages, tools)
