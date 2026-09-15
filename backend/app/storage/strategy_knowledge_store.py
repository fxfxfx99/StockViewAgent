"""TransactionAgent 策略知识库：公开来源、抓取摘录、策略标签与更新记录。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import settings
from app.storage import data_asset_manager

DB_NAME = "strategy_knowledge.db"


def db_path() -> Path:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings.data_dir / DB_NAME


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path()))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS knowledge_sources (
              id TEXT PRIMARY KEY,
              title TEXT NOT NULL,
              url TEXT,
              source_type TEXT NOT NULL,
              publisher TEXT,
              authors TEXT,
              year TEXT,
              language TEXT NOT NULL DEFAULT 'zh',
              access_policy TEXT NOT NULL DEFAULT 'public_excerpt',
              copyright_note TEXT NOT NULL DEFAULT '',
              strategy_tags_json TEXT NOT NULL DEFAULT '[]',
              priority INTEGER NOT NULL DEFAULT 50,
              status TEXT NOT NULL DEFAULT 'seeded',
              last_checked_at TEXT,
              fetch_error TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS knowledge_items (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              source_id TEXT NOT NULL,
              strategy TEXT NOT NULL,
              title TEXT NOT NULL,
              summary TEXT NOT NULL,
              key_points_json TEXT NOT NULL DEFAULT '[]',
              risks_json TEXT NOT NULL DEFAULT '[]',
              content_excerpt TEXT NOT NULL DEFAULT '',
              url TEXT,
              quality_score REAL NOT NULL DEFAULT 0.5,
              collected_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE(source_id, strategy, title),
              FOREIGN KEY(source_id) REFERENCES knowledge_sources(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS knowledge_refresh_runs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              started_at TEXT NOT NULL,
              finished_at TEXT,
              status TEXT NOT NULL,
              sources_checked INTEGER NOT NULL DEFAULT 0,
              items_upserted INTEGER NOT NULL DEFAULT 0,
              errors_json TEXT NOT NULL DEFAULT '[]'
            );

            CREATE INDEX IF NOT EXISTS idx_knowledge_items_strategy
              ON knowledge_items(strategy, quality_score DESC, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_knowledge_sources_status
              ON knowledge_sources(status, updated_at DESC);
            """
        )
    data_asset_manager.register_existing_asset(
        db_path(),
        kind="sqlite_db",
        key="strategy_knowledge",
        source="strategy_knowledge_store",
    )


def upsert_source(source: dict[str, Any]) -> None:
    init_db()
    now = _now_iso()
    tags = source.get("strategy_tags") or source.get("strategy_tags_json") or []
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except json.JSONDecodeError:
            tags = [tags]
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO knowledge_sources (
              id,title,url,source_type,publisher,authors,year,language,access_policy,
              copyright_note,strategy_tags_json,priority,status,last_checked_at,
              fetch_error,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
              title=excluded.title,
              url=excluded.url,
              source_type=excluded.source_type,
              publisher=excluded.publisher,
              authors=excluded.authors,
              year=excluded.year,
              language=excluded.language,
              access_policy=excluded.access_policy,
              copyright_note=excluded.copyright_note,
              strategy_tags_json=excluded.strategy_tags_json,
              priority=excluded.priority,
              updated_at=excluded.updated_at
            """,
            (
                source["id"],
                source["title"],
                source.get("url"),
                source.get("source_type") or "web",
                source.get("publisher"),
                source.get("authors"),
                str(source.get("year") or ""),
                source.get("language") or "zh",
                source.get("access_policy") or "public_excerpt",
                source.get("copyright_note") or "",
                json.dumps(tags, ensure_ascii=False),
                int(source.get("priority") or 50),
                source.get("status") or "seeded",
                source.get("last_checked_at"),
                source.get("fetch_error"),
                now,
                now,
            ),
        )


def upsert_item(item: dict[str, Any]) -> None:
    init_db()
    now = _now_iso()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO knowledge_items (
              source_id,strategy,title,summary,key_points_json,risks_json,content_excerpt,
              url,quality_score,collected_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(source_id,strategy,title) DO UPDATE SET
              summary=excluded.summary,
              key_points_json=excluded.key_points_json,
              risks_json=excluded.risks_json,
              content_excerpt=excluded.content_excerpt,
              url=excluded.url,
              quality_score=excluded.quality_score,
              updated_at=excluded.updated_at
            """,
            (
                item["source_id"],
                item["strategy"],
                item["title"],
                item.get("summary") or "",
                json.dumps(item.get("key_points") or [], ensure_ascii=False),
                json.dumps(item.get("risks") or [], ensure_ascii=False),
                item.get("content_excerpt") or "",
                item.get("url"),
                float(item.get("quality_score") or 0.5),
                item.get("collected_at") or now,
                now,
            ),
        )


def mark_source_checked(source_id: str, *, ok: bool, error: str = "") -> None:
    init_db()
    with connect() as conn:
        conn.execute(
            """
            UPDATE knowledge_sources
            SET status=?, last_checked_at=?, fetch_error=?, updated_at=?
            WHERE id=?
            """,
            ("ok" if ok else "error", _now_iso(), error[:800], _now_iso(), source_id),
        )


def list_sources() -> list[dict[str, Any]]:
    init_db()
    with connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM knowledge_sources ORDER BY priority DESC, id")]
    for row in rows:
        try:
            row["strategy_tags"] = json.loads(row.pop("strategy_tags_json") or "[]")
        except json.JSONDecodeError:
            row["strategy_tags"] = []
    return rows


def query_strategy(strategy: str, *, limit: int = 6) -> list[dict[str, Any]]:
    init_db()
    with connect() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                """
                SELECT ki.*, ks.title AS source_title, ks.publisher, ks.source_type, ks.copyright_note
                FROM knowledge_items ki
                JOIN knowledge_sources ks ON ks.id = ki.source_id
                WHERE ki.strategy=?
                ORDER BY ki.quality_score DESC, ki.updated_at DESC
                LIMIT ?
                """,
                (strategy, int(limit)),
            )
        ]
    for row in rows:
        for key in ("key_points_json", "risks_json"):
            target = "key_points" if key == "key_points_json" else "risks"
            try:
                row[target] = json.loads(row.pop(key) or "[]")
            except json.JSONDecodeError:
                row[target] = []
    return rows


def status() -> dict[str, Any]:
    init_db()
    with connect() as conn:
        source_count = conn.execute("SELECT COUNT(*) FROM knowledge_sources").fetchone()[0]
        item_count = conn.execute("SELECT COUNT(*) FROM knowledge_items").fetchone()[0]
        by_strategy = [
            dict(r)
            for r in conn.execute(
                "SELECT strategy, COUNT(*) AS count FROM knowledge_items GROUP BY strategy ORDER BY count DESC"
            )
        ]
        recent_runs = [dict(r) for r in conn.execute("SELECT * FROM knowledge_refresh_runs ORDER BY id DESC LIMIT 10")]
    stat = db_path().stat() if db_path().exists() else None
    return {
        "db": str(db_path()),
        "size_bytes": stat.st_size if stat else 0,
        "source_count": source_count,
        "item_count": item_count,
        "by_strategy": by_strategy,
        "recent_runs": recent_runs,
    }


def start_refresh_run() -> int:
    init_db()
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO knowledge_refresh_runs(started_at,status) VALUES(?,?)",
            (_now_iso(), "running"),
        )
        return int(cur.lastrowid)


def finish_refresh_run(run_id: int, *, status_value: str, sources_checked: int, items_upserted: int, errors: list[dict[str, Any]]) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE knowledge_refresh_runs
            SET finished_at=?, status=?, sources_checked=?, items_upserted=?, errors_json=?
            WHERE id=?
            """,
            (_now_iso(), status_value, sources_checked, items_upserted, json.dumps(errors, ensure_ascii=False), run_id),
        )
