"""Offline, copy-only data-root migration using only the Python standard library.

API/worker processes hold a shared directory lock for their entire lifetime.
Migration takes the exclusive lock, verifies a private staging tree, and uses a
no-replace rename to publish it. No source data file is deleted or rewritten;
the cooperative lock and SQLite's transient shared-memory file may be created.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from datetime import datetime, timezone
import errno
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import time

from app.runtime_paths import resolve_data_dir

LOCK_NAME = ".data-directory.lock"
MANIFEST_PATH = "_managed/migration-manifest.json"
SQLITE_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
SQLITE_HEADER = b"SQLite format 3\x00"
_READ_FLAGS = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
               | getattr(os, "O_NONBLOCK", 0))
_DIR_FLAGS = _READ_FLAGS | getattr(os, "O_DIRECTORY", 0)


class DataPortabilityError(RuntimeError):
    pass


class DataDirectoryInUse(DataPortabilityError):
    pass


def _root_path(value: Path, *, must_exist: bool = True) -> Path:
    raw = Path(value).expanduser().absolute()
    if raw.is_symlink():
        raise DataPortabilityError("Data directory must not be a symbolic link")
    root = raw.resolve()
    if must_exist and not root.is_dir():
        raise DataPortabilityError("Data directory does not exist or is not a directory")
    return root


def _make_private_directories(path: Path):
    """Create only missing ancestors with private permissions; leave existing parents alone."""
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink() or not directory.is_dir():
            raise DataPortabilityError("Data parent changed while creating a private directory")


@contextmanager
def data_directory_lock(root: Path, *, exclusive: bool = False, blocking: bool = False):
    """Cooperative process lock; a shared holder prevents an offline migration."""
    import fcntl

    root = _root_path(root, must_exist=False)
    _make_private_directories(root)
    directory = os.open(root, _DIR_FLAGS)
    descriptor = None
    try:
        descriptor = os.open(LOCK_NAME, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
                             | getattr(os, "O_CLOEXEC", 0), 0o600, dir_fd=directory)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise DataPortabilityError("Invalid data-directory lock file")
        os.fchmod(descriptor, 0o600)
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        if not blocking:
            operation |= fcntl.LOCK_NB
        try:
            fcntl.flock(descriptor, operation)
        except BlockingIOError:
            raise DataDirectoryInUse("Data directory is in use; stop API, worker and other storage commands first") from None
        try:
            yield root
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory)


def _relative_path(value) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise DataPortabilityError("Invalid relative manifest path")
    path = PurePosixPath(value)
    if path.is_absolute() or value != path.as_posix() or ".." in path.parts or value == "." or re.match(r"^[A-Za-z]:", value):
        raise DataPortabilityError("Invalid relative manifest path")
    return value


def _excluded(relative: str, *, directory: bool = False) -> bool:
    parts = PurePosixPath(relative).parts
    return (parts[0] == "pids" or parts[:2] == ("runtime", "pids")
            or (not directory and parts[-1].endswith(".lock")) or relative == MANIFEST_PATH)


def _fingerprint(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


@contextmanager
def _open_file(root_fd: int, relative: str, flags=_READ_FLAGS, mode=0o600):
    """Walk via directory descriptors, refusing symlinks in every component."""
    parts = PurePosixPath(_relative_path(relative)).parts
    parent = os.dup(root_fd)
    descriptor = None
    try:
        for part in parts[:-1]:
            child = os.open(part, _DIR_FLAGS, dir_fd=parent)
            os.close(parent)
            parent = child
        descriptor = os.open(parts[-1], flags, mode, dir_fd=parent)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise DataPortabilityError("Only regular data files are supported")
        yield descriptor
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)


def _scan_tree(root_fd: int):
    files, directories = {}, []

    def walk(directory, prefix=""):
        with os.scandir(directory) as entries:
            names = sorted(entry.name for entry in entries)
        for name in names:
            relative = _relative_path(f"{prefix}/{name}" if prefix else name)
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                raise DataPortabilityError(f"Symbolic link is not allowed: {relative}")
            if not stat.S_ISREG(info.st_mode) and not stat.S_ISDIR(info.st_mode):
                raise DataPortabilityError(f"Special file is not allowed: {relative}")
            if _excluded(relative, directory=stat.S_ISDIR(info.st_mode)):
                continue
            if stat.S_ISDIR(info.st_mode):
                directories.append(relative)
                child = os.open(name, _DIR_FLAGS, dir_fd=directory)
                try:
                    walk(child, relative)
                finally:
                    os.close(child)
            else:
                files[relative] = info

    walk(root_fd)
    return files, sorted(directories)


def _sha256(root_fd, relative):
    digest = hashlib.sha256()
    with _open_file(root_fd, relative) as descriptor, os.fdopen(os.dup(descriptor), "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sqlite_files(root_fd, files):
    databases = set()
    for relative in files:
        if relative.endswith(("-wal", "-shm", "-journal")):
            continue
        with _open_file(root_fd, relative) as descriptor:
            header = os.read(descriptor, len(SQLITE_HEADER))
        if header == SQLITE_HEADER or PurePosixPath(relative).suffix.lower() in SQLITE_SUFFIXES:
            databases.add(relative)
    sidecars = set()
    for relative in files:
        suffix = next((suffix for suffix in ("-wal", "-shm", "-journal") if relative.endswith(suffix)), None)
        if suffix:
            if relative[:-len(suffix)] not in databases:
                raise DataPortabilityError(f"SQLite sidecar has no database: {relative}")
            sidecars.add(relative)
    return databases, sidecars


def _sqlite_uri(path: Path):
    return path.as_uri() + "?mode=ro"


def _quick_check(path: Path):
    connection = sqlite3.connect(_sqlite_uri(path), uri=True, timeout=5)
    try:
        result = connection.execute("PRAGMA quick_check").fetchall()
        if result != [("ok",)]:
            raise DataPortabilityError("SQLite integrity check failed")
    finally:
        connection.close()


def _backup_sqlite(source: Path, destination: Path, *, timeout: float = 30):
    # SQLite's backup API copies the committed view, including WAL frames; a
    # raw copy of the main .db file can omit committed rows.
    source_db = sqlite3.connect(_sqlite_uri(source), uri=True, timeout=5)
    target_db = None
    last_progress, previous_remaining = time.monotonic(), None

    def progress(status, remaining, total):
        nonlocal last_progress, previous_remaining
        now = time.monotonic()
        if status == sqlite3.SQLITE_OK and (previous_remaining is None or remaining < previous_remaining):
            last_progress = now
        previous_remaining = remaining
        if now - last_progress > timeout:
            raise DataPortabilityError("SQLite backup did not make progress; stop external database writers")

    try:
        source_db.execute("BEGIN")
        if source_db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise DataPortabilityError("Source SQLite integrity check failed")
        # Create with restrictive permissions before sqlite opens it.
        descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        target_db = sqlite3.connect(destination)
        source_db.backup(target_db, pages=256, progress=progress, sleep=0.05)
        target_db.commit()
        if target_db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise DataPortabilityError("Copied SQLite integrity check failed")
        # Publish a standalone main DB. It can return to WAL on app startup.
        target_db.execute("PRAGMA journal_mode=DELETE")
    finally:
        if target_db is not None:
            target_db.close()
        source_db.close()
    with destination.open("rb") as handle:
        os.fsync(handle.fileno())
    os.chmod(destination, 0o600)


def _copy_regular(source_fd, target_fd, relative, expected):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    with _open_file(source_fd, relative) as source, _open_file(target_fd, relative, flags) as target:
        if _fingerprint(os.fstat(source)) != _fingerprint(expected):
            raise DataPortabilityError("Source changed during migration")
        digest = hashlib.sha256()
        while block := os.read(source, 1024 * 1024):
            digest.update(block)
            view = memoryview(block)
            while view:
                written = os.write(target, view)
                if not written:
                    raise DataPortabilityError("Unable to finish writing copied data")
                view = view[written:]
        os.fsync(target)
        os.fchmod(target, 0o600)
        if _fingerprint(os.fstat(source)) != _fingerprint(expected):
            raise DataPortabilityError("Source changed during migration")
        return digest.hexdigest()


def _verify_research(root_fd, files, hashes):
    checked = 0
    for relative in files:
        match = re.fullmatch(r"research/batches/([0-9a-f]{64})/manifest\.json", relative)
        if not match:
            continue
        with _open_file(root_fd, relative) as descriptor, os.fdopen(os.dup(descriptor), "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
        if not isinstance(manifest, dict) or manifest.get("batch_id") != match[1] or not isinstance(manifest.get("files"), dict) or set(manifest["files"]) != {"raw.json", "bars.parquet"}:
            raise DataPortabilityError("Research manifest is invalid")
        prefix = str(PurePosixPath(relative).parent)
        for filename, digest in manifest["files"].items():
            target = f"{prefix}/{filename}"
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or target not in files:
                raise DataPortabilityError("Research batch file is missing or invalid")
            if target not in hashes:
                hashes[target] = _sha256(root_fd, target)
            if hashes[target] != digest:
                raise DataPortabilityError("Research batch checksum mismatch")
        if hashes[f"{prefix}/raw.json"] != match[1]:
            raise DataPortabilityError("Research batch identity mismatch")
        checked += 1
    return checked


def _read_manifest(root_fd):
    try:
        with _open_file(root_fd, MANIFEST_PATH) as descriptor, os.fdopen(os.dup(descriptor), "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
    except FileNotFoundError:
        return None
    if not isinstance(manifest, dict) or manifest.get("format") != "stockviewagent-data-migration" or manifest.get("version") != 1:
        raise DataPortabilityError("Unsupported migration manifest")
    return manifest


def _verify_tree(root, root_fd, *, require_manifest=False):
    files, directories = _scan_tree(root_fd)
    databases, sidecars = _sqlite_files(root_fd, files)
    payload = {path: info for path, info in files.items() if path not in sidecars}
    hashes = {}
    manifest = _read_manifest(root_fd)
    if require_manifest and manifest is None:
        raise DataPortabilityError("Migration manifest is missing")
    if manifest is not None:
        entries = manifest.get("files")
        if not isinstance(entries, list):
            raise DataPortabilityError("Invalid migration file list")
        expected = {}
        for entry in entries:
            if not isinstance(entry, dict):
                raise DataPortabilityError("Invalid migration file entry")
            relative = _relative_path(entry.get("path"))
            if relative in expected or _excluded(relative) or entry.get("storage") not in {"file", "sqlite_backup"}:
                raise DataPortabilityError("Invalid or duplicate migration file entry")
            if type(entry.get("size_bytes")) is not int or entry["size_bytes"] < 0 or not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("sha256", ""))):
                raise DataPortabilityError("Invalid migration checksum entry")
            expected[relative] = entry
        if set(expected) != set(payload):
            raise DataPortabilityError("Data files differ from the migration snapshot")
        for relative, entry in expected.items():
            if payload[relative].st_size != entry["size_bytes"]:
                raise DataPortabilityError(f"File size differs from migration snapshot: {relative}")
            hashes[relative] = _sha256(root_fd, relative)
            if hashes[relative] != entry["sha256"]:
                raise DataPortabilityError(f"File checksum differs from migration snapshot: {relative}")
            if (relative in databases) != (entry["storage"] == "sqlite_backup"):
                raise DataPortabilityError("SQLite classification differs from migration snapshot")
        declared_dirs = manifest.get("directories")
        if not isinstance(declared_dirs, list) or any(_relative_path(path) != path for path in declared_dirs) or len(set(declared_dirs)) != len(declared_dirs):
            raise DataPortabilityError("Invalid migration directory list")
        if set(declared_dirs) != set(directories):
            raise DataPortabilityError("Data directories differ from migration snapshot")
    for relative in sorted(databases):
        _quick_check(root / relative)
    research_batches = _verify_research(root_fd, payload, hashes)
    return {"ok": True, "files": len(payload), "size_bytes": sum(info.st_size for info in payload.values()),
            "sqlite_databases": len(databases), "research_batches": research_batches,
            "manifest_present": manifest is not None, "checksum_verified_files": len(hashes)}


def verify_data_directory(root: Path) -> dict:
    """Validate offline DBs/research evidence and any migration checksum receipt.

    The receipt describes migration time. Ordinary application writes afterwards
    legitimately change files; they are reported as a changed snapshot.
    """
    root = _root_path(root)
    with data_directory_lock(root, exclusive=True), _directory_fd(root) as descriptor:
        return _verify_tree(root, descriptor)


@contextmanager
def _directory_fd(path):
    descriptor = os.open(path, _DIR_FLAGS)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def _publish_no_replace(parent_fd: int, stage_name: str, destination_name: str):
    """Atomic no-clobber rename: never replace even a concurrently created empty dir."""
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        function = getattr(library, "renameatx_np", None)
        flag = 0x00000004  # RENAME_EXCL, from Darwin sys/stdio.h.
    elif sys.platform.startswith("linux"):
        function = getattr(library, "renameat2", None)
        flag = 1  # RENAME_NOREPLACE; fail closed when unsupported by the filesystem.
    else:
        function = None
    if function is None:
        raise DataPortabilityError("This platform lacks atomic no-replace directory publication")
    function.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    function.restype = ctypes.c_int
    if function(parent_fd, os.fsencode(stage_name), parent_fd, os.fsencode(destination_name), flag):
        code = ctypes.get_errno()
        if code in (errno.EEXIST, errno.ENOTEMPTY):
            raise DataPortabilityError("Destination already exists; it was left unchanged")
        raise DataPortabilityError(f"Atomic destination publication failed (errno {code})")


def _source_unchanged(source_fd, before, directories):
    after, after_dirs = _scan_tree(source_fd)
    def stable(files):
        # Read-only SQLite connections may create an empty WAL/SHM. SHM is a
        # transient lock/index file, while a nonempty WAL is durable data.
        return {path: _fingerprint(info) for path, info in files.items()
                if not path.endswith("-shm") and not (path.endswith("-wal") and info.st_size == 0)}
    if stable(before) != stable(after) or directories != after_dirs:
        raise DataPortabilityError("Source changed during migration; destination was not published")


def migrate_data_directory(source: Path, destination: Path, *, sqlite_timeout: float = 30) -> dict:
    source = _root_path(source)
    raw_destination = Path(destination).expanduser().absolute()
    if os.path.lexists(raw_destination):
        raise DataPortabilityError("Destination must not already exist")
    destination = raw_destination.resolve()
    if source == destination or source in destination.parents or destination in source.parents:
        raise DataPortabilityError("Source and destination must be separate, non-nested directories")
    if type(sqlite_timeout) not in (int, float) or not math.isfinite(sqlite_timeout) or sqlite_timeout <= 0:
        raise DataPortabilityError("SQLite timeout must be positive")
    stage = None
    stage_identity = None
    with data_directory_lock(source, exclusive=True), _directory_fd(source) as source_fd:
        files, directories = _scan_tree(source_fd)
        databases, sidecars = _sqlite_files(source_fd, files)
        _verify_research(source_fd, files, {})
        _make_private_directories(destination.parent)
        with _directory_fd(destination.parent) as parent_fd:
            if os.path.lexists(destination):
                raise DataPortabilityError("Destination must not already exist")
            stage = Path(tempfile.mkdtemp(prefix=f".{destination.name}.migration-", dir=destination.parent))
            stage_identity = stage.stat().st_ino
            try:
                os.chmod(stage, 0o700)
                for relative in directories:
                    (stage / relative).mkdir(mode=0o700, parents=True, exist_ok=True)
                    os.chmod(stage / relative, 0o700)
                (stage / "_managed").mkdir(mode=0o700, exist_ok=True)
                entries = []
                with _directory_fd(stage) as stage_fd:
                    for relative, info in sorted(files.items()):
                        if relative in sidecars:
                            continue
                        target = stage / relative
                        if relative in databases:
                            # Validate no component changed to a symlink before
                            # SQLite opens by pathname; re-scan before publication.
                            with _open_file(source_fd, relative) as descriptor:
                                if _fingerprint(os.fstat(descriptor)) != _fingerprint(info):
                                    raise DataPortabilityError("Source changed during migration")
                            _backup_sqlite(source / relative, target, timeout=sqlite_timeout)
                            digest = _sha256(stage_fd, relative)
                            storage = "sqlite_backup"
                        else:
                            digest = _copy_regular(source_fd, stage_fd, relative, info)
                            os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns), follow_symlinks=False)
                            storage = "file"
                        entries.append({"path": relative, "size_bytes": target.stat().st_size, "sha256": digest, "storage": storage})
                    _source_unchanged(source_fd, files, directories)
                    _, target_directories = _scan_tree(stage_fd)
                    manifest = {"version": 1, "format": "stockviewagent-data-migration",
                                "created_at": datetime.now(timezone.utc).isoformat(),
                                "files": entries, "directories": target_directories,
                                "excluded": ["runtime/pids/", "pids/", "*.lock", "SQLite WAL/SHM/journal (merged into backup)", MANIFEST_PATH]}
                    encoded = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
                    with _open_file(stage_fd, MANIFEST_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)) as descriptor:
                        with os.fdopen(os.dup(descriptor), "wb") as handle:
                            handle.write(encoded)
                            handle.flush()
                            os.fsync(handle.fileno())
                    verified = _verify_tree(stage, stage_fd, require_manifest=True)
                    _source_unchanged(source_fd, files, directories)
                    for relative in reversed(target_directories):
                        with _directory_fd(stage / relative) as descriptor:
                            os.fsync(descriptor)
                    os.fsync(stage_fd)
                _publish_no_replace(parent_fd, stage.name, destination.name)
                stage = None
                os.fsync(parent_fd)
                return {**verified, "source_preserved": True, "destination": str(destination), "manifest": MANIFEST_PATH}
            finally:
                if stage is not None and stage.exists() and not stage.is_symlink() and stage.stat().st_ino == stage_identity:
                    shutil.rmtree(stage)
