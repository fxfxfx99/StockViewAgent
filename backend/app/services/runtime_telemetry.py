"""Small durable activity ledger. Counts measured requests, never budget estimates.

Only allowlisted fields are stored: no prompts, response bodies, API keys or raw
exceptions. Recording is best-effort and must not change business behavior.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import logging
import re
import sqlite3
import time
from urllib.parse import urlsplit
import uuid
from zoneinfo import ZoneInfo

from app.security.user_context import current_user_id
from app.storage.research_store import ResearchStore

logger = logging.getLogger(__name__)
_CN = ZoneInfo("Asia/Shanghai")
_FINAL = {"success", "failed", "cancelled"}


def _label(value, limit=120):
    value = str(value or "").strip()
    # Providers are hostnames, not URLs that may carry a key or signed query.
    if "://" in value:
        try:
            return (urlsplit(value).hostname or "")[:limit]
        except ValueError:
            return ""
    if any(marker in value.lower() for marker in ("bearer ", "api_key=", "token=", "password=", "secret=")):
        return "redacted"
    return re.sub(r"[\x00-\x1f\x7f]", "", value)[:limit]


def _count(value):
    return value if type(value) is int and 0 <= value <= 10**15 else None


def _usage(value):
    if not isinstance(value, dict):
        return None, None, None
    incoming = _count(value.get("prompt_tokens", value.get("input_tokens")))
    outgoing = _count(value.get("completion_tokens", value.get("output_tokens")))
    total = _count(value.get("total_tokens"))
    if total is None and incoming is not None and outgoing is not None:
        total = incoming + outgoing
    return incoming, outgoing, total


class RuntimeLedger:
    def __init__(self):
        self.db = ResearchStore()
        with self.db._schema_connection() as conn:
            conn.executescript("""
              CREATE TABLE IF NOT EXISTS runtime_events (
                user_id INTEGER NOT NULL, id TEXT NOT NULL, kind TEXT NOT NULL,
                operation TEXT NOT NULL, source TEXT NOT NULL, model TEXT,
                status TEXT NOT NULL, started_at REAL NOT NULL, finished_at REAL,
                input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER,
                records_received INTEGER NOT NULL DEFAULT 0,
                records_added INTEGER NOT NULL DEFAULT 0,
                bytes_stored INTEGER NOT NULL DEFAULT 0, error_code TEXT,
                PRIMARY KEY(user_id,id));
              CREATE INDEX IF NOT EXISTS runtime_user_time ON runtime_events(user_id,started_at DESC);
              CREATE TABLE IF NOT EXISTS runtime_meta (id INTEGER PRIMARY KEY, tracking_started_at REAL NOT NULL);
            """)
            conn.execute("INSERT OR IGNORE INTO runtime_meta VALUES(1,?)", (time.time(),))

    def start(self, kind, operation, *, source="", model=None, user_id=None, event_id=None):
        if kind not in {"llm", "collection"}:
            raise ValueError("Unknown runtime event kind")
        uid = user_id if user_id is not None else current_user_id.get()
        uid = (1 if kind == "llm" else 0) if uid is None else uid
        if type(uid) is not int or uid < 0 or (kind == "llm" and uid == 0):
            raise ValueError("Invalid event account")
        operation = _label(operation, 64)
        eid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{uid}:{kind}:{operation}:{event_id}")) if event_id else str(uuid.uuid4())
        with self.db.connection(write=True) as conn:
            conn.execute("""INSERT OR IGNORE INTO runtime_events
                (user_id,id,kind,operation,source,model,status,started_at)
                VALUES(?,?,?,?,?,?,'running',?)""", (uid, eid, kind, operation, _label(source), _label(model) or None, time.time()))
        # Captures the original actor even when a task finishes on another thread.
        return f"{uid}:{eid}"

    def finish(self, handle, *, status="success", usage=None, error_code=None, user_id=None,
               records_received=0, records_added=0, bytes_stored=0):
        if status not in _FINAL:
            raise ValueError("Invalid final runtime status")
        account, eid = handle.split(":", 1)
        uid = int(account)
        if user_id is not None and user_id != uid:
            raise ValueError("Runtime event belongs to another account")
        incoming, outgoing, total = _usage(usage)
        code = error_code if isinstance(error_code, str) and re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", error_code) else None
        counts = [_count(value) for value in (records_received, records_added, bytes_stored)]
        if any(value is None for value in counts):
            raise ValueError("Invalid runtime counter")
        with self.db.connection(write=True) as conn:
            # Final outcomes are write-once. Duplicate worker completions cannot
            # add tokens or downloaded rows a second time.
            conn.execute("""UPDATE runtime_events SET status=?,finished_at=?,input_tokens=?,output_tokens=?,
                total_tokens=?,records_received=?,records_added=?,bytes_stored=?,error_code=?
                WHERE user_id=? AND id=? AND status='running'""", (
                status, time.time(), incoming, outgoing, total, *counts, code, uid, eid))

    def overview(self, user_id, period="today", *, now=None):
        if period not in {"today", "7d", "all"} or type(user_id) is not int or user_id <= 0:
            raise ValueError("Invalid runtime overview query")
        stamp = time.time() if now is None else float(now)
        day = datetime.fromtimestamp(stamp, _CN).replace(hour=0, minute=0, second=0, microsecond=0)
        start = None if period == "all" else (day - timedelta(days=6) if period == "7d" else day).timestamp()
        lower = 0 if start is None else start
        llm_filter = "user_id=? AND kind='llm' AND started_at>=? AND started_at<=?"
        collection_filter = "user_id IN (0,?) AND kind='collection' AND started_at>=? AND started_at<=?"
        with self.db.connection() as conn:
            # A consistent read snapshot keeps totals and the activity list in sync.
            conn.execute("BEGIN")
            params = (user_id, lower, stamp)
            llm = dict(conn.execute(f"""SELECT COUNT(*) calls,
                COALESCE(SUM(status='success'),0) success,COALESCE(SUM(status='failed'),0) failed,
                COALESCE(SUM(status='running'),0) pending,COALESCE(SUM(status='cancelled'),0) cancelled,
                SUM(input_tokens) input_tokens,SUM(output_tokens) output_tokens,SUM(total_tokens) total_tokens,
                COALESCE(SUM(status!='running' AND total_tokens IS NULL),0) unknown_token_calls,
                COALESCE(SUM(total_tokens IS NOT NULL),0) reported_token_calls
                FROM runtime_events WHERE {llm_filter}""", params).fetchone())
            if not llm["calls"]:
                llm.update(input_tokens=0, output_tokens=0, total_tokens=0)
            collection = dict(conn.execute(f"""SELECT COUNT(*) requests,
                COALESCE(SUM(status='success'),0) success,COALESCE(SUM(status='failed'),0) failed,
                COALESCE(SUM(status='running'),0) pending,
                COALESCE(SUM(records_received),0) records_received,COALESCE(SUM(records_added),0) records_added,
                COALESCE(SUM(bytes_stored),0) bytes_stored,
                COALESCE(SUM(CASE WHEN user_id=0 THEN records_received ELSE 0 END),0) shared_records_received,
                COALESCE(SUM(CASE WHEN user_id=0 THEN records_added ELSE 0 END),0) shared_records_added
                FROM runtime_events WHERE {collection_filter}""", params).fetchone())
            rows = conn.execute("""SELECT * FROM runtime_events WHERE
                (user_id=? OR (user_id=0 AND kind='collection')) AND started_at>=? AND started_at<=?
                ORDER BY started_at DESC,id DESC LIMIT 20""", params).fetchall()
            recent = []
            for row in rows:
                item = dict(row)
                item["shared"] = item.pop("user_id") == 0
                if item["status"] == "running" and stamp - item["started_at"] >= 900:
                    item["status"] = "unfinished"
                recent.append(item)
            since = conn.execute("SELECT tracking_started_at FROM runtime_meta WHERE id=1").fetchone()[0]
        return {"period": period, "timezone": "Asia/Shanghai", "from_at": start, "to_at": stamp,
                "tracking_started_at": since, "llm": llm, "collection": collection, "recent": recent}


def start_event(kind, operation, *, source="", model=None, user_id=None, event_id=None):
    try:
        return RuntimeLedger().start(kind, operation, source=source, model=model, user_id=user_id, event_id=event_id)
    except (OSError, sqlite3.Error, ValueError, TypeError):
        logger.warning("运行日志暂时无法写入，业务继续执行")
        return ""


def finish_event(event_id, *, status="success", usage=None, error_code=None, user_id=None,
                 records_received=0, records_added=0, bytes_stored=0):
    if not event_id:
        return
    try:
        RuntimeLedger().finish(event_id, status=status, usage=usage, error_code=error_code, user_id=user_id,
                               records_received=records_received, records_added=records_added, bytes_stored=bytes_stored)
    except (OSError, sqlite3.Error, ValueError, TypeError):
        logger.warning("运行日志暂时无法完成记录，业务结果保留")


def record_collection(operation, source, *, records_received=0, records_added=0, bytes_stored=0,
                      status="success", error_code=None, user_id=None, event_id=None):
    handle = start_event("collection", operation, source=source, user_id=user_id, event_id=event_id)
    finish_event(handle, status=status, records_received=records_received, records_added=records_added,
                 bytes_stored=bytes_stored, error_code=error_code, user_id=user_id)
    return handle


def overview(user_id, period="today", *, now=None):
    return RuntimeLedger().overview(user_id, period, now=now)
