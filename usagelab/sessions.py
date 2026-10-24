# -*- coding: utf-8 -*-
"""会话与项目画像：耗时归属、关键词归类、会话卫生指标。

相对 reflect/analyze_step2.py 的升级：
- 归类词典外置成 categories.json（旧脚本硬编码，细则一变就得改代码）；
- 新增「会话卫生」指标：巨型会话（单轮输入 >1M token）里的轮次占比——
  这是训练计划「新任务开新会话」实践的量化验收；
- 时间窗参数化。
"""
import json
import os
import sqlite3
from .load import fetch_all, ms_range_clause, DEFAULT_DB
from .metrics import dist_summary

# 内置默认词典：仓库内无 categories.json 时使用；有则完全覆盖
DEFAULT_CATEGORIES = [
    {"name": "数模/国赛", "keywords": ["数学建模", "数学模型", "mathmodel", "国赛", "2026B"]},
    {"name": "综测", "keywords": ["综测", "奖学金", "综合测评"]},
    {"name": "PPT/演示", "keywords": ["ppt", "PPT", "演示", "幻灯"]},
    {"name": "数据库", "keywords": ["mysql", "MySQL", "数据库", "实验一"]},
    {"name": "Servlet/Web", "keywords": ["servlet", "Servlet", "web", "Web", "实验2", "实验3"]},
    {"name": "离散数学", "keywords": ["离散"]},
    {"name": "概率论", "keywords": ["概率"]},
    {"name": "数据结构", "keywords": ["数据结构", "栈和队列"]},
    {"name": "AI漫剧", "keywords": ["漫剧", "动漫"]},
    {"name": "Harness/Agent", "keywords": ["harness", "Harness", "agent", "Agent"]},
]

GIANT_TURN_TOKENS = 1_000_000  # 单轮输入超过 1M 视为「在巨型会话里工作」


def load_categories() -> list[dict]:
    """优先读同目录 categories.json（用户可自定义），否则用内置默认。"""
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "categories.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return DEFAULT_CATEGORIES


def classify(title: str, directory: str, cats: list[dict]) -> str:
    """标题+目录逐词典匹配，命中即返回；都不中归「其他」。
    顺序敏感：词典前面的优先，保证「数学建模ppt」归数模而非 PPT。"""
    text = f"{title or ''} {directory or ''}"
    for c in cats:
        if any(kw in text for kw in c["keywords"]):
            return c["name"]
    return "其他"


def session_profile(con: sqlite3.Connection, since=None, until=None, top=20) -> dict:
    """会话级画像：最耗时 TopN、按类别聚合、会话卫生指标。"""
    where, params = ms_range_clause(since, until, "t.started_at")
    rows = fetch_all(con, f"""
        SELECT s.title, s.directory, t.session_id, COUNT(*) AS turns,
               SUM(t.duration_ms) AS total_ms, SUM(t.input_tokens) AS in_tok,
               MAX(t.input_tokens) AS max_in_tok, MIN(t.started_at) AS t0,
               MAX(t.completed_at) AS t1
        FROM turn_usage t JOIN session s ON s.id = t.session_id
        WHERE t.completed_at IS NOT NULL{where}
        GROUP BY t.session_id""", params)

    cats = load_categories()
    by_cat: dict[str, dict] = {}
    for r in rows:
        cat = classify(r["title"], r["directory"], cats)
        a = by_cat.setdefault(cat, {"turns": 0, "total_ms": 0, "in_tok": 0})
        a["turns"] += r["turns"]
        a["total_ms"] += r["total_ms"] or 0
        a["in_tok"] += r["in_tok"] or 0

    top_sessions = sorted(rows, key=lambda r: -(r["total_ms"] or 0))[:top]

    # 会话卫生单独查全量轮（含取消/报错轮）：上下文膨胀的痛苦轮往往正是
    # 被取消的那轮，只看完成轮会把最坏的会话漏掉
    hrows = fetch_all(con, f"""
        SELECT t.session_id, COUNT(*) AS turns, MAX(t.input_tokens) AS max_in_tok
        FROM turn_usage t WHERE 1=1{where}
        GROUP BY t.session_id""", params)
    total_turns_all = sum(r["turns"] for r in hrows)
    giant_turns = sum(r["turns"] for r in hrows
                      if (r["max_in_tok"] or 0) > GIANT_TURN_TOKENS)

    return {
        "n_sessions": len(rows),
        "total_turns": sum(r["turns"] for r in rows),
        "top_sessions": top_sessions,
        "by_category": sorted(
            [{"name": k, **v} for k, v in by_cat.items()],
            key=lambda x: -x["total_ms"]),
        "hygiene": {
            "giant_threshold_tokens": GIANT_TURN_TOKENS,
            "turns_in_giant_sessions": giant_turns,
            "giant_share_pct": round(giant_turns / total_turns_all * 100, 1)
                               if total_turns_all else 0,
        },
    }
