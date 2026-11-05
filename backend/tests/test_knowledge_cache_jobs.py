"""Maintenance queue persistence, leases and resumable bounded progress."""
from concurrent.futures import ThreadPoolExecutor

from app.storage import knowledge_cache_jobs as jobs


def test_enqueue_deduplicates_same_active_scope_but_not_other_symbols():
    first = jobs.enqueue("stocks", "600519.SS", ["600519.SS"], actor_id=7)
    duplicate = jobs.enqueue("stocks", "600519.SS", ["600519.SS"])
    other = jobs.enqueue("stocks", "000001.SZ", ["000001.SZ"])
    assert not first["deduplicated"] and duplicate["deduplicated"]
    assert duplicate["job"]["id"] == first["job"]["id"]
    assert other["job"]["id"] != first["job"]["id"]
    assert first["job"]["actor_id"] == 7
    assert "owner" not in jobs.status()["active_job"]
    assert "lease_until" not in jobs.status()["active_job"]


def test_concurrent_enqueues_and_claims_admit_one_owner():
    with ThreadPoolExecutor(max_workers=6) as pool:
        queued = list(pool.map(lambda _: jobs.enqueue("relevance", None, ["600519.SS", "000001.SZ"]), range(6)))
    assert len({item["job"]["id"] for item in queued}) == 1
    assert sum(not item["deduplicated"] for item in queued) == 1
    with ThreadPoolExecutor(max_workers=6) as pool:
        claimed = list(pool.map(lambda _: jobs.claim(), range(6)))
    assert sum(item is not None for item in claimed) == 1


def test_expired_lease_recovers_pending_work_and_rejects_previous_owner(monkeypatch):
    monkeypatch.setattr(jobs.time, "time", lambda: 1000.0)
    result = jobs.enqueue("stocks", None, ["600519.SS", "000001.SZ", "AAPL"])
    first = jobs.claim(now=1000)
    assert jobs.complete_item(first, 0)
    assert jobs.claim(now=1599) is None
    second = jobs.claim(now=1601)
    assert second["id"] == result["job"]["id"] and second["owner"] != first["owner"]
    assert [item["ordinal"] for item in jobs.pending(second)] == [1, 2]
    assert not jobs.complete_item(first, 1)
    jobs.release(first)
    assert jobs.status()["active_job"]["status"] == "running"
    assert jobs.complete_item(second, 1)
    assert jobs.complete_item(second, 2)
    jobs.release(second)
    latest = jobs.status()["recent_jobs"][0]
    assert (latest["status"], latest["completed"], latest["progress_percent"]) == ("completed", 3, 100)
    assert latest["started_at"] == "1970-01-01T00:16:40+00:00"


def test_batched_pending_pages_resume_without_counting_items_twice():
    result = jobs.enqueue("relevance", None, [f"item-{i}" for i in range(23)])
    first = jobs.claim()
    page = jobs.pending(first, limit=10)
    assert [item["ordinal"] for item in page] == list(range(10))
    for item in page:
        assert jobs.complete_item(first, item["ordinal"])
        assert not jobs.complete_item(first, item["ordinal"])
    jobs.release(first)
    state = jobs.status()["active_job"]
    assert state["id"] == result["job"]["id"]
    assert (state["status"], state["completed"], state["total"]) == ("queued", 10, 23)
    second = jobs.claim()
    assert [item["ordinal"] for item in jobs.pending(second, limit=10)] == list(range(10, 20))
    for item in jobs.pending(second, limit=20):
        jobs.complete_item(second, item["ordinal"], error_code="source_failed" if item["ordinal"] == 22 else None)
    jobs.release(second)
    assert jobs.status()["active_job"] is None
    latest = jobs.status()["recent_jobs"][0]
    assert (latest["status"], latest["completed"], latest["failed"]) == ("partial", 23, 1)
    assert latest["progress_percent"] == 100


def test_failed_jobs_can_be_explicitly_requeued_without_erasing_history():
    old = jobs.enqueue("stocks", "600519.SS", ["600519.SS"])
    job = jobs.claim()
    jobs.complete_item(job, 0, error_code="source_unavailable")
    jobs.release(job)
    newer = jobs.enqueue("stocks", "600519.SS", ["600519.SS"])
    assert not newer["deduplicated"] and newer["job"]["id"] != old["job"]["id"]
    assert {row["status"] for row in jobs.status()["recent_jobs"]} == {"queued", "failed"}


def test_periodic_enqueue_is_daily_and_survives_separate_connections(monkeypatch):
    monkeypatch.setattr(jobs.time, "time", lambda: 1000.0)
    assert jobs.enqueue("prompts", None, ["__prompts__"], periodic=True)
    claimed = jobs.claim()
    jobs.complete_item(claimed, 0)
    jobs.release(claimed)
    assert jobs.enqueue("prompts", None, ["__prompts__"], periodic=True) is None
    assert jobs.status()["next_sync_at"] == "1970-01-02T00:16:40+00:00"
    monkeypatch.setattr(jobs.time, "time", lambda: 87401.0)
    assert jobs.enqueue("prompts", None, ["__prompts__"], periodic=True)


def test_renew_only_extends_a_live_owner_and_expired_jobs_must_be_reclaimed(monkeypatch):
    monkeypatch.setattr(jobs.time, "time", lambda: 1000.0)
    jobs.enqueue("stocks", None, ["600519.SS"])
    first = jobs.claim()
    assert jobs.renew(first, now=1500)
    assert jobs.claim(now=1601) is None  # Renewal extended the lease to 2100.
    assert not jobs.renew(first, now=2100)
    monkeypatch.setattr(jobs.time, "time", lambda: 2100.0)
    assert not jobs.complete_item(first, 0)
    second = jobs.claim()
    assert second["owner"] != first["owner"]
    assert not jobs.renew(first)
    assert jobs.renew(second)
    assert jobs.complete_item(second, 0)
    jobs.release(second)
    assert not jobs.renew(second)


def test_get_job_can_read_a_target_older_than_recent_window(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(jobs.time, "time", lambda: clock[0])
    old = jobs.enqueue("stocks", "old", ["old"])["job"]
    for index in range(21):
        clock[0] += 1
        jobs.enqueue("stocks", f"new-{index}", [f"new-{index}"])
    assert old["id"] not in {job["id"] for job in jobs.status()["recent_jobs"]}
    loaded = jobs.get_job(old["id"])
    assert loaded == old
    assert "owner" not in loaded and "lease_until" not in loaded
    assert jobs.get_job("unknown") is None
