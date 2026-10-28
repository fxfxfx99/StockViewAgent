"""Durable, user-scoped research ledger, leases and atomic budget reservations.

Immutable entities contain research evidence; mutable state only contains pointers and
workflow progress. SQLite is authoritative; no process-global cache survives tests or
configuration changes. All read/modify/write operations use BEGIN IMMEDIATE.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Any
import uuid
from zoneinfo import ZoneInfo

from app.config import settings

DEFAULT_CONFIG = {
    "enabled": True, "paused": False, "daily_calls": 30,
    "daily_tokens": 150_000, "weekly_candidates": 60,
    "initial_cash": 100_000.0, "max_symbol_weight": 0.2, "max_position_pct": 1.0,
}


class ImmutableConflict(ValueError):
    pass


class LeaseLost(RuntimeError):
    pass


class BudgetExceeded(ValueError):
    pass


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _uid(user_id: int) -> int:
    if isinstance(user_id, bool) or int(user_id) != user_id or int(user_id) < 0:
        raise ValueError("Invalid user id")
    return int(user_id)


def _decode(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    result = dict(row)
    for key in ("payload", "result", "value"):
        if key in result and result[key] is not None:
            result[key] = json.loads(result[key])
    return result


class ResearchStore:
    def __init__(self, data_dir: str | Path | None = None):
        self.root = Path(data_dir) if data_dir is not None else settings.data_dir
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "research.sqlite3"
        with self._schema_connection() as conn:
            conn.executescript("""
              CREATE TABLE IF NOT EXISTS entities (
                user_id INTEGER NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL,
                payload TEXT NOT NULL, content_hash TEXT NOT NULL, created_at REAL NOT NULL,
                PRIMARY KEY(user_id,kind,id));
              CREATE INDEX IF NOT EXISTS entity_listing ON entities(user_id,kind,created_at DESC);
              CREATE TRIGGER IF NOT EXISTS entities_no_update BEFORE UPDATE ON entities
                BEGIN SELECT RAISE(ABORT,'immutable entity'); END;
              CREATE TRIGGER IF NOT EXISTS entities_no_delete BEFORE DELETE ON entities
                BEGIN SELECT RAISE(ABORT,'immutable entity'); END;
              CREATE TABLE IF NOT EXISTS state (
                user_id INTEGER NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
                updated_at REAL NOT NULL, PRIMARY KEY(user_id,key));
              CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,kind TEXT NOT NULL,
                payload TEXT NOT NULL,idempotency_key TEXT NOT NULL,status TEXT NOT NULL,
                attempts INTEGER NOT NULL,max_attempts INTEGER NOT NULL,not_before REAL NOT NULL,
                lease_token TEXT,lease_owner TEXT,lease_until REAL,result TEXT,error TEXT,
                created_at REAL NOT NULL,updated_at REAL NOT NULL,
                UNIQUE(user_id,idempotency_key));
              CREATE INDEX IF NOT EXISTS job_queue ON jobs(status,not_before,created_at);
              CREATE TABLE IF NOT EXISTS budget_reservations (
                id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,reservation_key TEXT NOT NULL,
                day TEXT NOT NULL,week TEXT NOT NULL,calls INTEGER NOT NULL,tokens INTEGER NOT NULL,
                candidates INTEGER NOT NULL,status TEXT NOT NULL,created_at REAL NOT NULL,
                UNIQUE(user_id,reservation_key));
            """)

    @contextmanager
    def _schema_connection(self):
        import fcntl
        # journal_mode changes require an exclusive startup lock. Two fresh
        # SQLite connections can otherwise both try to upgrade the database
        # mode and fail immediately with SQLITE_BUSY despite busy_timeout.
        # The file lock serializes API threads and independent worker processes;
        # ordinary reads/writes continue to use SQLite's own transactions.
        with (self.root / "research.sqlite3.init.lock").open("a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                with self.connection() as conn:
                    if conn.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
                        conn.execute("PRAGMA journal_mode=WAL")
                    yield conn
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    @contextmanager
    def connection(self, *, write=False):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA foreign_keys=ON")
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if write:
                conn.commit()
        except BaseException:
            if write:
                conn.rollback()
            raise
        finally:
            conn.close()

    def put_entity(self, user_id, kind, payload, entity_id=None):
        uid, eid = _uid(user_id), entity_id or str(uuid.uuid4())
        if not kind or not isinstance(payload, dict):
            raise ValueError("Entity needs a kind and object payload")
        encoded, digest = canonical_json(payload), content_hash(payload)
        with self.connection(write=True) as conn:
            old = conn.execute("SELECT * FROM entities WHERE user_id=? AND kind=? AND id=?", (uid, kind, eid)).fetchone()
            if old is not None:
                if old["content_hash"] != digest:
                    raise ImmutableConflict("Entity id already contains different immutable evidence")
                return _decode(old)
            conn.execute("INSERT INTO entities VALUES(?,?,?,?,?,?)", (uid, kind, eid, encoded, digest, time.time()))
            return _decode(conn.execute("SELECT * FROM entities WHERE user_id=? AND kind=? AND id=?", (uid, kind, eid)).fetchone())

    def get_entity(self, user_id, kind, entity_id):
        with self.connection() as conn:
            return _decode(conn.execute("SELECT * FROM entities WHERE user_id=? AND kind=? AND id=?", (_uid(user_id), kind, entity_id)).fetchone())

    def list_entities(self, user_id, kind, limit=100):
        with self.connection() as conn:
            return [_decode(r) for r in conn.execute("SELECT * FROM entities WHERE user_id=? AND kind=? ORDER BY created_at DESC,id DESC LIMIT ?", (_uid(user_id), kind, max(1, min(int(limit), 10000))))]

    def get_state(self, user_id, key, default=None):
        with self.connection() as conn:
            row = conn.execute("SELECT value FROM state WHERE user_id=? AND key=?", (_uid(user_id), key)).fetchone()
            return json.loads(row[0]) if row else default

    def set_state(self, user_id, key, value):
        with self.connection(write=True) as conn:
            self._set_state(conn, _uid(user_id), key, value)
        return value

    @staticmethod
    def _set_state(conn, uid, key, value):
        conn.execute("INSERT INTO state VALUES(?,?,?,?) ON CONFLICT(user_id,key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (uid, key, canonical_json(value), time.time()))

    def compare_and_set_state(self, user_id, key, expected, value):
        uid = _uid(user_id)
        with self.connection(write=True) as conn:
            row = conn.execute("SELECT value FROM state WHERE user_id=? AND key=?", (uid, key)).fetchone()
            current = json.loads(row[0]) if row else None
            if canonical_json(current) != canonical_json(expected):
                return False
            self._set_state(conn, uid, key, value)
            return True

    def get_config(self, user_id):
        return {**DEFAULT_CONFIG, **self.get_state(user_id, "config", {})}

    def update_config(self, user_id, patch):
        if not isinstance(patch, dict) or set(patch) - set(DEFAULT_CONFIG):
            raise ValueError("Unknown research configuration keys")
        for key, val in patch.items():
            if key in ("enabled", "paused"):
                if type(val) is not bool:
                    raise ValueError(f"{key} must be boolean")
            elif key in ("daily_calls", "daily_tokens", "weekly_candidates"):
                if type(val) is not int or val < 0:
                    raise ValueError(f"{key} must be a nonnegative integer")
            else:
                if isinstance(val, bool) or not isinstance(val, (float, int)) or not 0 < val <= (1 if key != "initial_cash" else 1e12):
                    raise ValueError(f"Invalid {key}")
        with self.connection(write=True) as conn:
            row = conn.execute("SELECT value FROM state WHERE user_id=? AND key='config'", (_uid(user_id),)).fetchone()
            config = {**DEFAULT_CONFIG, **(json.loads(row[0]) if row else {}), **patch}
            self._set_state(conn, _uid(user_id), "config", config)
            return config

    def queue_job(self, user_id, kind, payload, idempotency_key, max_attempts=3, not_before=None):
        uid, now = _uid(user_id), time.time()
        if not kind or not idempotency_key or not isinstance(payload, dict) or not 1 <= int(max_attempts) <= 100:
            raise ValueError("Invalid job")
        encoded = canonical_json(payload)
        with self.connection(write=True) as conn:
            old = conn.execute("SELECT * FROM jobs WHERE user_id=? AND idempotency_key=?", (uid, idempotency_key)).fetchone()
            if old:
                if old["kind"] != kind or old["payload"] != encoded:
                    raise ImmutableConflict("Idempotency key reused with different job")
                return _decode(old)
            jid = str(uuid.uuid4())
            conn.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (jid, uid, kind, encoded, idempotency_key, "queued", 0, int(max_attempts), now if not_before is None else float(not_before), None, None, None, None, None, now, now))
            return _decode(conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())

    def get_job(self, user_id, job_id):
        with self.connection() as conn:
            return _decode(conn.execute("SELECT * FROM jobs WHERE user_id=? AND id=?", (_uid(user_id), job_id)).fetchone())

    def list_jobs(self, user_id, limit=100, status=None):
        with self.connection() as conn:
            return [_decode(r) for r in conn.execute("SELECT * FROM jobs WHERE user_id=? AND (? IS NULL OR status=?) ORDER BY created_at DESC LIMIT ?", (_uid(user_id), status, status, max(1, min(int(limit), 10000))))]

    def claim_job(self, worker_id, lease_seconds=120, user_id=None, kinds=None):
        if not worker_id or not 1 <= lease_seconds <= 86400:
            raise ValueError("Invalid worker or lease duration")
        now = time.time()
        with self.connection(write=True) as conn:
            conn.execute("UPDATE jobs SET status='failed',error='Lease expired after maximum attempts',updated_at=? WHERE status='running' AND lease_until<=? AND attempts>=max_attempts", (now, now))
            clauses = ["((status IN ('queued','retry') AND not_before<=?) OR (status='running' AND lease_until<=?))", "attempts<max_attempts"]
            args: list[Any] = [now, now]
            if user_id is not None:
                clauses.append("user_id=?"); args.append(_uid(user_id))
            if kinds is not None:
                if not kinds:
                    return None
                clauses.append("kind IN (%s)" % ",".join("?" for _ in kinds)); args.extend(kinds)
            # Pause checks belong in the same transaction as leasing, so paused users
            # never consume queue work even when another process toggles settings.
            rows = conn.execute("SELECT * FROM jobs WHERE " + " AND ".join(clauses) +
                                " ORDER BY CASE WHEN kind='watch_review' THEN 0 ELSE 1 END,not_before,created_at", args).fetchall()
            for row in rows:
                config_row = conn.execute("SELECT value FROM state WHERE user_id=? AND key='config'", (row["user_id"],)).fetchone()
                config = {**DEFAULT_CONFIG, **(json.loads(config_row[0]) if config_row else {})}
                if not config["enabled"] or (config["paused"] and row["kind"] not in {"collect", "paper", "signals", "rollback", "source_maintenance", "watch_review"}):
                    continue
                token = str(uuid.uuid4())
                conn.execute("UPDATE jobs SET status='running',attempts=attempts+1,lease_token=?,lease_owner=?,lease_until=?,updated_at=? WHERE id=?", (token, worker_id, now + lease_seconds, now, row["id"]))
                return _decode(conn.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())
            return None

    @staticmethod
    def _leased(conn, uid, job_id, token):
        row = conn.execute("SELECT * FROM jobs WHERE user_id=? AND id=? AND status='running' AND lease_token=? AND lease_until>?", (uid, job_id, token, time.time())).fetchone()
        if row is None:
            raise LeaseLost("Job lease expired or belongs to another worker")
        return row

    def renew_job(self, user_id, job_id, lease_token, lease_seconds=120):
        if not 1 <= lease_seconds <= 86400:
            raise ValueError("Invalid lease duration")
        with self.connection(write=True) as conn:
            self._leased(conn, _uid(user_id), job_id, lease_token)
            conn.execute("UPDATE jobs SET lease_until=?,updated_at=? WHERE id=?", (time.time() + lease_seconds, time.time(), job_id))
        return self.get_job(user_id, job_id)

    heartbeat_job = renew_job

    def complete_job(self, user_id, job_id, lease_token, result):
        with self.connection(write=True) as conn:
            self._leased(conn, _uid(user_id), job_id, lease_token)
            conn.execute("UPDATE jobs SET status='completed',result=?,lease_until=NULL,updated_at=? WHERE id=?", (canonical_json(result), time.time(), job_id))
        return self.get_job(user_id, job_id)

    def fail_job(self, user_id, job_id, lease_token, error, retry_delay=60):
        with self.connection(write=True) as conn:
            row = self._leased(conn, _uid(user_id), job_id, lease_token)
            status = "retry" if row["attempts"] < row["max_attempts"] else "failed"
            conn.execute("UPDATE jobs SET status=?,error=?,not_before=?,lease_until=NULL,updated_at=? WHERE id=?", (status, str(error)[:4000], time.time() + max(0, retry_delay), time.time(), job_id))
        return self.get_job(user_id, job_id)

    @staticmethod
    def _periods(now=None):
        day = datetime.fromtimestamp(time.time() if now is None else now, ZoneInfo("Asia/Shanghai")).date()
        return day.isoformat(), (day - timedelta(days=day.weekday())).isoformat()

    @staticmethod
    def _budget_event(conn, uid, reservation_id, action, payload):
        event = {"reservation_id": reservation_id, "action": action, **payload}
        conn.execute("INSERT OR IGNORE INTO entities VALUES(?,?,?,?,?,?)",
                     (uid, "budget_event", reservation_id + ":" + action,
                      canonical_json(event), content_hash(event), time.time()))

    @staticmethod
    def _usage(conn, uid, day, week):
        row = conn.execute("SELECT COALESCE(SUM(CASE WHEN day=? THEN calls ELSE 0 END),0) AS calls,COALESCE(SUM(CASE WHEN day=? THEN tokens ELSE 0 END),0) AS tokens,COALESCE(SUM(CASE WHEN week=? THEN candidates ELSE 0 END),0) AS candidates FROM budget_reservations WHERE user_id=? AND status IN ('reserved','settled') AND (day=? OR week=?)", (day, day, week, uid, day, week)).fetchone()
        return dict(row)

    def reserve_budget(self, user_id, reservation_key, calls=0, tokens=0, candidates=0, now=None):
        uid = _uid(user_id)
        if not reservation_key or any(type(v) is not int or v < 0 for v in (calls, tokens, candidates)):
            raise ValueError("Budget requests must be nonnegative integers")
        day, week = self._periods(now)
        with self.connection(write=True) as conn:
            old = conn.execute("SELECT * FROM budget_reservations WHERE user_id=? AND reservation_key=?", (uid, reservation_key)).fetchone()
            if old:
                if old["status"] == "released":
                    raise ValueError("Released reservation keys cannot be reused")
                if old["status"] == "reserved" and (old["calls"], old["tokens"], old["candidates"]) != (calls, tokens, candidates):
                    raise ImmutableConflict("Reservation key reused with different allowance")
                return dict(old)
            row = conn.execute("SELECT value FROM state WHERE user_id=? AND key='config'", (uid,)).fetchone()
            config = {**DEFAULT_CONFIG, **(json.loads(row[0]) if row else {})}
            if config["paused"] or not config["enabled"]:
                raise BudgetExceeded("Research is paused or disabled")
            usage = self._usage(conn, uid, day, week)
            for key, requested, cap in (("calls", calls, config["daily_calls"]), ("tokens", tokens, config["daily_tokens"]), ("candidates", candidates, config["weekly_candidates"])):
                if usage[key] + requested > cap:
                    raise BudgetExceeded(f"Research {key} budget exhausted")
            rid = str(uuid.uuid4())
            conn.execute("INSERT INTO budget_reservations VALUES(?,?,?,?,?,?,?,?,?,?)", (rid, uid, reservation_key, day, week, calls, tokens, candidates, "reserved", time.time() if now is None else now))
            self._budget_event(conn, uid, rid, "reserved", {"calls": calls, "tokens": tokens, "candidates": candidates, "day": day, "week": week})
            return dict(conn.execute("SELECT * FROM budget_reservations WHERE id=?", (rid,)).fetchone())

    def settle_budget(self, user_id, reservation_id, calls=None, tokens=None, candidates=None):
        with self.connection(write=True) as conn:
            row = conn.execute("SELECT * FROM budget_reservations WHERE user_id=? AND (id=? OR reservation_key=?)", (_uid(user_id), reservation_id, reservation_id)).fetchone()
            if row is None or row["status"] == "released":
                raise ValueError("Unknown or released reservation")
            actual = {k: row[k] if v is None else v for k, v in (("calls", calls), ("tokens", tokens), ("candidates", candidates))}
            if any(type(v) is not int or v < 0 or v > row[k] for k, v in actual.items()):
                raise BudgetExceeded("Actual usage exceeds reservation; reserve the maximum before dispatch")
            if row["status"] == "settled" and any(actual[k] != row[k] for k in actual):
                raise ImmutableConflict("Settled usage is immutable")
            conn.execute("UPDATE budget_reservations SET calls=?,tokens=?,candidates=?,status='settled' WHERE id=?", (actual["calls"], actual["tokens"], actual["candidates"], row["id"]))
            self._budget_event(conn, _uid(user_id), row["id"], "settled", actual)
            return dict(conn.execute("SELECT * FROM budget_reservations WHERE id=?", (row["id"],)).fetchone())

    def release_budget(self, user_id, reservation_id):
        with self.connection(write=True) as conn:
            row = conn.execute("SELECT * FROM budget_reservations WHERE user_id=? AND (id=? OR reservation_key=?) AND status='reserved'", (_uid(user_id), reservation_id, reservation_id)).fetchone()
            if row is None:
                return False
            conn.execute("UPDATE budget_reservations SET status='released' WHERE id=?", (row["id"],))
            self._budget_event(conn, _uid(user_id), row["id"], "released", {k: row[k] for k in ("calls", "tokens", "candidates")})
            return True

    def budget_status(self, user_id, now=None):
        day, week = self._periods(now)
        with self.connection() as conn:
            used = self._usage(conn, _uid(user_id), day, week)
        config = self.get_config(user_id)
        limits = {"calls": config["daily_calls"], "tokens": config["daily_tokens"], "candidates": config["weekly_candidates"]}
        return {"day": day, "week": week, "timezone": "Asia/Shanghai", "used": used, "limits": limits, "remaining": {k: max(0, limits[k] - used[k]) for k in used}}


def get_store() -> ResearchStore:
    return ResearchStore()


# Explicit wrappers avoid an import-time singleton binding the developer data dir.
def put_entity(user_id, kind, payload, entity_id=None): return get_store().put_entity(user_id, kind, payload, entity_id)
def get_entity(user_id, kind, entity_id): return get_store().get_entity(user_id, kind, entity_id)
def list_entities(user_id, kind, limit=100): return get_store().list_entities(user_id, kind, limit)
def get_state(user_id, key, default=None): return get_store().get_state(user_id, key, default)
def set_state(user_id, key, value): return get_store().set_state(user_id, key, value)
def compare_and_set_state(user_id, key, expected, value): return get_store().compare_and_set_state(user_id, key, expected, value)
def get_config(user_id): return get_store().get_config(user_id)
def update_config(user_id, patch): return get_store().update_config(user_id, patch)
def queue_job(user_id, kind, payload, idempotency_key, max_attempts=3, not_before=None): return get_store().queue_job(user_id, kind, payload, idempotency_key, max_attempts, not_before)
def claim_job(worker_id, lease_seconds=120, user_id=None, kinds=None): return get_store().claim_job(worker_id, lease_seconds, user_id, kinds)
def get_job(user_id, job_id): return get_store().get_job(user_id, job_id)
def list_jobs(user_id, limit=100, status=None): return get_store().list_jobs(user_id, limit, status)
def renew_job(user_id, job_id, lease_token, lease_seconds=120): return get_store().renew_job(user_id, job_id, lease_token, lease_seconds)
heartbeat_job = renew_job
def complete_job(user_id, job_id, lease_token, result): return get_store().complete_job(user_id, job_id, lease_token, result)
def fail_job(user_id, job_id, lease_token, error, retry_delay=60): return get_store().fail_job(user_id, job_id, lease_token, error, retry_delay)
def reserve_budget(user_id, reservation_key, calls=0, tokens=0, candidates=0, now=None): return get_store().reserve_budget(user_id, reservation_key, calls, tokens, candidates, now)
def settle_budget(user_id, reservation_id, calls=None, tokens=None, candidates=None): return get_store().settle_budget(user_id, reservation_id, calls, tokens, candidates)
def release_budget(user_id, reservation_id): return get_store().release_budget(user_id, reservation_id)
def budget_status(user_id, now=None): return get_store().budget_status(user_id, now)
