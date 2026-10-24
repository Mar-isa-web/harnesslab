# -*- coding: utf-8 -*-
"""M1 基准任务集与批量 runner。

设计原则（docs/architecture.md §7 M1 挂钩点）：
1. 判分确定性：让 Agent 把答案写进指定文件、精确比对文件内容——
   解析它的回答文本永远有措辞歧义，文件内容可以逐字符比；
2. 每个任务在独立临时工作目录里跑，用完即焚，不污染仓库；
3. 结果写入同库 benchmark_results 表，与遥测四表通过 turn_id 关联；
4. --self-test 不调模型，用已知答案反向验证"判分器本身是对的"，
   保证任务集自身的正确性（测测试的测试）。

用法：
    python benchmark.py --self-test                 # 离线自检判分器
    set HL_API_KEY=xx
    python benchmark.py --model glm-5.3-flash --label baseline   # 跑一轮基准
"""
import argparse
import os
import shutil
import sqlite3
import tempfile
import time

from harnesslab.agent import Agent
from harnesslab.context import NaiveTruncate, SlidingWindow
from harnesslab.fault import FaultyProvider
from harnesslab.permissions import PermissionPolicy
from harnesslab.provider import OpenAICompatProvider
from harnesslab.retry import ImmediateRetry, ExponentialBackoff
from harnesslab.telemetry import Telemetry
from harnesslab.tools import ToolRegistry

RESULTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS benchmark_results(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_label  TEXT,
  task_name  TEXT,
  turn_id    TEXT,
  passed     INTEGER,
  model_id   TEXT,
  duration_ms INTEGER,
  tool_calls INTEGER,
  model_requests INTEGER,
  error_type TEXT,
  time_created INTEGER);
"""

ANSWER_FILE = "result.txt"
WRITE_RULE = f"完成后必须把最终答案写入 {ANSWER_FILE} 文件，内容只写答案本身，不要任何多余文字。"


# ---------- 任务定义 ----------
class Task:
    def __init__(self, name, prompt, answer, setup=None):
        self.name = name
        self.prompt = prompt + "\n" + WRITE_RULE
        self.answers = {ANSWER_FILE: answer}  # 自检与判分共用同一份标准答案
        self.setup = setup or (lambda ws: None)

    def check(self, ws):
        for fn, expect in self.answers.items():
            path = os.path.join(ws, fn)
            if not os.path.exists(path):
                return False, f"缺少 {fn}"
            got = open(path, encoding="utf-8").read().strip()
            if got != expect.strip():
                return False, f"{fn} 期望[{expect.strip()[:40]}] 实际[{got[:40]}]"
        return True, ""


def _wf(ws, name, content):
    with open(os.path.join(ws, name), "w", encoding="utf-8") as f:
        f.write(content)


TASKS = [
    Task("count_lines", "读取 notes.txt，统计它的总行数",
         "7", lambda ws: _wf(ws, "notes.txt", "\n".join(f"第{i}行" for i in range(1, 8)) + "\n")),
    Task("sum_numbers", "读取 nums.txt，把所有数字相加，求总和",
         "50", lambda ws: _wf(ws, "nums.txt", "12\n30\n8\n")),
    Task("top_word", "读取 story.txt，找出出现次数最多的那个英文单词",
         "apple", lambda ws: _wf(ws, "story.txt",
                                 "apple banana apple cherry banana apple\n")),
    Task("find_largest", "当前目录有三个 txt 文件，找出内容最长（字符数最多）的那个文件",
         "big.txt", lambda ws: (_wf(ws, "small.txt", "a" * 10),
                                _wf(ws, "big.txt", "b" * 100),
                                _wf(ws, "mid.txt", "c" * 50))),
    Task("count_md", "统计当前目录里扩展名为 .md 的文件个数",
         "3", lambda ws: (_wf(ws, "a.md", "# a"), _wf(ws, "b.md", "# b"),
                          _wf(ws, "c.md", "# c"), _wf(ws, "x.txt", "x"),
                          _wf(ws, "y.txt", "y"))),
    Task("find_keyword", "a.txt、b.txt、c.txt 中恰有一个文件包含关键词 HARNESSLAB_SECRET，找出是哪个文件",
         "b.txt", lambda ws: (_wf(ws, "a.txt", "普通内容"),
                              _wf(ws, "b.txt", "这里有 HARNESSLAB_SECRET 藏着"),
                              _wf(ws, "c.txt", "也是普通内容"))),
    Task("merge_sorted", "把 a.txt 和 b.txt 的数字合并成一个升序序列",
         "1 2 3 4 5 6", lambda ws: (_wf(ws, "a.txt", "1\n3\n5\n"),
                                    _wf(ws, "b.txt", "2\n4\n6\n"))),
    Task("uppercase", "读取 original.txt，把它的内容转成全大写",
         "HELLO WORLD", lambda ws: _wf(ws, "original.txt", "hello world")),
    Task("add_two_files", "读取 a.txt 和 b.txt 里各一个整数，把两数之和写出",
         "42", lambda ws: (_wf(ws, "a.txt", "12"), _wf(ws, "b.txt", "30"))),
    Task("char_count", "统计 data.txt 的内容一共有多少个字符（不含换行符）",
         "6", lambda ws: _wf(ws, "data.txt", "abcdef")),
]
# merge_sorted 的标准答案写成单行空格分隔，判分会 strip 后比较——
# 提示词要求"只写答案"，允许模型用空格或换行分隔数字之一存在歧义，
# 这里统一要求空格分隔：判分器测的是"格式遵循能力"本身，也是 Agent 的真实考点。
TASKS[6].prompt = ("把 a.txt 和 b.txt 里的数字合并成一个升序序列，"
                   f"写入 {ANSWER_FILE}，数字之间用一个空格分隔，如：1 2 3\n" + WRITE_RULE)


# ---------- 自检：不调模型，反向验证判分器 ----------
def self_test():
    ok = 0
    for t in TASKS:
        ws = tempfile.mkdtemp(prefix=f"hl_st_{t.name}_")
        try:
            t.setup(ws)
            for fn, content in t.answers.items():
                _wf(ws, fn, content)  # 直接写入标准答案
            passed, note = t.check(ws)
            print(f"  [{'PASS' if passed else 'FAIL'}] {t.name} {note}")
            ok += passed
        finally:
            shutil.rmtree(ws, ignore_errors=True)
    print(f"自检结果: {ok}/{len(TASKS)}")
    return ok == len(TASKS)


# ---------- 批量 runner ----------
def run_benchmark(args):
    base_provider = OpenAICompatProvider(args.api_key, args.model, args.base_url)
    retry = ExponentialBackoff() if args.retry == "backoff" else ImmediateRetry()
    ctx_strategy = SlidingWindow() if args.context == "sliding" else NaiveTruncate()
    tel = Telemetry(args.db)
    tel.con.executescript(RESULTS_SCHEMA)
    label = args.label or f"{args.model}-{args.retry}-{args.context}-{time.strftime('%m%d_%H%M')}"
    tasks = TASKS[:args.limit] if args.limit else TASKS
    n_pass, total_ms = 0, 0
    print(f"[benchmark] label={label} model={args.model} tasks={len(tasks)} "
          f"retry={retry.name} context={ctx_strategy.name} "
          f"inject429={args.inject429 or 'off'} db={args.db}")
    for t in tasks:
        ws = tempfile.mkdtemp(prefix=f"hl_{t.name}_")
        try:
            t.setup(ws)
            registry = ToolRegistry(cwd=ws)
            registry.register_builtins()
            # 故障注入时每个任务用独立包装器（注入序号按"本任务内第几次调用"计）
            provider = FaultyProvider(
                base_provider,
                {i: ("http_429", "注入:模拟限流")
                 for i in range(1, args.inject429 + 1)}) if args.inject429 else base_provider
            agent = Agent(provider, registry, tel, PermissionPolicy("yolo"),
                          retry=retry, context_strategy=ctx_strategy,
                          max_steps=args.max_steps)
            r = agent.run(t.prompt, session_title=f"bench:{t.name}")
            passed, note = t.check(ws)
            n_pass += passed
            total_ms += r["duration_ms"]
            err = tel.con.execute(
                "SELECT error_type FROM turn_usage WHERE turn_id=?",
                (r["turn_id"],)).fetchone()
            err = err[0] if err else None
            tel.con.execute(
                """INSERT INTO benchmark_results(run_label, task_name, turn_id,
                   passed, model_id, duration_ms, tool_calls, model_requests,
                   error_type, time_created) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (label, t.name, r["turn_id"], int(passed), args.model,
                 r["duration_ms"], r["tool_calls"], r["model_requests"],
                 err, int(time.time() * 1000)))
            tel.con.commit()
            mark = "PASS" if passed else "FAIL"
            print(f"  [{mark}] {t.name:<14} {r['duration_ms']/1000:>6.1f}s "
                  f"工具{r['tool_calls']} 请求{r['model_requests']} "
                  f"status={r['status']}" + (f"  {note}" if note else ""))
        finally:
            shutil.rmtree(ws, ignore_errors=True)
    print(f"\n[benchmark] 通过率 {n_pass}/{len(tasks)}，总耗时 {total_ms/1000:.1f}s，"
          f"结果已写入 {args.db} (run_label={label})")
    print(f"生成报告: python analysis/report.py --db {args.db}")
    tel.close()
    return n_pass


def main():
    ap = argparse.ArgumentParser(description="HarnessLab 基准 runner")
    ap.add_argument("--self-test", action="store_true", help="离线自检判分器，不调模型")
    ap.add_argument("--model", default=os.environ.get("HL_MODEL", "glm-5.3-flash"))
    ap.add_argument("--base-url", default=os.environ.get(
        "HL_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"))
    ap.add_argument("--api-key", default=os.environ.get("HL_API_KEY"))
    ap.add_argument("--db", default=os.path.join("data", "bench.db"))
    ap.add_argument("--label", default=None, help="本轮标签（默认 模型-策略-时间）")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 个任务")
    ap.add_argument("--max-steps", type=int, default=15)
    ap.add_argument("--retry", default="immediate", choices=["immediate", "backoff"],
                    help="重试策略（M2-a A/B 变量）")
    ap.add_argument("--context", default="naive", choices=["naive", "sliding"],
                    help="上下文策略（M2-b A/B 变量）")
    ap.add_argument("--inject429", type=int, default=0,
                    help="每个任务前 N 次模型请求注入 http_429（故障 A/B 变量）")
    args = ap.parse_args()

    if args.self_test:
        raise SystemExit(0 if self_test() else 1)
    if not args.api_key:
        print("缺少 API key：设置 HL_API_KEY 或用 --api-key；离线自检用 --self-test")
        raise SystemExit(1)
    run_benchmark(args)


if __name__ == "__main__":
    main()
