# -*- coding: utf-8 -*-
"""报告生成：一次采集全部指标 -> markdown / 自包含 HTML / 历史快照。

设计决策：
1. 采集与渲染分离——build_report() 只产 dict，渲染层（md/html）各自消费。
   以后加新输出格式（如 PDF）不动采集逻辑。
2. HTML 单文件自包含（CSS 内联、图表 base64 内嵌）——报告要能直接发微信/
   存档/打印，不依赖目录里的图片文件。
3. 每次运行落一个指标快照 JSON 到 history/——本周与上周的对比不再需要
   「记得当时数字」，直接 diff 快照。
"""
import base64
import html
import json
import os
import sqlite3
from datetime import datetime

from .load import LOCAL_TZ, fmt_ms, ms2min
from . import metrics, models, sessions, signals, weekly


# ---------- 采集 ----------

def build_report(con: sqlite3.Connection, since=None, until=None) -> dict:
    """采集全部指标段。单段失败不炸整份报告（见 load.py 设计说明）。"""
    def safe(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except Exception as e:  # schema 差异等
            return {"__error__": f"{type(e).__name__}: {e}"}

    return {
        "generated_at": datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M"),
        "since": since,
        "until": until,
        "overview": safe(metrics.overview, con, since, until),
        "tools": safe(metrics.tool_profile, con, since, until),
        "context_vs_duration": safe(metrics.context_vs_duration, con, since, until),
        "slowest": safe(metrics.slowest_turns, con, since, until),
        "model_profile": safe(models.model_profile, con, since, until),
        "model_errors": safe(models.error_breakdown, con, since, until),
        "model_mix": safe(models.model_weekly_mix, con, since, until),
        "sessions": safe(sessions.session_profile, con, since, until),
        "signals": safe(signals.mine_signals, con, since, until),
        "bursts": safe(signals.burst_sessions, con, since, until),
        "dashboard": safe(weekly.dashboard, con, since, until),
    }


def save_snapshot(data: dict, history_dir: str) -> str:
    """核心指标快照存档，文件名含时间戳。"""
    ov = data.get("overview") or {}
    turns = ov.get("turns") or {}
    dur = ov.get("duration") or {}
    ttft = ov.get("ttft") or {}
    sg = ov.get("signals") or {}
    sess = data.get("sessions") or {}
    snap = {
        "generated_at": data["generated_at"],
        "since": data["since"], "until": data["until"],
        "n_turns": turns.get("n"), "total_min": ms2min(turns.get("total_ms")),
        "p50_ms": dur.get("p50"), "p90_ms": dur.get("p90"),
        "ttft_p50_ms": ttft.get("p50"),
        "cancelled": (sg.get("cancelled") or {}).get("n"),
        "model_profile": data.get("model_profile"),
        "giant_share_pct": (sess.get("hygiene") or {}).get("giant_share_pct"),
        "signal_weighted_total": (data.get("signals") or {}).get("weighted_total"),
    }
    os.makedirs(history_dir, exist_ok=True)
    name = "snapshot_" + datetime.now(LOCAL_TZ).strftime("%Y%m%d_%H%M%S") + ".json"
    path = os.path.join(history_dir, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=2)
    return path


# ---------- markdown 渲染 ----------

def _sec(ms):
    return f"{ms/1000:.0f}s" if ms is not None and ms < 120000 else f"{ms2min(ms)}min"


def _ok(section) -> bool:
    """段落可用判定：safe() 失败时返回 {'__error__': ...}，渲染层跳过。"""
    if not section:
        return False
    if isinstance(section, dict) and "__error__" in section:
        return False
    return True


def render_markdown(d: dict) -> str:
    L = []
    w = f"{fmt_ms(d['since'], '%Y-%m-%d') if d['since'] else '开始'} ~ " \
        f"{fmt_ms(d['until'], '%Y-%m-%d') if d['until'] else '现在'}"
    L.append(f"# UsageLab 使用复盘报告\n")
    L.append(f"> 时间窗：{w} ｜ 生成：{d['generated_at']}\n")

    ov = d.get("overview") or {}
    if _ok(ov):
        t = ov.get("turns") or {}
        dur = ov.get("duration") or {}
        tt = ov.get("ttft") or {}
        L.append("## 1. 总览\n")
        L.append(f"- 已完成轮次 **{t.get('n', 0)}**，累计 **{ms2min(t.get('total_ms'))} 分钟**"
                 f"（平均 {ms2min((t.get('total_ms') or 0) / max(t.get('n') or 1, 1))} 分钟/轮）")
        if dur:
            L.append(f"- 等待耗时：中位 {_sec(dur.get('p50'))} ｜ P90 **{_sec(dur.get('p90'))}**"
                     f" ｜ P99 {_sec(dur.get('p99'))} ｜ 最长 {_sec(dur.get('max'))}")
        if tt:
            L.append(f"- 首 token 等待（体感「开始动」）：中位 {_sec(tt.get('p50'))}"
                     f" ｜ P90 {_sec(tt.get('p90'))}")
        it, ot, ct = t.get("in_tok") or 0, t.get("out_tok") or 0, t.get("cache_tok") or 0
        L.append(f"- token：输入 {it/1e6:.1f}M（缓存命中 {ct/1e6:.1f}M）"
                 f"｜ 输出 {ot/1e6:.1f}M")
        sg = ov.get("signals") or {}
        cw = sg.get("cancel_wait") or {}
        L.append(f"- 体验信号：取消 {sg.get('cancelled', {}).get('n', 0)} 轮"
                 f"（取消前中位等待 {_sec(cw.get('p50'))}）｜ 上下文超限 {sg.get('context_exceeded', 0)}"
                 f" ｜ 报错轮 {sg.get('error_turns', 0)} ｜ 工具报错 {sg.get('tool_errors', 0)} 次\n")

        if dur:
            L.append("### 耗时分布\n")
            L.append("| 区间 | 轮数 |\n|---|---|")
            for b in ov.get("duration_buckets", []):
                L.append(f"| {b['label']} | {b['n']} |")
            L.append("")

    mp = d.get("model_profile") or []
    if _ok(mp):
        L.append("## 2. 模型画像（谁的限流少、谁更快）\n")
        L.append("| 模型 | 请求数 | 失败率 | 限流 | 耗时中位 | TTFT中位 | 均输出tok |")
        L.append("|---|---|---|---|---|---|---|")
        for r in mp:
            dd = r.get("dur") or {}
            tw = r.get("ttft") or {}
            L.append(f"| {r['model_id']} | {r['n']} | {r.get('fail_rate', 0)}% "
                     f"| {r.get('rate_limited', 0)} | {_sec(dd.get('p50'))} "
                     f"| {_sec(tw.get('p50'))} | {r.get('avg_out_tok', 0):.0f} |")
        L.append("")

    tools = d.get("tools") or []
    if _ok(tools):
        L.append("## 3. 工具画像\n")
        L.append("| 工具 | 次数 | 成功率 | 累计耗时 | 平均 |")
        L.append("|---|---|---|---|---|")
        for r in tools:
            L.append(f"| {r['tool_name']} | {r['n']} | {r['ok_rate']}% "
                     f"| {ms2min(r['total_ms'])}min | {r['avg_ms']/1000:.1f}s |")
        L.append("")

    cvd = d.get("context_vs_duration") or []
    if _ok(cvd):
        L.append("## 4. 上下文规模 × 耗时（会话越大越慢的证据）\n")
        L.append("| 单轮输入规模 | 轮数 | 中位耗时 | P90 |")
        L.append("|---|---|---|---|")
        for r in cvd:
            L.append(f"| {r['label']} | {r['n']} | {_sec(r.get('p50'))} | {_sec(r.get('p90'))} |")
        L.append("")

    db = d.get("dashboard") or {}
    if _ok(db) and db.get("weeks"):
        L.append("## 5. 周度仪表盘（训练验收：P90 < 3 分钟）\n")
        L.append(f"> 训练计划起始 {db.get('plan_start')}，目标线 P90 ≤ "
                 f"{db['p90_target_ms'] // 1000 // 60} 分钟；训练期达标 "
                 f"**{db.get('plan_weeks_met', 0)}/{db.get('plan_weeks_total', 0)}** 周\n")
        L.append("| 周 | 轮数 | P50 | P90 | 达标 | P90环比 | 取消率 | 不满信号 | 均上下文 |")
        L.append("|---|---|---|---|---|---|---|---|---|")
        for r in db["weeks"]:
            du = r.get("duration") or {}
            ok = "✅" if r.get("p90_target_met") else "❌"
            delta = f"{r['p90_delta_pct']:+.0f}%" if r.get("p90_delta_pct") is not None else "—"
            L.append(f"| {r['week']} | {r['n_turns']} | {_sec(du.get('p50'))} | "
                     f"**{_sec(du.get('p90'))}** | {ok} | {delta} | "
                     f"{r['cancel_rate_pct']}% | {r['signal_msgs']} | "
                     f"{r['avg_input_tokens'] / 1000:.0f}k |")
        L.append("")

    sess = d.get("sessions") or {}
    if _ok(sess):
        h = sess.get("hygiene") or {}
        L.append("## 6. 会话卫生与项目画像\n")
        L.append(f"- 巨型会话轮次占比（单轮输入 >1M token）：**{h.get('giant_share_pct', 0)}%**"
                 f"（{h.get('turns_in_giant_sessions', 0)} 轮）——训练目标：持续下降\n")
        L.append("### 时间都去哪了（按项目类别）\n")
        L.append("| 类别 | 轮次 | 累计耗时 | 输入token |")
        L.append("|---|---|---|---|")
        for c in sess.get("by_category", []):
            L.append(f"| {c['name']} | {c['turns']} | {ms2min(c['total_ms'])}min | {c['in_tok']/1e6:.1f}M |")
        L.append("")

    sg = d.get("signals") or {}
    if _ok(sg):
        L.append("## 7. 不满信号（隐式人类反馈）\n")
        L.append(f"- 用户消息 {sg.get('n_user_messages', 0)} 条，命中信号 "
                 f"{sum(sg.get('counts', {}).values())} 次，加权情绪分 "
                 f"**{sg.get('weighted_total', 0)}**（放弃/否定×3，纠错/重做×2，抱怨×1）\n")
        L.append("| 信号类别 | 次数 | 加权 |")
        L.append("|---|---|---|")
        for cat, n in sg.get("counts", {}).items():
            L.append(f"| {cat} | {n} | {sg['weighted'].get(cat, 0)} |")
        L.append("")
        for cat, samples in list(sg.get("samples", {}).items())[:3]:
            L.append(f"**[{cat}] 样本**")
            for s in samples[:3]:
                L.append(f"- 《{s['title'][:24]}》 {fmt_ms(s['time'])}：{s['text'][:60]}")
            L.append("")

    sl = d.get("slowest") or []
    if _ok(sl):
        L.append("## 8. 最慢轮次 Top15\n")
        L.append("| 耗时 | 时间 | 工具/请求 | 输入 | 会话 |")
        L.append("|---|---|---|---|---|")
        for r in sl:
            L.append(f"| {ms2min(r['duration_ms'])}min | {fmt_ms(r['started_at'])} "
                     f"| {r['tool_call_count']}/{r['model_request_count']} "
                     f"| {(r['input_tokens'] or 0)/1000:.0f}k | {(r['title'] or '?')[:30]} |")
        L.append("")

    L.append("---")
    L.append("*UsageLab · 数据源：ZCode 本地遥测库（只读） · "
             "口径与设计理由见 docs/architecture.md*")
    return "\n".join(L) + "\n"
