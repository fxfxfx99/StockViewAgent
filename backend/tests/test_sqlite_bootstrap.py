"""Unwarmed concurrent initialization, migration, and ordinary connection cost."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from app.storage import data_collection_store as store, sqlite_bootstrap as bootstrap


def test_simultaneous_first_connections_serialize_wal_switch_before_schema(monkeypatch, tmp_path):
    workers = 12
    all_probes_open = threading.Barrier(workers)
    all_waiting_for_init = threading.Event()
    wal_entered = threading.Event()
    release_wal = threading.Event()
    thread_local = threading.local()
    count_lock = threading.Lock()
    counts = {"lock_attempts": 0, "wal_calls": 0, "schema_calls": 0}
    connect, flock = sqlite3.connect, bootstrap.fcntl.flock

    class TracedConnection(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql.strip().upper() == "PRAGMA JOURNAL_MODE=WAL":
                with count_lock:
                    counts["wal_calls"] += 1
                wal_entered.set()
                assert release_wal.wait(10), "test must release the initializing thread"
            return super().execute(sql, *args)

        def executescript(self, sql):
            if "CREATE TABLE" in sql:
                with count_lock:
                    counts["schema_calls"] += 1
            return super().executescript(sql)

    def traced_connect(*args, **kwargs):
        conn = connect(*args, factory=TracedConnection, **kwargs)
        if threading.current_thread().name.startswith("first-db") and not getattr(thread_local, "opened", False):
            thread_local.opened = True
            all_probes_open.wait(timeout=10)
        return conn

    def traced_flock(handle, operation):
        if operation == bootstrap.fcntl.LOCK_EX:
            with count_lock:
                counts["lock_attempts"] += 1
                if counts["lock_attempts"] == workers:
                    all_waiting_for_init.set()
        return flock(handle, operation)

    monkeypatch.setattr(bootstrap.sqlite3, "connect", traced_connect)
    monkeypatch.setattr(bootstrap.fcntl, "flock", traced_flock)
    assert not (tmp_path / "data_center.sqlite3").exists()
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="first-db") as executor:
        jobs = [executor.submit(store.enqueue, "first_source", {"scheduled": False}) for _ in range(workers)]
        try:
            assert wal_entered.wait(10)
            assert all_waiting_for_init.wait(10)
            # Hold the real WAL switch while every other thread tries to enter
            # initialization: this establishes serialization deterministically.
            assert counts["wal_calls"] == 1
        finally:
            release_wal.set()
        outcomes = [job.result(timeout=10) for job in jobs]
    assert len({row["id"] for row in outcomes}) == 1
    assert counts["wal_calls"] == counts["schema_calls"] == 1


def test_independent_processes_can_create_and_enqueue_without_preheating(tmp_path):
    gate = tmp_path / "process-gate"
    script = '''
import json, sys, time
from pathlib import Path
from app.storage import data_collection_store as store
gate, ready = Path(sys.argv[1]), Path(sys.argv[2])
ready.write_text("ready")
deadline = time.monotonic() + 20
while not gate.exists():
    if time.monotonic() > deadline:
        raise RuntimeError("test process gate timed out")
    time.sleep(0.005)
print(json.dumps(store.enqueue("parallel_process_source", {"scheduled": False})))
'''
    backend = Path(__file__).resolve().parents[1]
    env = {**os.environ, "STOCKVIEW_DATA_DIR": str(tmp_path), "PYTHONPATH": str(backend)}
    processes = [subprocess.Popen([sys.executable, "-c", script, str(gate), str(tmp_path / f"ready-{index}")],
                                 env=env, cwd=backend, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for index in range(6)]
    try:
        deadline = time.monotonic() + 20
        while len(list(tmp_path.glob("ready-*"))) < len(processes):
            assert time.monotonic() < deadline, "child imports did not finish"
            assert all(process.poll() is None for process in processes), "child exited before the gate"
            time.sleep(0.005)
        assert not (tmp_path / "data_center.sqlite3").exists()
        gate.write_text("go")
        results = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            assert process.returncode == 0, stderr
            results.append(json.loads(stdout))
        assert len({result["id"] for result in results}) == 1
        with store.connection() as conn:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM dc_jobs").fetchone()[0] == 1
    finally:
        gate.touch()
        for process in processes:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=10)


def test_existing_connections_only_check_version_and_do_not_rebuild_schema(monkeypatch):
    store.set_state("retained", 42)
    statements = []
    connect = sqlite3.connect
    def traced_connect(*args, **kwargs):
        conn = connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn
    monkeypatch.setattr(bootstrap.sqlite3, "connect", traced_connect)
    assert store.get_state("retained") == 42
    store.usage()
    store.list_jobs()
    assert sum(statement.upper() == "PRAGMA USER_VERSION" for statement in statements) == 3
    assert not any("CREATE " in statement.upper() or "JOURNAL_MODE" in statement.upper() for statement in statements)


def test_existing_v0_schema_is_migrated_without_losing_data(tmp_path):
    path = tmp_path / "data_center.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.executescript(store._SCHEMA_SQL)
        conn.execute("INSERT INTO dc_settings VALUES('legacy','{\"kept\":true}')")
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert store.get_state("legacy") == {"kept": True}
    with store.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_failed_schema_initialization_does_not_mark_ready_and_can_retry(tmp_path):
    path = tmp_path / "interrupted.sqlite3"
    with pytest.raises(sqlite3.OperationalError):
        with bootstrap.connection(path, "CREATE TABLE complete(id INTEGER); INVALID SQL;"):
            pass
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='complete'").fetchone() is None
    with bootstrap.connection(path, "CREATE TABLE complete(id INTEGER);") as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1


def test_replaced_database_is_reinitialized_without_process_global_cache(tmp_path):
    store.set_state("retained", 42)
    path = tmp_path / "data_center.sqlite3"
    path.rename(tmp_path / "old.sqlite3")
    assert store.get_state("retained") is None
    with store.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    with sqlite3.connect(tmp_path / "old.sqlite3") as conn:
        assert json.loads(conn.execute("SELECT payload FROM dc_settings WHERE key='retained'").fetchone()[0]) == 42


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory", "fifo"])
def test_initialization_lock_rejects_links_and_nonregular_files(tmp_path, kind):
    path = tmp_path / "data_center.sqlite3.init.lock"
    target = tmp_path / "target"
    target.write_text("untouched")
    if kind == "symlink":
        path.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, path)
    elif kind == "directory":
        path.mkdir()
    else:
        os.mkfifo(path)
    with pytest.raises(ValueError, match="普通文件"):
        store.get_state("key")
    assert target.read_text() == "untouched"
