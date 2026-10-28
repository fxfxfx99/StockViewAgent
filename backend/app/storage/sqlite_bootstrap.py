"""Versioned SQLite setup shared by the new public-data stores.

Only initialization takes the cross-process file lock. Normal connections read
the on-disk version and leave contention to ordinary SQLite transactions. No
process cache can accidentally reuse initialization state after a file is moved
or replaced, and an interrupted v0 migration can safely be retried.
"""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import sqlite3
import stat


def _connect(path):
    if path.is_symlink():
        raise ValueError("本地数据库不能为符号链接")
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _initialise(path, schema, version):
    # journal_mode changes can return SQLITE_BUSY immediately despite SQLite's
    # busy timeout. Serialize the mode switch across API threads and workers.
    lock_path = path.with_name(path.name + ".init.lock")
    try:
        info = lock_path.lstat()
    except FileNotFoundError:
        info = None
    if info is not None and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
        raise ValueError("数据库初始化锁必须为无链接的普通文件")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(descriptor)
        raise ValueError("数据库初始化锁必须为无链接的普通文件")
    with os.fdopen(descriptor, "a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            conn = _connect(path)
            try:
                if conn.execute("PRAGMA user_version").fetchone()[0] >= version:
                    return
                path.chmod(0o600)
                if conn.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
                    conn.execute("PRAGMA journal_mode=WAL")
                # The version marker is committed with the schema. A failed
                # table/index/FTS creation must never advertise a ready database.
                conn.executescript("BEGIN IMMEDIATE;\n" + schema + f"\nPRAGMA user_version={version};\nCOMMIT;")
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def connection(path: Path, schema: str, *, version: int = 1):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(path)
    try:
        if conn.execute("PRAGMA user_version").fetchone()[0] < version:
            # Close the probe before waiting for the lock: it must not retain a
            # reader while the owner changes journal mode on a fresh database.
            conn.close()
            conn = None
            _initialise(path, schema, version)
            conn = _connect(path)
        yield conn
        conn.commit()
    except BaseException:
        if conn is not None:
            conn.rollback()
        raise
    finally:
        if conn is not None:
            conn.close()
