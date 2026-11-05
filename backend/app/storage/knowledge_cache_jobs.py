"""Resumable local rebuild tasks in the shared knowledge registry."""
from contextlib import contextmanager
import time
import uuid

from app.storage import knowledge_cache_store as cache

_SCHEMA = """
CREATE TABLE IF NOT EXISTS maintenance_jobs (
 id TEXT PRIMARY KEY, scope TEXT NOT NULL, symbol TEXT, status TEXT NOT NULL,
 requested_at REAL NOT NULL, started_at REAL, finished_at REAL,
 total INTEGER NOT NULL, completed INTEGER NOT NULL DEFAULT 0,
 failed INTEGER NOT NULL DEFAULT 0, actor_id INTEGER, reason TEXT NOT NULL,
 owner TEXT, lease_until REAL NOT NULL DEFAULT 0, error_code TEXT);
CREATE TABLE IF NOT EXISTS maintenance_items (
 job_id TEXT NOT NULL, ordinal INTEGER NOT NULL, symbol TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', error_code TEXT,
 PRIMARY KEY(job_id,ordinal));
CREATE TABLE IF NOT EXISTS maintenance_schedule (key TEXT PRIMARY KEY,value REAL NOT NULL);
"""


@contextmanager
def connection():
    # Reuse the registry's cross-process descriptor/path guard. No independent
    # connection may bypass its file lock or directory integrity checks.
    with cache._locked() as (_, conn):
        conn.executescript(_SCHEMA)
        yield conn


def _job(row):
    if row is None:
        return None
    value = dict(row)
    for field in ("requested_at", "started_at", "finished_at"):
        value[field] = cache._iso(value[field])
    value["progress_percent"] = round(100 * value["completed"] / max(1, value["total"]), 1)
    value.pop("owner", None)
    value.pop("lease_until", None)
    return value


def enqueue(scope, symbol, items, *, actor_id=None, reason="手动重建", periodic=False):
    now, identifier = time.time(), uuid.uuid4().hex
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if periodic:
            next_run = conn.execute("SELECT value FROM maintenance_schedule WHERE key='next_sync'").fetchone()
            if next_run and next_run[0] > now:
                return None
            conn.execute("INSERT INTO maintenance_schedule VALUES('next_sync',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (now + 86400,))
        existing = conn.execute("SELECT * FROM maintenance_jobs WHERE scope=? AND symbol IS ? AND status IN ('queued','running') ORDER BY requested_at LIMIT 1", (scope, symbol)).fetchone()
        if existing:
            return {"job": _job(existing), "deduplicated": True}
        if conn.execute("SELECT COUNT(*) FROM maintenance_jobs WHERE status IN ('queued','running')").fetchone()[0] >= 10000:
            raise ValueError("知识更新队列已满，请稍后重试")
        conn.execute("INSERT INTO maintenance_jobs(id,scope,symbol,status,requested_at,total,actor_id,reason) VALUES(?,?,?,'queued',?,?,?,?)",
                     (identifier, scope, symbol, now, len(items), actor_id, reason))
        conn.executemany("INSERT INTO maintenance_items(job_id,ordinal,symbol) VALUES(?,?,?)", [(identifier, i, s) for i, s in enumerate(items)])
        row = conn.execute("SELECT * FROM maintenance_jobs WHERE id=?", (identifier,)).fetchone()
    return {"job": _job(row), "deduplicated": False}


def claim(*, now=None):
    now = time.time() if now is None else now
    owner = uuid.uuid4().hex
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM maintenance_jobs WHERE status='running' AND lease_until>?", (now,)).fetchone():
            return None
        row = conn.execute("SELECT * FROM maintenance_jobs WHERE status IN ('queued','running') ORDER BY requested_at,id LIMIT 1").fetchone()
        if not row:
            return None
        conn.execute("UPDATE maintenance_jobs SET owner=?,lease_until=?,status='running',started_at=COALESCE(started_at,?) WHERE id=?",
                     (owner, now + 600, now, row["id"]))
        return {**dict(row), "owner": owner}


def pending(job, limit=10):
    with connection() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM maintenance_items WHERE job_id=? AND status='pending' ORDER BY ordinal LIMIT ?", (job["id"], limit))]


def renew(job, *, now=None):
    """Atomically verify a live owner and extend its lease before doing work.

    An expired owner cannot resurrect itself: it must claim the job again. The
    short transaction ends before callers access the knowledge cache.
    """
    now = time.time() if now is None else now
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        return bool(conn.execute("UPDATE maintenance_jobs SET lease_until=? WHERE id=? AND owner=? "
                                 "AND status='running' AND lease_until>?", (now + 600, job["id"], job["owner"], now)).rowcount)


def complete_item(job, ordinal, *, error_code=None):
    now = time.time()
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        owner = conn.execute("SELECT owner,status,lease_until FROM maintenance_jobs WHERE id=?", (job["id"],)).fetchone()
        if not owner or owner["owner"] != job["owner"] or owner["status"] != "running" or owner["lease_until"] <= now:
            return False
        updated = conn.execute("UPDATE maintenance_items SET status=?,error_code=? WHERE job_id=? AND ordinal=? AND status='pending'",
                               ("failed" if error_code else "completed", error_code, job["id"], ordinal)).rowcount
        if updated:
            conn.execute("UPDATE maintenance_jobs SET completed=completed+1,failed=failed+?,lease_until=?,error_code=COALESCE(?,error_code) WHERE id=?",
                         (int(bool(error_code)), now + 600, error_code, job["id"]))
        return bool(updated)


def release(job):
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM maintenance_jobs WHERE id=? AND owner=?", (job["id"], job["owner"])).fetchone()
        if not row:
            return
        done = row["completed"] >= row["total"]
        status = ("completed" if not row["failed"] else "failed" if row["failed"] == row["total"] else "partial") if done else "queued"
        conn.execute("UPDATE maintenance_jobs SET status=?,finished_at=?,owner=NULL,lease_until=0 WHERE id=?",
                     (status, time.time() if done else None, job["id"]))


def status():
    with connection() as conn:
        active = conn.execute("SELECT * FROM maintenance_jobs WHERE status IN ('queued','running') ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END,requested_at LIMIT 1").fetchone()
        recent = conn.execute("SELECT * FROM maintenance_jobs ORDER BY requested_at DESC LIMIT 20").fetchall()
        next_run = conn.execute("SELECT value FROM maintenance_schedule WHERE key='next_sync'").fetchone()
    return {"active_job": _job(active), "recent_jobs": [_job(row) for row in recent],
            "next_sync_at": cache._iso(next_run[0]) if next_run else None}


def get_job(job_id):
    """Read one task independently of the recent-job display window."""
    with connection() as conn:
        row = conn.execute("SELECT * FROM maintenance_jobs WHERE id=?", (job_id,)).fetchone()
    return _job(row)
