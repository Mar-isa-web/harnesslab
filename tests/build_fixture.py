# -*- coding: utf-8 -*-
"""合成遥测库构造器：造一个与 ZCode 真实 schema 同构的小库。

为什么测试要自建库而不是跑真库：
1. 真库数据天天变，断言写不了「P90 必须等于 4.0s」这种硬数字；
2. 合成库里每个数字都是已知的，指标算错必然被抓住；
3. 不碰生产数据——测试和工具一样守只读戒律。
"""
import json
import os
import sqlite3
import tempfile

# 与真实 ZCode db.sqlite 同构的最小列集（只建被查询用到的列）
SCHEMA = """
CREATE TABLE session (
    id TEXT PRIMARY KEY, directory TEXT, title TEXT,
    time_created INTEGER, time_updated INTEGER
);
CREATE TABLE message (
    id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER,
    data TEXT, sequence INTEGER
);
CREATE TABLE part (
    id INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT, session_id TEXT,
    time_created INTEGER, data TEXT, sequence INTEGER
);
CREATE TABLE turn_usage (
    session_id TEXT, turn_id TEXT, status TEXT, started_at INTEGER,
    completed_at INTEGER, duration_ms INTEGER, time_to_first_token_ms INTEGER,
    model_request_count INTEGER, model_retry_count INTEGER,
    tool_call_count INTEGER, tool_error_count INTEGER,
    input_tokens INTEGER, output_tokens INTEGER, reasoning_tokens INTEGER,
    cache_read_input_tokens INTEGER, cancelled_by_user INTEGER,
    context_exceeded INTEGER, error_type TEXT, error_code TEXT
);
CREATE TABLE tool_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, turn_id TEXT,
    tool_name TEXT, status TEXT, started_at INTEGER, completed_at INTEGER,
    duration_ms INTEGER
);
CREATE TABLE model_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT, logical_request_id TEXT,
    attempt_index INTEGER, session_id TEXT, turn_id TEXT, model_id TEXT,
    status TEXT, started_at INTEGER, completed_at INTEGER, duration_ms INTEGER,
    time_to_first_token_ms INTEGER, input_tokens INTEGER, output_tokens INTEGER,
    error_type TEXT
);
"""

# 已知的测试常量：所有断言以这些数字为准
W1 = "2026-10-12"   # 周一
W2 = "2026-10-19"   # 周一


def ts(date: str, hh=10, mm=0):
    """'2026-10-12', 10, 30 -> 本地毫秒时间戳"""
    import datetime
    from usagelab.load import LOCAL_TZ
    dt = datetime.datetime.strptime(date, "%Y-%m-%d").replace(
        hour=hh, minute=mm, tzinfo=LOCAL_TZ)
    return int(dt.timestamp() * 1000)


def user_msg(con, sid, mid, created, text):
    con.execute("INSERT INTO message(id,session_id,time_created,data,sequence) "
                "VALUES (?,?,?,?,1)",
                (mid, sid, created,
                 json.dumps({"role": "user"})))
    con.execute("INSERT INTO part(message_id,session_id,time_created,data,sequence) "
                "VALUES (?,?,?,? ,1)",
                (mid, sid, created,
                 json.dumps({"type": "text", "text": text})))


def build(path: str):
    if os.path.exists(path):
        os.remove(path)
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)

    con.execute("INSERT INTO session(id,directory,title,time_created) VALUES "
                "(?,?,?,?)",
                ("s1", "C:/proj/数学建模", "数学建模国赛B题", ts(W1, 9)))
    con.execute("INSERT INTO session(id,directory,title,time_created) VALUES "
                "(?,?,?,?)",
                ("s2", "C:/proj/ppt", "形势与政策PPT", ts(W1, 20)))

    # 周1：完成轮耗时 [10,20,30,40]s → p50=20s p90=40s；1 轮取消（等了 60s）
    for i, d in enumerate([10_000, 20_000, 30_000, 40_000]):
        t = ts(W1, 10 + i)
        con.execute("INSERT INTO turn_usage(session_id,started_at,completed_at,"
                    "duration_ms,time_to_first_token_ms,input_tokens,"
                    "model_request_count,tool_call_count) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    ("s1", t, t + d, d, d // 10, 100_000 + i * 50_000, 1, 2))
    con.execute("INSERT INTO turn_usage(session_id,started_at,duration_ms,"
                "cancelled_by_user,input_tokens) VALUES (?,?,?,?,?)",
                ("s1", ts(W1, 15), 60_000, 1, 3_000_000))  # 巨型会话里的取消轮

    # 周2：完成轮 [30,60,90]s → p50=60s p90=90s（达标线内仍是 90s<180s ✅）
    for i, d in enumerate([30_000, 60_000, 90_000]):
        t = ts(W2, 11 + i)
        con.execute("INSERT INTO turn_usage(session_id,started_at,completed_at,"
                    "duration_ms,input_tokens,model_request_count) "
                    "VALUES (?,?,?,?,?,?)", ("s2", t, t + d, d, 50_000, 2))

    # 模型请求：Flash 4 次（1 次 rate_limited）、5.3 2 次（全成）
    rows = [
        ("m1", 0, "s1", "GLM-5.3-Flash", "completed", ts(W1, 10), 12_000, 2_000),
        ("m2", 0, "s1", "GLM-5.3-Flash", "error", ts(W1, 10, 30), 1_000, None),
        ("m3", 1, "s1", "GLM-5.3-Flash", "completed", ts(W1, 10, 31), 9_000, 1_500),
        ("m4", 0, "s1", "GLM-5.3-Flash", "completed", ts(W1, 11), 8_000, 1_000),
        ("m5", 0, "s2", "GLM-5.3", "completed", ts(W2, 11), 40_000, 4_000),
        ("m6", 0, "s2", "GLM-5.3", "completed", ts(W2, 12), 35_000, 3_000),
    ]
    for rid, att, sid, model, status, t, dur, ttft in rows:
        err = "rate_limited" if status == "error" else None
        out_tok = 500 if status == "completed" else 0
        con.execute("INSERT INTO model_usage(logical_request_id,attempt_index,"
                    "session_id,model_id,status,started_at,duration_ms,"
                    "time_to_first_token_ms,output_tokens,error_type) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (rid, att, sid, model, status, t, dur, ttft, out_tok, err))

    # 工具：read 3 次全成、write 1 次失败
    for i in range(3):
        con.execute("INSERT INTO tool_usage(session_id,tool_name,status,"
                    "started_at,completed_at,duration_ms) VALUES (?,?,?,?,?,?)",
                    ("s1", "read_file", "completed", ts(W1, 10),
                     ts(W1, 10) + 500, 500))
    con.execute("INSERT INTO tool_usage(session_id,tool_name,status,"
                "started_at,completed_at,duration_ms) VALUES (?,?,?,?,?,?)",
                ("s1", "write_file", "error", ts(W1, 11), ts(W1, 11) + 200, 200))

    # 用户消息：信号 + 连发
    user_msg(con, "s1", "u1", ts(W1, 12), "这张图片不对，重新生成")
    user_msg(con, "s1", "u2", ts(W1, 12, 1), "算了，就这样吧")     # 60s 内连发
    user_msg(con, "s2", "u3", ts(W2, 13), "普通消息，没有信号")
    user_msg(con, "s2", "u4", ts(W2, 14), "[@plugin] 系统注入，不算")

    con.commit()
    con.close()
    return path


def build_tmp():
    """在系统临时目录造库，返回路径（测试结束由调用方清理目录）。"""
    d = tempfile.mkdtemp(prefix="usagelab_test_")
    return build(os.path.join(d, "fixture.db"))
