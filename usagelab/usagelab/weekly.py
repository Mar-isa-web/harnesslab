# -*- coding: utf-8 -*-
"""周度聚合 + 训练验收仪表盘。

为什么这个模块是整个项目的灵魂：复盘（2026-10-12）定下的四周训练计划里，
最硬的验收指标是「P90 单轮等待 < 3 分钟」，靠人肉每周重跑旧脚本对数字是
坚持不下去的。本模块把「每周 → 指标 → 与目标线比对 → 环比」全自动成一张表，
从此训练计划有了仪表盘。

周界 = 本地时区（UTC+8）周一 00:00。
"""
import sqlite3
from datetime import datetime, timedelta
from .load import LOCAL_TZ, week_key, fetch_all, ms_range_clause
from .metrics import dist_summary
from .signals import SIGNAL_PATTERNS, is_user_text
import re
import json

P90_TARGET_MS = 180_000  # 训练计划目标线：P90 等待 < 3 分钟
PLAN_START = "2026-10-12"  # 训练计划启动日（复盘当天）


def week_range(key: str) -> tuple[int, int]:
    """'YYYY-MM-DD'（周一）-> 该周 [start_ms, end_ms) 毫秒区间。"""
    monday = datetime.strptime(key, "%Y-%m-%d").replace(tzinfo=LOCAL_TZ)
    start = int(monday.timestamp() * 1000)
    return start, start + 7 * 86400_000


def weekly_turns(con: sqlite3.Connection, since=None, until=None) -> list[dict]:
    """逐周转次拉平（Python 侧按本地周分桶），供 dashboard 聚合。"""
    where, params = ms_range_clause(since, until, "started_at")
    rows = fetch_all(con, f"""
        SELECT started_at, completed_at, duration_ms, cancelled_by_user, input_tokens
        FROM turn_usage WHERE started_at IS NOT NULL{where}""", params)
    weeks: dict[str, list] = {}
    for r in rows:
        weeks.setdefault(week_key(r["started_at"]), []).append(r)
    out = []
    for wk in sorted(weeks):
        rs = weeks[wk]
        # 耗时分布只统计完成轮；取消轮单独算 cancel_wait——
        # 两类等待混进同一个分位数会让 P90 语义变模糊
        completed = [r["duration_ms"] for r in rs
                     if r["completed_at"] is not None
                     and r["duration_ms"] is not None]
        cancelled = [r for r in rs if r["cancelled_by_user"]]
        cancel_waits = [r["duration_ms"] for r in cancelled
                        if r["duration_ms"] is not None]
        in_toks = [r["input_tokens"] or 0 for r in rs]
        out.append({
            "week": wk,
            "n_turns": len(rs),
            "duration": dist_summary(completed),
            "n_cancelled": len(cancelled),
            "cancel_rate_pct": round(len(cancelled) / len(rs) * 100, 1) if rs else 0,
            "cancel_wait": dist_summary(cancel_waits),
            "avg_input_tokens": round(sum(in_toks) / len(in_toks)) if in_toks else 0,
        })
    return out


def weekly_signal_counts(con: sqlite3.Connection, since=None, until=None) -> dict:
    """逐周的不满信号消息数（一条消息命中任意信号算 1，与 signals 口径一致）。"""
    where, params = ms_range_clause(since, until, "m.time_created")
    rows = fetch_all(con, f"""
        SELECT p.data, m.time_created FROM message m
        JOIN part p ON p.message_id = m.id
        WHERE json_extract(m.data, '$.role') = 'user'
          AND json_extract(p.data, '$.type') = 'text'{where}""", params)
    counts: dict[str, int] = {}
    for r in rows:
        try:
            text = json.loads(r["data"]).get("text", "")
        except Exception:
            continue
        if not is_user_text(text):
            continue
        snippet = text[:500]
        if any(re.search(pat, snippet) for pat, _ in SIGNAL_PATTERNS.values()):
            wk = week_key(r["time_created"])
            counts[wk] = counts.get(wk, 0) + 1
    return dict(sorted(counts.items()))


def dashboard(con: sqlite3.Connection, since=None, until=None) -> dict:
    """训练验收仪表盘：逐周指标 + P90 达标判定 + 环比变化。"""
    weeks = weekly_turns(con, since, until)
    # 信号统计依赖 message 表；数据源没有会话消息层时（如 HarnessLab
    # 自采实验库只存遥测不存对话）自动降级为空，仪表盘其余指标照常
    try:
        sig = weekly_signal_counts(con, since, until)
    except sqlite3.Error:
        sig = {}
    plan_start_key = week_key(int(datetime.strptime(
        PLAN_START, "%Y-%m-%d").replace(tzinfo=LOCAL_TZ).timestamp() * 1000))

    rows = []
    prev = None
    for w in weeks:
        p90 = w["duration"]["p90"] if w["duration"] else None
        row = {
            **w,
            "signal_msgs": sig.get(w["week"], 0),
            "p90_target_met": (p90 is not None and p90 <= P90_TARGET_MS),
        }
        if prev is not None and p90 and prev.get("p90"):
            row["p90_delta_pct"] = round((p90 - prev["p90"]) / prev["p90"] * 100, 1)
        rows.append(row)
        prev = {"p90": p90}

    met = sum(1 for r in rows if r["p90_target_met"])
    in_plan = [r for r in rows if r["week"] >= plan_start_key]
    return {
        "weeks": rows,
        "p90_target_ms": P90_TARGET_MS,
        "plan_start": PLAN_START,
        "weeks_met_target": met,
        "weeks_total": len(rows),
        # 训练期内的达标情况（复盘 2026-10-12 之后）
        "plan_weeks_met": sum(1 for r in in_plan if r["p90_target_met"]),
        "plan_weeks_total": len(in_plan),
    }
