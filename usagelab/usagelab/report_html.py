# -*- coding: utf-8 -*-
"""HTML 报告渲染：单文件自包含（内联 CSS + base64 图表）。

markdown 版给人贴笔记，HTML 版给人「看得下去」——表格带样式、关键指标
大字号、图表直读。生成后双击即开，可直接发文件给别人。
图表依赖 matplotlib（可选）：没有则自动降级为纯表格，核心指标不缺。
"""
import base64
import html
import io
import os

from .report import _ok

E = html.escape


def _sec(ms):
    if ms is None:
        return "—"
    return f"{ms/1000:.0f}s" if ms < 120000 else f"{ms/60000:.1f}min"


def _min(ms):
    return f"{(ms or 0)/60000:.1f}"


def _table(headers, rows) -> str:
    h = "".join(f"<th>{E(str(x))}</th>" for x in headers)
    b = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<table><thead><tr>{h}</tr></thead><tbody>{b}</tbody></table>'


def _card(label, value, sub="") -> str:
    sub_html = f'<div class="sub">{E(sub)}</div>' if sub else ""
    return f'<div class="card"><div class="label">{E(label)}</div>' \
           f'<div class="value">{value}</div>{sub_html}</div>'


def make_charts(d: dict, out_dir: str | None = None) -> list[tuple[str, str]]:
    """生成图表，返回 [(标题, base64_png)]。out_dir 同时存 PNG 文件（可选）。
    matplotlib 不可用时返回空列表（调用方自动降级）。"""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return []
    # 中文字体：Windows 自带微软雅黑；找不到了就退 DejaVu（中文会变方框，
    # 数字和坐标轴仍可读，不影响结论）
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    charts: list[tuple[str, str]] = []

    def _finish(fig, name):
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
        plt.close(fig)
        b64 = base64.b64encode(buf.getvalue()).decode()
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
            with open(os.path.join(out_dir, name), "wb") as f:
                f.write(base64.b64decode(b64))
        charts.append((name, b64))

    # 图1：周度 P90 折线 + 3 分钟目标线（训练仪表盘的核心图）
    db = d.get("dashboard") or {}
    weeks = db.get("weeks") or []
    if weeks:
        xs = [w["week"][5:] for w in weeks]
        ys = [(w.get("duration") or {}).get("p90") for w in weeks]
        fig, ax = plt.subplots(figsize=(8, 3.2))
        ax.plot(xs, [y / 1000 if y else None for y in ys], marker="o",
                label="P90 等待", color="#2563eb")
        ax.axhline(db.get("p90_target_ms", 180000) / 1000, ls="--", color="#dc2626",
                   label="目标线 3min")
        ax.set_ylabel("秒")
        ax.set_title("每周 P90 单轮等待 vs 训练目标")
        ax.legend()
        ax.grid(alpha=.3)
        _finish(fig, "weekly_p90.png")

    # 图2：项目类别耗时条形图
    sess = d.get("sessions") or {}
    cats = (sess.get("by_category") or [])[:10]
    if cats:
        fig, ax = plt.subplots(figsize=(8, 3.2))
        names = [c["name"] for c in cats][::-1]
        vals = [(c["total_ms"] or 0) / 60000 for c in cats][::-1]
        ax.barh(names, vals, color="#0891b2")
        ax.set_xlabel("分钟")
        ax.set_title("时间都去哪了（按项目类别）")
        _finish(fig, "categories.png")

    # 图3：上下文规模 × 耗时（中位 + P90 双线）
    cvd = d.get("context_vs_duration") or []
    if cvd:
        fig, ax = plt.subplots(figsize=(8, 3.2))
        xs = [r["label"] for r in cvd]
        ax.plot(xs, [r.get("p50", 0) / 1000 for r in cvd], marker="s", label="中位")
        ax.plot(xs, [r.get("p90", 0) / 1000 for r in cvd], marker="^", label="P90")
        ax.set_ylabel("秒")
        ax.set_title("上下文越大越慢（单轮输入规模 × 耗时）")
        ax.legend()
        ax.grid(alpha=.3)
        _finish(fig, "context_vs_duration.png")

    # 图4：模型请求量按周堆叠（迁移轨迹）
    mix = d.get("model_mix") or []
    if mix:
        fig, ax = plt.subplots(figsize=(8, 3.2))
        weeks_x = [m["week"][5:] for m in mix]
        model_ids = sorted({mid for m in mix for mid in m["models"]})
        bottom = [0] * len(mix)
        palette = ["#2563eb", "#f59e0b", "#10b981", "#ef4444", "#8b5cf6"]
        for i, mid in enumerate(model_ids):
            vals = [m["models"].get(mid, 0) for m in mix]
            ax.bar(weeks_x, vals, bottom=bottom, label=mid,
                   color=palette[i % len(palette)])
            bottom = [b + v for b, v in zip(bottom, vals)]
        ax.set_ylabel("模型请求数")
        ax.set_title("每周模型使用构成（迁移轨迹）")
        ax.legend(fontsize=8)
        _finish(fig, "model_mix.png")

    return charts


_CSS = """
body{font-family:'Microsoft YaHei',system-ui,sans-serif;max-width:960px;margin:24px auto;
     padding:0 16px;color:#1e293b;line-height:1.6}
h1{font-size:26px;border-bottom:3px solid #2563eb;padding-bottom:8px}
h2{font-size:19px;margin-top:32px;border-left:4px solid #2563eb;padding-left:10px}
.meta{color:#64748b;font-size:13px}
.cards{display:flex;flex-wrap:wrap;gap:12px;margin:16px 0}
.card{background:#f1f5f9;border-radius:10px;padding:12px 18px;min-width:140px}
.card .label{font-size:12px;color:#64748b}
.card .value{font-size:22px;font-weight:700;color:#0f172a}
.card .sub{font-size:11px;color:#94a3b8}
table{border-collapse:collapse;width:100%;font-size:13px;margin:10px 0}
th{background:#1e293b;color:#fff;padding:6px 10px;text-align:left;font-weight:500}
td{border-bottom:1px solid #e2e8f0;padding:5px 10px}
tr:hover td{background:#f8fafc}
img{max-width:100%;border:1px solid #e2e8f0;border-radius:8px;margin:6px 0}
.ok{color:#16a34a;font-weight:700}.bad{color:#dc2626;font-weight:700}
.warn{background:#fef3c7;padding:8px 12px;border-radius:8px;font-size:13px}
footer{color:#94a3b8;font-size:12px;margin-top:40px;border-top:1px solid #e2e8f0;padding-top:10px}
"""


def render_html(d: dict, charts: list[tuple[str, str]] | None = None) -> str:
    charts = charts if charts is not None else make_charts(d)
    ov = d.get("overview") or {}
    t = ov.get("turns") or {}
    dur = ov.get("duration") or {}
    tt = ov.get("ttft") or {}
    sg = (ov.get("signals") or {})

    H = ['<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">',
         f"<title>UsageLab 复盘报告 {E(d['generated_at'])}</title>",
         f"<style>{_CSS}</style></head><body>"]
    w = f"{d['since'] and E(str(d['since'])) or '开始'} ~ {d['until'] or '现在'}"
    H.append(f"<h1>UsageLab 使用复盘报告</h1>"
             f"<div class='meta'>时间窗 {E(str(w))} ｜ 生成于 {E(d['generated_at'])}</div>")

    # 指标卡
    cards = [
        _card("完成轮次", t.get("n") or 0, f"累计 {_min(t.get('total_ms'))} 分钟"),
        _card("P90 等待", _sec(dur.get("p90")), f"中位 {_sec(dur.get('p50'))}"),
        _card("首token中位", _sec(tt.get("p50")), "体感「开始动」"),
        _card("取消轮", (sg.get("cancelled") or {}).get("n") or 0,
              f"取消前中位等 {_sec((sg.get('cancel_wait') or {}).get('p50'))}"),
    ]
    H.append(f"<div class='cards'>{''.join(cards)}</div>")

    db = d.get("dashboard") or {}
    if _ok(db) and db.get("weeks"):
        H.append("<h2>周度仪表盘 · 训练验收</h2>")
        H.append(f"<div class='warn'>目标：P90 ≤ {db['p90_target_ms']//1000//60} 分钟 ｜ "
                 f"训练期（自 {E(db['plan_start'])}）达标 "
                 f"<b>{db.get('plan_weeks_met')}/{db.get('plan_weeks_total')}</b> 周</div>")
        rows = []
        for r in db["weeks"]:
            du = r.get("duration") or {}
            flag = "<span class='ok'>✅</span>" if r.get("p90_target_met") \
                else "<span class='bad'>❌</span>"
            delta = f"{r['p90_delta_pct']:+.0f}%" if r.get("p90_delta_pct") is not None else "—"
            rows.append([E(r["week"]), r["n_turns"], _sec(du.get("p50")),
                         f"<b>{_sec(du.get('p90'))}</b>", flag, delta,
                         f"{r['cancel_rate_pct']}%", r["signal_msgs"],
                         f"{r['avg_input_tokens']/1000:.0f}k"])
        H.append(_table(["周", "轮数", "P50", "P90", "达标", "环比", "取消率",
                         "不满信号", "均上下文"], rows))

    mp = d.get("model_profile") or []
    if _ok(mp):
        H.append("<h2>模型画像</h2>")
        rows = []
        for r in mp:
            dd = r.get("dur") or {}
            tw = r.get("ttft") or {}
            rows.append([E(r["model_id"]), r["n"], f"{r.get('fail_rate', 0)}%",
                         r.get("rate_limited", 0), _sec(dd.get("p50")),
                         _sec(tw.get("p50")), f"{r.get('avg_out_tok', 0):.0f}"])
        H.append(_table(["模型", "请求数", "失败率", "限流", "耗时中位",
                         "TTFT中位", "均输出tok"], rows))

    tools = d.get("tools") or []
    if _ok(tools):
        H.append("<h2>工具画像</h2>")
        rows = [[E(r["tool_name"]), r["n"], f"{r['ok_rate']}%",
                 _min(r["total_ms"]) + "min", f"{r['avg_ms']/1000:.1f}s"]
                for r in tools]
        H.append(_table(["工具", "次数", "成功率", "累计", "平均"], rows))

    sess = d.get("sessions") or {}
    if _ok(sess):
        h = sess.get("hygiene") or {}
        H.append("<h2>会话卫生与项目画像</h2>")
        H.append(f"<div class='warn'>巨型会话轮次占比（单轮输入&gt;1M token）："
                 f"<b>{h.get('giant_share_pct', 0)}%</b>"
                 f"（{h.get('turns_in_giant_sessions', 0)} 轮）——目标：持续下降</div>")
        rows = [[E(c["name"]), c["turns"], _min(c["total_ms"]) + "min",
                 f"{c['in_tok']/1e6:.1f}M"] for c in sess.get("by_category", [])]
        H.append(_table(["类别", "轮次", "累计耗时", "输入token"], rows))

    sgd = d.get("signals") or {}
    if _ok(sgd):
        H.append("<h2>不满信号（隐式人类反馈）</h2>")
        H.append(f"<div class='meta'>用户消息 {sgd.get('n_user_messages', 0)} 条，"
                 f"加权情绪分 <b>{sgd.get('weighted_total', 0)}</b>"
                 f"（放弃/否定×3 ｜ 纠错/重做×2 ｜ 抱怨×1）</div>")
        rows = [[E(cat), n, sgd["weighted"].get(cat, 0)]
                for cat, n in sgd.get("counts", {}).items()]
        H.append(_table(["信号类别", "次数", "加权"], rows))

    cvd = d.get("context_vs_duration") or []
    if _ok(cvd):
        H.append("<h2>上下文规模 × 耗时</h2>")
        rows = [[E(r["label"]), r["n"], _sec(r.get("p50")), _sec(r.get("p90"))]
                for r in cvd]
        H.append(_table(["单轮输入规模", "轮数", "中位", "P90"], rows))

    sl = d.get("slowest") or []
    if _ok(sl):
        H.append("<h2>最慢轮次 Top15</h2>")
        from .load import fmt_ms
        rows = [[_min(r["duration_ms"]) + "min", E(fmt_ms(r["started_at"])),
                 f"{r['tool_call_count']}/{r['model_request_count']}",
                 f"{(r['input_tokens'] or 0)/1000:.0f}k",
                 E((r["title"] or "?")[:34])] for r in sl]
        H.append(_table(["耗时", "时间", "工具/请求", "输入", "会话"], rows))

    if charts:
        H.append("<h2>图表</h2>")
        for name, b64 in charts:
            H.append(f"<img alt='{E(name)}' src='data:image/png;base64,{b64}'>")

    H.append(f"<footer>UsageLab · 数据源：ZCode 本地遥测库（只读） · "
             f"生成 {E(d['generated_at'])}</footer></body></html>")
    return "".join(H)
