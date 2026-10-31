"""Persistent public overseas headlines, source health, and cross-worker leases."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator

from app.config import settings
from app.storage.sqlite_bootstrap import connection


def _path():
    return settings.data_dir / "overseas_news.db"


_SCHEMA = """
    CREATE TABLE IF NOT EXISTS headlines (
        id TEXT PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL,
        summary TEXT NOT NULL, source TEXT NOT NULL, source_id TEXT NOT NULL,
        published_ts REAL, fetched_at REAL NOT NULL, UNIQUE(source_id,url)
    );
    CREATE INDEX IF NOT EXISTS headline_published ON headlines(published_ts DESC,fetched_at DESC);
    CREATE INDEX IF NOT EXISTS headline_source ON headlines(source_id,published_ts DESC);
    CREATE TABLE IF NOT EXISTS sources (id TEXT PRIMARY KEY, state TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS leases (name TEXT PRIMARY KEY, owner TEXT NOT NULL, expires_at REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS archive_outbox (id TEXT PRIMARY KEY,payload TEXT NOT NULL,fetched_at REAL NOT NULL);
"""


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    with connection(_path(), _SCHEMA) as conn:
        yield conn


def init_db() -> None:
    with _connect():
        pass


def sources() -> dict[str, dict[str, Any]]:
    with _connect() as conn:
        return {row["id"]: json.loads(row["state"]) for row in conn.execute("SELECT * FROM sources")}


def update_source(source_id: str, **changes: Any) -> dict[str, Any]:
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT state FROM sources WHERE id=?", (source_id,)).fetchone()
        state = json.loads(row[0]) if row else {}
        state.update(changes)
        conn.execute("INSERT INTO sources(id,state) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state",
                     (source_id, json.dumps(state, ensure_ascii=False)))
        return state


def metadata(key: str, default: Any = None) -> Any:
    with _connect() as conn:
        row = conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default


def set_metadata(key: str, value: Any) -> None:
    with _connect() as conn:
        conn.execute("INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, json.dumps(value, ensure_ascii=False)))


def acquire_lease(name: str, owner: str, ttl: float, *, now: float | None = None) -> bool:
    stamp = time.time() if now is None else now
    with _connect() as conn:
        result = conn.execute("""INSERT INTO leases(name,owner,expires_at) VALUES(?,?,?)
            ON CONFLICT(name) DO UPDATE SET owner=excluded.owner,expires_at=excluded.expires_at
            WHERE leases.expires_at<=? OR leases.owner=excluded.owner""", (name, owner, stamp + ttl, stamp))
        return result.rowcount == 1


def renew_lease(name: str, owner: str, ttl: float) -> bool:
    with _connect() as conn:
        result = conn.execute("UPDATE leases SET expires_at=? WHERE name=? AND owner=? AND expires_at>?",
                              (time.time() + ttl, name, owner, time.time()))
        return result.rowcount == 1


def release_lease(name: str, owner: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM leases WHERE name=? AND owner=?", (name, owner))


def lease_active(name: str) -> bool:
    with _connect() as conn:
        return conn.execute("SELECT 1 FROM leases WHERE name=? AND expires_at>?", (name, time.time())).fetchone() is not None


def upsert_many(items: list[dict[str, Any]]) -> int:
    """A publisher URL remains the same event when its headline is corrected."""
    if not items:
        return 0
    with _connect() as conn:
        conn.executemany("""INSERT INTO headlines(id,title,url,summary,source,source_id,published_ts,fetched_at)
            VALUES(:id,:title,:url,:summary,:source,:source_id,:published_ts,:fetched_at)
            ON CONFLICT(source_id,url) DO UPDATE SET title=excluded.title,summary=excluded.summary,
                source=excluded.source,published_ts=COALESCE(excluded.published_ts,headlines.published_ts),
                fetched_at=MAX(headlines.fetched_at,excluded.fetched_at)""", items)
        conn.executemany("""INSERT INTO archive_outbox(id,payload,fetched_at) VALUES(?,?,?)
            ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,fetched_at=excluded.fetched_at""",
            [(item["id"], json.dumps(item, ensure_ascii=False), item["fetched_at"]) for item in items])
    return len(items)


def pending_archive(limit: int = 500) -> list[dict[str, Any]]:
    with _connect() as conn:
        return [json.loads(row[0]) for row in conn.execute(
            "SELECT payload FROM archive_outbox ORDER BY fetched_at,id LIMIT ?", (limit,))]


def acknowledge_archive(items: list[dict[str, Any]]) -> None:
    with _connect() as conn:
        conn.executemany("DELETE FROM archive_outbox WHERE id=? AND fetched_at<=?",
                         [(item["id"], item["fetched_at"]) for item in items])


def archive_pending_count() -> int:
    with _connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM archive_outbox").fetchone()[0]


def list_feed(source: str | None = None, query: str | None = None, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    clauses, args = [], []
    if source:
        clauses.append("source_id=?")
        args.append(source)
    if query and query.strip():
        # Search is literal (a '%' query should not select the entire archive).
        escaped = query.strip()[:200].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append("(title LIKE ? ESCAPE '\\' OR summary LIKE ? ESCAPE '\\')")
        args.extend([f"%{escaped}%", f"%{escaped}%"])
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with _connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM headlines" + where, args).fetchone()[0]
        rows = conn.execute("SELECT * FROM headlines" + where +
                            " ORDER BY COALESCE(published_ts,fetched_at) DESC,id DESC LIMIT ? OFFSET ?",
                            [*args, max(1, min(200, int(limit))), max(0, int(offset))]).fetchall()
        updated = conn.execute("SELECT MAX(fetched_at) FROM headlines" + where, args).fetchone()[0]
    return {"items": [dict(row) for row in rows], "total": total, "updated_at": updated}
