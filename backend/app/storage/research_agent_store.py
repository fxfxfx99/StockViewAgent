"""投研 Agent 历史记录（SQLite，按 user_id 隔离）。"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any

from app.config import settings


def _connect() -> sqlite3.Connection:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    path = settings.data_dir / "research_agent.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = _connect()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS research_runs (
          id TEXT PRIMARY KEY,
          user_id INTEGER NOT NULL,
          query TEXT NOT NULL,
          payload TEXT NOT NULL,
          created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_research_user ON research_runs(user_id, created_at DESC);
        """
    )
    conn.commit()
    conn.close()


def insert_run(user_id: int, query: str, payload: dict[str, Any]) -> tuple[str, float]:
    init_db()
    rid = str(uuid.uuid4())
    ts = time.time()
    conn = _connect()
    conn.execute(
        "INSERT INTO research_runs (id, user_id, query, payload, created_at) VALUES (?,?,?,?,?)",
        (rid, int(user_id), query[:8000], json.dumps(payload, ensure_ascii=False), ts),
    )
    conn.commit()
    conn.close()
    return rid, ts


def list_runs(user_id: int, limit: int = 50) -> list[dict[str, Any]]:
    init_db()
    lim = max(1, min(int(limit), 100))
    conn = _connect()
    rows = conn.execute(
        """
        SELECT id, query, created_at FROM research_runs
        WHERE user_id = ? ORDER BY created_at DESC LIMIT ?
        """,
        (int(user_id), lim),
    ).fetchall()
    conn.close()
    return [{"id": r["id"], "query": r["query"], "created_at": r["created_at"]} for r in rows]


def get_run(user_id: int, run_id: str) -> dict[str, Any] | None:
    init_db()
    conn = _connect()
    row = conn.execute(
        "SELECT id, user_id, query, payload, created_at FROM research_runs WHERE id = ? AND user_id = ?",
        (run_id, int(user_id)),
    ).fetchone()
    conn.close()
    if not row:
        return None
    try:
        payload = json.loads(row["payload"])
    except json.JSONDecodeError:
        payload = {}
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "query": row["query"],
        "created_at": row["created_at"],
        **payload,
    }


def delete_run(user_id: int, run_id: str) -> bool:
    init_db()
    conn = _connect()
    cur = conn.execute("DELETE FROM research_runs WHERE id = ? AND user_id = ?", (run_id, int(user_id)))
    conn.commit()
    n = cur.rowcount
    conn.close()
    return n > 0
