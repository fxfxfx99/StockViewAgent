"""Versioned local knowledge registry with disposable, human-readable projections.

SQLite identifies authoritative immutable objects. Current files are never
imported implicitly: missing or edited projections are repaired from verified
objects on access. Objects and registry are durable knowledge, not TTL caches.
Callers must supply deliberately selected public data, never whole settings or
credential files. The existing stock_features.sqlite3 remains untouched.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import time
import uuid
import zlib

from app.config import settings

KINDS = ("stock_features", "news_relevance", "prompts")
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
_MAX_CONTENT = 16 * 1024 * 1024
_SCHEMA = """
CREATE TABLE IF NOT EXISTS objects (
  content_hash TEXT NOT NULL, format TEXT NOT NULL,
  byte_size INTEGER NOT NULL, stored_bytes INTEGER NOT NULL, PRIMARY KEY(content_hash,format));
CREATE TABLE IF NOT EXISTS revisions (
  seq INTEGER PRIMARY KEY, revision_id TEXT UNIQUE NOT NULL, kind TEXT NOT NULL,
  document_key TEXT NOT NULL, content_hash TEXT NOT NULL, format TEXT NOT NULL,
  created_at REAL NOT NULL, reason TEXT NOT NULL, actor_id TEXT, provenance TEXT NOT NULL,
  FOREIGN KEY(content_hash,format) REFERENCES objects(content_hash,format));
CREATE INDEX IF NOT EXISTS revisions_document ON revisions(kind,document_key,seq DESC);
CREATE TABLE IF NOT EXISTS heads (
  kind TEXT NOT NULL, document_key TEXT NOT NULL, revision_id TEXT NOT NULL,
  updated_at REAL NOT NULL, PRIMARY KEY(kind,document_key),
  FOREIGN KEY(revision_id) REFERENCES revisions(revision_id));
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY, event_id TEXT UNIQUE NOT NULL, kind TEXT NOT NULL,
  document_key TEXT NOT NULL, event_type TEXT NOT NULL, revision_id TEXT,
  created_at REAL NOT NULL, reason TEXT NOT NULL, actor_id TEXT, details TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_document ON events(kind,document_key,seq DESC);
CREATE TRIGGER IF NOT EXISTS revisions_no_update BEFORE UPDATE ON revisions
  BEGIN SELECT RAISE(ABORT,'knowledge revisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS revisions_no_delete BEFORE DELETE ON revisions
  BEGIN SELECT RAISE(ABORT,'knowledge revisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS objects_no_update BEFORE UPDATE ON objects
  BEGIN SELECT RAISE(ABORT,'knowledge objects are immutable'); END;
CREATE TRIGGER IF NOT EXISTS objects_no_delete BEFORE DELETE ON objects
  BEGIN SELECT RAISE(ABORT,'knowledge objects are immutable'); END;
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
  BEGIN SELECT RAISE(ABORT,'knowledge events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
  BEGIN SELECT RAISE(ABORT,'knowledge events are immutable'); END;
"""
_README = """# 本地知识缓存与版本管理

此目录由 StockViewAgent 自动管理。registry.sqlite3 保存不可变版本、当前启用版本及变更记录；objects/ 按 SHA-256 去重保存权威正文（JSON gzip 压缩）；current/ 是便于阅读的当前版本，manifests/ 保存版本来源和路径。不要手动修改数据库。

目录结构：
- registry.sqlite3：知识管理登记库（元数据、版本和审计记录，不重复存放正文）
- objects/sha256/<前两位>/<哈希>.json.gz 或 .md：不可变权威内容，压缩前正文 SHA-256 校验
- current/stock_features|news_relevance|prompts/<标识>.json 或 .md：当前启用内容
- manifests/stock_features|news_relevance|prompts/<标识>.json：当前启用版本及来源
- registry.lock：跨进程读写锁

个股特征的权威证据仍保存在上一级 stock_features.sqlite3，此目录不会移动、删除或替代它；个股镜像可由业务服务重建。新闻相关性知识和提示词的正文、版本与启用状态保存在 objects/ 和 registry.sqlite3，请连同该数据库的 SQLite WAL 一起安全备份，或停止服务后备份整个目录。objects/ 和 registry.sqlite3 禁止作为普通缓存清理。

current/ 和 manifests/ 是可重建投影，不接受手工文件修改作为启用操作。读取时会核对并从权威对象修复缺失或被改写的投影；权威对象损坏则明确报错，请恢复可信备份或由业务服务重新生成。符号链接和硬链接会被拒绝。编辑提示词、发布和回滚应使用应用管理接口，以保留审计和乐观并发检查。

只写入业务服务明确提供的数据；本模块不会扫描或导入账户、令牌、密码、配置文件。没有自动清空、过期删除或历史回收操作。
"""


class KnowledgeCacheError(ValueError):
    """Invalid content, path, or damaged authoritative registry."""


class RevisionConflict(KnowledgeCacheError):
    """The active revision changed since the caller inspected it."""


def root_path():
    return Path(settings.data_dir).absolute() / "knowledge_cache"


def _identity(kind, key):
    if kind not in KINDS:
        raise KnowledgeCacheError("无效知识类型")
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", key):
        raise KnowledgeCacheError("知识标识仅允许字母、数字、点、下划线和连字符，长度不超过 128")
    return kind, key


def _revision(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise KnowledgeCacheError("无效版本标识")
    return value


def _json(value):
    try:
        return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise KnowledgeCacheError("内容必须是有效 JSON，且不能包含 NaN 或无穷值") from exc


def _encode(content, format):
    if format == "json":
        payload = _json(content)
    elif format == "markdown" and isinstance(content, str):
        try:
            payload = content.encode("utf-8")
        except UnicodeError as exc:
            raise KnowledgeCacheError("Markdown 必须是有效 UTF-8 文本") from exc
    else:
        raise KnowledgeCacheError("仅支持 JSON 内容或 Markdown 文本")
    if len(payload) > _MAX_CONTENT:
        raise KnowledgeCacheError("单份知识内容不能超过 16 MiB")
    return payload


def _text(value, *, maximum=4000):
    if not isinstance(value, str) or len(value) > maximum or "\x00" in value:
        raise KnowledgeCacheError("变更说明或事件类型无效")
    return value


def _actor(value):
    if value is None:
        return None
    return _text(str(value), maximum=128)


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else None


@contextmanager
def _parent(root, relative, *, create=False):
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or "\\" in relative or relative != path.as_posix():
        raise KnowledgeCacheError("知识缓存路径必须位于缓存根目录内")
    directory = os.dup(root)
    try:
        for part in path.parts[:-1]:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=directory)
                except FileExistsError:
                    pass
            child = os.open(part, _DIR_FLAGS, dir_fd=directory)
            os.close(directory)
            directory = child
        yield directory, path.name
    finally:
        os.close(directory)


def _regular(directory, name, *, missing=True):
    try:
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        if missing:
            return None
        raise
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise KnowledgeCacheError("知识缓存只允许无符号链接或硬链接的普通文件")
    return info


def _read_bytes(root, relative):
    try:
        with _parent(root, relative) as (directory, name):
            _regular(directory, name, missing=False)
            fd = os.open(name, _FILE_FLAGS, dir_fd=directory)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise KnowledgeCacheError("知识缓存文件链接无效")
                if info.st_size > _MAX_CONTENT + 65536:
                    return None  # Oversized projections are replaced, never parsed.
                with os.fdopen(os.dup(fd), "rb") as handle:
                    return handle.read(_MAX_CONTENT + 65537)
            finally:
                os.close(fd)
    except FileNotFoundError:
        return None


def _atomic_write(root, relative, payload):
    with _parent(root, relative, create=True) as (directory, name):
        _regular(directory, name)
        temporary = f".{name}.{uuid.uuid4().hex}.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            _regular(directory, name)
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass


def _ensure_bytes(root, relative, payload):
    if _read_bytes(root, relative) == payload:
        return False
    _atomic_write(root, relative, payload)
    return True


def _remove_projection(root, relative):
    try:
        with _parent(root, relative) as (directory, name):
            if _regular(directory, name) is not None:
                os.unlink(name, dir_fd=directory)
                os.fsync(directory)
                return True
    except FileNotFoundError:
        pass
    return False


@contextmanager
def _locked():
    data = Path(settings.data_dir).absolute()
    if data.is_symlink():
        raise KnowledgeCacheError("数据根目录不能是符号链接")
    data.mkdir(mode=0o700, parents=True, exist_ok=True)
    data_fd = os.open(data, _DIR_FLAGS)
    root = lock = None
    conn = None
    try:
        try:
            os.mkdir("knowledge_cache", 0o700, dir_fd=data_fd)
        except FileExistsError:
            pass
        root = os.open("knowledge_cache", _DIR_FLAGS, dir_fd=data_fd)
        _regular(root, "registry.lock")
        lock_flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        # Darwin may return ENOENT when concurrent openat(O_CREAT|O_NOFOLLOW)
        # races on a missing file. Exclusive creation followed by guarded open
        # avoids that first-use race without weakening link protection.
        try:
            lock = os.open("registry.lock", lock_flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=root)
        except FileExistsError:
            lock = os.open("registry.lock", lock_flags, dir_fd=root)
        if not stat.S_ISREG(os.fstat(lock).st_mode) or os.fstat(lock).st_nlink != 1:
            raise KnowledgeCacheError("知识缓存锁文件无效")
        fcntl.flock(lock, fcntl.LOCK_EX)
        for name in ("registry.sqlite3", "registry.sqlite3-wal", "registry.sqlite3-shm", "registry.sqlite3-journal"):
            _regular(root, name)
        path = root_path()
        root_info, path_info = os.fstat(root), os.stat(path)
        if path.is_symlink() or (path_info.st_dev, path_info.st_ino) != (root_info.st_dev, root_info.st_ino):
            raise KnowledgeCacheError("知识缓存根目录发生变化")
        # Set permissions at creation rather than briefly exposing a new DB under
        # the user's broader umask. Check the inode again after SQLite opens it.
        database_fd = os.open("registry.sqlite3", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=root)
        try:
            before = os.fstat(database_fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise KnowledgeCacheError("知识登记库文件无效")
        finally:
            os.close(database_fd)
        conn = sqlite3.connect(path / "registry.sqlite3", timeout=30)
        after = _regular(root, "registry.sqlite3", missing=False)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise KnowledgeCacheError("知识登记库文件发生变化")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        if conn.execute("PRAGMA user_version").fetchone()[0] == 0:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.executescript("BEGIN IMMEDIATE;\n" + _SCHEMA + "\nPRAGMA user_version=1;\nCOMMIT;")
            os.chmod("registry.sqlite3", 0o600, dir_fd=root, follow_symlinks=False)
        if conn.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise KnowledgeCacheError("知识登记库版本不兼容")
        # Documentation is generated once, without reading any external data.
        if _read_bytes(root, "README.md") is None:
            _atomic_write(root, "README.md", _README.encode("utf-8"))
        yield root, conn
        conn.commit()
    except (OSError, sqlite3.Error) as exc:
        if conn is not None:
            conn.rollback()
        raise KnowledgeCacheError(f"知识缓存读写失败：{type(exc).__name__}") from exc
    except BaseException:
        if conn is not None:
            conn.rollback()
        raise
    finally:
        if conn is not None:
            conn.close()
        if lock is not None:
            os.close(lock)
        if root is not None:
            os.close(root)
        os.close(data_fd)


def _head(conn, kind, key):
    return conn.execute("SELECT revision_id,updated_at FROM heads WHERE kind=? AND document_key=?", (kind, key)).fetchone()


def _row(conn, kind, key, revision_id=None):
    if revision_id is None:
        head = _head(conn, kind, key)
        if head is None:
            return None
        revision_id = head["revision_id"]
    return conn.execute("SELECT r.*,o.byte_size FROM revisions r JOIN objects o USING(content_hash,format) "
                        "WHERE r.kind=? AND r.document_key=? AND r.revision_id=?", (kind, key, revision_id)).fetchone()


def _metadata(conn, row):
    head = _head(conn, row["kind"], row["document_key"])
    ext = "json" if row["format"] == "json" else "md"
    digest = row["content_hash"]
    return {"kind": row["kind"], "key": row["document_key"], "revision_id": row["revision_id"],
            "content_hash": digest, "format": row["format"], "byte_size": row["byte_size"],
            "created_at": _iso(row["created_at"]), "updated_at": _iso(head["updated_at"]) if head else None,
            "reason": row["reason"], "actor_id": row["actor_id"], "provenance": json.loads(row["provenance"]),
            "active_revision": head["revision_id"] if head else None,
            "is_active": bool(head and head["revision_id"] == row["revision_id"]),
            "object_path": f"objects/sha256/{digest[:2]}/{digest}.{ext}" + (".gz" if ext == "json" else ""),
            "current_path": f"current/{row['kind']}/{row['document_key']}.{ext}",
            "manifest_path": f"manifests/{row['kind']}/{row['document_key']}.json",
            "projection_status": "unchecked"}


def _object_path(digest, format):
    extension = "json.gz" if format == "json" else "md"
    return f"objects/sha256/{digest[:2]}/{digest}.{extension}"


def _ensure_object(root, digest, format, payload):
    relative = _object_path(digest, format)
    existing = _read_bytes(root, relative)
    if existing is None:
        try:
            with _parent(root, relative) as (directory, name):
                if _regular(directory, name) is not None:
                    raise KnowledgeCacheError("知识权威对象超过读取上限；请恢复可信备份")
        except FileNotFoundError:
            pass
    if existing is not None:
        # A publish operation must not silently heal damaged authority and hide
        # the failure. Existing valid compression may differ without changing
        # the content identity; preserve those original bytes.
        if _payload(root, {"content_hash": digest, "format": format}) != payload:
            raise KnowledgeCacheError("知识权威对象与待写入内容不匹配")
        return len(existing)
    stored = gzip.compress(payload, compresslevel=6, mtime=0) if format == "json" else payload
    _atomic_write(root, relative, stored)
    return len(stored)


def _payload(root, row):
    stored = _read_bytes(root, _object_path(row["content_hash"], row["format"]))
    if stored is None:
        raise KnowledgeCacheError("知识权威对象缺失；请恢复可信备份或重新生成")
    try:
        if row["format"] == "json":
            # Bound decompression as well as disk reads to avoid zip bombs.
            import io
            with gzip.GzipFile(fileobj=io.BytesIO(stored)) as handle:
                payload = handle.read(_MAX_CONTENT + 1)
        else:
            payload = stored
    except (OSError, EOFError, zlib.error) as exc:
        raise KnowledgeCacheError("知识权威对象解压失败；请恢复可信备份或重新生成") from exc
    if len(payload) > _MAX_CONTENT or hashlib.sha256(payload).hexdigest() != row["content_hash"]:
        raise KnowledgeCacheError("知识权威对象内容校验失败；请恢复可信备份或重新生成")
    return payload


def _project(root, conn, row, *, active=False):
    metadata = _metadata(conn, row)
    payload = _payload(root, row)
    repaired = False
    if active:
        repaired = _ensure_bytes(root, metadata["current_path"], payload)
        manifest = {k: v for k, v in metadata.items() if k != "projection_status"}
        repaired = _ensure_bytes(root, metadata["manifest_path"], _json(manifest)) or repaired
        other_extension = "md" if row["format"] == "json" else "json"
        repaired = _remove_projection(root, f"current/{row['kind']}/{row['document_key']}.{other_extension}") or repaired
    return "repaired" if repaired else "healthy"


def _check_expected(head, expected_revision):
    if expected_revision is not None and expected_revision != (head["revision_id"] if head else ""):
        raise RevisionConflict("启用版本已发生变化，请刷新后再提交")


def _append_event(conn, kind, key, event_type, revision_id, reason, actor_id, details):
    now = time.time()
    event = {"event_id": uuid.uuid4().hex, "kind": kind, "key": key, "event_type": event_type,
             "revision_id": revision_id, "created_at": _iso(now), "reason": reason,
             "actor_id": actor_id, "details": details}
    conn.execute("INSERT INTO events(event_id,kind,document_key,event_type,revision_id,created_at,reason,actor_id,details) VALUES(?,?,?,?,?,?,?,?,?)",
                 (event["event_id"], kind, key, event_type, revision_id, now, reason, actor_id, _json(details).decode()))
    return event


def put_document(kind, key, content, *, format="json", reason="", actor_id=None,
                 provenance=None, expected_revision=None, activate=True):
    """Append a version and optionally activate it; identical current content is reused.

    expected_revision=None disables the optimistic check; '' requires no head.
    Files written before a failed transaction remain disposable projections; the
    previous database head is retained and repaired by the next successful read.
    """
    _identity(kind, key)
    payload = _encode(content, format)
    reason, actor_id = _text(reason), _actor(actor_id)
    if provenance is None:
        provenance = {}
    if not isinstance(provenance, dict) or len(_json(provenance)) > 65536:
        raise KnowledgeCacheError("来源必须是 64 KiB 以内的 JSON 对象")
    digest = hashlib.sha256(payload).hexdigest()
    with _locked() as (root, conn):
        conn.execute("BEGIN IMMEDIATE")
        head = _head(conn, kind, key)
        _check_expected(head, expected_revision)
        previous = _row(conn, kind, key)
        stored_bytes = _ensure_object(root, digest, format, payload)
        if previous and previous["content_hash"] == digest and previous["format"] == format:
            status = _project(root, conn, previous, active=True)
            _append_event(conn, kind, key, "checked", previous["revision_id"], reason, actor_id,
                          {"unchanged": True, "provenance": provenance})
            return {**_metadata(conn, previous), "projection_status": status, "unchanged": True}
        now, revision_id = time.time(), uuid.uuid4().hex
        conn.execute("INSERT OR IGNORE INTO objects(content_hash,format,byte_size,stored_bytes) VALUES(?,?,?,?)", (digest, format, len(payload), stored_bytes))
        conn.execute("INSERT INTO revisions(revision_id,kind,document_key,content_hash,format,created_at,reason,actor_id,provenance) VALUES(?,?,?,?,?,?,?,?,?)",
                     (revision_id, kind, key, digest, format, now, reason, actor_id, _json(provenance).decode()))
        if activate:
            conn.execute("INSERT INTO heads(kind,document_key,revision_id,updated_at) VALUES(?,?,?,?) "
                         "ON CONFLICT(kind,document_key) DO UPDATE SET revision_id=excluded.revision_id,updated_at=excluded.updated_at", (kind, key, revision_id, now))
        row = _row(conn, kind, key, revision_id)
        status = _project(root, conn, row, active=activate)
        _append_event(conn, kind, key, "published" if activate else "drafted", revision_id, reason, actor_id,
                      {"previous_revision": head["revision_id"] if head else None, "content_hash": digest})
        # Commit before returning success; a commit failure must never be hidden.
        conn.commit()
        return {**_metadata(conn, row), "projection_status": status, "unchanged": False}


def get_document(kind, key, revision_id=None):
    _identity(kind, key)
    if revision_id is not None:
        _revision(revision_id)
    with _locked() as (root, conn):
        row = _row(conn, kind, key, revision_id)
        if row is None:
            return None
        metadata = _metadata(conn, row)
        payload = _payload(root, row)
        try:
            metadata["projection_status"] = _project(root, conn, row, active=metadata["is_active"])
        except (OSError, KnowledgeCacheError) as exc:
            # The verified immutable content remains available even when a projection
            # cannot be repaired. Never read through a link or adopt an edited file.
            metadata["projection_status"] = "repair_failed"
            metadata["projection_error"] = type(exc).__name__
        return {"metadata": metadata, "content": json.loads(payload) if row["format"] == "json" else payload.decode("utf-8")}


def get_head(kind, key):
    """Read active metadata without loading or repairing content projections."""
    _identity(kind, key)
    with _locked() as (_, conn):
        row = _row(conn, kind, key)
        return _metadata(conn, row) if row else None


def cache_epoch(kind=None):
    """Small activation signature: rollback also changes the returned token."""
    if kind is not None and kind not in KINDS:
        raise KnowledgeCacheError("无效知识类型")
    with _locked() as (_, conn):
        row = conn.execute("SELECT COALESCE(MAX(seq),0) FROM events WHERE event_type IN ('published','activated')" +
                           (" AND kind=?" if kind else ""), (kind,) if kind else ()).fetchone()
        return str(row[0])


def _page(offset, limit, *, maximum=200):
    if isinstance(offset, bool) or isinstance(limit, bool) or not isinstance(offset, int) or not isinstance(limit, int) or offset < 0 or not 1 <= limit <= maximum:
        raise KnowledgeCacheError(f"分页参数无效，limit 必须为 1 至 {maximum}")


def list_documents(kind=None, q="", offset=0, limit=50):
    if kind is not None and kind not in KINDS:
        raise KnowledgeCacheError("无效知识类型")
    _page(offset, limit)
    _text(q, maximum=128)
    where, args = [], []
    source = " FROM heads h JOIN revisions r ON r.revision_id=h.revision_id"
    if kind:
        where.append("h.kind=?")
        args.append(kind)
    if q:
        # The active stock title also names its same-key relevance document.
        # Both joins use existing unique keys: do not read compressed bodies or
        # search every stock revision in a per-result correlated subquery.
        source += (" LEFT JOIN heads stock_h ON stock_h.kind='stock_features' "
                   "AND stock_h.document_key=h.document_key AND h.kind='news_relevance'"
                   " LEFT JOIN revisions stock_r ON stock_r.revision_id=stock_h.revision_id")
        where.append("(h.document_key LIKE ? ESCAPE '\\' "
                     "OR (h.kind='stock_features' AND json_extract(r.provenance,'$.title') LIKE ? ESCAPE '\\') "
                     "OR (h.kind='news_relevance' AND json_extract(stock_r.provenance,'$.title') LIKE ? ESCAPE '\\'))")
        pattern = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        args.extend([pattern, pattern, pattern])
    condition = " WHERE " + " AND ".join(where) if where else ""
    with _locked() as (_, conn):
        total = conn.execute("SELECT COUNT(*)" + (source if q else " FROM heads h") + condition, args).fetchone()[0]
        rows = conn.execute("SELECT r.*,o.byte_size" + source +
                            " JOIN objects o ON o.content_hash=r.content_hash AND o.format=r.format" + condition +
                            " ORDER BY h.updated_at DESC,h.kind,h.document_key LIMIT ? OFFSET ?", [*args, limit, offset]).fetchall()
        return {"items": [_metadata(conn, row) for row in rows], "total": total, "offset": offset, "limit": limit}


def history(kind, key, limit=20):
    _identity(kind, key)
    _page(0, limit)
    with _locked() as (_, conn):
        rows = conn.execute("SELECT r.*,o.byte_size FROM revisions r JOIN objects o USING(content_hash,format) "
                            "WHERE kind=? AND document_key=? ORDER BY seq DESC LIMIT ?", (kind, key, limit)).fetchall()
        return [_metadata(conn, row) for row in rows]


def history_page(kind, key, offset=0, limit=50):
    """Page every historical revision, including versions older than 200 rows."""
    _identity(kind, key)
    _page(offset, limit)
    with _locked() as (_, conn):
        total = conn.execute("SELECT COUNT(*) FROM revisions WHERE kind=? AND document_key=?", (kind, key)).fetchone()[0]
        rows = conn.execute("SELECT r.*,o.byte_size FROM revisions r JOIN objects o USING(content_hash,format) "
                            "WHERE kind=? AND document_key=? ORDER BY seq DESC LIMIT ? OFFSET ?", (kind, key, limit, offset)).fetchall()
        return {"items": [_metadata(conn, row) for row in rows], "total": total, "offset": offset, "limit": limit}


def previous_revision(kind, key, revision_id):
    """Find the actual preceding version of this document in append order."""
    _identity(kind, key)
    _revision(revision_id)
    with _locked() as (_, conn):
        selected = _row(conn, kind, key, revision_id)
        if selected is None:
            return None
        row = conn.execute("SELECT r.*,o.byte_size FROM revisions r JOIN objects o USING(content_hash,format) "
                           "WHERE kind=? AND document_key=? AND seq<? ORDER BY seq DESC LIMIT 1", (kind, key, selected["seq"])).fetchone()
        return _metadata(conn, row) if row else None


def activate_revision(kind, key, revision_id, reason="", actor_id=None, expected_revision=None):
    _identity(kind, key)
    _revision(revision_id)
    reason, actor_id = _text(reason), _actor(actor_id)
    with _locked() as (root, conn):
        conn.execute("BEGIN IMMEDIATE")
        head = _head(conn, kind, key)
        _check_expected(head, expected_revision)
        row = _row(conn, kind, key, revision_id)
        if row is None:
            raise KnowledgeCacheError("指定版本不属于此知识文档或不存在")
        conn.execute("INSERT INTO heads(kind,document_key,revision_id,updated_at) VALUES(?,?,?,?) "
                     "ON CONFLICT(kind,document_key) DO UPDATE SET revision_id=excluded.revision_id,updated_at=excluded.updated_at", (kind, key, revision_id, time.time()))
        status = _project(root, conn, row, active=True)
        _append_event(conn, kind, key, "activated", revision_id, reason, actor_id, {"previous_revision": head["revision_id"] if head else None})
        conn.commit()
        return {**_metadata(conn, row), "projection_status": status}


def record_event(kind, key, event_type, *, revision_id=None, reason="", actor_id=None, details=None):
    _identity(kind, key)
    _text(event_type, maximum=64)
    if not event_type:
        raise KnowledgeCacheError("事件类型不能为空")
    reason, actor_id = _text(reason), _actor(actor_id)
    if details is None:
        details = {}
    if not isinstance(details, dict) or len(_json(details)) > 65536:
        raise KnowledgeCacheError("事件详情必须是 64 KiB 以内的 JSON 对象")
    with _locked() as (_, conn):
        if revision_id is not None:
            _revision(revision_id)
            if _row(conn, kind, key, revision_id) is None:
                raise KnowledgeCacheError("事件版本不存在或属于其他文档")
        return _append_event(conn, kind, key, event_type, revision_id, reason, actor_id, details)


def list_events(kind=None, key=None, limit=50, offset=0):
    _page(offset, limit)
    if kind is not None and kind not in KINDS:
        raise KnowledgeCacheError("无效知识类型")
    if key is not None:
        _identity(kind or KINDS[0], key)
    where, args = [], []
    for column, value in (("kind", kind), ("document_key", key)):
        if value is not None:
            where.append(column + "=?")
            args.append(value)
    condition = " WHERE " + " AND ".join(where) if where else ""
    with _locked() as (_, conn):
        total = conn.execute("SELECT COUNT(*) FROM events" + condition, args).fetchone()[0]
        rows = conn.execute("SELECT * FROM events" + condition + " ORDER BY seq DESC LIMIT ? OFFSET ?", [*args, limit, offset]).fetchall()
    items = [{"event_id": row["event_id"], "kind": row["kind"], "key": row["document_key"], "event_type": row["event_type"],
              "revision_id": row["revision_id"], "created_at": _iso(row["created_at"]), "reason": row["reason"],
              "actor_id": row["actor_id"], "details": json.loads(row["details"])} for row in rows]
    return {"items": items, "total": total, "offset": offset, "limit": limit}


def overview():
    """Registry-derived counts and bounded stat calls; never recursively scan data."""
    with _locked() as (root, conn):
        kinds = {kind: {"documents": 0, "revisions": 0} for kind in KINDS}
        for row in conn.execute("SELECT kind,COUNT(*) AS count FROM heads GROUP BY kind"):
            kinds[row["kind"]]["documents"] = row["count"]
        for row in conn.execute("SELECT kind,COUNT(*) AS count FROM revisions GROUP BY kind"):
            kinds[row["kind"]]["revisions"] = row["count"]
        objects = conn.execute("SELECT COUNT(*),COALESCE(SUM(stored_bytes),0),COALESCE(SUM(byte_size),0) FROM objects").fetchone()
        current_bytes = conn.execute("SELECT COALESCE(SUM(o.byte_size),0) FROM heads h JOIN revisions r ON r.revision_id=h.revision_id JOIN objects o USING(content_hash,format)").fetchone()[0]
        database_bytes = sum((info.st_size if info else 0) for info in (_regular(root, name) for name in ("registry.sqlite3", "registry.sqlite3-wal", "registry.sqlite3-shm")))
        events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        last_updated = conn.execute("SELECT MAX(updated_at) FROM heads").fetchone()[0]
        return {"root_path": str(root_path()), "schema_version": 1,
                "counts": {"documents": sum(v["documents"] for v in kinds.values()),
                           "revisions": sum(v["revisions"] for v in kinds.values()), "objects": objects[0], "events": events},
                "kinds": kinds, "bytes": {"objects": objects[1], "objects_uncompressed": objects[2], "current": current_bytes, "database": database_bytes,
                                          "estimated_total": objects[1] + current_bytes + database_bytes},
                "last_updated_at": _iso(last_updated), "directories": ["objects/sha256", "current", "manifests"],
                "registry_path": "registry.sqlite3", "readme_path": "README.md", "projection_policy": "immutable_objects_authoritative_repair_current_on_read",
                "stock_features_source": "../stock_features.sqlite3", "automatic_deletion": False}
