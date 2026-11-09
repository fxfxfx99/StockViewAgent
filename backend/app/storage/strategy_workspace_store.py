"""Private strategy documents and RAG settings, with immutable revision history."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.config import settings

DB_NAME = "strategy_workspaces.db"


def db_path() -> Path:
    return settings.data_dir / DB_NAME


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect():
    db_path().parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path()), timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS strategy_document_revisions (
                user_id INTEGER NOT NULL,
                strategy_id TEXT NOT NULL,
                document_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                source_url TEXT NOT NULL DEFAULT '',
                published_at TEXT,
                tags_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                deleted INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, strategy_id, document_id, version)
            );
            CREATE INDEX IF NOT EXISTS idx_strategy_docs_scope
                ON strategy_document_revisions(user_id, strategy_id, updated_at);
            CREATE TABLE IF NOT EXISTS strategy_rag_revisions (
                user_id INTEGER NOT NULL,
                strategy_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                config_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(user_id, strategy_id, version)
            );
        """)


def _document(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["id"] = result.pop("document_id")
    result["tags"] = json.loads(result.pop("tags_json"))
    result.pop("user_id", None)
    result.pop("deleted", None)
    result["origin"] = "personal"
    return result


def list_documents(user_id: int | None, strategy_id: str, *, before: str | None = None) -> list[dict[str, Any]]:
    if user_id is None or not db_path().exists():
        return []
    with connect() as conn:
        # Select the latest *known revision*, then honor its deletion. A future
        # edit or deletion must never replace the historical document body.
        rows = conn.execute("""
            SELECT d.* FROM strategy_document_revisions d
            JOIN (
                SELECT document_id, MAX(version) AS version
                FROM strategy_document_revisions
                WHERE user_id=? AND strategy_id=? AND (? IS NULL OR updated_at < ?)
                GROUP BY document_id
            ) latest ON d.document_id=latest.document_id AND d.version=latest.version
            WHERE d.user_id=? AND d.strategy_id=? AND d.deleted=0
            ORDER BY d.updated_at DESC, d.document_id
        """, (user_id, strategy_id, before, before, user_id, strategy_id)).fetchall()
    return [_document(row) for row in rows]


def write_document(user_id: int, strategy_id: str, data: dict[str, Any], *, document_id: str | None = None) -> dict[str, Any]:
    init_db()
    now = _now_iso()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        prior = None
        if document_id is not None:
            prior = conn.execute("""
                SELECT * FROM strategy_document_revisions
                WHERE user_id=? AND strategy_id=? AND document_id=? ORDER BY version DESC LIMIT 1
            """, (user_id, strategy_id, document_id)).fetchone()
            if prior is None or prior["deleted"]:
                raise KeyError("知识条目不存在")
        doc_id = document_id or uuid4().hex
        version = int(prior["version"]) + 1 if prior else 1
        created_at = prior["created_at"] if prior else now
        conn.execute("""
            INSERT INTO strategy_document_revisions
                (user_id,strategy_id,document_id,version,title,content,source_url,
                 published_at,tags_json,created_at,updated_at,deleted)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,0)
        """, (user_id, strategy_id, doc_id, version, data["title"], data["content"],
              data.get("source_url", ""), data.get("published_at"),
              json.dumps(data.get("tags", []), ensure_ascii=False), created_at, now))
        row = conn.execute("""
            SELECT * FROM strategy_document_revisions
            WHERE user_id=? AND strategy_id=? AND document_id=? AND version=?
        """, (user_id, strategy_id, doc_id, version)).fetchone()
    return _document(row)


def delete_document(user_id: int, strategy_id: str, document_id: str) -> None:
    init_db()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("""
            SELECT * FROM strategy_document_revisions
            WHERE user_id=? AND strategy_id=? AND document_id=? ORDER BY version DESC LIMIT 1
        """, (user_id, strategy_id, document_id)).fetchone()
        if row is None or row["deleted"]:
            raise KeyError("知识条目不存在")
        data = dict(row)
        data.update(version=data["version"] + 1, updated_at=_now_iso(), deleted=1)
        conn.execute("""
            INSERT INTO strategy_document_revisions
                (user_id,strategy_id,document_id,version,title,content,source_url,
                 published_at,tags_json,created_at,updated_at,deleted)
            VALUES(:user_id,:strategy_id,:document_id,:version,:title,:content,:source_url,
                   :published_at,:tags_json,:created_at,:updated_at,:deleted)
        """, data)


def get_config(user_id: int | None, strategy_id: str, *, before: str | None = None) -> dict[str, Any] | None:
    if user_id is None or not db_path().exists():
        return None
    with connect() as conn:
        row = conn.execute("""
            SELECT * FROM strategy_rag_revisions
            WHERE user_id=? AND strategy_id=? AND (? IS NULL OR updated_at < ?)
            ORDER BY version DESC LIMIT 1
        """, (user_id, strategy_id, before, before)).fetchone()
    if row is None:
        return None
    return {**json.loads(row["config_json"]), "version": row["version"], "updated_at": row["updated_at"]}


def save_config(user_id: int, strategy_id: str, config: dict[str, Any]) -> dict[str, Any]:
    init_db()
    now = _now_iso()
    encoded = json.dumps(config, ensure_ascii=False, sort_keys=True)
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        prior = conn.execute("""
            SELECT * FROM strategy_rag_revisions WHERE user_id=? AND strategy_id=?
            ORDER BY version DESC LIMIT 1
        """, (user_id, strategy_id)).fetchone()
        if prior is not None and prior["config_json"] == encoded:
            return {**config, "version": prior["version"], "updated_at": prior["updated_at"]}
        version = int(prior["version"]) + 1 if prior else 1
        conn.execute("""
            INSERT INTO strategy_rag_revisions(user_id,strategy_id,version,config_json,updated_at)
            VALUES(?,?,?,?,?)
        """, (user_id, strategy_id, version, encoded, now))
    return {**config, "version": version, "updated_at": now}
