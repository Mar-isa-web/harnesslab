# -*- coding: utf-8 -*-
"""指标体系：总体耗时 / token 构成 / 工具画像 / 体验信号。

相对 reflect/analyze_step1.py 的升级：
- 耗时分位用「最近邻插值」精确定义（旧脚本的 int(n*p) 在小样本下会漂）；
- 新增 TTFT（首 token 等待）分位——用户体感「AI 开始动没动」的直接度量；
- 全部指标带时间窗参数，不再只有全量视角。
"""
import sqlite3
from .load import fetch_all, fetch_one, ms_range_clause

# 分位数：最近邻插值（与 numpy percentile method='lower' 一致），
# 小样本（fixture 测试）下结果可精确断言
def percentile(sorted_vals: list[float], p: float) -> float | None:
    if not sorted_vals:
        return None
    idx = min(len(sorted_vals) - 1, int(len(sorted_vals) * p))
    return sorted_vals[idx]


def dist_summary(vals: list) -> dict | None:
    """一组毫秒耗时的通用摘要：中位/P75/P90/P99/最长。"""
    if not vals:
        return None
    v = sorted(vals)
    return {
        "n": len(v),
        "p50": percentile(v, 0.50),
        "p75": percentile(v, 0.75),
        "p90": percentile(v, 0.90),
        "p99": percentile(v, 0.99),
        "max": v[-1],
    }


BUCKETS = [(0, 30, "<30s"), (30, 120, "30s-2min"), (120, 300, "2-5min"),
           (300, 600, "5-10min"), (600, 1200, "10-20min"), (1200, 10**9, ">20min")]


def overview(con: sqlite3.Connection, since=None, until=None) -> dict:
    """总览：轮次量、耗时分布、TTFT、token 构成、体验信号计数。"""
    where, params = ms_range_clause(since, until, "t.started_at")
    out: dict = {"window": {"since": since, "until": until}}

    # ---- 轮次总量 ----
    row = fetch_one(con, f"""
        SELECT COUNT(*) AS n, SUM(t.duration_ms) AS total_ms,
               SUM(t.input_tokens) AS in_tok, SUM(t.output_tokens) AS out_tok,
               SUM(t.reasoning_tokens) AS reason_tok,
               SUM(t.cache_read_input_tokens) AS cache_tok,
               SUM(t.model_request_count) AS reqs,
               SUM(t.model_retry_count) AS retries
        FROM turn_usage t WHERE t.completed_at IS NOT NULL{where}""", params)
    out["turns"] = row

    # ---- 耗时分布（完成轮）----
    durs = [r["duration_ms"] for r in fetch_all(con, f"""
        SELECT t.duration_ms FROM turn_usage t
        WHERE t.completed_at IS NOT NULL AND t.duration_ms IS NOT NULL{where}
        """, params)]
    out["duration"] = dist_summary(durs)
    out["duration_buckets"] = [
        {"label": label, "n": sum(1 for d in durs if lo * 1000 <= d < hi * 1000)}
        for lo, hi, label in BUCKETS
    ]

    # ---- TTFT（首 token 等待）：体感「多久开始动」----
    ttfts = [r["ttft"] for r in fetch_all(con, f"""
        SELECT t.time_to_first_token_ms AS ttft FROM turn_usage t
        WHERE t.completed_at IS NOT NULL AND t.time_to_first_token_ms > 0{where}
        """, params)]
    out["ttft"] = dist_summary(ttfts)

    # ---- 体验信号：取消 / 报错 / 上下文超限 / 工具报错 ----
    w2, p2 = ms_range_clause(since, until, "started_at")
    out["signals"] = {
        "cancelled": fetch_one(con,
            f"SELECT COUNT(*) AS n, SUM(duration_ms) AS ms FROM turn_usage "
            f"WHERE cancelled_by_user = 1{w2}", p2),
        "context_exceeded": fetch_one(con,
            f"SELECT COUNT(*) AS n FROM turn_usage WHERE context_exceeded = 1{w2}", p2)["n"],
        "error_turns": fetch_one(con,
            f"SELECT COUNT(*) AS n FROM turn_usage WHERE error_type IS NOT NULL{w2}", p2)["n"],
        "tool_errors": fetch_one(con,
            f"SELECT SUM(tool_error_count) AS n FROM turn_usage WHERE 1=1{w2}", p2)["n"] or 0,
    }
    # 取消前等待分布（等了多久才放弃）
    cancel_durs = [r["duration_ms"] for r in fetch_all(con, f"""
        SELECT duration_ms FROM turn_usage
        WHERE cancelled_by_user = 1 AND duration_ms IS NOT NULL{w2}""", p2)]
    out["signals"]["cancel_wait"] = dist_summary(cancel_durs)

    # 报错类型 TopN
    out["error_types"] = fetch_all(con, f"""
        SELECT error_type, error_code, COUNT(*) AS n FROM turn_usage
        WHERE error_type IS NOT NULL{w2} GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 8""", p2)

    return out


def tool_profile(con: sqlite3.Connection, since=None, until=None, limit=12) -> list[dict]:
    """工具画像：调用次数 / 成功率 / 累计与平均耗时。"""
    where, params = ms_range_clause(since, until, "started_at")
    rows = fetch_all(con, f"""
        SELECT tool_name, COUNT(*) AS n,
               SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS ok,
               SUM(duration_ms) AS total_ms
        FROM tool_usage WHERE completed_at IS NOT NULL{where}
        GROUP BY tool_name ORDER BY total_ms DESC LIMIT {limit}""", params)
    for r in rows:
        r["ok_rate"] = round(r["ok"] / r["n"] * 100, 1) if r["n"] else None
        r["avg_ms"] = round(r["total_ms"] / r["n"], 0) if r["n"] else None
    return rows


def context_vs_duration(con: sqlite3.Connection, since=None, until=None) -> list[dict]:
    """上下文规模 × 耗时：验证「会话越拖越大越慢」的量化证据。"""
    where, params = ms_range_clause(since, until, "started_at")
    rows = fetch_all(con, f"""
        SELECT duration_ms, input_tokens FROM turn_usage
        WHERE completed_at IS NOT NULL AND input_tokens > 0{where}""", params)
    buckets = [(0, 200e3, "<200k"), (200e3, 500e3, "200-500k"),
               (500e3, 1e6, "0.5-1M"), (1e6, 2e6, "1-2M"), (2e6, 1e12, ">2M")]
    out = []
    for lo, hi, label in buckets:
        ds = sorted(r["duration_ms"] for r in rows
                    if lo <= r["input_tokens"] < hi)
        s = dist_summary(ds)
        if s:
            out.append({"label": label, **s})
    return out


def slowest_turns(con: sqlite3.Connection, since=None, until=None, limit=15) -> list[dict]:
    """最慢轮次 TopN（带会话标题定位）。"""
    where, params = ms_range_clause(since, until, "t.started_at")
    return fetch_all(con, f"""
        SELECT t.duration_ms, t.reasoning_tokens, t.tool_call_count,
               t.model_request_count, t.input_tokens, t.started_at, s.title
        FROM turn_usage t JOIN session s ON s.id = t.session_id
        WHERE t.completed_at IS NOT NULL{where}
        ORDER BY t.duration_ms DESC LIMIT {limit}""", params)
