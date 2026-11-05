"""Immutable, public per-issuer feature snapshots and resumable refresh leases."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import time
import uuid

from app.config import settings
from app.services.stock_code_utils import normalize_symbol
from app.storage import sqlite_bootstrap

_SCHEMA = """
CREATE TABLE IF NOT EXISTS feature_versions (
  seq INTEGER PRIMARY KEY, version_id TEXT NOT NULL UNIQUE, symbol TEXT NOT NULL,
  as_of REAL NOT NULL, created_at REAL NOT NULL, content_hash TEXT NOT NULL, payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS feature_symbol_time ON feature_versions(symbol,created_at DESC,seq DESC);
CREATE TABLE IF NOT EXISTS feature_refresh (
  symbol TEXT PRIMARY KEY, next_due REAL NOT NULL DEFAULT 0,
  last_attempt REAL, last_success REAL, error_code TEXT,
  lease_token TEXT, lease_until REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS feature_checks (
  symbol TEXT NOT NULL, checked_at REAL NOT NULL, as_of REAL NOT NULL,
  version_id TEXT NOT NULL, status TEXT NOT NULL, PRIMARY KEY(symbol,checked_at));
INSERT OR IGNORE INTO feature_checks(symbol,checked_at,as_of,version_id,status)
  SELECT symbol,created_at,as_of,version_id,'updated' FROM feature_versions;
CREATE INDEX IF NOT EXISTS feature_check_time ON feature_checks(symbol,version_id,checked_at DESC);
CREATE TRIGGER IF NOT EXISTS feature_check_no_update BEFORE UPDATE ON feature_checks
  BEGIN SELECT RAISE(ABORT, 'feature checks are immutable'); END;
CREATE TRIGGER IF NOT EXISTS feature_check_no_delete BEFORE DELETE ON feature_checks
  BEGIN SELECT RAISE(ABORT, 'feature checks are immutable'); END;
CREATE TRIGGER IF NOT EXISTS feature_no_update BEFORE UPDATE ON feature_versions
  BEGIN SELECT RAISE(ABORT, 'feature versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS feature_no_delete BEFORE DELETE ON feature_versions
  BEGIN SELECT RAISE(ABORT, 'feature versions are immutable'); END;
"""


@contextmanager
def connection():
    with sqlite_bootstrap.connection(settings.data_dir / "stock_features.sqlite3", _SCHEMA, version=2) as conn:
        yield conn


def symbol_key(value):
    raw = str(value or "").strip().upper()
    if not re.fullmatch(r"(?:\d{6}(?:\.(?:SS|SH|SZ|BJ))?|(?:SH|SZ|BJ)\d{6}|\d{1,5}\.HK|[A-Z]{1,6}(?:[.-][A-Z])?)", raw):
        raise ValueError("无效股票代码")
    symbol = normalize_symbol(raw)
    if not symbol:
        raise ValueError("无效股票代码")
    return symbol


def timestamp(value):
    if isinstance(value, bool):
        raise ValueError("无效时间")
    if isinstance(value, (float, int)):
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
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else None


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def register(symbols):
    keys = sorted({symbol_key(s) for s in symbols})
    with connection() as conn:
        conn.executemany("INSERT OR IGNORE INTO feature_refresh(symbol) VALUES(?)", [(s,) for s in keys])


def state(symbol):
    with connection() as conn:
        row = conn.execute("SELECT * FROM feature_refresh WHERE symbol=?", (symbol_key(symbol),)).fetchone()
    return dict(row) if row else {}


def due_symbols(*, now=None, limit=10):
    now = time.time() if now is None else now
    with connection() as conn:
        rows = conn.execute("SELECT symbol FROM feature_refresh WHERE next_due<=? AND lease_until<=? "
                            "ORDER BY next_due,symbol LIMIT ?", (now, now, limit)).fetchall()
    return [row[0] for row in rows]


def claim(symbol, *, now, force=False):
    symbol = symbol_key(symbol)
    token = uuid.uuid4().hex
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT OR IGNORE INTO feature_refresh(symbol) VALUES(?)", (symbol,))
        row = conn.execute("SELECT * FROM feature_refresh WHERE symbol=?", (symbol,)).fetchone()
        if row["lease_until"] > now:
            return None, "in_progress"
        if row["last_attempt"] is not None and now - row["last_attempt"] < 30:
            return None, "cooldown"
        if not force and row["next_due"] > now:
            return None, "unchanged"
        conn.execute("UPDATE feature_refresh SET lease_token=?,lease_until=?,last_attempt=? WHERE symbol=?",
                     (token, now + 600, now, symbol))
    return token, "claimed"


def get_snapshot(symbol, *, as_of=None, version_id=None):
    symbol = symbol_key(symbol)
    cutoff = time.time() if as_of is None else timestamp(as_of)
    sql = "SELECT payload,content_hash FROM feature_versions WHERE symbol=? AND created_at<=? AND as_of<=?"
    args = [symbol, cutoff, cutoff]
    if version_id is not None:
        sql += " AND version_id=?"
        args.append(version_id)
    with connection() as conn:
        row = conn.execute(sql + " ORDER BY created_at DESC,seq DESC LIMIT 1", args).fetchone()
    return {**json.loads(row[0]), "content_hash": row[1]} if row else None


def snapshot_identity(symbol, *, as_of=None):
    cutoff = time.time() if as_of is None else timestamp(as_of)
    with connection() as conn:
        row = conn.execute("SELECT symbol,version_id,as_of,created_at,content_hash FROM feature_versions "
                           "WHERE symbol=? AND created_at<=? AND as_of<=? ORDER BY created_at DESC,seq DESC LIMIT 1",
                           (symbol_key(symbol), cutoff, cutoff)).fetchone()
    return {"stock_code": row["symbol"], "version_id": row["version_id"], "as_of": iso(row["as_of"]),
            "created_at": iso(row["created_at"]), "content_hash": row["content_hash"]} if row else None


def _changes(previous, snapshot):
    old = {f["id"]: f for f in (previous or {}).get("features", [])}
    new = {f["id"]: f for f in snapshot.get("features", [])}
    def brief(row):
        return {"id": row["id"], "label": row.get("label") or row.get("name") or row["id"],
                "value": row.get("value"), "unit": row.get("unit"), "status": row.get("status")}
    return {"added": [brief(new[k]) for k in new.keys() - old.keys()],
            "removed": [brief(old[k]) for k in old.keys() - new.keys()],
            "changed": [{"id": k, "label": new[k].get("label") or new[k].get("name") or k,
                         "before": old[k].get("value"), "after": new[k].get("value"),
                         "before_status": old[k].get("status"), "after_status": new[k].get("status")}
                        for k in new.keys() & old.keys() if brief(new[k]) != brief(old[k])]}


def publish(symbol, snapshot, *, token, now, next_due):
    """Commit snapshot + cadence atomically; never backdate a newly built version."""
    symbol = symbol_key(symbol)
    if symbol_key(snapshot["stock_code"]) != symbol or timestamp(snapshot["as_of"]) > now:
        raise ValueError("特征标的或截至时间不匹配")
    # The build time is metadata; source observation times remain in the content.
    fingerprint = {k: v for k, v in snapshot.items() if k not in {"as_of", "created_at", "version_id", "changes"}}
    digest = hashlib.sha256(canonical(fingerprint).encode()).hexdigest()
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        owner = conn.execute("SELECT lease_token FROM feature_refresh WHERE symbol=?", (symbol,)).fetchone()
        if not owner or owner[0] != token:
            return None, "superseded"
        prior = conn.execute("SELECT * FROM feature_versions WHERE symbol=? ORDER BY seq DESC LIMIT 1", (symbol,)).fetchone()
        previous = json.loads(prior["payload"]) if prior else None
        # Reuse unchanged evidence across weeks. A small immutable check records
        # the observation without duplicating the entire market's source texts.
        reuse = prior and prior["content_hash"] == digest
        if reuse:
            result, status = previous, "unchanged"
        else:
            result = {**snapshot, "stock_code": symbol, "created_at": iso(now),
                      "version_id": uuid.uuid4().hex, "changes": _changes(previous, snapshot)}
            conn.execute("INSERT INTO feature_versions(version_id,symbol,as_of,created_at,content_hash,payload) VALUES(?,?,?,?,?,?)",
                         (result["version_id"], symbol, timestamp(snapshot["as_of"]), now, digest, canonical(result)))
            status = "updated"
        conn.execute("INSERT INTO feature_checks(symbol,checked_at,as_of,version_id,status) VALUES(?,?,?,?,?)",
                     (symbol, now, timestamp(snapshot["as_of"]), result["version_id"], status))
        conn.execute("UPDATE feature_refresh SET last_success=?,next_due=?,error_code=NULL,lease_token=NULL,lease_until=0 WHERE symbol=?",
                     (now, next_due, symbol))
    return result, status


def fail(symbol, *, token, now, error_code="build_failed"):
    with connection() as conn:
        conn.execute("UPDATE feature_refresh SET error_code=?,next_due=?,lease_token=NULL,lease_until=0 "
                     "WHERE symbol=? AND lease_token=?", (error_code, now + 3600, symbol_key(symbol), token))


def history(symbol, *, limit=12):
    with connection() as conn:
        rows = conn.execute("SELECT payload FROM feature_versions WHERE symbol=? ORDER BY created_at DESC,seq DESC LIMIT ?",
                            (symbol_key(symbol), limit)).fetchall()
    return [{k: v for k, v in json.loads(row[0]).items() if k in
             {"version_id", "stock_code", "name", "as_of", "created_at", "coverage", "changes"}} for row in rows]


def checks(symbol, *, limit=12, as_of=None, version_id=None):
    cutoff = time.time() if as_of is None else timestamp(as_of)
    sql = "SELECT version_id,checked_at,as_of,status FROM feature_checks WHERE symbol=? AND checked_at<=? AND as_of<=?"
    args = [symbol_key(symbol), cutoff, cutoff]
    if version_id:
        sql += " AND version_id=?"
        args.append(version_id)
    with connection() as conn:
        rows = conn.execute(sql + " ORDER BY checked_at DESC LIMIT ?", [*args, limit]).fetchall()
    return [{**dict(row), "checked_at": iso(row["checked_at"]), "as_of": iso(row["as_of"])} for row in rows]
