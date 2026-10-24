# -*- coding: utf-8 -*-
"""上下文管理（M2 改造点 b）：组装策略做成可插拔。

两种策略：
- NaiveTruncate：M0 基线，只保留最近 N 条（对照组）；
- SlidingWindow：保留任务头部 + 滑动窗口；被裁掉的中间段可选生成摘要。
  summarizer 是回调 fn(dropped_messages)->str——真实摘要接模型，
  离线测试接假函数，主循环不关心摘要从哪来。

一个真实的坑（这里显式处理）：窗口边界可能把 assistant(tool_calls) 裁掉、
留下配对的孤儿 tool 消息——多数 API 会因孤儿 tool 消息直接报 400。
策略里做边界修剪并计数 orphan_trimmed，这个计数器本身就是观测指标。
"""


class ContextStrategy:
    name = "base"

    def select(self, messages: list, counters: dict) -> list:
        """输入全部历史消息，返回保留的消息列表（不含 system）。"""
        raise NotImplementedError


class NaiveTruncate(ContextStrategy):
    name = "naive-truncate"

    def __init__(self, max_messages: int = 40):
        self.max_messages = max_messages

    def select(self, messages, counters):
        if len(messages) > self.max_messages:
            counters["truncated"] += len(messages) - self.max_messages
            return messages[-self.max_messages:]
        return messages


class SlidingWindow(ContextStrategy):
    name = "sliding-window"

    def __init__(self, window: int = 24, keep_head: int = 2, summarizer=None):
        self.window = window          # 保留最近多少条
        self.keep_head = keep_head    # 保留最早多少条（任务描述所在，裁掉会失忆）
        self.summarizer = summarizer  # 可选：对被裁中间段做摘要

    def select(self, messages, counters):
        if len(messages) <= self.window + self.keep_head:
            return messages
        head = messages[:self.keep_head]
        dropped = messages[self.keep_head:-self.window]
        tail = messages[-self.window:]
        counters["truncated"] += len(dropped)

        out = list(head)
        if self.summarizer:
            out.append({"role": "system",
                        "content": f"[历史摘要] {self.summarizer(dropped)}"})
            counters["summarized"] += 1
        out += tail

        # 修剪窗口开头的孤儿 tool 消息（配对的 assistant tool_calls 已被裁掉）
        start = len(out) - len(tail)
        j = start
        while j < len(out) and out[j].get("role") == "tool":
            j += 1
        if j > start:
            counters["orphan_trimmed"] += j - start
            out = out[:start] + out[j:]
        return out


class Context:
    """消息容器 + 策略委托。counters 暴露给遥测与报告。"""

    def __init__(self, system_prompt: str, strategy: ContextStrategy = None):
        self.system = {"role": "system", "content": system_prompt}
        self.messages = []
        self.strategy = strategy or NaiveTruncate()
        self.counters = {"truncated": 0, "summarized": 0, "orphan_trimmed": 0}

    def add(self, msg: dict):
        self.messages.append(msg)

    def assemble(self) -> list:
        return [self.system] + self.strategy.select(self.messages, self.counters)

    @property
    def truncated(self):
        return self.counters["truncated"]
