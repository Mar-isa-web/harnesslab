# -*- coding: utf-8 -*-
"""UsageLab 命令行入口。

用法：
  python cli.py report                      # 全量报告（md + html + 图表 + 快照）
  python cli.py report --since 2026-10-12   # 只看训练期
  python cli.py weekly                      # 只看周度仪表盘
  python cli.py models                      # 只看模型画像
  python cli.py signals                     # 只看不满信号
选项：
  --db PATH     覆盖默认数据库路径（默认 %USERPROFILE%\\.zcode\\cli\\db\\db.sqlite
                或环境变量 USAGELAB_DB）
  --out DIR     报告输出目录（默认 reports/）
"""
import argparse
import json
import os
import sys

from usagelab import load, metrics, models, signals, weekly
from usagelab.report import (build_report, render_markdown, save_snapshot,
                             list_snapshots, diff_snapshots)
from usagelab.report_html import render_html, make_charts


def _con(args):
    try:
        return load.connect(args.db or load.DEFAULT_DB)
    except FileNotFoundError as e:
        print(f"[错误] {e}")
        print("用 --db 指定 ZCode 遥测库路径，或设置环境变量 USAGELAB_DB")
        sys.exit(1)


def cmd_report(args):
    con = _con(args)
    since = load.parse_since(args.since)
    until = load.parse_until(args.until)
    data = build_report(con, since, until)

    out_dir = args.out or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "reports")
    os.makedirs(out_dir, exist_ok=True)
    stamp = load.datetime_now()

    md = render_markdown(data)
    md_path = os.path.join(out_dir, f"report_{stamp}.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)

    html_path = None
    if not args.no_html:
        charts = [] if args.no_charts else make_charts(
            data, os.path.join(out_dir, "charts"))
        html_path = os.path.join(out_dir, f"report_{stamp}.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(render_html(data, charts))

    snap_path = save_snapshot(data, os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "history"))

    print(f"[OK] 报告已生成：\n  {md_path}")
    if html_path:
        print(f"  {html_path}")
    print(f"  快照：{snap_path}")
    # 终端速览：仪表盘摘要
    db = data.get("dashboard") or {}
    if db.get("weeks"):
        print(f"\n训练验收：P90≤{db['p90_target_ms']//1000//60}min 达标 "
              f"{db.get('plan_weeks_met', 0)}/{db.get('plan_weeks_total', 0)} 周（自 {db['plan_start']}）")
        for r in db["weeks"][-6:]:
            du = r.get("duration") or {}
            flag = "✅" if r.get("p90_target_met") else "❌"
            print(f"  {r['week']}  {r['n_turns']:>3}轮  P90 "
                  f"{(du.get('p90') or 0)/1000:>5.0f}s  {flag}  "
                  f"取消 {r['cancel_rate_pct']}%  信号 {r['signal_msgs']}")
    con.close()


def cmd_weekly(args):
    con = _con(args)
    db = weekly.dashboard(con, load.parse_since(args.since),
                          load.parse_until(args.until))
    print(f"P90 目标 {db['p90_target_ms']//1000//60}min ｜ "
          f"训练期达标 {db['plan_weeks_met']}/{db['plan_weeks_total']} 周")
    print(f"{'周':<12}{'轮数':>5}{'P50':>8}{'P90':>8}  达标  取消率  信号")
    for r in db["weeks"]:
        du = r.get("duration") or {}
        flag = "✅" if r.get("p90_target_met") else "❌"
        print(f"{r['week']:<12}{r['n_turns']:>5}"
              f"{(du.get('p50') or 0)/1000:>7.0f}s{(du.get('p90') or 0)/1000:>7.0f}s"
              f"   {flag}   {r['cancel_rate_pct']:>5}%  {r['signal_msgs']}")
    con.close()


def cmd_models(args):
    con = _con(args)
    since, until = load.parse_since(args.since), load.parse_until(args.until)
    rows = models.model_profile(con, since, until)
    print(f"{'模型':<28}{'请求':>6}{'失败率':>8}{'限流':>6}{'耗时中位':>10}{'TTFT中位':>10}")
    for r in rows:
        dd, tw = r.get("dur") or {}, r.get("ttft") or {}
        print(f"{r['model_id']:<28}{r['n']:>6}{r.get('fail_rate', 0):>7}%"
              f"{r.get('rate_limited', 0):>6}"
              f"{(dd.get('p50') or 0)/1000:>9.0f}s{(tw.get('p50') or 0)/1000:>9.0f}s")
    con.close()


def cmd_signals(args):
    con = _con(args)
    s = signals.mine_signals(con, load.parse_since(args.since),
                             load.parse_until(args.until))
    print(f"用户消息 {s['n_user_messages']} 条 ｜ 加权情绪分 {s['weighted_total']}")
    for cat, n in s["counts"].items():
        print(f"  {cat:<14} {n:>4} 次（加权 {s['weighted'][cat]}）")
    con.close()


def cmd_diff(args):
    """对比最近两次快照（每周复盘的核心动作）。"""
    hist = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history")
    snaps = list_snapshots(hist)
    if len(snaps) < 2:
        print(f"快照不足 2 份（现有 {len(snaps)} 份）——先多跑几次 "
              f"`python cli.py report`，每周一次即可积累。")
        return
    with open(snaps[-2], encoding="utf-8") as f:
        old = json.load(f)
    with open(snaps[-1], encoding="utf-8") as f:
        new = json.load(f)
    print(f"对比：{old['generated_at']}  →  {new['generated_at']}\n")
    print(f"{'指标':<12}{'上次':>10}{'本次':>10}{'变化':>10}  趋势")
    for r in diff_snapshots(old, new):
        mark = "🟢 变好" if r["better"] else ("🔴 变差" if r["better"] is False else "")
        print(f"{r['name']:<12}{r['old']:>10}{r['new']:>10}"
              f"{r['delta_str']:>10}  {mark}")
    if args.all:
        print(f"\n全部快照（{len(snaps)} 份）：")
        for p in snaps:
            with open(p, encoding="utf-8") as f:
                s = json.load(f)
            p90 = s.get("p90_ms")
            p90_s = f"{p90/1000:.0f}s" if p90 else "—"
            print(f"  {s['generated_at']}  轮次 {s.get('n_turns')}  "
                  f"P90 {p90_s}  加权分 {s.get('signal_weighted_total')}")


def main():
    p = argparse.ArgumentParser(prog="usagelab",
                                description="AI 协作使用观测系统")
    p.add_argument("--db", default=None, help="ZCode 遥测库路径（默认自动探测）")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--since", default=None, help="起始日期 YYYY-MM-DD")
        sp.add_argument("--until", default=None, help="结束日期 YYYY-MM-DD")

    sp = sub.add_parser("report", help="全量报告（md/html/图表/快照）")
    common(sp)
    sp.add_argument("--out", default=None, help="输出目录")
    sp.add_argument("--no-html", action="store_true", help="只出 markdown")
    sp.add_argument("--no-charts", action="store_true", help="禁用图表（无 matplotlib 时自动禁用）")
    sp.set_defaults(func=cmd_report)

    sp = sub.add_parser("weekly", help="周度训练验收仪表盘")
    common(sp)
    sp.set_defaults(func=cmd_weekly)

    sp = sub.add_parser("models", help="模型画像")
    common(sp)
    sp.set_defaults(func=cmd_models)

    sp = sub.add_parser("signals", help="不满信号统计")
    common(sp)
    sp.set_defaults(func=cmd_signals)

    sp = sub.add_parser("diff", help="对比最近两次快照（周度复盘）")
    sp.add_argument("--all", action="store_true", help="附全部快照趋势列表")
    sp.set_defaults(func=cmd_diff)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
