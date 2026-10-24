# -*- coding: utf-8 -*-
"""遥测层：把主循环的每一步沉淀到 SQLite。

表结构刻意对齐 ZCode CLI 本地库（session/turn_usage/model_usage/tool_usage），
理由见 docs/architecture.md 第 4 节——reflect/ 里的分析脚本可低成本复用，
且观测维度（重试/取消/首token/上下文超限）直接借鉴生产设计。

约定：所有时间戳为毫秒整数；status 取值 completed/running/error/denied/cancelled。
"""
import os
import sqlite3
import time
import uuid

SCHEMA = """
CREATE TABLE IF NOT EXISTS session(
  id          TEXT PRIMARY KEY,   -- sess_xxx
  title       TEXT,
  directory   TEXT,               -- 工作目录（复刻 zcode：按目录归因任务）
  time_created INTEGER,
  time_ended  INTEGER);

CREATE TABLE IF NOT EXISTS turn_usage(
  turn_id     TEXT PRIMARY KEY,   -- turn_xxx：一轮"任务"（一个 user task 到结束）
  session_id  TEXT,
  status      TEXT,               -- running/completed/error/cancelled/max_steps
  started_at  INTEGER,
  completed_at INTEGER,
  duration_ms INTEGER,
  input_tokens  INTEGER DEFAULT 0,
  output_tokens INTEGER DEFAULT 0,
  reasoning_tokens INTEGER DEFAULT 0,        -- 推理型模型的思考 token（面板 token 构成用）
  cache_read_input_tokens INTEGER DEFAULT 0, -- 服务端缓存命中
  time_to_first_token_ms INTEGER,            -- 接流式后启用，非流式为 NULL
  tool_call_count      INTEGER DEFAULT 0,
  tool_error_count     INTEGER DEFAULT 0,
  model_request_count  INTEGER DEFAULT 0,
  model_retry_count    INTEGER DEFAULT 0,
  cancelled_by_user  INTEGER DEFAULT 0,
  context_exceeded   INTEGER DEFAULT 0,
  retry_policy      TEXT,           -- immediate / exp-backoff（M2 A/B 分组标注）
  context_strategy  TEXT,           -- naive-truncate / sliding-window
  error_type   TEXT,
  error_code   TEXT,
  error_message TEXT);

CREATE TABLE IF NOT EXISTS model_usage(
  id          TEXT PRIMARY KEY,   -- req_xxx：一次模型请求（含失败的尝试）
  logical_request_id TEXT,        -- 同一逻辑请求的重试共享此 id
  attempt_index INTEGER,          -- 第几次尝试（0 起）
  session_id  TEXT,
  turn_id     TEXT,
  provider_id TEXT,               -- mock / openai-compat
  model_id    TEXT,               -- glm-5.3-flash / deepseek-chat / ...
  status      TEXT,
  started_at  INTEGER,
  completed_at INTEGER,
  duration_ms INTEGER,
  time_to_first_token_ms INTEGER, -- M0 非流式为 NULL，M3+ 接流式后启用
  finish_reason TEXT,             -- stop / tool-calls
  tool_call_count INTEGER,
  input_tokens  INTEGER,
  output_tokens INTEGER,
  error_type   TEXT,
  error_message TEXT);

CREATE TABLE IF NOT EXISTS tool_usage(
  id          TEXT PRIMARY KEY,   -- tool_xxx：一次工具调用
  turn_id     TEXT,
  tool_name   TEXT,
  status      TEXT,               -- completed / error / denied
  started_at  INTEGER,
  completed_at INTEGER,
  duration_ms INTEGER,
  attempt     INTEGER DEFAULT 0,  -- M2 重试改造后启用
  error_message TEXT);
"""


def _now() -> int:
    return int(time.time() * 1000)


class Telemetry:
    def __init__(self, db_path: str):
        parent = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(parent, exist_ok=True)
        self.con = sqlite3.connect(db_path)
        self.con.executescript(SCHEMA)
        self._migrate()
        self.con.commit()

    def _migrate(self):
        """轻量迁移：老库补齐新增列（CREATE IF NOT EXISTS 不会改已有表）。"""
        cols = {r[1] for r in self.con.execute("PRAGMA table_info(turn_usage)")}
        for col, ddl in (
            ("retry_policy", "TEXT"),
            ("context_strategy", "TEXT"),
            ("reasoning_tokens", "INTEGER DEFAULT 0"),
            ("cache_read_input_tokens", "INTEGER DEFAULT 0"),
            ("time_to_first_token_ms", "INTEGER"),
            ("tool_error_count", "INTEGER DEFAULT 0"),
            ("error_code", "TEXT"),
        ):
            if col not in cols:
                self.con.execute(
                    f"ALTER TABLE turn_usage ADD COLUMN {col} {ddl}")

    @staticmethod
    def _id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:12]}"

    # ---- session ----
    def session_start(self, title: str, directory: str) -> str:
        sid = self._id("sess")
        self.con.execute(
            "INSERT INTO session(id, title, directory, time_created) VALUES(?,?,?,?)",
            (sid, title, directory, _now()))
        self.con.commit()
        return sid

    def session_end(self, sid: str):
        self.con.execute("UPDATE session SET time_ended=? WHERE id=?", (_now(), sid))
        self.con.commit()

    # ---- turn ----
    def turn_start(self, session_id: str) -> str:
        tid = self._id("turn")
        self.con.execute(
            "INSERT INTO turn_usage(turn_id, session_id, status, started_at) VALUES(?,?,?,?)",
            (tid, session_id, "running", _now()))
        self.con.commit()
        return tid

    def turn_end(self, turn_id: str, status: str = "completed", **kw):
        self.con.execute(
            """UPDATE turn_usage SET status=?, completed_at=?, duration_ms=?,
               input_tokens=?, output_tokens=?, reasoning_tokens=?,
               cache_read_input_tokens=?, time_to_first_token_ms=?,
               tool_call_count=?, tool_error_count=?,
               model_request_count=?, model_retry_count=?,
               cancelled_by_user=?, context_exceeded=?,
               retry_policy=?, context_strategy=?,
               error_type=?, error_code=?, error_message=? WHERE turn_id=?""",
            (status, _now(), kw.get("duration_ms", 0),
             kw.get("input_tokens", 0), kw.get("output_tokens", 0),
             kw.get("reasoning_tokens", 0), kw.get("cache_read_input_tokens", 0),
             kw.get("time_to_first_token_ms"),
             kw.get("tool_call_count", 0), kw.get("tool_error_count", 0),
             kw.get("model_request_count", 0),
             kw.get("model_retry_count", 0),
             1 if kw.get("cancelled_by_user") else 0,
             1 if kw.get("context_exceeded") else 0,
             kw.get("retry_policy"), kw.get("context_strategy"),
             kw.get("error_type"), kw.get("error_code"),
             kw.get("error_message"), turn_id))
        self.con.commit()

    # ---- model request（每次尝试一行，成功失败都记）----
    def model_request(self, session_id, turn_id, provider_id, model_id, status,
                      started_at, duration_ms, *, logical_request_id=None,
                      attempt_index=0, input_tokens=0, output_tokens=0,
                      finish_reason=None, tool_call_count=0,
                      error_type=None, error_message=None) -> str:
        rid = self._id("req")
        lrid = logical_request_id or rid
        self.con.execute(
            """INSERT INTO model_usage(id, logical_request_id, attempt_index,
               session_id, turn_id, provider_id, model_id, status,
               started_at, completed_at, duration_ms, time_to_first_token_ms,
               finish_reason, tool_call_count, input_tokens, output_tokens,
               error_type, error_message)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?,?,?,?)""",
            (rid, lrid, attempt_index, session_id, turn_id, provider_id, model_id,
             status, started_at, started_at + duration_ms, duration_ms,
             finish_reason, tool_call_count, input_tokens, output_tokens,
             error_type, error_message))
        self.con.commit()
        return rid

    # ---- tool call ----
    def tool_call(self, turn_id, tool_name, status, started_at, duration_ms,
                  error_message=None, attempt=0) -> str:
        rid = self._id("tool")
        self.con.execute(
            "INSERT INTO tool_usage VALUES(?,?,?,?,?,?,?,?,?)",
            (rid, turn_id, tool_name, status, started_at,
             started_at + duration_ms, duration_ms, attempt, error_message))
        self.con.commit()
        return rid

    def close(self):
        self.con.close()
