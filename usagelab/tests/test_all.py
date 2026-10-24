# -*- coding: utf-8 -*-
"""UsageLab 离线测试：全部跑合成库，数字全部已知可硬断言。

运行：python tests/test_all.py
覆盖：只读保障 / 分位数 / 总览 / 模型画像 / 周分桶 / 训练达标 /
      信号挖掘（含系统注入过滤）/ 连发 / 会话卫生 / 项目归类 /
      报告渲染不崩 / 快照落盘。
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.build_fixture import build, W1, W2  # noqa: E402
from usagelab import load, metrics, models, sessions, signals, weekly  # noqa: E402
from usagelab.report import build_report, render_markdown, save_snapshot  # noqa: E402
from usagelab.report_html import render_html  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


def main():
    tmp = tempfile.mkdtemp(prefix="usagelab_t_")
    db = build(os.path.join(tmp, "fixture.db"))

    print("[组1] 只读保障")
    con = load.connect(db)
    try:
        con.execute("CREATE TABLE _x(a)")
        check("只读连接拒绝写入", False, "居然写成功了？!")
    except sqlite3.OperationalError:
        check("只读连接拒绝写入", True)
    try:
        load.connect(os.path.join(tmp, "nope.db"))
        check("不存在的库报 FileNotFoundError", False)
    except FileNotFoundError:
        check("不存在的库报 FileNotFoundError", True)

    print("[组2] 分位数函数")
    from usagelab.metrics import dist_summary, percentile
    s = dist_summary([10, 20, 30, 40])
    # 口径 = 旧 analyze 脚本的 int(n*p)（保守最近邻），与线上报告一致
    check("p50 int(n*p)=30", s["p50"] == 30, str(s))
    check("p90 int(n*p)=40", s["p90"] == 40, str(s))
    check("空序列返回 None", dist_summary([]) is None)
    check("percentile 越界安全", percentile([5], 0.99) == 5)

    print("[组3] 总览指标")
    ov = metrics.overview(con)
    t = ov["turns"]
    check("完成轮数=7", t["n"] == 7, str(t))
    check("累计时长=280s", t["total_ms"] == 280_000, str(t))
    check("取消轮=1", ov["signals"]["cancelled"]["n"] == 1)
    check("取消前等待 p50=60s", ov["signals"]["cancel_wait"]["p50"] == 60_000)
    durs = ov["duration"]
    check("全窗 p50=30s", durs["p50"] == 30_000, str(durs))
    check("全窗 p90=90s", durs["p90"] == 90_000, str(durs))

    print("[组4] 时间窗过滤")
    ov2 = metrics.overview(con, since=load.parse_since(W2))
    check("周2 窗口只剩 3 轮", ov2["turns"]["n"] == 3, str(ov2["turns"]))

    print("[组5] 模型画像")
    mp = {r["model_id"]: r for r in models.model_profile(con)}
    fl = mp["GLM-5.3-Flash"]
    check("Flash 请求数=4", fl["n"] == 4, str(fl))
    check("Flash 失败率=25%", fl["fail_rate"] == 25.0)
    eb = models.error_breakdown(con)
    check("错误表含 rate_limited 1 次",
          any(r["model_id"] == "GLM-5.3-Flash" and r["error_type"] == "rate_limited"
              and r["n"] == 1 for r in eb))
    mix = models.model_weekly_mix(con)
    wk2 = next(m for m in mix if m["week"] == W2)
    check("周2 模型构成只有 5.3 且=2", wk2["models"] == {"GLM-5.3": 2}, str(mix))

    print("[组6] 周分桶与训练达标")
    weeks = {w["week"]: w for w in weekly.weekly_turns(con)}
    check("恰好两周", set(weeks) == {W1, W2}, str(set(weeks)))
    check("周1 p90=40s", weeks[W1]["duration"]["p90"] == 40_000)
    check("周1 取消率 20%", weeks[W1]["cancel_rate_pct"] == 20.0)
    check("周1 均上下文含巨型轮",
          weeks[W1]["avg_input_tokens"] > 600_000, str(weeks[W1]))
    dbd = weekly.dashboard(con)
    ws = {w["week"]: w for w in dbd["weeks"]}
    check("两周均达标（p90<180s）",
          ws[W1]["p90_target_met"] and ws[W2]["p90_target_met"])
    check("训练期周数=2（计划起点 10-12 当周起）",
          dbd["plan_weeks_total"] == 2, str(dbd["plan_weeks_total"]))
    check("周2 有环比（40s→90s = +125%）",
          ws[W2].get("p90_delta_pct") == 125.0, str(ws[W2]))

    print("[组7] 信号挖掘")
    sg = signals.mine_signals(con)
    check("用户文本消息=3（过滤系统注入）", sg["n_user_messages"] == 3,
          str(sg["n_user_messages"]))
    check("视觉类=1", sg["counts"].get("视觉类抱怨") == 1, str(sg["counts"]))
    check("重做类=1", sg["counts"].get("重做-重新/再来") == 1, str(sg["counts"]))
    check("放弃类=1", sg["counts"].get("放弃信号") == 1, str(sg["counts"]))
    # u1「这张图片不对，重新生成」同时命中 视觉(1)+纠错(2)+重做(2)=5，
    # u2「算了，就这样吧」命中 放弃(3) → 加权总分 8（同一消息可命中多类）
    check("加权分=1+2+2+3=8", sg["weighted_total"] == 8, str(sg["weighted"]))
    check("信号周归属：周1=2条消息", sg["weekly"].get(W1) == 2, str(sg["weekly"]))

    print("[组8] 连发检测")
    bs = signals.burst_sessions(con)
    check("s1 有 1 次 90s 内连发", any(b["bursts"] == 1 for b in bs), str(bs))

    print("[组9] 会话画像与归类")
    sp = sessions.session_profile(con)
    check("两个会话", sp["n_sessions"] == 2)
    cats = {c["name"]: c for c in sp["by_category"]}
    check("数学建模类存在", "数模/国赛" in cats, str(list(cats)))
    check("PPT 类存在", "PPT/演示" in cats, str(list(cats)))
    hy = sp["hygiene"]
    # 语义：属于「曾出现单轮输入>1M 会话」的全部轮次占比（含取消轮）。
    # s1 曾达 3M → 它的 5 轮全算；s2 的 3 轮不算 → 5/8 = 62.5%
    check("巨型会话轮次占比=5/8=62.5%",
          hy["giant_share_pct"] == 62.5, str(hy))

    print("[组10] 报告渲染")
    since, until = None, None
    data = build_report(con, since, until)
    md = render_markdown(data)
    check("markdown 含仪表盘章节", "周度仪表盘" in md)
    check("markdown 含模型画像", "模型画像" in md)
    check("markdown 不含错误占位", "__error__" not in md)
    html_doc = render_html(data, charts=[])  # 测试不依赖 matplotlib
    check("html 生成且自包含", html_doc.startswith("<!DOCTYPE html>")
          and "UsageLab" in html_doc)
    snap = save_snapshot(data, os.path.join(tmp, "hist"))
    snap_data = json.load(open(snap, encoding="utf-8"))
    check("快照含核心指标", snap_data["n_turns"] == 7 and snap_data["p90_ms"] == 90_000,
          str(snap_data))

    print("[组11] 快照对比 diff")
    from usagelab.report import diff_snapshots, list_snapshots
    old = {"p90_ms": 120_000, "p50_ms": 60_000, "cancelled": 10,
           "signal_weighted_total": 50, "giant_share_pct": 80.0,
           "n_turns": 100, "total_min": 500.0, "ttft_p50_ms": 8_000}
    new = {"p90_ms": 90_000, "p50_ms": 60_000, "cancelled": 10,
           "signal_weighted_total": 25, "giant_share_pct": 75.0,
           "n_turns": 80, "total_min": 400.0, "ttft_p50_ms": 7_000}
    rows = {r["name"]: r for r in diff_snapshots(old, new)}
    check("P90 120s→90s 判定变好", rows["P90 等待"]["delta"] == -30_000
          and rows["P90 等待"]["better"] is True)
    check("加权分 50→25 判定变好", rows["不满加权分"]["better"] is True)
    check("P50 持平 delta=0 better=None",
          rows["P50 等待"]["delta"] == 0 and rows["P50 等待"]["better"] is None)
    check("轮数/分钟等工作量指标不着色",
          rows["完成轮数"]["better"] is None and rows["累计分钟"]["better"] is None)
    partial = {"p90_ms": 90_000}  # 缺字段的快照
    rows2 = {r["name"]: r for r in diff_snapshots(partial, new)}
    check("缺数据一侧显示 — 且不崩", rows2["不满加权分"]["old"] == "—")
    snaps = list_snapshots(os.path.join(tmp, "hist"))
    check("快照目录列出 1 份", len(snaps) == 1, str(snaps))

    con.close()
    shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
