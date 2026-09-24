import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json

import pytest

from app.config import settings
from app.security.user_context import current_user_id
from app.services import xueqiu_comments_scheduler as scheduler
from app.services import xueqiu_comments_service as service
from app.storage import user_credentials_store, users_store
from app.storage import xueqiu_comments_store as store

SYMBOL = "600519.SS"


@pytest.fixture(autouse=True)
def isolated_comments(monkeypatch):
    users_store.init_db()
    for uid in (1, 2):
        user_credentials_store.merge_user_credentials(uid, {"llm": {"api_key": f"key-{uid}"}})
    monkeypatch.setattr(service.xueqiu_http, "effective_xueqiu_cookies", lambda: "fake-cookie")
    monkeypatch.setattr(settings, "enable_scheduler", True)
    monkeypatch.setattr(type(settings), "xueqiu_comments_auto_refresh_enabled", True, raising=False)

    def no_network(*args, **kwargs):
        raise AssertionError("test attempted an external call")

    monkeypatch.setattr(service, "fetch_comments", no_network)
    monkeypatch.setattr(service, "select_comments", no_network)


def watchlist(uid, symbols):
    directory = settings.data_dir / "watchlists"
    directory.mkdir(exist_ok=True)
    (directory / f"{uid}.json").write_text(json.dumps({"symbols": symbols}), encoding="utf-8")


def enqueue(uid=1, symbol=SYMBOL, now=1000, source="manual", mode="latest", target_date=None):
    return store.enqueue(uid, symbol, mode=mode, target_date=target_date, source=source, now=now)


def finish_success(job, owner="worker", now=1002, text="selected"):
    return store.finish(job, owner, {
        "status": "ready", "items": [{"id": "1", "text": text}], "raw_count": 8,
        "analyzed_count": 7, "selected_count": 1, "rejected_count": 6, "fetched_at": 1001,
        "warnings": [],
    }, now=now)


def test_get_only_reads_local_and_missing_key_is_blocked(monkeypatch):
    assert service.get_snapshot(1, SYMBOL)["status"] == "not_started"
    user_credentials_store.merge_user_credentials(2, {"llm": {}})
    before = service.get_snapshot(2, SYMBOL)
    assert before["status"] == "blocked"
    assert before["prerequisites"] == {"xueqiu_configured": True, "xueqiu_auto_session": True, "llm_configured": False}
    after = service.enqueue_refresh(2, SYMBOL)
    assert after["status"] == "blocked"
    assert after["job"] is None
    assert store.claim("worker") is None
    monkeypatch.setattr(service.xueqiu_http, "effective_xueqiu_cookies", lambda: "")
    assert service.get_snapshot(1, SYMBOL)["prerequisites"]["xueqiu_configured"] is False


def test_without_manual_cookie_status_and_enqueue_do_not_acquire_session(monkeypatch):
    monkeypatch.setattr(service.xueqiu_http, "effective_xueqiu_cookies", lambda: "")

    def fail(*args, **kwargs):
        pytest.fail("Cookie acquisition must happen only in the background worker")

    monkeypatch.setattr(service.xueqiu_http, "_warmup_anonymous_cookies", fail)
    snapshot = service.get_snapshot(1, SYMBOL)
    assert snapshot["status"] == "not_started"
    assert snapshot["prerequisites"] == {
        "xueqiu_configured": False, "xueqiu_auto_session": True, "llm_configured": True,
    }
    assert snapshot["xueqiu_session"]["mode"] == "automatic"
    assert service.enqueue_refresh(1, SYMBOL)["status"] == "queued"
    assert store.claim("worker") is not None


def test_daily_schedule_runs_without_manual_cookie(monkeypatch):
    monkeypatch.setattr(service.xueqiu_http, "effective_xueqiu_cookies", lambda: "")
    watchlist(1, [SYMBOL])
    before = datetime(2026, 9, 21, 0, 59, tzinfo=timezone.utc)
    due = datetime(2026, 9, 21, 1, tzinfo=timezone.utc)
    info = scheduler.schedule_info(1, SYMBOL, configured=service.prerequisites(1), now=before)
    assert info["next_run_at"] == int(due.timestamp())
    assert scheduler.run_daily_batch(due) == 1
    assert scheduler.run_daily_batch(due) == 0


def test_background_worker_fetches_and_selects_without_manual_cookie(monkeypatch):
    monkeypatch.setattr(service.xueqiu_http, "effective_xueqiu_cookies", lambda: "")
    calls = []

    def fetch(symbol, **kwargs):
        calls.append(("fetch", symbol, kwargs["limit"]))
        return {"items": [{"id": "1", "text": "comment"}], "raw_count": 1, "auth_status": "connected"}

    def select(symbol, items, progress):
        calls.append(("select", symbol, len(items)))
        progress(1, 1)
        return {"items": items, "analyzed_count": 1, "rejected_count": 0}

    monkeypatch.setattr(service, "fetch_comments", fetch)
    monkeypatch.setattr(service, "select_comments", select)
    assert service.enqueue_refresh(1, SYMBOL)["status"] == "queued"
    asyncio.run(service.process_job(store.claim("worker"), "worker"))
    snapshot = service.get_snapshot(1, SYMBOL)
    assert snapshot["status"] == "ready"
    assert snapshot["selected_count"] == 1
    assert calls == [("fetch", SYMBOL, 200), ("select", SYMBOL, 1)]


def test_missing_model_still_blocks_automatic_session_job(monkeypatch):
    monkeypatch.setattr(service.xueqiu_http, "effective_xueqiu_cookies", lambda: "")
    user_credentials_store.merge_user_credentials(1, {"llm": {}})
    snapshot = service.enqueue_refresh(1, SYMBOL)
    assert snapshot["status"] == "blocked"
    assert "请先配置个人 LLM" in snapshot["error"]
    assert "请先配置雪球 Cookie" not in snapshot["error"]
    assert snapshot["job"] is None


def test_success_is_isolated_by_user_and_metadata_stays_with_items():
    enqueue()
    job = store.claim("worker", now=1000)
    finish_success(job)
    first = service.get_snapshot(1, SYMBOL)
    assert first["status"] == "ready"
    assert service.get_snapshot(2, SYMBOL)["items"] == []
    enqueue(now=1060, mode="previous_day", target_date="2026-09-20")
    queued = service.get_snapshot(1, SYMBOL)
    assert queued["status"] == "queued"
    for key in ("items", "mode", "target_date", "fetched_at", "updated_at", "raw_count", "analyzed_count", "selected_count", "rejected_count"):
        assert queued[key] == first[key]
    assert queued["job"]["mode"] == "previous_day"
    assert queued["job"]["target_date"] == "2026-09-20"
    assert queued["stale"] is True


def test_active_jobs_merge_and_manual_cooldown():
    assert enqueue() == "queued"
    assert enqueue(now=1001) == "merged"
    job = store.claim("worker", now=1001)
    assert enqueue(now=1002, mode="previous_day", target_date="2026-09-20") == "merged"
    finish_success(job)
    assert enqueue(now=1059) == "cooldown"
    assert enqueue(now=1060) == "queued"


def test_concurrent_claim_and_expiry_fence_old_worker():
    enqueue()
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda owner: store.claim(owner, now=1000), ["a", "b"]))
    claimed = [job for job in claims if job]
    assert len(claimed) == 1
    original = claimed[0]
    old_owner = original["owner"]
    assert store.heartbeat(original["id"], old_owner, now=1200)
    assert store.claim("restart", now=1400) is None
    replacement = store.claim("restart", now=1501)
    assert replacement["id"] == original["id"]
    assert replacement["attempts"] == 2
    assert not finish_success(original, old_owner, now=1502)
    assert not store.heartbeat(original["id"], old_owner, now=1502)
    assert finish_success(replacement, "restart", now=1502)


def test_repeated_crash_marks_interrupted_and_allows_manual_retry():
    enqueue()
    assert store.claim("first", now=1000)
    assert store.claim("restart", now=1301)
    assert store.claim("third", now=1602) is None
    snapshot = service.get_snapshot(1, SYMBOL)
    assert snapshot["status"] == "error"
    assert "中断" in snapshot["error"]
    assert enqueue(now=1603) == "queued"


def test_error_retains_last_success_and_does_not_share_to_other_user():
    enqueue()
    job = store.claim("worker", now=1000)
    finish_success(job)
    enqueue(now=1060)
    failed = store.claim("worker", now=1060)
    store.finish(failed, "worker", {"status": "error", "error": "upstream unavailable"}, now=1062)
    snapshot = service.get_snapshot(1, SYMBOL)
    assert snapshot["items"] == [{"id": "1", "text": "selected"}]
    assert snapshot["status"] == "error"
    assert snapshot["stale"] is True
    assert snapshot["updated_at"] == 1002
    assert snapshot["last_attempt_at"] == 1062
    assert service.get_snapshot(2, SYMBOL)["items"] == []


def test_worker_threads_get_each_users_context_and_reset(monkeypatch):
    seen = []

    def fetch(symbol, **kwargs):
        seen.append(("fetch", current_user_id.get(), settings.kline_llm_key))
        return {"items": [{"id": "1", "text": "comment"}], "raw_count": 1, "fetched_at": 1001}

    def select(symbol, items, progress):
        seen.append(("select", current_user_id.get(), settings.kline_llm_key))
        progress(1, 1)
        return {"items": [{"id": "1", "text": f"user-{current_user_id.get()}"}], "analyzed_count": 1, "rejected_count": 0}

    monkeypatch.setattr(service, "fetch_comments", fetch)
    monkeypatch.setattr(service, "select_comments", select)
    for uid in (1, 2):
        enqueue(uid=uid)
        job = store.claim("worker", now=1000)
        asyncio.run(service.process_job(job, "worker"))
    assert seen == [("fetch", 1, "key-1"), ("select", 1, "key-1"), ("fetch", 2, "key-2"), ("select", 2, "key-2")]
    assert current_user_id.get() is None
    for uid in (1, 2):
        snapshot = service.get_snapshot(uid, SYMBOL)
        assert snapshot["items"][0]["text"] == f"user-{uid}"
        assert snapshot["job"]["progress"] == snapshot["job"]["total"] == 1


def test_worker_failed_llm_and_expired_cookie_preserve_old_results(monkeypatch):
    enqueue()
    finish_success(store.claim("worker", now=1000))
    monkeypatch.setattr(service, "fetch_comments", lambda *a, **k: {"items": [{"id": "2"}], "raw_count": 1})
    monkeypatch.setattr(service, "select_comments", lambda *a, **k: {"items": [], "analyzed_count": 0, "rejected_count": 0, "error": "LLM unavailable", "partial": True})
    enqueue(now=1060)
    asyncio.run(service.process_job(store.claim("worker", now=1060), "worker"))
    assert service.get_snapshot(1, SYMBOL)["status"] == "error"
    assert service.get_snapshot(1, SYMBOL)["items"][0]["text"] == "selected"
    monkeypatch.setattr(service, "fetch_comments", lambda *a, **k: {"items": [], "auth_status": "expired", "error": "Cookie expired"})
    enqueue(now=1120)
    asyncio.run(service.process_job(store.claim("worker", now=1120), "worker"))
    assert service.get_snapshot(1, SYMBOL)["status"] == "blocked"
    assert service.get_snapshot(1, SYMBOL)["items"][0]["text"] == "selected"


def test_worker_cancellation_restores_context_and_leaves_recoverable_lease(monkeypatch):
    enqueue()
    job = store.claim("worker", now=1000)

    async def cancel(*args, **kwargs):
        raise asyncio.CancelledError

    async def run():
        monkeypatch.setattr(service.asyncio, "to_thread", cancel)
        with pytest.raises(asyncio.CancelledError):
            await service.process_job(job, "worker")
        assert current_user_id.get() is None

    asyncio.run(run())
    assert store.read(1, SYMBOL)[1]["status"] == "running"
    assert store.claim("restart", now=1301)["attempts"] == 2


def test_daily_scheduler_idempotent_after_restart_and_uses_real_watchlists():
    watchlist(1, [SYMBOL])
    watchlist(2, ["000001.SZ"])
    watchlist(999, [SYMBOL])  # Deleted/nonexistent user must not consume LLM calls.
    now = datetime(2026, 9, 20, 3, tzinfo=timezone.utc)  # Sunday 11:00 Shanghai.
    assert scheduler.run_daily_batch(now) == 2
    store.init_db()  # Same persisted DB after process restart.
    assert scheduler.run_daily_batch(now) == 0
    with store.connect() as db:
        jobs = db.execute("SELECT uid,target_date,mode FROM comment_jobs ORDER BY uid").fetchall()
    assert [tuple(row) for row in jobs] == [(1, "2026-09-19", "previous_day"), (2, "2026-09-19", "previous_day")]
    assert not (settings.data_dir / "watchlists" / "3.json").exists()


def test_credentials_recovery_does_not_lock_out_today():
    watchlist(1, [SYMBOL])
    user_credentials_store.merge_user_credentials(1, {"llm": {}})
    now = datetime(2026, 9, 21, 2, tzinfo=timezone.utc)
    assert scheduler.run_daily_batch(now) == 0
    assert not store.daily_exists(1, SYMBOL, "2026-09-20")
    user_credentials_store.merge_user_credentials(1, {"llm": {"api_key": "restored"}})
    assert scheduler.run_daily_batch(now) == 1
    assert scheduler.run_daily_batch(now) == 0
    assert current_user_id.get() is None


def test_shanghai_midnight_and_nine_oclock_boundaries():
    watchlist(1, [SYMBOL])
    before_midnight = datetime(2026, 9, 20, 15, 59, tzinfo=timezone.utc)
    after_midnight = datetime(2026, 9, 20, 16, 1, tzinfo=timezone.utc)
    assert scheduler.daily_window(before_midnight)[1] == "2026-09-19"
    assert scheduler.daily_window(after_midnight)[1] == "2026-09-20"
    assert scheduler.run_daily_batch(datetime(2026, 9, 21, 0, 59, 59, tzinfo=timezone.utc)) == 0
    assert scheduler.run_daily_batch(datetime(2026, 9, 21, 1, tzinfo=timezone.utc)) == 1
    # Starting late tomorrow queues only yesterday, never a history backfill.
    assert scheduler.run_daily_batch(datetime(2026, 9, 23, 5, tzinfo=timezone.utc)) == 1
    assert store.read(1, SYMBOL)[1]["target_date"] == "2026-09-22"


def test_next_run_matches_catchup_and_idempotent_daily_state():
    watchlist(1, [SYMBOL])
    configured = {"xueqiu_configured": True, "llm_configured": True}
    before = datetime(2026, 9, 21, 0, tzinfo=timezone.utc)
    due = datetime(2026, 9, 21, 1, tzinfo=timezone.utc)
    after = datetime(2026, 9, 21, 3, tzinfo=timezone.utc)
    assert scheduler.schedule_info(1, SYMBOL, configured=configured, now=before)["next_run_at"] == int(due.timestamp())
    assert scheduler.schedule_info(1, SYMBOL, configured=configured, now=after)["next_run_at"] == int(after.timestamp())
    scheduler.run_daily_batch(after)
    assert scheduler.schedule_info(1, SYMBOL, configured=configured, now=after)["next_run_at"] == int(due.timestamp()) + 86400


def test_scheduled_failure_retries_once_without_new_daily_job():
    now = 1789952400
    enqueue(now=now, source="scheduled", mode="previous_day", target_date="2026-09-20")
    first = store.claim("worker", now=now)
    store.finish(first, "worker", {"status": "error", "error": "temporary"}, now=now + 1)
    assert store.claim("worker", now=now + 299) is None
    second = store.claim("worker", now=now + 301)
    assert second["id"] == first["id"]
    store.finish(second, "worker", {"status": "error", "error": "temporary"}, now=now + 302)
    assert store.claim("worker", now=now + 1000) is None
    assert enqueue(now=now + 1001, source="scheduled", mode="previous_day", target_date="2026-09-20") == "daily_done"


def test_disabled_scheduler_does_not_disable_manual_queue(monkeypatch):
    watchlist(1, [SYMBOL])
    monkeypatch.setattr(settings, "enable_scheduler", False)
    assert scheduler.run_daily_batch(datetime(2026, 9, 21, 3, tzinfo=timezone.utc)) == 0
    assert service.enqueue_refresh(1, SYMBOL)["status"] == "queued"
    assert store.claim("worker") is not None


def test_queue_and_snapshot_storage_are_bounded(monkeypatch):
    monkeypatch.setattr(store, "MAX_USER_SYMBOLS", 1)
    assert enqueue() == "queued"
    assert enqueue(symbol="000001.SZ") == "limited"
    assert enqueue(uid=2) == "queued"
    job = store.claim("worker", now=1000)
    monkeypatch.setattr(store, "MAX_RESULT_BYTES", 1000)
    finish_success(job, text="x" * 2000)
    assert store.read(1, SYMBOL)[0]["status"] == "partial"


def test_previous_day_mode_defaults_to_shanghai_yesterday():
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    expected = (datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=1)).isoformat()
    snapshot = service.enqueue_refresh(1, "600519.SH", mode="previous_day")
    assert snapshot["symbol"] == SYMBOL
    assert snapshot["job"]["mode"] == "previous_day"
    assert snapshot["job"]["target_date"] == expected


def test_successfully_empty_selection_clears_previous_results():
    enqueue()
    finish_success(store.claim("worker", now=1000))
    enqueue(now=1060)
    job = store.claim("worker", now=1060)
    store.finish(job, "worker", {"status": "empty", "items": [], "raw_count": 10, "analyzed_count": 10, "selected_count": 0, "rejected_count": 10}, now=1061)
    snapshot = service.get_snapshot(1, SYMBOL)
    assert snapshot["status"] == "empty"
    assert snapshot["items"] == []
    assert snapshot["rejected_count"] == 10
    assert snapshot["stale"] is False


def test_config_removed_after_enqueue_can_schedule_again_when_restored():
    watchlist(1, [SYMBOL])
    now = datetime(2026, 9, 21, 2, tzinfo=timezone.utc)
    assert scheduler.run_daily_batch(now) == 1
    user_credentials_store.merge_user_credentials(1, {"llm": {}})
    job = store.claim("worker", now=int(now.timestamp()))
    asyncio.run(service.process_job(job, "worker"))
    assert not store.daily_exists(1, SYMBOL, "2026-09-20")
    user_credentials_store.merge_user_credentials(1, {"llm": {"api_key": "restored"}})
    assert scheduler.run_daily_batch(now) == 1


def test_expired_daily_queue_is_not_backfilled_after_long_downtime():
    now = int(datetime(2026, 9, 21, 2, tzinfo=timezone.utc).timestamp())
    enqueue(now=now, source="scheduled", mode="previous_day", target_date="2026-09-20")
    assert store.claim("restart", now=now + 3 * 86400) is None
    assert service.get_snapshot(1, SYMBOL)["status"] == "error"


def test_long_running_worker_renews_lease_without_progress_callback(monkeypatch):
    from threading import Event

    enqueue()
    job = store.claim("worker", now=1000)
    renewed = Event()
    original_sleep = asyncio.sleep
    original_heartbeat = store.heartbeat
    calls = []

    async def short_heartbeat_sleep(delay):
        await original_sleep(0.01 if delay == 30 else delay)

    def heartbeat(*args, **kwargs):
        calls.append(args)
        result = original_heartbeat(*args, now=1200, **kwargs)
        renewed.set()
        return result

    def slow_fetch(*args, **kwargs):
        assert renewed.wait(timeout=2)
        assert store.claim("competing-worker", now=1400) is None
        return {"items": [], "raw_count": 0}

    monkeypatch.setattr(service.asyncio, "sleep", short_heartbeat_sleep)
    monkeypatch.setattr(store, "heartbeat", heartbeat)
    monkeypatch.setattr(service, "fetch_comments", slow_fetch)
    asyncio.run(service.process_job(job, "worker"))
    assert calls
    assert service.get_snapshot(1, SYMBOL)["status"] == "empty"


def test_normal_200_comment_cap_is_ready_and_selection_is_capped_at_20(monkeypatch):
    comments = [{"id": str(index), "text": "comment"} for index in range(200)]
    monkeypatch.setattr(service, "fetch_comments", lambda *a, **k: {"items": comments, "raw_count": 200, "truncated": True, "partial": False})
    monkeypatch.setattr(service, "select_comments", lambda *a, **k: {"items": comments[:25], "analyzed_count": 200, "rejected_count": 180, "partial": False})
    enqueue()
    asyncio.run(service.process_job(store.claim("worker", now=1000), "worker"))
    snapshot = service.get_snapshot(1, SYMBOL)
    assert snapshot["status"] == "ready"
    assert snapshot["raw_count"] == snapshot["analyzed_count"] == 200
    assert len(snapshot["items"]) == snapshot["selected_count"] == 20


@pytest.mark.parametrize("auth_status", ["missing", "expired", "verification_required"])
def test_session_errors_do_not_auto_retry_but_manual_refresh_recovers(monkeypatch, auth_status):
    watchlist(1, [SYMBOL])
    now = datetime(2026, 9, 21, 2, tzinfo=timezone.utc)
    scheduler.run_daily_batch(now)
    monkeypatch.setattr(service, "fetch_comments", lambda *a, **k: {"items": [], "auth_status": auth_status, "error": "session rejected"})
    asyncio.run(service.process_job(store.claim("worker", now=int(now.timestamp())), "worker"))
    snapshot = service.get_snapshot(1, SYMBOL)
    assert snapshot["status"] == "blocked"
    assert "手动刷新" in snapshot["warnings"][0]
    assert scheduler.run_daily_batch(now) == 0
    assert store.claim("worker", now=int(now.timestamp()) + 301) is None
    assert service.enqueue_refresh(1, SYMBOL)["status"] == "queued"


def test_cancelled_worker_stops_pipeline_before_next_paid_batch(monkeypatch):
    from threading import Event
    from app.services import xueqiu_comments_pipeline as pipeline

    started, release, finished = Event(), Event(), Event()
    comments = [{"id": str(index), "text": "comment", "created_at": 1000} for index in range(40)]
    calls = []

    def completion(*args, **kwargs):
        calls.append(True)
        started.set()
        assert release.wait(timeout=3)
        return None, "temporary failure"

    def select(symbol, items, progress):
        try:
            return pipeline.select_comments(symbol, items, progress)
        finally:
            finished.set()

    monkeypatch.setattr(service, "fetch_comments", lambda *a, **k: {"items": comments, "raw_count": 40})
    monkeypatch.setattr(service, "select_comments", select)
    monkeypatch.setattr(pipeline.llm_client, "chat_completion_sync", completion)
    enqueue()
    job = store.claim("worker", now=1000)

    async def run():
        task = asyncio.create_task(service.process_job(job, "worker"))
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)
        assert current_user_id.get() is None

    asyncio.run(run())
    assert len(calls) == 1
    assert store.read(1, SYMBOL)[1]["status"] == "running"


def test_snapshot_and_job_share_one_view_when_worker_finishes_between_reads(monkeypatch):
    from contextlib import contextmanager

    enqueue()
    finish_success(store.claim("worker", now=1000), text="old")
    enqueue(now=1060)
    pending = store.claim("worker", now=1060)
    original_connect = store.connect
    interleaved = False

    with ThreadPoolExecutor(max_workers=1) as pool:
        class SnapshotCursor:
            def __init__(self, cursor):
                self.cursor = cursor

            def fetchone(self):
                nonlocal interleaved
                old_snapshot = self.cursor.fetchone()
                if not interleaved:
                    interleaved = True
                    # A real second connection commits while the reader is paused
                    # between its snapshot SELECT and its latest-job SELECT.
                    assert pool.submit(finish_success, pending, "worker", 1062, "new").result(timeout=3)
                return old_snapshot

        class ReadConnection:
            def __init__(self, db):
                self.db = db

            def execute(self, sql, *args):
                cursor = self.db.execute(sql, *args)
                if sql.startswith("SELECT * FROM comment_snapshots"):
                    return SnapshotCursor(cursor)
                return cursor

        @contextmanager
        def interleaved_connect():
            with original_connect() as db:
                yield ReadConnection(db)

        monkeypatch.setattr(store, "connect", interleaved_connect)
        state, job = store.read(1, SYMBOL)
        assert interleaved
        assert state["result"]["items"][0]["text"] == "old"
        assert state["updated_at"] == 1002
        assert job["status"] == "running"  # Keep UI polling this old snapshot.

        state, job = store.read(1, SYMBOL)
        assert state["result"]["items"][0]["text"] == "new"
        assert state["updated_at"] == 1062
        assert job["status"] == "ready"
