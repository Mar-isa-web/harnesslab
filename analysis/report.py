# -*- coding: utf-8 -*-
"""M1 报告生成器：读遥测库，产出 markdown 复盘报告。

这是 reflect/ 三个分析脚本的移植版：轮次耗时/token 构成/工具画像/失败信号，
外加本项目特有的两节——基准通过率与模型对比（M2 双模型 A/B 的地基）。

用法：
    python analysis/report.py --db data/bench.db --out reports/baseline.md
"""
import argparse
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def pct(sorted_list, p):
    if not sorted_list:
        return 0
    return sorted_list[min(len(sorted_list) - 1, int(len(sorted_list) * p))]


def fmt_ms(ms):
    return f"{ms/1000:.1f}s" if ms is not None else "-"


def build_report(db_path):
    con = sqlite3.connect(db_path)
    L = []
    L.append(f"# HarnessLab 遥测复盘报告")
    L.append(f"\n> 数据源: `{db_path}` ｜ 生成时间: {time.strftime('%Y-%m-%d %H:%M')}\n")

    # ---- 1. 总览 ----
    n = con.execute("SELECT COUNT(*) FROM turn_usage").fetchone()[0]
    L.append("## 1. 总览\n")
    if n == 0:
        L.append("暂无轮次数据。\n")
        con.close()
        return "\n".join(L)
    statuses = con.execute(
        "SELECT status, COUNT(*) FROM turn_usage GROUP BY status ORDER BY 2 DESC").fetchall()
    durs = sorted(r[0] for r in con.execute(
        "SELECT duration_ms FROM turn_usage WHERE duration_ms IS NOT NULL"))
    in_tok, out_tok = con.execute(
        "SELECT SUM(input_tokens), SUM(output_tokens) FROM turn_usage").fetchone()
    L.append(f"- 轮次总数: **{n}**（" + "，".join(f"{s} {c}" for s, c in statuses) + "）")
    L.append(f"- 耗时: 中位 {fmt_ms(pct(durs, .5))} ｜ P90 {fmt_ms(pct(durs, .9))}"
             f" ｜ 最长 {fmt_ms(durs[-1] if durs else 0)}")
    L.append(f"- 累计 tokens: 输入 {(in_tok or 0)/1000:.1f}k ｜ 输出 {(out_tok or 0)/1000:.1f}k\n")

    # ---- 2. 工具画像 ----
    L.append("## 2. 工具画像\n")
    rows = con.execute(
        """SELECT tool_name, COUNT(*),
                  SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END),
                  AVG(duration_ms)
           FROM tool_usage GROUP BY tool_name ORDER BY 2 DESC""").fetchall()
    if rows:
        L.append("| 工具 | 调用次数 | 成功率 | 平均耗时 |")
        L.append("|---|---|---|---|")
        for name, cnt, okc, avg in rows:
            L.append(f"| {name} | {cnt} | {okc/max(cnt,1)*100:.0f}% | {fmt_ms(avg)} |")
        L.append("")
    else:
        L.append("暂无工具调用。\n")

    # ---- 3. 模型请求与失败信号 ----
    L.append("## 3. 模型请求与失败信号\n")
    req, retry = con.execute(
        "SELECT COUNT(*), SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) "
        "FROM model_usage").fetchone()
    L.append(f"- 模型请求共 {req} 次，其中失败 {(retry or 0)} 次")
    errs = con.execute(
        """SELECT error_type, COUNT(*) FROM model_usage
           WHERE error_type IS NOT NULL GROUP BY 1 ORDER BY 2 DESC""").fetchall()
    for et, c in errs:
        L.append(f"  - `{et}`: {c} 次")
    denied = con.execute(
        "SELECT COUNT(*) FROM tool_usage WHERE status='denied'").fetchone()[0]
    if denied:
        L.append(f"- 被权限门拒绝的工具调用: {denied} 次")
    L.append("")

    # ---- 4. 基准通过率 ----
    has_bench = con.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='benchmark_results'").fetchone()[0]
    if has_bench and con.execute("SELECT COUNT(*) FROM benchmark_results").fetchone()[0]:
        L.append("## 4. 基准通过率（按轮次）\n")
        L.append("| run_label | 模型 | 通过 | 任务数 | 通过率 | 总耗时 |")
        L.append("|---|---|---|---|---|---|")
        for row in con.execute(
                """SELECT run_label, model_id, SUM(passed), COUNT(*),
                          SUM(duration_ms)/1000.0
                   FROM benchmark_results GROUP BY run_label, model_id
                   ORDER BY time_created"""):
            label, model, npass, total, secs = row
            L.append(f"| {label} | {model} | {npass} | {total} | "
                     f"{npass/max(total,1)*100:.0f}% | {secs:.1f}s |")
        L.append("")
        fails = con.execute(
            """SELECT task_name, error_type FROM benchmark_results
               WHERE passed=0 ORDER BY time_created""").fetchall()
        if fails:
            L.append("失败任务明细: " + "，".join(
                f"{t}({e or '判分不过'})" for t, e in fails) + "\n")

    # ---- 5. 模型对比（M2 的地基）----
    models = con.execute(
        "SELECT DISTINCT model_id FROM model_usage").fetchall()
    if len(models) > 1:
        L.append("## 5. 模型对比\n")
        L.append("| 模型 | 请求数 | 失败 | 平均耗时 | 平均输出token |")
        L.append("|---|---|---|---|---|")
        for (mid,) in models:
            cnt, errc, avg_d, avg_o = con.execute(
                """SELECT COUNT(*), SUM(CASE WHEN status='error' THEN 1 ELSE 0 END),
                          AVG(duration_ms), AVG(output_tokens)
                   FROM model_usage WHERE model_id=?""", (mid,)).fetchone()
            L.append(f"| {mid} | {cnt} | {errc or 0} | {fmt_ms(avg_d)} | "
                     f"{(avg_o or 0):.0f} |")
        L.append("")

    con.close()
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="HarnessLab 复盘报告生成器")
    ap.add_argument("--db", default=os.path.join("data", "bench.db"))
    ap.add_argument("--out", default=os.path.join("reports", "baseline.md"))
    args = ap.parse_args()
    if not os.path.exists(args.db):
        print(f"库不存在: {args.db}")
        raise SystemExit(1)
    report = build_report(args.db)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"报告已生成: {args.out}")
    print(report)


if __name__ == "__main__":
    main()
