"""按用户隔离的本机凭证存储；API 永不返回密钥明文。"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.config import settings


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(settings.data_dir / "users.db"))
    c.row_factory = sqlite3.Row
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS user_credentials (
            user_id INTEGER PRIMARY KEY,
            credentials TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )
    return c


def load_user_credentials(user_id: int) -> dict[str, Any]:
    with _conn() as c:
        row = c.execute("SELECT credentials FROM user_credentials WHERE user_id=?", (int(user_id),)).fetchone()
    if not row:
        return {}
    try:
        data = json.loads(row["credentials"] or "{}")
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def merge_user_credentials(user_id: int, patch: dict[str, Any]) -> dict[str, Any]:
    cur = load_user_credentials(user_id)
    for key, value in patch.items():
        if value is None:
            continue
        if isinstance(value, str):
            value = value.strip()
        if value == "":
            cur.pop(key, None)
        else:
            cur[key] = value
    raw = json.dumps(cur, ensure_ascii=False)
    with _conn() as c:
        c.execute(
            """INSERT INTO user_credentials(user_id, credentials, updated_at)
               VALUES (?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(user_id) DO UPDATE SET credentials=excluded.credentials, updated_at=CURRENT_TIMESTAMP""",
            (int(user_id), raw),
        )
        c.commit()
    return cur

