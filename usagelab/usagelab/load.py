# -*- coding: utf-8 -*-
"""数据装载层：只读连接 + 时间过滤 + 列容错。

设计决策（为什么这么写）：
1. 只读 URI（mode=ro）——观测工具的第一戒律是绝不写坏生产库。ZCode 的 db.sqlite
   是用户全部会话历史的唯一载体，任何一次误写都不可逆。
2. 时间过滤在 SQL 层做（started_at BETWEEN）——把 1379+ 轮全拉进内存再筛，
   数据量涨十倍后就会变慢；筛选下推到数据库是观测系统的基本功。
3. 每个 section 的查询独立 try/except——不同版本 ZCode 的 schema 有差异
   （列增减），缺列时该节显示"数据不可用"而不是整个报告崩掉。
   观测系统的可用性 > 单个指标的完备性。
"""
import os
import sqlite3
from datetime import datetime, timezone, timedelta

# 本机默认路径；可用环境变量 USAGELAB_DB 或 --db 覆盖
DEFAULT_DB = os.environ.get(
    "USAGELAB_DB",
    os.path.expanduser(r"~\.zcode\cli\db\db.sqlite"),
)

# 本地时区（Windows 无 tz 数据库时的兜底：固定 +08:00，ZCode 时间戳本身是
# 毫秒 epoch，转本地日界只需一致偏移即可，不依赖系统 tz 设置）
LOCAL_TZ = timezone(timedelta(hours=8))


def connect(db_path: str = DEFAULT_DB) -> sqlite3.Connection:
    """以只读模式打开 ZCode 数据库。文件不存在时抛 FileNotFoundError。"""
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"数据库不存在: {db_path}")
    uri = f"file:{db_path.replace(chr(92), '/')}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


def parse_since(since: str | None) -> int | None:
    """'2026-09-12' -> 毫秒 epoch（本地时区当日 00:00）。"""
    if not since:
        return None
    dt = datetime.strptime(since, "%Y-%m-%d")
    return int(dt.replace(tzinfo=LOCAL_TZ).timestamp() * 1000)


def parse_until(until: str | None) -> int | None:
    """'2026-10-24' -> 毫秒 epoch（当日 23:59:59.999，含端点）。"""
    if not until:
        return None
    dt = datetime.strptime(until, "%Y-%m-%d")
    end = dt.replace(tzinfo=LOCAL_TZ) + timedelta(days=1, milliseconds=-1)
    return int(end.timestamp() * 1000)


def columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def fetch_all(con: sqlite3.Connection, sql: str, params=()) -> list[dict]:
    """执行查询返回 dict 列表；列缺失/表缺失时抛 sqlite3.Error 由上层降级。"""
    return [dict(r) for r in con.execute(sql, params).fetchall()]


def fetch_one(con: sqlite3.Connection, sql: str, params=()):
    row = con.execute(sql, params).fetchone()
    return dict(row) if row else None


def ms_range_clause(since_ms: int | None, until_ms: int | None,
                    col: str = "started_at") -> tuple[str, list]:
    """生成 ' AND col >= ? AND col <= ?' 片段，空条件返回 ('', [])。"""
    clause, params = "", []
    if since_ms is not None:
        clause += f" AND {col} >= ?"
        params.append(since_ms)
    if until_ms is not None:
        clause += f" AND {col} <= ?"
        params.append(until_ms)
    return clause, params


def fmt_ms(ms, fmt="%m-%d %H:%M") -> str:
    if not ms:
        return "?"
    return datetime.fromtimestamp(ms / 1000, LOCAL_TZ).strftime(fmt)


def ms2min(ms) -> float:
    return round((ms or 0) / 60000, 1)


def datetime_now(fmt="%Y%m%d_%H%M%S") -> str:
    """报告文件名时间戳（本地时区）。"""
    return datetime.now(LOCAL_TZ).strftime(fmt)


def week_key(ts_ms: int) -> str:
    """毫秒时间戳 -> 'YYYY-MM-DD'（所在周的周一日期，本地时区）。
    放在 load 层供 signals/weekly 共用，避免二者互相 import 成环。"""
    dt = datetime.fromtimestamp(ts_ms / 1000, LOCAL_TZ)
    monday = dt - timedelta(days=dt.weekday())
    return monday.strftime("%Y-%m-%d")
