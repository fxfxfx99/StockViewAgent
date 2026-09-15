"""36氪 / 虎嗅 RSS 本地缓存：仅保留最近 N 条（默认 100），与 articles 表分离。"""
from __future__ import annotations

import sqlite3
import time
from typing import Any

from app.config import settings


def _connect() -> sqlite3.Connection:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    path = settings.data_dir / "news.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = _connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tech_media_items (
          id TEXT PRIMARY KEY,
          title TEXT NOT NULL,
          link TEXT NOT NULL,
          summary TEXT,
          published TEXT,
          published_ts INTEGER,
          source TEXT,
          deep_hint INTEGER NOT NULL DEFAULT 0,
          saved_at REAL NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tech_media_pub ON tech_media_items(published_ts DESC)")
    conn.commit()
    conn.close()


def upsert_and_prune(items: list[dict[str, Any]], keep: int = 100) -> None:
    """写入或更新条目，并删除超出 keep 条的旧记录（按报道时间优先）。"""
    init_db()
    if not items:
        return
    keep = max(10, min(int(keep), 500))
    now = time.time()
    conn = _connect()
    for it in items:
        aid = str(it.get("id") or "").strip()
        if not aid:
            continue
        dh = 1 if it.get("deep_research_hint") else 0
        conn.execute(
            """
            INSERT INTO tech_media_items (
              id, title, link, summary, published, published_ts, source, deep_hint, saved_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              title = excluded.title,
              link = excluded.link,
              summary = excluded.summary,
              published = excluded.published,
              published_ts = excluded.published_ts,
              source = excluded.source,
              deep_hint = excluded.deep_hint,
              saved_at = excluded.saved_at
            """,
            (
                aid,
                str(it.get("title") or "")[:2000],
                str(it.get("link") or "")[:2000],
                str(it.get("summary") or "")[:8000],
                str(it.get("published") or "")[:500],
                it.get("published_ts"),
                str(it.get("source") or "")[:500],
                dh,
                now,
            ),
        )
    cur = conn.execute(
        """
        SELECT id FROM tech_media_items
        ORDER BY COALESCE(published_ts, 0) DESC, saved_at DESC
        LIMIT ?
        """,
        (keep,),
    )
    keep_ids = [str(r[0]) for r in cur.fetchall()]
    if not keep_ids:
        conn.commit()
        conn.close()
        return
    placeholders = ",".join("?" * len(keep_ids))
    conn.execute(f"DELETE FROM tech_media_items WHERE id NOT IN ({placeholders})", keep_ids)
    conn.commit()
    conn.close()


def list_recent(limit: int = 100) -> list[dict[str, Any]]:
    init_db()
    lim = max(1, min(int(limit), 200))
    conn = _connect()
    rows = conn.execute(
        """
        SELECT id, title, link, summary, published, published_ts, source, deep_hint, saved_at
        FROM tech_media_items
        ORDER BY COALESCE(published_ts, 0) DESC, saved_at DESC
        LIMIT ?
        """,
        (lim,),
    ).fetchall()
    conn.close()
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "id": r["id"],
                "title": r["title"],
                "link": r["link"],
                "summary": r["summary"] or "",
                "published": r["published"] or "",
                "published_ts": r["published_ts"],
                "source": r["source"] or "",
                "deep_research_hint": bool(r["deep_hint"]),
            }
        )
    return out


def stats() -> dict[str, Any]:
    init_db()
    conn = _connect()
    n = conn.execute("SELECT COUNT(*) FROM tech_media_items").fetchone()[0]
    conn.close()
    return {"tech_media_stored": int(n)}
