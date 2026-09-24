"""SQLite snapshots and leased jobs; every selected result belongs to one user."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
import json
import sqlite3
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.config import settings

LEASE_SECONDS = 300
MAX_ATTEMPTS = 2
MAX_ACTIVE_JOBS = 1000
MAX_USER_JOBS = 100
MAX_USER_SYMBOLS = 100
MAX_SYMBOLS = 5000
MAX_RESULT_BYTES = 512 * 1024
_SH = ZoneInfo("Asia/Shanghai")


@contextmanager
def connect():
    path = settings.data_dir / "xueqiu_comments.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=10000")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS comment_snapshots (
                uid INTEGER NOT NULL, symbol TEXT NOT NULL,
                result_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'not_started',
                error TEXT, warnings_json TEXT NOT NULL DEFAULT '[]',
                last_attempt_at INTEGER, updated_at INTEGER, last_manual_at INTEGER,
                PRIMARY KEY(uid, symbol)
            );
            CREATE TABLE IF NOT EXISTS comment_jobs (
                id TEXT PRIMARY KEY, uid INTEGER NOT NULL, symbol TEXT NOT NULL,
                status TEXT NOT NULL, mode TEXT NOT NULL, target_date TEXT, source TEXT NOT NULL,
                progress INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL,
                available_at INTEGER NOT NULL, owner TEXT, lease_until INTEGER,
                finished_at INTEGER
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_comment_job
                ON comment_jobs(uid,symbol) WHERE status IN ('queued','running');
            CREATE INDEX IF NOT EXISTS comment_jobs_claim ON comment_jobs(status,available_at,created_at);
            CREATE TABLE IF NOT EXISTS comment_daily_keys (
                uid INTEGER NOT NULL, symbol TEXT NOT NULL, target_date TEXT NOT NULL,
                job_id TEXT NOT NULL, PRIMARY KEY(uid,symbol,target_date)
            );
        """)
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def init_db():
    with connect():
        pass


def read(uid: int, symbol: str) -> tuple[dict, dict | None]:
    with connect() as db:
        # Both rows describe one view: a worker may finish between these SELECTs.
        # Keep the old running job with old results, or the new completed pair.
        db.execute("BEGIN")
        row = db.execute("SELECT * FROM comment_snapshots WHERE uid=? AND symbol=?", (uid, symbol)).fetchone()
        job = db.execute(
            "SELECT * FROM comment_jobs WHERE uid=? AND symbol=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (uid, symbol),
        ).fetchone()
    state = dict(row) if row else {}
    state["result"] = json.loads(state.pop("result_json", "{}"))
    state["warnings"] = json.loads(state.pop("warnings_json", "[]"))
    return state, dict(job) if job else None


def daily_exists(uid: int, symbol: str, target_date: str) -> bool:
    with connect() as db:
        return db.execute(
            "SELECT 1 FROM comment_daily_keys WHERE uid=? AND symbol=? AND target_date=?",
            (uid, symbol, target_date),
        ).fetchone() is not None


def enqueue(uid: int, symbol: str, *, mode: str, target_date: str | None, source: str, now: int | None = None) -> str:
    """Return queued/merged/cooldown/daily_done/limited. No network or hashing."""
    now = int(time.time()) if now is None else now
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        cutoff = (datetime.fromtimestamp(now, _SH).date() - timedelta(days=2)).isoformat()
        db.execute("DELETE FROM comment_daily_keys WHERE target_date<?", (cutoff,))
        if source == "scheduled" and db.execute(
            "SELECT 1 FROM comment_daily_keys WHERE uid=? AND symbol=? AND target_date=?", (uid, symbol, target_date)
        ).fetchone():
            return "daily_done"
        active = db.execute(
            "SELECT * FROM comment_jobs WHERE uid=? AND symbol=? AND status IN ('queued','running')", (uid, symbol)
        ).fetchone()
        if active and source == "scheduled" and active["source"] == "scheduled" and active["status"] == "queued" and (active["target_date"] or "") < (target_date or ""):
            # A restart must catch up yesterday, not drain a backlog of older days.
            db.execute("UPDATE comment_jobs SET status='error',finished_at=? WHERE id=?", (now, active["id"]))
            active = None
        if active:
            if source == "scheduled" and active["mode"] == mode and active["target_date"] == target_date:
                db.execute("INSERT OR IGNORE INTO comment_daily_keys VALUES(?,?,?,?)", (uid, symbol, target_date, active["id"]))
            return "merged"
        state = db.execute("SELECT * FROM comment_snapshots WHERE uid=? AND symbol=?", (uid, symbol)).fetchone()
        if source == "manual" and state and state["last_manual_at"] is not None and now - state["last_manual_at"] < 60:
            return "cooldown"
        if db.execute("SELECT COUNT(*) FROM comment_jobs WHERE status IN ('queued','running')").fetchone()[0] >= MAX_ACTIVE_JOBS:
            return "limited"
        if db.execute("SELECT COUNT(*) FROM comment_jobs WHERE uid=? AND status IN ('queued','running')", (uid,)).fetchone()[0] >= MAX_USER_JOBS:
            return "limited"
        if not state:
            if db.execute("SELECT COUNT(*) FROM comment_snapshots").fetchone()[0] >= MAX_SYMBOLS:
                return "limited"
            if db.execute("SELECT COUNT(*) FROM comment_snapshots WHERE uid=?", (uid,)).fetchone()[0] >= MAX_USER_SYMBOLS:
                return "limited"
            db.execute("INSERT INTO comment_snapshots(uid,symbol) VALUES(?,?)", (uid, symbol))
        # Keep one current/recent job per symbol; daily keys survive job pruning.
        db.execute("DELETE FROM comment_jobs WHERE uid=? AND symbol=? AND status NOT IN ('queued','running')", (uid, symbol))
        job_id = uuid4().hex
        db.execute(
            "INSERT INTO comment_jobs(id,uid,symbol,status,mode,target_date,source,created_at,available_at) VALUES(?,?,?,'queued',?,?,?,?,?)",
            (job_id, uid, symbol, mode, target_date, source, now, now),
        )
        db.execute(
            "UPDATE comment_snapshots SET last_attempt_at=?,last_manual_at=CASE WHEN ?='manual' THEN ? ELSE last_manual_at END WHERE uid=? AND symbol=?",
            (now, source, now, uid, symbol),
        )
        if source == "scheduled":
            db.execute("INSERT INTO comment_daily_keys VALUES(?,?,?,?)", (uid, symbol, target_date, job_id))
    return "queued"


def claim(owner: str, *, now: int | None = None) -> dict | None:
    now = int(time.time()) if now is None else now
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        expired = db.execute("SELECT * FROM comment_jobs WHERE status='running' AND lease_until<=?", (now,)).fetchall()
        for row in expired:
            if row["attempts"] < MAX_ATTEMPTS:
                db.execute("UPDATE comment_jobs SET status='queued',owner=NULL,lease_until=NULL,available_at=? WHERE id=?", (now, row["id"]))
            else:
                db.execute("UPDATE comment_jobs SET status='error',finished_at=?,owner=NULL,lease_until=NULL WHERE id=?", (now, row["id"]))
                db.execute(
                    "UPDATE comment_snapshots SET status='error',error=?,warnings_json='[]',last_attempt_at=? WHERE uid=? AND symbol=?",
                    ("任务因服务中断未完成，请手动重试", now, row["uid"], row["symbol"]),
                )
        yesterday = (datetime.fromtimestamp(now, _SH).date() - timedelta(days=1)).isoformat()
        outdated = db.execute(
            "SELECT * FROM comment_jobs WHERE status='queued' AND source='scheduled' AND target_date<?", (yesterday,)
        ).fetchall()
        for row in outdated:
            db.execute("UPDATE comment_jobs SET status='error',finished_at=? WHERE id=?", (now, row["id"]))
            db.execute(
                "UPDATE comment_snapshots SET status='error',error=? WHERE uid=? AND symbol=?",
                ("旧日期自动任务已跳过，将按当日计划刷新", row["uid"], row["symbol"]),
            )
        job = db.execute(
            "SELECT * FROM comment_jobs WHERE status='queued' AND available_at<=? ORDER BY created_at,rowid LIMIT 1", (now,)
        ).fetchone()
        if job is None:
            return None
        db.execute(
            "UPDATE comment_jobs SET status='running',owner=?,lease_until=?,attempts=attempts+1,progress=0,total=0 WHERE id=?",
            (owner, now + LEASE_SECONDS, job["id"]),
        )
        return dict(db.execute("SELECT * FROM comment_jobs WHERE id=?", (job["id"],)).fetchone())


def heartbeat(job_id: str, owner: str, *, progress: int | None = None, total: int | None = None, now: int | None = None) -> bool:
    now = int(time.time()) if now is None else now
    with connect() as db:
        if progress is None:
            result = db.execute("UPDATE comment_jobs SET lease_until=? WHERE id=? AND owner=? AND status='running'", (now + LEASE_SECONDS, job_id, owner))
        else:
            total = max(0, min(int(total or 0), 200))
            progress = max(0, min(int(progress), total))
            result = db.execute(
                "UPDATE comment_jobs SET lease_until=?,progress=?,total=? WHERE id=? AND owner=? AND status='running'",
                (now + LEASE_SECONDS, progress, total, job_id, owner),
            )
        return result.rowcount == 1


def finish(job: dict, owner: str, result: dict, *, now: int | None = None) -> bool:
    now = int(time.time()) if now is None else now
    status = result["status"]
    error = str(result.get("error") or "")[:500] or None
    warnings = [str(w)[:500] for w in result.get("warnings", [])[:20]]
    payload = {key: result.get(key) for key in ("items", "raw_count", "analyzed_count", "selected_count", "rejected_count", "fetched_at", "model")}
    payload.update(mode=job["mode"], target_date=job["target_date"])
    raw = json.dumps(payload, ensure_ascii=False)
    # The UI needs a recent selection, not an unbounded archive of full threads.
    while len(raw.encode("utf-8")) > MAX_RESULT_BYTES and payload.get("items"):
        payload["items"] = payload["items"][:-1]
        payload["selected_count"] = len(payload["items"])
        raw = json.dumps(payload, ensure_ascii=False)
        status = "partial"
    if status == "partial" and "结果已按存储上限截取" not in warnings and len(payload.get("items") or []) < len(result.get("items") or []):
        warnings.append("结果已按存储上限截取")
    replace = status in {"ready", "empty"} or (status == "partial" and bool(payload.get("items")))
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM comment_jobs WHERE id=? AND owner=? AND status='running'", (job["id"], owner)).fetchone()
        if not row:
            return False
        retry = status == "error" and row["source"] == "scheduled" and row["attempts"] < MAX_ATTEMPTS
        db.execute(
            "UPDATE comment_jobs SET status=?,finished_at=?,available_at=?,owner=NULL,lease_until=NULL WHERE id=?",
            ("queued" if retry else status, None if retry else now, now + 300 if retry else now, job["id"]),
        )
        db.execute(
            "UPDATE comment_snapshots SET status=?,error=?,warnings_json=?,last_attempt_at=? WHERE uid=? AND symbol=?",
            (status, error, json.dumps(warnings, ensure_ascii=False), now, job["uid"], job["symbol"]),
        )
        if replace:
            db.execute("UPDATE comment_snapshots SET result_json=?,updated_at=? WHERE uid=? AND symbol=?", (raw, now, job["uid"], job["symbol"]))
        if result.get("retry_when_configured"):
            db.execute("DELETE FROM comment_daily_keys WHERE job_id=?", (job["id"],))
    return True
