# -*- coding: utf-8 -*-
"""不满信号挖掘：从用户消息文本里提取隐式人类反馈。

相对 reflect/analyze_step3.py 的升级：
- 信号词典带「强度权重」（放弃/否定是重度信号，追问是轻度），能算加权情绪分；
- 过滤更严：系统注入片段（[@plugin、<plugin_reference、Available agent 等）全跳过，
  旧脚本只跳了两种前缀；
- 按周聚合信号数——训练计划的「格式类返工归零」目标从此可验收；
- 采样保留消息 id 与时间，报告里可回跳定位。
"""
import json
import re
import sqlite3
from collections import defaultdict
from .load import fetch_all, ms_range_clause, week_key

# 信号词典：name -> (正则, 权重)。权重 3=重度（放弃/明确否定），
# 2=中度（要求重做/纠错），1=轻度（抱怨/追问）
SIGNAL_PATTERNS = {
    "放弃信号": (r"算了|放弃|不要了|不管了|算了吧|就这样吧", 3),
    "否定-不是我要的": (r"不是这个|不是我要|不是我要的|不是这样|完全不对|牛头不对马嘴", 3),
    "纠错-不对/错了": (r"不对|错了|错啦|有错|有问题|出错了|报错", 2),
    "重做-重新/再来": (r"重新|重来|再改|重做|重截|重画|重生成|重写|再来一", 2),
    "格式类抱怨": (r"格式(不对|错了|问题|又乱)|排版|乱码|字体|字号|模板", 1),
    "视觉类抱怨": (r"图片|截图|图(片|又|还是)?(糊|坏|错|歪|裁)|模糊|看不清|重叠|遮挡|乱七八糟", 1),
    "等待类抱怨": (r"太慢|等太久|等了这么久|怎么这么久|卡(住|死)|还在跑|还没完|还在转", 1),
    "追问-为什么还没": (r"怎么还没|为什么还没|还没好|好了吗|完成了吗|怎么样了", 1),
}

# 系统注入片段的特征：这些不是用户打的字，混进来会污染信号统计
SKIP_PREFIXES = ("[@", "<plugin", "Available agent", "You are", "The TodoWrite",
                 "This is capability", "<system", "[#")
SKIP_CONTAINS = ("system-reminder", "currentDate", "skill uses the load parameters")


def is_user_text(text: str) -> bool:
    if not text:
        return False
    for p in SKIP_PREFIXES:
        if text.startswith(p):
            return False
    return True


def mine_signals(con: sqlite3.Connection, since=None, until=None,
                 sample_per_cat=5) -> dict:
    """扫描全部用户文本消息，返回分类计数 + 加权分 + 样本 + 按周聚合。"""
    where, params = ms_range_clause(since, until, "m.time_created")
    rows = fetch_all(con, f"""
        SELECT s.title, p.data, m.time_created, m.id AS msg_id
        FROM message m
        JOIN part p ON p.message_id = m.id
        JOIN session s ON s.id = m.session_id
        WHERE json_extract(m.data, '$.role') = 'user'
          AND json_extract(p.data, '$.type') = 'text'{where}""", params)

    cat_counts: dict[str, int] = defaultdict(int)
    cat_weighted: dict[str, int] = defaultdict(int)
    cat_samples: dict[str, list] = defaultdict(list)
    weekly_counts: dict[str, int] = defaultdict(int)
    n_msgs = 0
    weighted_total = 0

    for r in rows:
        try:
            text = json.loads(r["data"]).get("text", "")
        except Exception:
            continue
        if not is_user_text(text):
            continue
        n_msgs += 1
        snippet = text[:500]  # 超长粘贴只看前 500 字
        hit_any = False
        for cat, (pat, weight) in SIGNAL_PATTERNS.items():
            if re.search(pat, snippet):
                cat_counts[cat] += 1
                cat_weighted[cat] += weight
                weighted_total += weight
                hit_any = True
                if len(cat_samples[cat]) < sample_per_cat:
                    cat_samples[cat].append({
                        "title": (r["title"] or "?")[:40],
                        "text": snippet.strip().replace("\n", " ")[:100],
                        "time": r["time_created"],
                    })
        if hit_any:
            weekly_counts[week_key(r["time_created"])] += 1

    return {
        "n_user_messages": n_msgs,
        "counts": dict(sorted(cat_counts.items(), key=lambda x: -x[1])),
        "weighted": dict(cat_weighted),
        "weighted_total": weighted_total,
        "samples": dict(cat_samples),
        "weekly": dict(sorted(weekly_counts.items())),
    }


def burst_sessions(con: sqlite3.Connection, since=None, until=None,
                   window_ms=90_000, top=10) -> list[dict]:
    """90 秒内连发消息的会话 TOP N——等待焦虑的代理指标。"""
    where, params = ms_range_clause(since, until, "m.time_created")
    rows = fetch_all(con, f"""
        SELECT m.session_id, m.time_created FROM message m
        WHERE json_extract(m.data, '$.role') = 'user'{where}
        ORDER BY m.session_id, m.time_created""", params)
    bursts: dict[str, int] = defaultdict(int)
    prev_sid, prev_t = None, 0
    for r in rows:
        if r["session_id"] == prev_sid and r["time_created"] - prev_t < window_ms:
            bursts[r["session_id"]] += 1
        prev_sid, prev_t = r["session_id"], r["time_created"]
    if not bursts:
        return []
    titles = {r["id"]: r["title"] for r in fetch_all(
        con, "SELECT id, title FROM session")}
    return [{"title": (titles.get(sid) or "?"), "bursts": n}
            for sid, n in sorted(bursts.items(), key=lambda x: -x[1])[:top]]
