"""User-scoped research notes, append-only history and atomic local scan cursors."""
from __future__ import annotations

from contextlib import contextmanager
import json
import time
import uuid

from app.config import settings
from app.schemas.tracking import ThesisCreate, ThesisPatch, ThesisReview
from app.storage import sqlite_bootstrap

_SCHEMA = """
CREATE TABLE IF NOT EXISTS theses (
  user_id INTEGER NOT NULL, id TEXT NOT NULL, symbol TEXT NOT NULL, status TEXT NOT NULL,
  revision INTEGER NOT NULL, review_at TEXT NOT NULL, payload TEXT NOT NULL,
  created_at REAL NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY(user_id,id));
CREATE INDEX IF NOT EXISTS thesis_user_status ON theses(user_id,status,review_at);
CREATE TABLE IF NOT EXISTS thesis_events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, user_id INTEGER NOT NULL,
  thesis_id TEXT NOT NULL, revision INTEGER NOT NULL, kind TEXT NOT NULL,
  payload TEXT NOT NULL, created_at REAL NOT NULL, UNIQUE(user_id,thesis_id,revision));
CREATE INDEX IF NOT EXISTS thesis_history ON thesis_events(user_id,thesis_id,seq);
CREATE TRIGGER IF NOT EXISTS thesis_history_no_update BEFORE UPDATE ON thesis_events
 BEGIN SELECT RAISE(ABORT,'thesis history is immutable'); END;
CREATE TRIGGER IF NOT EXISTS thesis_history_no_delete BEFORE DELETE ON thesis_events
 BEGIN SELECT RAISE(ABORT,'thesis history is immutable'); END;
CREATE TABLE IF NOT EXISTS tracking_changes (
  user_id INTEGER NOT NULL, id TEXT NOT NULL, kind TEXT NOT NULL, symbol TEXT,
  priority INTEGER NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL,
  is_read INTEGER NOT NULL DEFAULT 0, feedback TEXT, resolved_at REAL,
  PRIMARY KEY(user_id,id));
CREATE INDEX IF NOT EXISTS tracking_feed ON tracking_changes(user_id,priority DESC,created_at DESC);
CREATE TABLE IF NOT EXISTS tracking_change_theses (
  user_id INTEGER NOT NULL, change_id TEXT NOT NULL, thesis_id TEXT NOT NULL,
  PRIMARY KEY(user_id,change_id,thesis_id));
CREATE INDEX IF NOT EXISTS tracking_thesis_changes ON tracking_change_theses(user_id,thesis_id);
CREATE TRIGGER IF NOT EXISTS tracking_change_content_no_update
 BEFORE UPDATE OF payload,kind,symbol,priority,created_at ON tracking_changes
 BEGIN SELECT RAISE(ABORT,'tracking evidence is immutable'); END;
CREATE TABLE IF NOT EXISTS tracking_scan_state (
  user_id INTEGER NOT NULL, source TEXT NOT NULL, cursor INTEGER NOT NULL,
  metadata TEXT NOT NULL, updated_at REAL NOT NULL, version INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(user_id,source));
"""


class RevisionConflict(ValueError):
    pass


class ScanConflict(ValueError):
    pass


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


@contextmanager
def connection():
    with sqlite_bootstrap.connection(settings.data_dir / "tracking.sqlite3", _SCHEMA) as conn:
        yield conn


def init_db():
    with connection():
        pass


def _get(conn, user_id, thesis_id):
    row = conn.execute("SELECT payload FROM theses WHERE user_id=? AND id=?", (int(user_id), thesis_id)).fetchone()
    return json.loads(row[0]) if row else None


def _event(conn, user_id, thesis, kind, review=None):
    payload = {"thesis": thesis}
    if review is not None:
        payload["review"] = review
    conn.execute("INSERT INTO thesis_events(id,user_id,thesis_id,revision,kind,payload,created_at) VALUES(?,?,?,?,?,?,?)",
                 (str(uuid.uuid4()), int(user_id), thesis["id"], thesis["revision"], kind, _json(payload), thesis["updated_at"]))


def create_thesis(user_id, payload):
    from app.services.tracking_evidence import freeze_evidence_refs, validate_signal

    value = ThesisCreate.model_validate(payload).model_dump(mode="json")
    validate_signal(user_id, value["symbol"], value.get("signal_id"))
    value["evidence_refs"] = freeze_evidence_refs(user_id, value["evidence_refs"])
    now = time.time()
    thesis = {**value, "id": str(uuid.uuid4()), "status": "active", "revision": 1,
              "created_at": now, "updated_at": now}
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO theses VALUES(?,?,?,?,?,?,?,?,?)", (int(user_id), thesis["id"], thesis["symbol"],
                     thesis["status"], 1, thesis["review_at"], _json(thesis), now, now))
        _event(conn, user_id, thesis, "created")
    return thesis


def get_thesis(user_id, thesis_id):
    with connection() as conn:
        return _get(conn, user_id, thesis_id)


def list_theses(user_id, *, status=None, symbol=None, limit=500):
    clauses, args = ["user_id=?"], [int(user_id)]
    if status:
        clauses.append("status=?")
        args.append(status)
    if symbol:
        clauses.append("symbol=?")
        args.append(symbol)
    args.append(max(1, min(int(limit), 10000)))
    with connection() as conn:
        return [json.loads(row[0]) for row in conn.execute(
            f"SELECT payload FROM theses WHERE {' AND '.join(clauses)} ORDER BY review_at,created_at,id LIMIT ?", args)]


def list_active_theses(user_id):
    return list_theses(user_id, status="active")


def list_tracking_users():
    with connection() as conn:
        return [row[0] for row in conn.execute("SELECT user_id FROM theses UNION SELECT user_id FROM tracking_scan_state")]


def _resolve_due(conn, user_id, thesis_id, now):
    conn.execute("""UPDATE tracking_changes SET resolved_at=? WHERE user_id=? AND kind='review_due'
      AND resolved_at IS NULL AND id IN (SELECT change_id FROM tracking_change_theses WHERE user_id=? AND thesis_id=?)""",
      (now, int(user_id), int(user_id), thesis_id))


def _save_revision(conn, user_id, thesis, kind, review=None):
    conn.execute("UPDATE theses SET status=?,revision=?,review_at=?,payload=?,updated_at=? WHERE user_id=? AND id=?",
                 (thesis["status"], thesis["revision"], thesis["review_at"], _json(thesis), thesis["updated_at"], int(user_id), thesis["id"]))
    _event(conn, user_id, thesis, kind, review)
    _resolve_due(conn, user_id, thesis["id"], thesis["updated_at"])


def update_thesis(user_id, thesis_id, payload):
    from app.services.tracking_evidence import freeze_evidence_refs

    patch = ThesisPatch.model_validate(payload).model_dump(mode="json", exclude_unset=True)
    revision = patch.pop("revision")
    previous = get_thesis(user_id, thesis_id)
    if previous is None:
        return None
    if "evidence_refs" in patch:
        patch["evidence_refs"] = freeze_evidence_refs(user_id, patch["evidence_refs"], existing_refs=previous["evidence_refs"])
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        old = _get(conn, user_id, thesis_id)
        if old is None:
            return None
        if old["revision"] != revision:
            raise RevisionConflict("判断已有新版本，请保留输入并重新载入后重试")
        thesis = {**old, **patch, "revision": revision + 1, "updated_at": time.time()}
        _save_revision(conn, user_id, thesis, "revised")
    return thesis


def review_thesis(user_id, thesis_id, payload):
    from app.services.tracking_evidence import freeze_evidence_refs

    review = ThesisReview.model_validate(payload).model_dump(mode="json", exclude_none=True)
    changes = review.get("changes", {})
    previous = get_thesis(user_id, thesis_id)
    if previous is None:
        return None
    if "evidence_refs" in changes:
        changes["evidence_refs"] = freeze_evidence_refs(user_id, changes["evidence_refs"], existing_refs=previous["evidence_refs"])
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        old = _get(conn, user_id, thesis_id)
        if old is None:
            return None
        if old["revision"] != review["revision"]:
            raise RevisionConflict("判断已有新版本，请保留输入并重新载入后重试")
        if old["status"] != "active":
            raise ValueError("已结束判断须先重新开启后才能复盘")
        thesis = {**old, **changes, "status": "closed" if review["action"] == "close" else "active",
                  "review_at": review.get("review_at", old["review_at"]),
                  "revision": old["revision"] + 1, "updated_at": time.time()}
        _save_revision(conn, user_id, thesis, "reviewed", review)
    return thesis


def list_timeline(user_id, thesis_id):
    with connection() as conn:
        rows = conn.execute("SELECT * FROM thesis_events WHERE user_id=? AND thesis_id=? ORDER BY seq", (int(user_id), thesis_id))
        return [{**{key: row[key] for key in ("id", "thesis_id", "revision", "kind", "created_at")},
                 "payload": json.loads(row["payload"])} for row in rows]


def _change(row):
    return {**json.loads(row["payload"]), "is_read": bool(row["is_read"]), "feedback": row["feedback"],
            "resolved_at": row["resolved_at"], "resolved": row["resolved_at"] is not None}


def list_changes(user_id, *, symbol=None, thesis_id=None, unread_only=False, include_resolved=False, limit=200):
    clauses, args = ["c.user_id=?"], [int(user_id)]
    if symbol:
        clauses.append("c.symbol=?")
        args.append(symbol)
    if thesis_id:
        clauses.append("EXISTS(SELECT 1 FROM tracking_change_theses t WHERE t.user_id=c.user_id AND t.change_id=c.id AND t.thesis_id=?)")
        args.append(thesis_id)
    if unread_only:
        clauses.append("c.is_read=0")
    if not include_resolved:
        clauses.append("c.resolved_at IS NULL")
    args.append(max(1, min(int(limit), 500)))
    with connection() as conn:
        return [_change(row) for row in conn.execute(
            f"""SELECT c.* FROM tracking_changes c WHERE {' AND '.join(clauses)}
            ORDER BY CASE WHEN c.is_read=0 AND COALESCE(c.feedback,'')!='irrelevant' THEN 0 ELSE 1 END,
                     priority DESC,created_at DESC,id LIMIT ?""", args)]


def get_change(user_id, change_id):
    with connection() as conn:
        row = conn.execute("SELECT * FROM tracking_changes WHERE user_id=? AND id=?", (int(user_id), change_id)).fetchone()
        return _change(row) if row else None


def update_change(user_id, change_id, patch):
    from app.schemas.tracking import ChangePatch

    patch = ChangePatch.model_validate(patch).model_dump(exclude_unset=True)
    assignments, args = [], []
    for field, value in patch.items():
        assignments.append(field + "=?")
        args.append(int(value) if field == "is_read" else value)
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(f"UPDATE tracking_changes SET {','.join(assignments)} WHERE user_id=? AND id=?",
                              [*args, int(user_id), change_id])
        if not cursor.rowcount:
            return None
        return _change(conn.execute("SELECT * FROM tracking_changes WHERE user_id=? AND id=?", (int(user_id), change_id)).fetchone())


def _put_changes(conn, user_id, changes):
    inserted = 0
    for raw in changes:
        value = dict(raw)
        if not isinstance(value.get("id"), str) or not value["id"] or len(value["id"]) > 240:
            raise ValueError("变化需要稳定标识")
        if value.get("kind") not in {"news", "calendar", "review_due", "data_issue"}:
            raise ValueError("未知变化类型")
        thesis_ids = list(dict.fromkeys(value.get("thesis_ids") or []))
        # No personal note from another account may be attached by an adapter.
        if any(_get(conn, user_id, identity) is None for identity in thesis_ids):
            raise ValueError("关联判断不属于当前账户")
        value.update(thesis_ids=thesis_ids, created_at=value.get("created_at") or time.time(),
                     priority=int(value.get("priority") or 0))
        if value["kind"] == "review_due":
            # A scan can race a review; never insert the old revision's reminder.
            valid = [thesis_id for thesis_id in thesis_ids if (thesis := _get(conn, user_id, thesis_id))
                     and thesis["status"] == "active"
                     and (value.get("thesis_revision") is None or thesis["revision"] == value["thesis_revision"])
                     and (value.get("review_at") is None or thesis["review_at"] == value["review_at"])]
            if len(valid) != len(thesis_ids) or not valid:
                continue
        cursor = conn.execute("""INSERT OR IGNORE INTO tracking_changes(user_id,id,kind,symbol,priority,payload,created_at)
          VALUES(?,?,?,?,?,?,?)""", (int(user_id), value["id"], value["kind"], value.get("symbol"), value["priority"], _json(value), value["created_at"]))
        if cursor.rowcount:
            inserted += 1
            conn.executemany("INSERT INTO tracking_change_theses VALUES(?,?,?)",
                             [(int(user_id), value["id"], tid) for tid in thesis_ids])
    return inserted


def put_changes(user_id, changes):
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        return _put_changes(conn, user_id, changes)


def get_scan_state(user_id, source):
    with connection() as conn:
        row = conn.execute("SELECT * FROM tracking_scan_state WHERE user_id=? AND source=?", (int(user_id), source)).fetchone()
        return {**json.loads(row["metadata"]), "cursor": row["cursor"], "version": row["version"],
                "updated_at": row["updated_at"]} if row else {"cursor": 0, "version": 0}


def commit_scan(user_id, source, expected_cursor, next_cursor, changes, metadata, *, expected_version=None):
    if any(type(value) is not int or value < 0 for value in (expected_cursor, next_cursor)) or next_cursor < expected_cursor:
        raise ValueError("扫描游标必须单调递增")
    if not isinstance(metadata, dict):
        raise ValueError("扫描状态须为对象")
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT cursor,version,metadata FROM tracking_scan_state WHERE user_id=? AND source=?", (int(user_id), source)).fetchone()
        cursor = row[0] if row else 0
        version = row[1] if row else 0
        if cursor != expected_cursor or (expected_version is not None and version != expected_version):
            raise ScanConflict("已有扫描推进游标，请读取新状态后重试")
        if source == "health" and (not row or json.loads(row["metadata"]).get("fingerprint") != metadata.get("fingerprint")):
            conn.execute("UPDATE tracking_changes SET resolved_at=? WHERE user_id=? AND kind='data_issue' AND resolved_at IS NULL",
                         (time.time(), int(user_id)))
        inserted = _put_changes(conn, user_id, changes)
        now = time.time()
        conn.execute("""INSERT INTO tracking_scan_state VALUES(?,?,?,?,?,?) ON CONFLICT(user_id,source)
          DO UPDATE SET cursor=excluded.cursor,metadata=excluded.metadata,updated_at=excluded.updated_at,version=excluded.version""",
          (int(user_id), source, next_cursor, _json(metadata), now, version + 1))
    return {**metadata, "cursor": next_cursor, "version": version + 1, "inserted": inserted, "updated_at": now}
