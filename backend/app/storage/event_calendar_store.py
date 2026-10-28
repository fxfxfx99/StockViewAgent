"""Durable public event revisions, retrieval chunks, and account-scoped preferences.

Observation time is recorded locally. A newly downloaded historical calendar is
never evidence that this installation knew its contents in the past.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from app.config import settings
from app.storage import sqlite_bootstrap

SHANGHAI = ZoneInfo("Asia/Shanghai")
CATEGORIES = ("inflation", "employment", "growth", "central_bank", "trade", "other")
REGIONS = ("US", "EU", "CN", "JP", "GB", "CA", "AU", "CH")
DEFAULT_CONFIG = {"enabled": True, "agents": {"news": True, "research": True, "strategies": True, "kline": True},
                  "lookback_days": 14, "lookahead_days": 30, "min_importance": 2,
                  "regions": [], "categories": [], "top_k": 8}
CATEGORY_TERMS = {
    "inflation": "通胀 物价 价格 CPI PPI PCE inflation consumer producer prices",
    "employment": "就业 非农 失业 薪资 劳动力 employment jobs payroll unemployment wages labor",
    "growth": "增长 GDP 经济 生产 消费 零售 投资 PMI 制造业 growth output retail production investment",
    "central_bank": "央行 利率 货币政策 降息 加息 流动性 美联储 欧洲央行 日本央行 议息 FOMC ECB BOJ rates monetary policy central bank",
    "trade": "贸易 进出口 关税 出口 进口 trade exports imports tariffs",
    "other": "经济 金融 economic financial",
}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def timestamp(value):
    if isinstance(value, bool):
        raise ValueError("时间不能为布尔值")
    if isinstance(value, (int, float)):
        result = float(value)
    else:
        date = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if date.tzinfo is None:
            raise ValueError("时间必须包含时区")
        result = date.timestamp()
    if not math.isfinite(result) or result <= 0:
        raise ValueError("无效时间")
    return result


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def tokens(text):
    words = re.findall(r"[a-zA-Z0-9_]{2,40}|[\u4e00-\u9fff]+", str(text).lower())
    output = []
    for word in words:
        output.extend([word] if not re.fullmatch(r"[\u4e00-\u9fff]+", word) else
                      [word[i:i + 2] for i in range(len(word) - 1)])
    return list(dict.fromkeys(output))


_SCHEMA_SQL = """
          CREATE TABLE IF NOT EXISTS event_revisions (
            seq INTEGER PRIMARY KEY, revision_id TEXT NOT NULL UNIQUE, event_id TEXT NOT NULL,
            observed_at REAL NOT NULL, content_hash TEXT NOT NULL, payload TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS event_observation ON event_revisions(event_id,observed_at,seq);
          CREATE TABLE IF NOT EXISTS event_heads (event_id TEXT PRIMARY KEY, revision_id TEXT NOT NULL,
            content_hash TEXT NOT NULL, last_seen_at REAL NOT NULL);
          CREATE VIRTUAL TABLE IF NOT EXISTS event_chunks USING fts5(
            revision_id UNINDEXED, event_id UNINDEXED, text, terms);
          CREATE TABLE IF NOT EXISTS event_sources (source_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS event_preferences (user_id INTEGER PRIMARY KEY, payload TEXT NOT NULL);
          CREATE TRIGGER IF NOT EXISTS event_revision_no_update BEFORE UPDATE ON event_revisions
            BEGIN SELECT RAISE(ABORT, 'event revisions are immutable'); END;
          CREATE TRIGGER IF NOT EXISTS event_revision_no_delete BEFORE DELETE ON event_revisions
            BEGIN SELECT RAISE(ABORT, 'event revisions are immutable'); END;
        """


@contextmanager
def connection():
    path = settings.data_dir / "economic_events.sqlite3"
    with sqlite_bootstrap.connection(path, _SCHEMA_SQL) as conn:
        yield conn


def _text(value, limit=2000):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _result(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool) or (isinstance(value, (float, int)) and not math.isfinite(value)):
        raise ValueError("无效事件数值")
    return str(value)[:120]


def normalise(raw, now):
    source_id, upstream_id = _text(raw.get("source_id"), 80), _text(raw.get("upstream_id"), 500)
    title = _text(raw.get("title"), 400)
    url = str(raw.get("source_url") or "")[:2000]
    parts = urlsplit(url)
    if not source_id or not upstream_id or not title or parts.scheme not in {"http", "https"} or not parts.netloc or parts.username or parts.password:
        raise ValueError("事件缺少稳定标识、标题或有效来源链接")
    scheduled = timestamp(raw.get("scheduled_at"))
    zone = ZoneInfo(str(raw.get("timezone") or "UTC"))
    local_date = datetime.fromtimestamp(scheduled, zone).date().isoformat()
    all_day = raw.get("all_day", False)
    importance = raw.get("importance", 2)
    category = raw.get("category", "other")
    if type(all_day) is not bool or type(importance) is not int or not 1 <= importance <= 3 or category not in CATEGORIES:
        raise ValueError("无效事件类别、重要性或时间精度")
    status = raw.get("status", "scheduled")
    if status not in {"scheduled", "released", "cancelled"}:
        raise ValueError("无效事件状态")
    actual = _result(raw.get("actual"))
    if scheduled > now:
        # Future schedules are not actual releases, even if a feed mixes fields.
        actual = None
        if status == "released":
            status = "scheduled"
    elif actual is not None and status != "cancelled":
        status = "released"
    event_id = hashlib.sha256(f"{source_id}:{upstream_id}".encode()).hexdigest()[:32]
    return {"id": event_id, "source_id": source_id, "upstream_id": upstream_id,
            "source": _text(raw.get("source") or source_id, 120), "source_url": url,
            "title": title, "description": _text(raw.get("description"), 6000),
            "region": _text(raw.get("region"), 8).upper(), "currency": _text(raw.get("currency"), 8),
            "category": category, "importance": importance, "scheduled_at": iso(scheduled),
            "scheduled_date": local_date, "timezone": str(zone), "all_day": all_day,
            "calendar_date": local_date if all_day else datetime.fromtimestamp(scheduled, SHANGHAI).date().isoformat(),
            "status": status, "actual": actual, "forecast": _result(raw.get("forecast")),
            "previous": _result(raw.get("previous")), "unit": _text(raw.get("unit"), 40)}


def document_text(event):
    lines = [event["title"], event.get("description") or "",
             f"地区 {event['region']}；类别 {event['category']}；日程 {event['scheduled_at']}；状态 {event['status']}"]
    if event["all_day"]:
        lines.append(f"来源日期 {event['scheduled_date']}，准确发布时间待定")
    for key, label in (("actual", "公布值"), ("forecast", "市场预期"), ("previous", "前值")):
        if event.get(key) is not None:
            lines.append(f"{label}：{event[key]} {event.get('unit') or ''}")
    lines.append(f"来源：{event['source']} {event['source_url']}")
    return "\n".join(lines)


def ingest_events(rows, *, observed_at=None):
    now = time.time() if observed_at is None else timestamp(observed_at)
    events = [normalise(row, now) for row in rows]
    if len(events) > 10000:
        raise ValueError("单来源事件过多")
    added = 0
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for event in events:
            event_id = event["id"]
            payload = canonical(event)
            digest = hashlib.sha256(payload.encode()).hexdigest()
            head = conn.execute("SELECT * FROM event_heads WHERE event_id=?", (event_id,)).fetchone()
            if head and head["last_seen_at"] > now:
                raise ValueError("不能倒填已观察事件的采集时间")
            if head is None or head["content_hash"] != digest:
                prior = head["revision_id"] if head else ""
                revision = hashlib.sha256(f"{event_id}:{digest}:{now}:{prior}".encode()).hexdigest()
                conn.execute("INSERT INTO event_revisions(revision_id,event_id,observed_at,content_hash,payload) VALUES(?,?,?,?,?)",
                             (revision, event_id, now, digest, payload))
                text = document_text(event)
                for start in range(0, len(text), 1000):
                    chunk = text[max(0, start - 120):start + 1000]
                    terms = " ".join(tokens(event["title"] + " " + CATEGORY_TERMS[event["category"]] + " " + chunk))
                    conn.execute("INSERT INTO event_chunks VALUES(?,?,?,?)", (revision, event_id, chunk, terms))
                added += 1
            else:
                revision = head["revision_id"]
            conn.execute("INSERT INTO event_heads VALUES(?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET revision_id=excluded.revision_id,content_hash=excluded.content_hash,last_seen_at=excluded.last_seen_at",
                         (event_id, revision, digest, now))
    return {"received": len(events), "added": added}


_VISIBLE = """WITH visible AS (SELECT *, ROW_NUMBER() OVER (
    PARTITION BY event_id ORDER BY observed_at DESC,seq DESC) AS rank
    FROM event_revisions WHERE observed_at<=?)"""


def visible_events(*, as_of=None):
    cutoff = time.time() if as_of is None else timestamp(as_of)
    with connection() as conn:
        rows = conn.execute(_VISIBLE + " SELECT * FROM visible WHERE rank=1", (cutoff,)).fetchall()
    return [{**json.loads(row["payload"]), "revision_id": row["revision_id"],
             "observed_at": iso(row["observed_at"])} for row in rows]


def matching_chunks(query, *, as_of=None):
    words = tokens(query)[:40]
    if not words:
        return {}
    cutoff = time.time() if as_of is None else timestamp(as_of)
    match = " OR ".join('"' + word.replace('"', '""') + '"' for word in words)
    with connection() as conn:
        # BM25 corpus statistics must also be point-in-time. Filtering results
        # from the full historical FTS table alone would let future documents
        # change IDF/average length and thus the old top-k ordering.
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("CREATE VIRTUAL TABLE temp.visible_chunks USING fts5(revision_id UNINDEXED,text,terms)")
        conn.execute(_VISIBLE + """ INSERT INTO visible_chunks SELECT event_chunks.revision_id,event_chunks.text,event_chunks.terms
            FROM event_chunks JOIN visible ON visible.revision_id=event_chunks.revision_id AND visible.rank=1""", (cutoff,))
        rows = conn.execute("""SELECT revision_id,text,bm25(visible_chunks) AS relevance FROM visible_chunks
            WHERE visible_chunks MATCH ? ORDER BY relevance,revision_id,rowid LIMIT 1000""", (match,)).fetchall()
    found = {}
    for row in rows:
        found.setdefault(row["revision_id"], {"snippet": row["text"], "rank": row["relevance"]})
    return found


def get_event(event_id):
    with connection() as conn:
        rows = conn.execute("SELECT * FROM event_revisions WHERE event_id=? ORDER BY observed_at DESC,seq DESC", (event_id,)).fetchall()
    revisions = [{**json.loads(row["payload"]), "revision_id": row["revision_id"], "observed_at": iso(row["observed_at"])} for row in rows]
    return {"event": revisions[0], "revisions": revisions} if revisions else None


def get_config(user_id):
    with connection() as conn:
        row = conn.execute("SELECT payload FROM event_preferences WHERE user_id=?", (int(user_id or 0),)).fetchone()
    value = json.loads(canonical(DEFAULT_CONFIG))
    if row:
        saved = json.loads(row[0])
        value.update(saved)
        value["agents"] = {**DEFAULT_CONFIG["agents"], **saved.get("agents", {})}
    return value


def update_config(user_id, patch):
    # Validation is shared by internal callers and the HTTP boundary.
    if not isinstance(patch, dict) or set(patch) - set(DEFAULT_CONFIG):
        raise ValueError("配置包含未知字段")
    for key, value in patch.items():
        if key == "enabled" and type(value) is not bool:
            raise ValueError("enabled 必须为布尔值")
        if key == "agents" and (not isinstance(value, dict) or set(value) - set(DEFAULT_CONFIG["agents"]) or any(type(v) is not bool for v in value.values())):
            raise ValueError("Agent 配置无效")
        bounds = {"lookback_days": (0, 365), "lookahead_days": (0, 365), "min_importance": (1, 3), "top_k": (1, 30)}
        if key in bounds and (type(value) is not int or not bounds[key][0] <= value <= bounds[key][1]):
            raise ValueError(f"{key} 超出范围")
        if key in {"regions", "categories"} and (not isinstance(value, list) or len(value) > 10 or any(v not in (REGIONS if key == "regions" else CATEGORIES) for v in value)):
            raise ValueError(f"{key} 无效")
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT payload FROM event_preferences WHERE user_id=?", (int(user_id or 0),)).fetchone()
        value = {**json.loads(canonical(DEFAULT_CONFIG)), **(json.loads(row[0]) if row else {})}
        value.update({k: v for k, v in patch.items() if k != "agents"})
        value["agents"] = {**DEFAULT_CONFIG["agents"], **value["agents"], **patch.get("agents", {})}
        conn.execute("INSERT INTO event_preferences VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET payload=excluded.payload", (int(user_id or 0), canonical(value)))
    return value


def source_states():
    with connection() as conn:
        return {row[0]: json.loads(row[1]) for row in conn.execute("SELECT * FROM event_sources")}


def set_source_state(source_id, value):
    with connection() as conn:
        conn.execute("INSERT INTO event_sources VALUES(?,?) ON CONFLICT(source_id) DO UPDATE SET payload=excluded.payload", (source_id, canonical(value)))
