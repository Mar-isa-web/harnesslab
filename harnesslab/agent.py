# -*- coding: utf-8 -*-
"""主循环：一次任务的完整生命周期（docs/architecture.md §5）。

    user task → Context 组装 → Provider 调用（重试策略）→ 解析 tool_calls
    → 权限门 → 工具执行 → 结果回填 → 继续推理 → 最终文字回答 → 落库聚合

每一次模型请求（含失败尝试）与每一次工具调用都写 telemetry；
重试策略与上下文策略的名字随轮次落库——M2 的 A/B 实验靠它们区分组别。
"""
import json
import time
import uuid

from .context import Context
from .provider import BaseProvider, ProviderError
from .retry import RetryPolicy, ImmediateRetry
from .tools import ToolRegistry, MAX_OUTPUT_CHARS
from .permissions import PermissionPolicy

SYSTEM_PROMPT = """你是 HarnessLab 的执行 Agent，通过调用工具完成用户任务。
规则：
1. 优先用工具获取真实信息，不要凭空猜测文件内容或命令结果。
2. 信息足够后直接给出最终文字回答，不再调用工具。
3. 工具输出过长会被截断，需要更多信息时换更精确的参数分段获取。"""


def _now() -> int:
    return int(time.time() * 1000)


class Agent:
    def __init__(self, provider: BaseProvider, registry: ToolRegistry,
                 telemetry, policy: PermissionPolicy,
                 retry=None, context_strategy=None, max_steps: int = 20):
        self.provider = provider
        self.registry = registry
        self.tel = telemetry
        self.policy = policy
        # 兼容旧用法：retry 传 int 表示"立即重试 N 次"
        self.retry = retry if isinstance(retry, RetryPolicy) \
            else ImmediateRetry(retry if retry is not None else 1)
        self.default_strategy = context_strategy
        self.max_steps = max_steps

    # ---------- 对外主入口 ----------
    def run(self, task: str, session_title: str = None) -> dict:
        t_start = _now()
        session_id = self.tel.session_start(session_title or task[:40],
                                            self.registry.cwd)
        turn_id = self.tel.turn_start(session_id)
        ctx = Context(SYSTEM_PROMPT, strategy=self.default_strategy)
        ctx.add({"role": "user", "content": task})
        stats = dict(tool_calls=0, model_requests=0, model_retries=0,
                     in_tok=0, out_tok=0)
        status, err_type, err_msg, final_text = "completed", None, None, ""
        try:
            for _step in range(self.max_steps):
                resp = self._call_model(ctx, session_id, turn_id, stats)
                if not resp.tool_calls:
                    final_text = resp.text
                    ctx.add({"role": "assistant", "content": resp.text})
                    break
                ctx.add(self._assistant_msg(resp))
                for call in resp.tool_calls:
                    content = self._run_tool(call, turn_id, stats)
                    ctx.add({"role": "tool", "tool_call_id": call.id,
                             "content": content})
            else:
                status, err_type = "max_steps", "max_steps_exceeded"
        except KeyboardInterrupt:
            status, err_type = "cancelled", "user_cancel"
        except ProviderError as e:
            status, err_type, err_msg = "error", e.error_type, str(e)

        duration = _now() - t_start
        self.tel.turn_end(
            turn_id, status, duration_ms=duration,
            input_tokens=stats["in_tok"], output_tokens=stats["out_tok"],
            tool_call_count=stats["tool_calls"],
            model_request_count=stats["model_requests"],
            model_retry_count=stats["model_retries"],
            context_exceeded=bool(stats.get("context_exceeded")),
            retry_policy=self.retry.name,
            context_strategy=ctx.strategy.name,
            error_type=err_type, error_message=err_msg)
        self.tel.session_end(session_id)
        return {"status": status, "answer": final_text, "turn_id": turn_id,
                "session_id": session_id, "duration_ms": duration,
                "retry_policy": self.retry.name,
                "context_strategy": ctx.strategy.name,
                "counters": dict(ctx.counters), **stats}

    # ---------- 模型调用（策略化重试，每次尝试都落库）----------
    def _call_model(self, ctx, session_id, turn_id, stats):
        logical_id = uuid.uuid4().hex[:16]
        for attempt in range(self.retry.max_retries + 1):
            t0 = _now()
            try:
                resp = self.provider.chat(ctx.assemble(), self.registry.schemas())
                dur = _now() - t0
                stats["model_requests"] += 1
                stats["in_tok"] += resp.input_tokens
                stats["out_tok"] += resp.output_tokens
                self.tel.model_request(
                    session_id, turn_id, self.provider.name,
                    self.provider.model_name, "completed", t0, dur,
                    logical_request_id=logical_id, attempt_index=attempt,
                    input_tokens=resp.input_tokens,
                    output_tokens=resp.output_tokens,
                    finish_reason=resp.finish_reason,
                    tool_call_count=len(resp.tool_calls))
                return resp
            except ProviderError as e:
                dur = _now() - t0
                self.tel.model_request(
                    session_id, turn_id, self.provider.name,
                    self.provider.model_name, "error", t0, dur,
                    logical_request_id=logical_id, attempt_index=attempt,
                    error_type=e.error_type, error_message=str(e)[:300])
                if e.error_type == "context_length_exceeded":
                    # 超限标记不能只靠外层 except：重试成功时异常被吞，
                    # 标记会丢。在这里记 stats，无论最终成败都落库。
                    stats["context_exceeded"] = 1
                if attempt < self.retry.max_retries:
                    stats["model_retries"] += 1
                    self.retry.wait_before_retry(attempt)
                    continue
                raise

    # ---------- 工具调用（过权限门，成功失败都落库）----------
    def _run_tool(self, call, turn_id, stats) -> str:
        t0 = _now()
        stats["tool_calls"] += 1
        tool = self.registry.get(call.name)
        if tool is None:
            result, status = self.registry.invoke(call.name, call.arguments), "error"
        elif not self.policy.check(tool):
            result, status = None, "denied"
        else:
            result, status = self.registry.invoke(call.name, call.arguments), None
        if status == "denied":
            content = f"工具执行失败: permission denied ({self.policy.mode} 模式拒绝 {call.name})"
        elif result.ok:
            content = result.output[:MAX_OUTPUT_CHARS]
            status = "completed"
        else:
            content = f"工具执行失败: {result.error}"
            status = "error"
        self.tel.tool_call(turn_id, call.name, status, t0, _now() - t0,
                           error_message=None if status == "completed" else content[:200])
        return content

    @staticmethod
    def _assistant_msg(resp) -> dict:
        return {"role": "assistant", "content": resp.text or "",
                "tool_calls": [{"id": c.id, "type": "function", "function": {
                    "name": c.name,
                    "arguments": json.dumps(c.arguments, ensure_ascii=False)}}
                    for c in resp.tool_calls]}
