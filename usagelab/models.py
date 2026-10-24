# -*- coding: utf-8 -*-
"""模型画像：按 model_usage 表逐请求统计各模型的行为差异。

这是三个旧脚本完全没有的能力：model_usage 每次模型调用一行（含失败尝试），
能回答「Flash 和 5.3 到底谁好」——不是听别人评测，是用自己 9000+ 次真实请求算。

关键指标：
- 限流率/错误率：error_type 分组计数（rate_limited 是 Start Plan 用户最疼的）；
- TTFT / 耗时分位：体感速度；
- 每轮平均输出 token： verbosity；
- 每周模型迁移曲线：什么时候从 Flash 切到 5.3 的，切了以后指标变化。
"""
import sqlite3
from .load import fetch_all, ms_range_clause
from .metrics import dist_summary


def model_profile(con: sqlite3.Connection, since=None, until=None) -> list[dict]:
    """每个 model_id 一行：请求数/错误率/限流数/耗时/TTFT/输出 token。"""
    where, params = ms_range_clause(since, until, "started_at")
    rows = fetch_all(con, f"""
        SELECT model_id,
               COUNT(*) AS n,
               SUM(CASE WHEN status != 'completed' THEN 1 ELSE 0 END) AS failed,
               SUM(CASE WHEN error_type = 'rate_limited' THEN 1 ELSE 0 END) AS rate_limited,
               SUM(input_tokens) AS in_tok, SUM(output_tokens) AS out_tok
        FROM model_usage WHERE 1=1{where}
        GROUP BY model_id ORDER BY n DESC""")
    for r in rows:
        durs, ttfts = _dur_ttft(con, r["model_id"], since, until)
        r["dur"] = dist_summary(durs)
        r["ttft"] = dist_summary(ttfts)
        r["fail_rate"] = round(r["failed"] / r["n"] * 100, 1) if r["n"] else None
        r["avg_out_tok"] = round(r["out_tok"] / r["n"], 0) if r["n"] else None
    return rows


def _dur_ttft(con, model_id, since, until):
    where, params = ms_range_clause(since, until, "started_at")
    rows = fetch_all(con, f"""
        SELECT duration_ms, time_to_first_token_ms AS ttft FROM model_usage
        WHERE model_id = ? AND status = 'completed'{where}""",
        [model_id] + params)
    return ([r["duration_ms"] for r in rows if r["duration_ms"]],
            [r["ttft"] for r in rows if r["ttft"]])


def error_breakdown(con: sqlite3.Connection, since=None, until=None) -> list[dict]:
    """全部模型错误类型分布（含失败尝试，按 error_type 聚合）。"""
    where, params = ms_range_clause(since, until, "started_at")
    return fetch_all(con, f"""
        SELECT model_id, error_type, COUNT(*) AS n
        FROM model_usage WHERE error_type IS NOT NULL{where}
        GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 15""", params)


def model_weekly_mix(con: sqlite3.Connection, since=None, until=None) -> list[dict]:
    """每周各模型的请求数：看模型迁移轨迹（周界=本地时区周一 00:00）。

    分桶放 Python 侧而不是 SQL：SQLite 里按 UTC 天切周，UTC+8 的周一凌晨
    请求会落进上一周——训练验收仪表盘对周界敏感，宁可多拉几行 raw 数据。
    """
    from .weekly import week_key
    where, params = ms_range_clause(since, until, "started_at")
    rows = fetch_all(con, f"""
        SELECT started_at, model_id FROM model_usage
        WHERE started_at IS NOT NULL{where}""", params)
    mix: dict[str, dict[str, int]] = {}
    for r in rows:
        wk = week_key(r["started_at"])
        mix.setdefault(wk, {})
        mix[wk][r["model_id"]] = mix[wk].get(r["model_id"], 0) + 1
    return [{"week": wk, "models": models} for wk, models in sorted(mix.items())]
