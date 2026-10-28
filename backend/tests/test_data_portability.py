"""Exercise real filesystem publication, SQLite WAL backup and process locks."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import sqlite3
import stat
import subprocess
import sys

import pytest

from app.storage import data_portability as portability


@pytest.fixture
def roots(tmp_path):
    source = tmp_path / "original"
    source.mkdir()
    return source, tmp_path / "moved library"


def write(root, relative, body="preserved data"):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def no_staging(destination):
    assert list(destination.parent.glob(f".{destination.name}.migration-*")) == []


def test_migration_private_relative_manifest_and_exclusions(roots):
    source, destination = roots
    write(source, "llm_runtime.json", '{"api_key":"private-fixture"}')
    write(source, "issuer_uploads/company.txt", "企业资料")
    write(source, "runtime/logs/api.log", "saved log")
    write(source, "runtime/pids/supervisor.json", '{"pid":123}')
    write(source, "pids/legacy.pid")
    write(source, "nested/obsolete.lock")
    (source / "empty").mkdir()
    result = portability.migrate_data_directory(source, destination)

    assert result["source_preserved"] and result["files"] == 3
    assert (source / "llm_runtime.json").read_bytes() == (destination / "llm_runtime.json").read_bytes()
    assert not (destination / "runtime/pids").exists()
    assert not (destination / "pids").exists()
    assert not (destination / "nested/obsolete.lock").exists()
    assert (destination / "empty").is_dir()
    manifest_text = (destination / portability.MANIFEST_PATH).read_text()
    manifest = json.loads(manifest_text)
    assert str(source) not in manifest_text and str(destination) not in manifest_text
    assert "private-fixture" not in manifest_text
    for item in manifest["files"]:
        assert not Path(item["path"]).is_absolute()
        assert hashlib.sha256((destination / item["path"]).read_bytes()).hexdigest() == item["sha256"]
    for path in [destination, *destination.rglob("*")]:
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)
    assert portability.verify_data_directory(destination)["checksum_verified_files"] == 3
    no_staging(destination)


@pytest.mark.parametrize("database_name", ["research.sqlite3", "tracking.sqlite3"])
def test_sqlite_backup_includes_committed_wal_without_changing_source(roots, database_name):
    source, destination = roots
    database = source / database_name
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("CREATE TABLE evidence (value TEXT)")
        connection.execute("INSERT INTO evidence VALUES ('committed-only-in-wal')")
        connection.commit()
        wal = Path(str(database) + "-wal")
        before, before_wal = database.read_bytes(), wal.read_bytes()
        result = portability.migrate_data_directory(source, destination)
        assert result["sqlite_databases"] == 1
        assert database.read_bytes() == before and wal.read_bytes() == before_wal
        assert not Path(str(destination / database.name) + "-wal").exists()
        with sqlite3.connect(destination / database.name) as copied:
            assert copied.execute("SELECT value FROM evidence").fetchall() == [("committed-only-in-wal",)]
            assert copied.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        assert portability.verify_data_directory(destination)["ok"]
    finally:
        connection.close()


def test_research_snapshot_remains_readable_after_verified_copy(roots, monkeypatch):
    from app.config import settings
    from app.services.research_data import ResearchData
    source, destination = roots
    active = [source]
    monkeypatch.setattr(type(settings), "data_dir", property(lambda self: active[0]))
    data = ResearchData()
    snapshot = data.import_snapshot(11, {"600000.SS": {"candles": [
        {"t": 1736121600, "o": 10, "h": 12, "l": 9, "c": 11, "v": 10000}],
        "kline_source": "fixture", "price_basis": "raw", "volume_unit": "shares"}}, metadata={})
    expected = data.read_snapshot(11, snapshot["id"])
    result = portability.migrate_data_directory(source, destination)
    assert result["research_batches"] == 1
    assert portability.verify_data_directory(destination)["ok"]
    active[0] = destination
    reopened = ResearchData()
    assert reopened.read_snapshot(11, snapshot["id"]) == expected


@pytest.mark.parametrize("target", ["same", "child", "parent", "existing", "symlink"])
def test_reject_conflicting_destinations_without_touching_source(roots, target):
    source, destination = roots
    original = write(source, "saved.txt")
    if target == "same":
        destination = source
    elif target == "child":
        destination = source / "child"
    elif target == "parent":
        destination = source.parent
    elif target == "existing":
        destination.mkdir()
    else:
        destination.symlink_to(source, target_is_directory=True)
    with pytest.raises(portability.DataPortabilityError):
        portability.migrate_data_directory(source, destination)
    assert original.read_text() == "preserved data"
    no_staging(destination)


@pytest.mark.parametrize("kind", ["file_symlink", "directory_symlink", "fifo", "root_symlink"])
def test_reject_symlinks_and_special_files(roots, kind):
    source, destination = roots
    external = write(source.parent, "external/secret.txt", "must not copy")
    if kind == "file_symlink":
        (source / "linked").symlink_to(external)
    elif kind == "directory_symlink":
        (source / "linked").symlink_to(external.parent, target_is_directory=True)
    elif kind == "fifo":
        os.mkfifo(source / "pipe")
    else:
        link = source.parent / "linked-source"
        link.symlink_to(source, target_is_directory=True)
        source = link
    with pytest.raises(portability.DataPortabilityError):
        portability.migrate_data_directory(source, destination)
    assert not destination.exists() and external.read_text() == "must not copy"
    no_staging(destination)


def test_copy_failure_cleans_private_stage_preserves_original(roots, monkeypatch):
    source, destination = roots
    saved = write(source, "saved.txt")
    original = portability._copy_regular
    def fail_after_copy(*args):
        original(*args)
        raise OSError("simulated disk failure")
    monkeypatch.setattr(portability, "_copy_regular", fail_after_copy)
    with pytest.raises(OSError, match="simulated"):
        portability.migrate_data_directory(source, destination)
    assert not destination.exists() and saved.read_text() == "preserved data"
    no_staging(destination)


def test_source_change_during_copy_prevents_publication(roots, monkeypatch):
    source, destination = roots
    saved = write(source, "saved.txt")
    original = portability._copy_regular
    def concurrent_edit(*args):
        digest = original(*args)
        saved.write_text("new outside writer")
        return digest
    monkeypatch.setattr(portability, "_copy_regular", concurrent_edit)
    with pytest.raises(portability.DataPortabilityError, match="Source changed"):
        portability.migrate_data_directory(source, destination)
    assert not destination.exists() and saved.read_text() == "new outside writer"
    no_staging(destination)


def test_atomic_publication_does_not_replace_raced_empty_directory(roots, monkeypatch):
    source, destination = roots
    write(source, "saved.txt")
    original = portability._publish_no_replace
    raced_inode = []
    def race(*args):
        destination.mkdir()
        raced_inode.append(destination.stat().st_ino)
        return original(*args)
    monkeypatch.setattr(portability, "_publish_no_replace", race)
    with pytest.raises(portability.DataPortabilityError, match="Destination already exists"):
        portability.migrate_data_directory(source, destination)
    assert destination.stat().st_ino == raced_inode[0] and list(destination.iterdir()) == []
    assert (source / "saved.txt").exists()
    no_staging(destination)


def test_other_process_shared_lock_blocks_migration_and_releases(roots):
    source, destination = roots
    write(source, "saved.txt")
    script = """import sys
from pathlib import Path
from app.storage.data_portability import data_directory_lock
with data_directory_lock(Path(sys.argv[1])):
    print('ready', flush=True)
    sys.stdin.readline()
"""
    process = subprocess.Popen([sys.executable, "-c", script, str(source)], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               cwd=Path(__file__).resolve().parents[1])
    try:
        assert process.stdout.readline().strip() == "ready"
        with portability.data_directory_lock(source):
            pass  # API and worker can coexist.
        with pytest.raises(portability.DataDirectoryInUse):
            portability.migrate_data_directory(source, destination)
        with pytest.raises(portability.DataDirectoryInUse):
            portability.verify_data_directory(source)
        assert not destination.exists()
        process.communicate("stop\n", timeout=5)
        assert process.returncode == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
    assert portability.migrate_data_directory(source, destination)["ok"]


def test_exclusive_lock_blocks_new_shared_holder_and_releases_on_error(roots):
    source, _ = roots
    with pytest.raises(RuntimeError, match="fixture"):
        with portability.data_directory_lock(source, exclusive=True):
            with pytest.raises(portability.DataDirectoryInUse):
                with portability.data_directory_lock(source):
                    pytest.fail("shared lock incorrectly acquired")
            raise RuntimeError("fixture")
    with portability.data_directory_lock(source, exclusive=True):
        pass


@pytest.mark.parametrize("change", ["same_size_edit", "extra_file", "missing_file", "invalid_manifest_path"])
def test_verify_detects_snapshot_changes_and_unsafe_manifest(roots, change):
    source, destination = roots
    write(source, "saved.txt", "original")
    portability.migrate_data_directory(source, destination)
    if change == "same_size_edit":
        (destination / "saved.txt").write_text("tampered")
    elif change == "extra_file":
        write(destination, "unexpected.txt")
    elif change == "missing_file":
        (destination / "saved.txt").unlink()
    else:
        path = destination / portability.MANIFEST_PATH
        manifest = json.loads(path.read_text())
        manifest["files"][0]["path"] = "../../outside-secret"
        path.write_text(json.dumps(manifest))
    with pytest.raises(portability.DataPortabilityError):
        portability.verify_data_directory(destination)


def test_corrupt_sqlite_refused_before_publication(roots):
    source, destination = roots
    write(source, "broken.db", "not a database")
    with pytest.raises((sqlite3.DatabaseError, portability.DataPortabilityError)):
        portability.migrate_data_directory(source, destination)
    assert not destination.exists() and (source / "broken.db").read_text() == "not a database"
    no_staging(destination)


def test_no_receipt_does_not_claim_all_files_checksum_verified(roots):
    source, _ = roots
    write(source, "saved.txt")
    result = portability.verify_data_directory(source)
    assert result["ok"] and not result["manifest_present"] and result["checksum_verified_files"] == 0


def test_normal_directory_named_lock_is_preserved_and_new_ancestors_private(roots):
    source, destination = roots
    write(source, "documents.lock/keep.txt")
    destination = destination.parent / "new parent" / "nested" / destination.name
    portability.migrate_data_directory(source, destination)
    assert (destination / "documents.lock/keep.txt").read_text() == "preserved data"
    assert stat.S_IMODE(destination.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(destination.parent.parent.stat().st_mode) == 0o700


def test_sqlite_magic_is_detected_without_database_extension(roots):
    source, destination = roots
    with sqlite3.connect(source / "unusual.data") as database:
        database.execute("CREATE TABLE evidence (value INTEGER)")
        database.execute("INSERT INTO evidence VALUES (7)")
    assert portability.migrate_data_directory(source, destination)["sqlite_databases"] == 1
    with sqlite3.connect(destination / "unusual.data") as database:
        assert database.execute("SELECT value FROM evidence").fetchone() == (7,)


def test_orphan_wal_is_not_silently_discarded(roots):
    source, destination = roots
    write(source, "lost.db-wal")
    with pytest.raises(portability.DataPortabilityError, match="sidecar has no database"):
        portability.migrate_data_directory(source, destination)
    assert not destination.exists()


def test_research_file_corruption_rejected_before_destination_is_published(roots):
    source, destination = roots
    raw = b'{"fixture":true}'
    batch = hashlib.sha256(raw).hexdigest()
    prefix = f"research/batches/{batch}"
    write(source, prefix + "/raw.json", raw.decode())
    write(source, prefix + "/bars.parquet", "original bytes")
    write(source, prefix + "/manifest.json", json.dumps({"batch_id": batch, "files": {
        "raw.json": batch, "bars.parquet": hashlib.sha256(b"original bytes").hexdigest()}}))
    write(source, prefix + "/bars.parquet", "modified bytes")
    with pytest.raises(portability.DataPortabilityError, match="checksum mismatch"):
        portability.migrate_data_directory(source, destination)
    assert not destination.exists()


def test_lock_symlink_does_not_touch_external_file(roots):
    source, _ = roots
    external = write(source.parent, "external-lock", "external contents")
    before = external.stat()
    (source / portability.LOCK_NAME).symlink_to(external)
    with pytest.raises(OSError):
        with portability.data_directory_lock(source):
            pytest.fail("followed unsafe lock link")
    assert external.read_text() == "external contents" and external.stat().st_mode == before.st_mode


def test_cli_status_index_migrate_and_verify_from_outside_repo(roots):
    source, destination = roots
    write(source, "llm_runtime.json", '{"api_key":"fixture-not-for-output"}')
    script = Path(__file__).resolve().parents[2] / "scripts/storage.py"
    environ = dict(os.environ, STOCKVIEW_DATA_DIR=str(source), PYTHONDONTWRITEBYTECODE="1")
    def run(*args, root=source):
        return subprocess.run([sys.executable, str(script), *args], cwd=source.parent,
                              env={**environ, "STOCKVIEW_DATA_DIR": str(root)}, capture_output=True, text=True, timeout=30)
    status = run("status")
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout)["data_root"] == str(source)
    assert "fixture-not-for-output" not in status.stdout
    indexed = run("index")
    assert indexed.returncode == 0, indexed.stderr
    assert (source / "_managed/catalog.json").exists()
    moved = run("migrate", "--to", str(destination))
    assert moved.returncode == 0, moved.stderr
    result = json.loads(moved.stdout)
    assert shlex.split(result["next_environment"]["shell_command"]) == ["export", "STOCKVIEW_DATA_DIR=" + str(destination)]
    assert "fixture-not-for-output" not in moved.stdout
    verified = run("verify", root=destination)
    assert verified.returncode == 0, verified.stderr
    assert json.loads(verified.stdout)["ok"]
    duplicate = run("migrate", "--to", str(destination))
    assert duplicate.returncode == 1 and json.loads(duplicate.stderr)["ok"] is False


def test_migrate_and_verify_cli_require_only_standard_library(roots):
    source, destination = roots
    write(source, "saved.txt")
    script = Path(__file__).resolve().parents[2] / "scripts/storage.py"
    environment = dict(os.environ, STOCKVIEW_DATA_DIR=str(source), PYTHONDONTWRITEBYTECODE="1")
    # -S disables site-packages. Offline recovery must not depend on a working
    # application environment, API credentials, a network request or bootstrap.
    moved = subprocess.run([sys.executable, "-S", str(script), "migrate", "--to", str(destination)],
                           env=environment, capture_output=True, text=True, timeout=15)
    assert moved.returncode == 0, moved.stderr
    verified = subprocess.run([sys.executable, "-S", str(script), "verify"],
                              env={**environment, "STOCKVIEW_DATA_DIR": str(destination)},
                              capture_output=True, text=True, timeout=15)
    assert verified.returncode == 0, verified.stderr
    assert json.loads(verified.stdout)["ok"]
