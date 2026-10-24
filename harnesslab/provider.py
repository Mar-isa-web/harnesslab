# -*- coding: utf-8 -*-
"""Provider 抽象层：统一接口对接任意模型服务。

设计要点（docs/architecture.md §6）：
- 手写 OpenAI 兼容请求（stdlib urllib，零依赖），逼自己看懂线上 JSON 格式；
- Mock Provider 按脚本逐条返回，让主循环脱离 API 就能被确定性测试；
- 重试不放在这一层——重试是策略，策略放在 Agent 层并可观测。
"""
import json
import urllib.error
import urllib.request
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ToolCallRequest:
    """模型输出里解析出的一次工具调用请求。"""
    id: str
    name: str
    arguments: dict


@dataclass
class ModelResponse:
    """Provider 返回的统一结构。"""
    text: str = ""
    tool_calls: list = field(default_factory=list)  # list[ToolCallRequest]
    input_tokens: int = 0
    output_tokens: int = 0
    finish_reason: str = "stop"  # stop / tool-calls
    raw: dict = field(default_factory=dict)


class ProviderError(Exception):
    """模型调用失败。error_type 用于落库归类（network_error/http_429/...）。"""

    def __init__(self, error_type: str, message: str):
        super().__init__(message)
        self.error_type = error_type


class BaseProvider(ABC):
    name: str = "base"
    model_name: str = "-"

    @abstractmethod
    def chat(self, messages: list, tools: list = None) -> ModelResponse:
        """输入组装好的消息与工具 schema，返回统一响应。失败抛 ProviderError。"""


class MockProvider(BaseProvider):
    """离线 Provider：按脚本逐条返回预置响应。

    用途：① 主循环的确定性测试；② M1 基准任务的离线回放。
    errors_first：前 N 次调用注入 network_error，用于测试重试路径。
    """

    name = "mock"
    model_name = "mock-script"

    def __init__(self, script, errors_first: int = 0):
        self.script = list(script)
        self.errors_first = errors_first
        self.calls = 0

    def chat(self, messages, tools=None):
        self.calls += 1
        if self.calls <= self.errors_first:
            raise ProviderError("network_error", "mock 注入的失败")
        if not self.script:
            raise ProviderError("script_exhausted", "Mock 脚本已用完")
        return self.script.pop(0)


class OpenAICompatProvider(BaseProvider):
    """OpenAI 兼容接口的真实 Provider（GLM 开放平台 / DeepSeek 等均可接）。

    零依赖：用 stdlib urllib 手写请求，请求/响应的线上格式在 _parse 里完全可见。
    """

    name = "openai-compat"

    def __init__(self, api_key: str, model: str,
                 base_url: str = "https://open.bigmodel.cn/api/paas/v4",
                 timeout: int = 180):
        self.api_key = api_key
        self.model = model
        self.model_name = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def chat(self, messages, tools=None):
        body = {"model": self.model, "messages": messages}
        if tools:
            body["tools"] = tools
        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            raise ProviderError(f"http_{e.code}", detail or str(e))
        except Exception as e:  # 超时/断网/DNS 等
            raise ProviderError("network_error", str(e)[:300])
        return self._parse(data)

    @staticmethod
    def _parse(data: dict) -> ModelResponse:
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        usage = data.get("usage") or {}
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            raw_args = fn.get("arguments")
            try:
                args = json.loads(raw_args) if raw_args else {}
            except (json.JSONDecodeError, TypeError):
                args = {"_raw": str(raw_args)}  # 模型吐了非法 JSON 时的兜底
            calls.append(ToolCallRequest(
                id=tc.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                name=fn.get("name", ""), arguments=args))
        return ModelResponse(
            text=msg.get("content") or "",
            tool_calls=calls,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            finish_reason="tool-calls" if calls else "stop",
            raw=data)
