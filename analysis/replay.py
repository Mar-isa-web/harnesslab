# -*- coding: utf-8 -*-
"""事件流回放器：把遥测四表按毫秒时间轴合并，还原一次或全部任务的完整过程。

这是 reflect 精神的核心工具：出问题时不是猜，而是把时间轴摊开看。
    python analysis/replay.py --db data/bench.db
    python analysis/replay.py --db data/bench.db --turn turn_xxx
"""
import argparse
import datetime
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def ts(ms):
    return datetime.datetime.fromtimestamp(ms / 1000).strftime("%H:%M:%S.%f")[:-3]


def replay(db_path, turn_id=None):
    con = sqlite3.connect(db_path)
    events = []
    for sid, title, t0 in con.execute(
            "SELECT id, title, time_created FROM session"):
        events.append((t0, f"SESSION {sid} 《{(title or '?')[:36]}》"))
    q = ("SELECT turn_id, status, started_at, duration_ms, tool_call_count, "
         "model_request_count, model_retry_count, error_type "
         "FROM turn_usage" + (" WHERE turn_id=?" if turn_id else ""))
    for t in con.execute(q, (turn_id,) if turn_id else ()):
        events.append((t[2], f"  TURN {t[0]} 开始"))
        events.append((t[2] + (t[3] or 0), 
                       f"  TURN 结束 {t[1]} {t[3]}ms 工具{t[4]}次 "
                       f"请求{t[5]}次 重试{t[6]}次" + (f" err={t[7]}" if t[7] else "")))
    q = ("SELECT turn_id, started_at, provider_id, model_id, status, duration_ms, "
         "finish_reason, tool_call_count, attempt_index, error_type FROM model_usage"
         + (" WHERE turn_id=?" if turn_id else ""))
    for m in con.execute(q, (turn_id,) if turn_id else ()):
        line = (f"    MODEL {m[2]}/{m[3]} {m[4]} {m[5]}ms finish={m[6]} "
                f"tool_calls={m[7]} attempt={m[8]}")
        if m[9]:
            line += f" err={m[9]}"
        events.append((m[1], line))
    q = ("SELECT turn_id, started_at, tool_name, status, duration_ms, error_message "
         "FROM tool_usage" + (" WHERE turn_id=?" if turn_id else ""))
    for u in con.execute(q, (turn_id,) if turn_id else ()):
        line = f"    TOOL  {u[2]} {u[3]} {u[4]}ms"
        if u[5]:
            line += f"  {u[5][:60]}"
        events.append((u[1], line))

    if not events:
        print("无数据。")
        return
    for t, line in sorted(events):
        print(f"{ts(t)}  {line}")
    con.close()


def main():
    ap = argparse.ArgumentParser(description="HarnessLab 事件流回放器")
    ap.add_argument("--db", default=os.path.join("data", "bench.db"))
    ap.add_argument("--turn", default=None, help="只回放指定 turn_id")
    args = ap.parse_args()
    if not os.path.exists(args.db):
        print(f"库不存在: {args.db}")
        raise SystemExit(1)
    replay(args.db, args.turn)


if __name__ == "__main__":
    main()
