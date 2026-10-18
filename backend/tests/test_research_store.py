import concurrent.futures
import sqlite3

import pytest

from app.storage.research_store import BudgetExceeded, ImmutableConflict, LeaseLost, ResearchStore


def test_immutable_entities_and_user_isolation(tmp_path):
    s = ResearchStore(tmp_path)
    first = s.put_entity(1, "strategy", {"value": 1}, "same-id")
    assert s.put_entity(1, "strategy", {"value": 1}, "same-id") == first
    with pytest.raises(ImmutableConflict):
        s.put_entity(1, "strategy", {"value": 2}, "same-id")
    assert s.get_entity(2, "strategy", "same-id") is None
    assert s.list_entities(2, "strategy") == []
    with s.connection(write=True) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM entities")
    assert ResearchStore(tmp_path).get_entity(1, "strategy", "same-id") == first


def test_compare_and_set_is_atomic(tmp_path):
    s = ResearchStore(tmp_path)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(lambda i: s.compare_and_set_state(1, "frozen", None, i), range(16)))
    assert sum(outcomes) == 1
    assert s.get_state(2, "frozen") is None


def test_job_idempotency_expiration_fencing_and_retry(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("app.storage.research_store.time.time", lambda: now[0])
    s = ResearchStore(tmp_path)
    queued = s.queue_job(1, "backfill", {"symbol": "x"}, "daily-x", max_attempts=2)
    assert s.queue_job(1, "backfill", {"symbol": "x"}, "daily-x")["id"] == queued["id"]
    with pytest.raises(ImmutableConflict):
        s.queue_job(1, "backfill", {"symbol": "y"}, "daily-x")
    first = s.claim_job("worker-a", lease_seconds=10)
    assert first["attempts"] == 1 and s.claim_job("worker-b") is None
    assert s.get_job(2, queued["id"]) is None
    now[0] += 11
    second = s.claim_job("worker-b", lease_seconds=10)
    assert second["attempts"] == 2
    with pytest.raises(LeaseLost):
        s.complete_job(1, first["id"], first["lease_token"], {})
    s.renew_job(1, second["id"], second["lease_token"], lease_seconds=600)
    now[0] += 20
    done = s.complete_job(1, second["id"], second["lease_token"], {"rows": 3})
    assert done["status"] == "completed"
    assert ResearchStore(tmp_path).get_job(1, done["id"])["result"] == {"rows": 3}


def test_failed_jobs_retry_and_pause(tmp_path):
    s = ResearchStore(tmp_path)
    s.queue_job(1, "fetch", {}, "key", max_attempts=2)
    s.update_config(1, {"paused": True})
    assert s.claim_job("worker") is None
    s.update_config(1, {"paused": False})
    job = s.claim_job("worker")
    assert s.fail_job(1, job["id"], job["lease_token"], "offline", retry_delay=0)["status"] == "retry"
    job = s.claim_job("worker")
    assert s.fail_job(1, job["id"], job["lease_token"], "offline")["status"] == "failed"
    assert s.claim_job("worker") is None
    with pytest.raises(ValueError):
        s.update_config(1, {"mystery": 1})


def test_budget_atomic_reservation_limits_and_settlement(tmp_path):
    s = ResearchStore(tmp_path)
    s.update_config(1, {"daily_calls": 3})
    def reserve(i):
        try:
            return s.reserve_budget(1, str(i), calls=1, tokens=1000)
        except BudgetExceeded:
            return None
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        reservations = [r for r in pool.map(reserve, range(12)) if r]
    assert len(reservations) == 3
    first = reservations[0]
    assert s.reserve_budget(1, first["reservation_key"], calls=1, tokens=1000)["id"] == first["id"]
    s.settle_budget(1, first["id"], tokens=100)
    assert s.budget_status(1)["used"] == {"calls": 3, "tokens": 2100, "candidates": 0}
    assert not s.release_budget(1, first["id"])
    assert s.release_budget(1, reservations[1]["id"])
    assert s.budget_status(1)["remaining"]["calls"] == 1
    assert s.budget_status(2)["used"]["calls"] == 0


def test_weekly_budget_rollover_and_persistent_reserved_usage(tmp_path):
    s = ResearchStore(tmp_path)
    # 2025-01-06 noon CST (Monday)
    monday = 1736136000
    s.reserve_budget(1, "week", candidates=60, now=monday)
    with pytest.raises(BudgetExceeded):
        ResearchStore(tmp_path).reserve_budget(1, "next", candidates=1, now=monday + 86400)
    assert s.reserve_budget(1, "next-week", candidates=1, now=monday + 7 * 86400)["candidates"] == 1
    assert s.budget_status(1, now=monday)["used"]["candidates"] == 60


def test_atomic_claim_and_budget_audit(tmp_path):
    s = ResearchStore(tmp_path)
    job = s.queue_job(1, "fetch", {}, "once")
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda n: s.claim_job(f"worker-{n}"), range(8)))
    assert len([c for c in claims if c]) == 1
    assert next(c for c in claims if c)["id"] == job["id"]
    r = s.reserve_budget(1, "llm", calls=1, tokens=100)
    s.settle_budget(1, r["id"], tokens=50)
    events = s.list_entities(1, "budget_event")
    assert {e["payload"]["action"] for e in events} == {"reserved", "settled"}
    assert next(e for e in events if e["payload"]["action"] == "reserved")["payload"]["tokens"] == 100
    assert next(e for e in events if e["payload"]["action"] == "settled")["payload"]["tokens"] == 50


def _fresh_store_process(path, barrier, output, index):
    import traceback
    try:
        barrier.wait(timeout=10)
        store = ResearchStore(path)
        store.put_entity(1, "startup_probe", {"worker": index}, str(index))
        output.put({"worker": index, "error": None})
    except Exception:
        barrier.abort()
        output.put({"worker": index, "error": traceback.format_exc()})


def test_fresh_database_initialization_is_safe_across_processes(tmp_path):
    import multiprocessing
    context = multiprocessing.get_context("spawn")
    barrier, output = context.Barrier(4), context.Queue()
    workers = [context.Process(target=_fresh_store_process, args=(str(tmp_path), barrier, output, i)) for i in range(4)]
    for worker in workers:
        worker.start()
    try:
        outcomes = [output.get(timeout=15) for _ in workers]
    finally:
        for worker in workers:
            worker.join(timeout=5)
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=5)
        output.close()
    assert all(worker.exitcode == 0 for worker in workers)
    assert not [row for row in outcomes if row["error"]], outcomes
    store = ResearchStore(tmp_path)
    assert len(store.list_entities(1, "startup_probe")) == 4
    with store.connection() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
