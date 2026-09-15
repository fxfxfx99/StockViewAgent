"""本地用户与权限（SQLite）。默认角色：admin、user。"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import settings
from app.security.passwords import hash_password

_DB_NAME = "users.db"


def _db_path() -> Path:
    p = settings.data_dir / _DB_NAME
    return p


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(_db_path()))
    c.row_factory = sqlite3.Row
    return c


@dataclass
class UserRecord:
    id: int
    username: str
    role: str
    display_name: str
    preferences: dict[str, Any]


def _row_to_user(r: sqlite3.Row) -> UserRecord:
    prefs: dict[str, Any] = {}
    try:
        raw = r["preferences"] or "{}"
        prefs = json.loads(raw) if isinstance(raw, str) else {}
    except Exception:
        prefs = {}
    return UserRecord(
        id=int(r["id"]),
        username=str(r["username"]),
        role=str(r["role"] or "user"),
        display_name=str(r["display_name"] or ""),
        preferences=prefs if isinstance(prefs, dict) else {},
    )


def init_db() -> None:
    _db_path().parent.mkdir(parents=True, exist_ok=True)
    with _conn() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                salt TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                display_name TEXT NOT NULL DEFAULT '',
                preferences TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        c.commit()
    ensure_bootstrap_users()


def ensure_bootstrap_users() -> None:
    with _conn() as c:
        n = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        if n > 0:
            return
    admin_pwd = (settings.auth_bootstrap_admin_password or "admin123").strip()
    user_pwd = (settings.auth_bootstrap_user_password or "user123").strip()
    s1, h1 = hash_password(admin_pwd)
    s2, h2 = hash_password(user_pwd)
    with _conn() as c:
        c.execute(
            "INSERT INTO users (username, salt, password_hash, role, display_name) VALUES (?,?,?,?,?)",
            ("admin", s1, h1, "admin", "管理员"),
        )
        c.execute(
            "INSERT INTO users (username, salt, password_hash, role, display_name) VALUES (?,?,?,?,?)",
            ("user", s2, h2, "user", "普通用户"),
        )
        c.commit()


def get_by_username(username: str) -> tuple[UserRecord, str, str] | None:
    u = (username or "").strip().lower()
    with _conn() as c:
        r = c.execute(
            "SELECT id, username, role, display_name, preferences, salt, password_hash FROM users WHERE lower(username)=?",
            (u,),
        ).fetchone()
    if not r:
        return None
    rec = _row_to_user(r)
    return rec, str(r["salt"]), str(r["password_hash"])


def get_by_id(uid: int) -> UserRecord | None:
    with _conn() as c:
        r = c.execute(
            "SELECT id, username, role, display_name, preferences FROM users WHERE id=?",
            (int(uid),),
        ).fetchone()
    return _row_to_user(r) if r else None


def get_open_local_user() -> UserRecord:
    """本地开放模式：无登录时使用的默认用户（优先 admin）。"""
    ensure_bootstrap_users()
    with _conn() as c:
        r = c.execute(
            "SELECT id, username, role, display_name, preferences FROM users "
            "WHERE role='admin' ORDER BY id LIMIT 1"
        ).fetchone()
        if not r:
            r = c.execute(
                "SELECT id, username, role, display_name, preferences FROM users ORDER BY id LIMIT 1"
            ).fetchone()
    if not r:
        raise RuntimeError("本地用户库为空，无法进入开放模式")
    return _row_to_user(r)


def list_users() -> list[UserRecord]:
    with _conn() as c:
        rows = c.execute(
            "SELECT id, username, role, display_name, preferences FROM users ORDER BY id"
        ).fetchall()
    return [_row_to_user(r) for r in rows]


def create_user(username: str, password: str, role: str = "user", display_name: str = "") -> UserRecord:
    salt, ph = hash_password(password)
    un = username.strip().lower()
    if not un or len(un) > 64:
        raise ValueError("无效用户名")
    if role not in ("admin", "user"):
        raise ValueError("role 须为 admin 或 user")
    with _conn() as c:
        c.execute(
            "INSERT INTO users (username, salt, password_hash, role, display_name) VALUES (?,?,?,?,?)",
            (un, salt, ph, role, (display_name or "").strip()),
        )
        c.commit()
        uid = c.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    u = get_by_id(int(uid))
    assert u
    return u


def update_user(uid: int, *, role: str | None = None, display_name: str | None = None) -> UserRecord | None:
    parts: list[str] = []
    vals: list[Any] = []
    if role is not None:
        if role not in ("admin", "user"):
            raise ValueError("role 须为 admin 或 user")
        parts.append("role=?")
        vals.append(role)
    if display_name is not None:
        parts.append("display_name=?")
        vals.append(display_name.strip())
    if not parts:
        return get_by_id(uid)
    vals.append(int(uid))
    with _conn() as c:
        c.execute(f"UPDATE users SET {', '.join(parts)} WHERE id=?", vals)
        c.commit()
    return get_by_id(uid)


def set_password(uid: int, new_password: str) -> None:
    salt, ph = hash_password(new_password)
    with _conn() as c:
        c.execute("UPDATE users SET salt=?, password_hash=? WHERE id=?", (salt, ph, int(uid)))
        c.commit()


def delete_user(uid: int) -> bool:
    with _conn() as c:
        cur = c.execute("DELETE FROM users WHERE id=?", (int(uid),))
        c.commit()
        return cur.rowcount > 0


def save_preferences(uid: int, prefs: dict[str, Any]) -> UserRecord | None:
    raw = json.dumps(prefs or {}, ensure_ascii=False)
    with _conn() as c:
        c.execute("UPDATE users SET preferences=? WHERE id=?", (raw, int(uid)))
        c.commit()
    return get_by_id(uid)
