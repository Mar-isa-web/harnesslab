# -*- coding: utf-8 -*-
"""工具系统：工具注册表 + 内置工具。

M0 只带 4 个最小工具（read_file / list_dir / write_file / run_shell）。
M3 的故障注入器会在这里追加"可调失败率"的测试工具。

铁律：任何工具的输出都必须截断——单次工具结果不能撑爆上下文。
"""
import os
import subprocess
from dataclasses import dataclass

MAX_OUTPUT_CHARS = 8000  # 工具输出进上下文前的硬上限（本身就是可调参数）


def _clip(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[输出过长，已截断，原文 {len(text)} 字符]"


@dataclass
class ToolResult:
    ok: bool
    output: str = ""
    error: str = ""


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict            # JSON Schema
    func: object                # (args: dict, cwd: str) -> str
    write: bool = False         # 是否有副作用（决定权限门怎么审）

    def to_schema(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": self.parameters}}


# ---------- 内置工具实现：签名统一为 (args, cwd) -> str ----------

def _read_file(args, cwd):
    path = os.path.join(cwd, args.get("path", ""))
    with open(path, encoding="utf-8", errors="replace") as f:
        return _clip(f.read())


def _list_dir(args, cwd):
    path = os.path.join(cwd, args.get("path", "."))
    items = sorted(os.listdir(path))[:200]
    lines = [("d " if os.path.isdir(os.path.join(path, n)) else "f ") + n
             for n in items]
    return _clip("\n".join(lines) or "(空目录)")


def _write_file(args, cwd):
    path = os.path.join(cwd, args.get("path", ""))
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    content = args.get("content", "")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return f"已写入 {len(content)} 字符到 {args.get('path')}"


def _run_shell(args, cwd):
    cmd = args.get("command", "")
    timeout = int(args.get("timeout", 30))
    p = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True,
                       timeout=timeout)
    out = (f"exit_code={p.returncode}\n--- stdout ---\n"
           f"{p.stdout.decode('utf-8', errors='replace')}\n"
           f"--- stderr ---\n{p.stderr.decode('utf-8', errors='replace')}")
    return _clip(out)


BUILTINS = [
    Tool("read_file", "读取指定路径的文本文件内容",
         {"type": "object", "properties": {"path": {"type": "string"}},
          "required": ["path"]}, _read_file, write=False),
    Tool("list_dir", "列出目录内容，每行一项，目录带 d 前缀",
         {"type": "object", "properties": {"path": {"type": "string"}}},
         _list_dir, write=False),
    Tool("write_file", "把文本内容写入指定路径（会覆盖已有文件）",
         {"type": "object",
          "properties": {"path": {"type": "string"},
                         "content": {"type": "string"}},
          "required": ["path", "content"]}, _write_file, write=True),
    Tool("run_shell", "在工作目录执行 shell 命令并返回退出码与输出",
         {"type": "object",
          "properties": {"command": {"type": "string"},
                         "timeout": {"type": "integer"}},
          "required": ["command"]}, _run_shell, write=True),
]


class ToolRegistry:
    def __init__(self, cwd: str = None):
        self.cwd = os.path.abspath(cwd or os.getcwd())
        self.tools = {}

    def register(self, tool: Tool):
        self.tools[tool.name] = tool

    def register_builtins(self):
        for t in BUILTINS:
            self.register(t)

    def get(self, name: str):
        return self.tools.get(name)

    def schemas(self) -> list:
        return [t.to_schema() for t in self.tools.values()]

    def invoke(self, name: str, args: dict) -> ToolResult:
        tool = self.get(name)
        if tool is None:
            return ToolResult(False, error=f"未知工具: {name}")
        try:
            return ToolResult(True, output=str(tool.func(args or {}, self.cwd)))
        except Exception as e:
            return ToolResult(False, error=f"{type(e).__name__}: {e}")
