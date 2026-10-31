import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.schemas import monitoring as schemas
from app.storage import monitoring_store as store


@pytest.fixture
def clock(monkeypatch):
    value = [time.time()]
    monkeypatch.setattr(store.time, "time", lambda: value[0])
    return value


def plan(**changes):
    return {"title": "观察上证指数", "target_type": "index", "target_code": "SH_COMP",
            "goal": "观察成交与趋势是否支持当前判断", "watch_conditions": "成交持续放大",
            "invalidation_conditions": "趋势持续走弱", "interval_minutes": 60,
            "review_at": (schemas._today() + timedelta(days=7)).isoformat(), **changes}


def running(uid=1, **changes):
    saved = store.create_plan(uid, plan(**changes))
    store.enqueue_run(uid, saved["id"])
    run = store.claim_run("test-worker")
    return saved, run


def report(assessment="uncertain", condition="unknown", **extra):
    return {"report": {"summary": "有待继续观察", "assessment": assessment,
                       "conditions": [{"condition": "成交持续放大", "status": condition}]},
            "evidence": [], "coverage": [], "input_hash": "hash", "model": "test", **extra}


@pytest.mark.parametrize("changes", [
    {"title": " "}, {"goal": ""}, {"target_type": "crypto"}, {"target_code": "SPX"},
    {"target_type": "stock", "target_code": "AAPL"}, {"target_type": "topic", "target_code": ""},
    {"interval_minutes": 5}, {"interval_minutes": True}, {"review_at": "2026-02-30"},
    {"keywords": ["x"] * 11}, {"keywords": [" "]}, {"keywords": ["x" * 101]},
    {"goal": "x" * 4001}, {"watch_conditions": "x" * 2001}, {"extra": "unknown"},
])
def test_plan_schema_rejects_invalid_configuration(changes):
    with pytest.raises((ValueError, ValidationError)):
        store.create_plan(1, plan(**changes))
    assert store.list_plans(1) == []


def test_stock_topic_and_index_normalization():
    stock = store.create_plan(1, plan(target_type="stock", target_code="600000.sh"))
    topic = store.create_plan(1, plan(target_type="topic", target_code="", keywords=[" 半导体 ", "半导体"]))
    assert stock["target_code"] == "600000.SS"
    assert topic["keywords"] == ["半导体"]
    assert topic["status"] == "active" and topic["revision"] == 1


def test_legacy_plan_without_list_field_can_be_read_edited_and_reviewed():
    saved = store.create_plan(1, plan(target_type="stock", target_code="600519.SS"))
    legacy = {key: value for key, value in saved.items() if key != "target_symbols"}
    with store.connection() as conn:
        conn.execute("UPDATE monitoring_plans SET payload=? WHERE id=?", (json.dumps(legacy), saved["id"]))
    assert store.get_plan(1, saved["id"])["target_symbols"] == []
    assert store.list_plans(1)[0]["target_symbols"] == []
    assert store.schedule_due()["queued"] == 1
    assert store.list_runs(1)[0]["plan_snapshot"]["target_symbols"] == []
    paused = store.update_plan(1, saved["id"], {"revision": 1, "status": "paused"})
    assert paused["target_symbols"] == []
    closed = store.review_plan(1, saved["id"], {"revision": 2, "action": "close", "reason": "保留旧计划复盘",
                                               "evidence_assessment": "uncertain"})
    assert closed["status"] == "closed"


def test_list_membership_is_versioned_cancels_old_queue_and_preserves_snapshot():
    saved = store.create_plan(1, plan(target_type="stock_list", target_code="",
                                      target_symbols=["600519.SS", "000001.SZ", "600519.sh"]))
    assert saved["target_symbols"] == ["600519.SS", "000001.SZ"]
    queued = store.enqueue_run(1, saved["id"])
    updated = store.update_plan(1, saved["id"], {"revision": 1, "target_symbols": ["600519.SS", "300750.SZ"]})
    assert updated["target_symbols"] == ["600519.SS", "300750.SZ"]
    original = store.list_runs(1)[0]
    assert original["id"] == queued["id"] and original["status"] == "cancelled"
    assert original["plan_snapshot"]["target_symbols"] == ["600519.SS", "000001.SZ"]
    reviewed = store.review_plan(1, saved["id"], {"revision": 2, "action": "revise", "reason": "更新观察名单",
                                                 "evidence_assessment": "uncertain",
                                                 "changes": {"target_symbols": ["000001.SZ", "300750.SZ"]}})
    assert reviewed["revision"] == 3 and reviewed["target_symbols"] == ["000001.SZ", "300750.SZ"]
    assert store.usage(1)["used"] == 0
    assert store.list_reviews(2, saved["id"]) == []
    single = store.update_plan(1, saved["id"], {"revision": 3, "target_type": "stock", "target_code": "000001.SZ",
                                               "target_symbols": []})
    assert single["target_code"] == "000001.SZ" and single["target_symbols"] == []


def test_member_assessment_change_notifies_without_narrative_or_order_noise(clock):
    saved, run = running(target_type="stock_list", target_code="", target_symbols=["600519.SS", "000001.SZ"])
    def result(state="watch", reverse=False):
        value = report()
        members = [{"symbol": "600519.SS", "assessment": state, "summary": str(clock[0])},
                   {"symbol": "000001.SZ", "assessment": "watch", "summary": "观察"}]
        value["report"]["members"] = list(reversed(members)) if reverse else members
        return value
    store.complete_run(1, run["id"], run["lease_token"], result())
    for state, reverse in [("attention", False), ("attention", True)]:
        clock[0] += 1
        store.enqueue_run(1, saved["id"])
        run = store.claim_run("worker")
        store.complete_run(1, run["id"], run["lease_token"], result(state, reverse))
    assert len(store.list_notifications(1)) == 1
    same = store.update_plan(1, saved["id"], {"revision": 1, "target_symbols": ["000001.SZ", "600519.SS"]})
    assert same.get("last_assessment_signature")
    changed = store.update_plan(1, saved["id"], {"revision": 2, "target_symbols": ["300750.SZ"]})
    assert "last_assessment_signature" not in changed


def test_user_isolation_covers_plans_queue_history_and_notifications(clock):
    saved, run = running()
    assert store.get_plan(2, saved["id"]) is None
    assert store.update_plan(2, saved["id"], {"revision": 1, "title": "Other"}) is None
    assert store.enqueue_run(2, saved["id"]) is None
    assert store.list_runs(2) == []
    with pytest.raises(store.PlanUnavailable):
        store.reserve_call(2, run_id=run["id"])
    with pytest.raises(store.LeaseLost):
        store.complete_run(2, run["id"], run["lease_token"], report())
    store.fail_run(1, run["id"], run["lease_token"], "network_error")
    notification = store.list_notifications(1)[0]
    assert store.mark_notification(2, notification["id"]) is None
    assert store.list_notifications(2) == []
    assert not store.list_notifications(1)[0]["is_read"]
    assert store.mark_notification(1, notification["id"])["is_read"]
    assert store.list_notifications(1, unread_only=True) == []


def test_optimistic_revisions_reject_stale_updates_and_preserve_history():
    saved = store.create_plan(1, plan())
    updated = store.update_plan(1, saved["id"], {"revision": 1, "title": "新观察目标"})
    assert updated["revision"] == 2
    with pytest.raises(store.RevisionConflict):
        store.update_plan(1, saved["id"], {"revision": 1, "goal": "stale"})
    with store.connection() as conn:
        snapshots = [json.loads(row[0]) for row in conn.execute("SELECT snapshot FROM monitoring_revisions ORDER BY revision")]
        assert [value["title"] for value in snapshots] == [saved["title"], updated["title"]]
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE monitoring_revisions SET kind='changed'")


def test_manual_review_is_immutable_and_account_scoped():
    saved = store.create_plan(1, plan())
    next_date = (schemas._today() + timedelta(days=14)).isoformat()
    updated = store.review_plan(1, saved["id"], {"revision": 1, "action": "revise", "reason": "新证据修订观察条件",
                                              "evidence_assessment": "uncertain", "next_review_at": next_date,
                                              "changes": {"watch_conditions": "新的条件"}})
    assert updated["watch_conditions"] == "新的条件" and updated["review_at"] == next_date
    reviews = store.list_reviews(1, saved["id"])
    assert len(reviews) == 1 and reviews[0]["review"]["reason"] == "新证据修订观察条件"
    assert store.list_reviews(2, saved["id"]) == []
    closed = store.review_plan(1, saved["id"], {"revision": 2, "action": "close", "reason": "结束观察",
                                             "evidence_assessment": "contradicts"})
    assert closed["status"] == "closed" and len(store.list_reviews(1, saved["id"])) == 2


def test_concurrent_enqueue_deduplicates_one_active_run():
    saved = store.create_plan(1, plan())
    with ThreadPoolExecutor(max_workers=8) as executor:
        outcomes = list(executor.map(lambda _: store.enqueue_run(1, saved["id"]), range(20)))
    assert len({row["id"] for row in outcomes}) == 1
    assert sum(row.get("deduplicated", False) for row in outcomes) == 19


@pytest.mark.parametrize("active_kind,requested_kind", [("observe", "review"), ("review", "observe")])
@pytest.mark.parametrize("active_status", ["queued", "running"])
def test_manual_different_kind_is_conflict_without_consuming_request(active_kind, requested_kind, active_status):
    saved = store.create_plan(1, plan())
    active = store.enqueue_run(1, saved["id"], kind=active_kind)
    if active_status == "running":
        active = store.claim_run("worker")

    with pytest.raises(store.PlanUnavailable, match="已有观察/复盘任务正在处理"):
        store.enqueue_run(1, saved["id"], kind=requested_kind, trigger="manual")
    history = store.list_runs(1, plan_id=saved["id"])
    assert len(history) == 1
    assert (history[0]["id"], history[0]["kind"], history[0]["status"]) == (active["id"], active_kind, active_status)
    assert store.usage(1)["used"] == 0

    same = store.enqueue_run(1, saved["id"], kind=active_kind, trigger="manual")
    assert same["id"] == active["id"] and same["deduplicated"]
    scheduled = store.enqueue_run(1, saved["id"], kind=requested_kind, trigger="scheduled")
    assert scheduled["id"] == active["id"] and scheduled["deduplicated"]

    if active_status == "queued":
        active = store.claim_run("worker")
    store.complete_run(1, active["id"], active["lease_token"], report())
    requested = store.enqueue_run(1, saved["id"], kind=requested_kind, trigger="manual")
    assert requested["id"] != active["id"] and requested["kind"] == requested_kind


def test_concurrent_claims_are_exclusive_and_limit_each_account():
    for uid in (1, 1, 2):
        saved = store.create_plan(uid, plan())
        store.enqueue_run(uid, saved["id"])
    with ThreadPoolExecutor(max_workers=8) as executor:
        outcomes = list(executor.map(lambda index: store.claim_run(f"worker-{index}"), range(8)))
    claimed = [run for run in outcomes if run]
    assert len(claimed) == 2 and {run["user_id"] for run in claimed} == {1, 2}


def test_budget_reservation_atomic_counts_verification_and_resets_shanghai_midnight():
    store.update_config(1, {"daily_call_limit": 3})
    before = datetime(2026, 11, 1, 15, 59, tzinfo=timezone.utc).timestamp()

    def reserve(_):
        try:
            return store.reserve_call(1, now=before)
        except store.BudgetExceeded:
            return None

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(reserve, range(12)))
    assert len({value for value in results if value}) == 3
    assert store.usage(1, now=before)["used"] == 3
    assert store.usage(2, now=before)["used"] == 0
    after = before + 120
    assert store.reserve_call(1, now=after)
    assert store.usage(1, now=after)["used"] == 1
    assert store.usage(1, now=after)["day"] == "2026-11-02"


def test_run_can_dispatch_only_once_and_failure_keeps_budget(clock):
    saved, run = running()
    attempt = store.reserve_call(1, run_id=run["id"], kind="observe")
    assert isinstance(attempt, str)
    assert store.list_runs(1)[0]["dispatched"]
    with pytest.raises(store.PlanUnavailable):
        store.reserve_call(1, run_id=run["id"])
    store.fail_run(1, run["id"], run["lease_token"], "timeout")
    assert store.usage(1)["used"] == 1
    assert store.claim_run("next") is None
    assert store.get_plan(1, saved["id"])["next_run_at"] >= clock[0] + 3600


def test_expired_lease_fences_old_worker_and_recovers_only_undispatched(clock):
    saved, first = running()
    clock[0] += store.LEASE_SECONDS + 1
    second = store.claim_run("replacement")
    assert second["id"] == first["id"] and second["lease_token"] != first["lease_token"]
    with pytest.raises(store.LeaseLost):
        store.renew_run(1, first["id"], first["lease_token"])
    with pytest.raises(store.LeaseLost):
        store.complete_run(1, first["id"], first["lease_token"], report())
    assert store.complete_run(1, second["id"], second["lease_token"], report())["status"] == "succeeded"


def test_dispatched_crash_is_unknown_and_never_automatically_repeated(clock):
    saved, run = running()
    store.reserve_call(1, run_id=run["id"])
    assert store.recover_interrupted() == {"requeued": 0, "unknown": 1}
    assert store.claim_run("replacement") is None
    crashed = store.list_runs(1)[0]
    assert crashed["status"] == "failed" and crashed["error_code"] == "unknown_result"
    assert store.usage(1)["used"] == 1
    assert store.schedule_due()["queued"] == 0


def test_pause_cancels_queue_and_blocks_already_claimed_dispatch(clock):
    saved = store.create_plan(1, plan())
    store.enqueue_run(1, saved["id"])
    store.update_plan(1, saved["id"], {"revision": 1, "status": "paused"})
    assert store.list_runs(1)[0]["status"] == "cancelled"
    assert store.claim_run("worker") is None
    with pytest.raises(store.PlanUnavailable):
        store.enqueue_run(1, saved["id"])
    store.update_plan(1, saved["id"], {"revision": 2, "status": "active"})
    store.enqueue_run(1, saved["id"])
    run = store.claim_run("worker")
    store.update_config(1, {"enabled": False})
    with pytest.raises(store.PlanUnavailable):
        store.reserve_call(1, run_id=run["id"])
    with pytest.raises(store.PlanUnavailable):
        store.reserve_call(1, kind="observe")
    assert store.usage(1)["used"] == 0


def test_result_for_edited_or_paused_plan_preserves_old_snapshot_without_overwrite(clock):
    saved, run = running()
    store.reserve_call(1, run_id=run["id"])
    updated = store.update_plan(1, saved["id"], {"revision": 1, "status": "paused", "goal": "新的观察目标"})
    clock[0] += 1
    done = store.complete_run(1, run["id"], run["lease_token"], report())
    assert done["plan_snapshot"]["goal"] == saved["goal"]
    assert store.get_plan(1, saved["id"]) == updated
    assert store.list_notifications(1) == []


def test_notifications_only_change_conditions_review_due_or_first_failure(clock):
    saved, run = running(review_at=(schemas._today() - timedelta(days=1)).isoformat())
    store.complete_run(1, run["id"], run["lease_token"], report())
    assert store.list_notifications(1) == []  # Initial baseline remains quiet.
    for assessment, condition in [("uncertain", "unknown"), ("supports", "met"), ("supports", "met")]:
        clock[0] += 1
        store.enqueue_run(1, saved["id"])
        current = store.claim_run("worker")
        store.complete_run(1, current["id"], current["lease_token"], report(assessment, condition))
    assert len(store.list_notifications(1)) == 1
    assert store.schedule_due()["review_notifications"] == 1
    assert store.schedule_due()["review_notifications"] == 0
    for _ in range(2):
        store.enqueue_run(1, saved["id"])
        current = store.claim_run("worker")
        store.fail_run(1, current["id"], current["lease_token"], "network_error")
    kinds = [row["kind"] for row in store.list_notifications(1)]
    assert kinds.count("change") == kinds.count("review_due") == kinds.count("error") == 1


def test_scheduling_deduplicates_and_completed_review_does_not_repeat(clock):
    saved = store.create_plan(1, plan(review_at=(schemas._today() - timedelta(days=1)).isoformat()))
    assert store.schedule_due()["queued"] == 1
    assert store.schedule_due()["queued"] == 0
    run = store.claim_run("worker")
    assert run["kind"] == "review"
    store.complete_run(1, run["id"], run["lease_token"], report(model_called=True))
    assert store.get_plan(1, saved["id"])["last_review_run_date"] == saved["review_at"]
    clock[0] += 3601
    assert store.schedule_due()["queued"] == 1
    assert store.claim_run("worker")["kind"] == "observe"


def test_early_manual_review_does_not_complete_future_due_review(clock):
    review_date = (datetime.fromtimestamp(clock[0], schemas.SHANGHAI).date() + timedelta(days=1)).isoformat()
    saved = store.create_plan(1, plan(review_at=review_date))
    store.enqueue_run(1, saved["id"], kind="review", trigger="manual")
    run = store.claim_run("worker")
    store.complete_run(1, run["id"], run["lease_token"], report(model_called=True))
    assert store.get_plan(1, saved["id"])["last_review_run_date"] is None

    clock[0] += 86400
    assert store.schedule_due()["queued"] == 1
    assert store.claim_run("worker")["kind"] == "review"


@pytest.mark.parametrize("call_details", [{}, {"model_called": False}])
def test_placeholder_review_retries_at_interval_until_actual_due_model_review(clock, call_details):
    # 16:00 UTC is already the next date in Shanghai.
    clock[0] = datetime(2026, 11, 1, 16, 0, tzinfo=timezone.utc).timestamp()
    saved = store.create_plan(1, plan(review_at="2026-11-02"))
    assert store.schedule_due()["queued"] == 1
    placeholder = store.claim_run("worker")
    assert placeholder["kind"] == "review"
    store.complete_run(1, placeholder["id"], placeholder["lease_token"], report(**call_details))
    assert store.get_plan(1, saved["id"])["last_review_run_date"] is None
    assert store.schedule_due()["queued"] == 0

    clock[0] += 3601
    assert store.schedule_due()["queued"] == 1
    actual = store.claim_run("worker")
    assert actual["kind"] == "review"
    store.complete_run(1, actual["id"], actual["lease_token"], report(model_called=True))
    assert store.get_plan(1, saved["id"])["last_review_run_date"] == "2026-11-02"
    clock[0] += 3601
    assert store.schedule_due()["queued"] == 1
    assert store.claim_run("worker")["kind"] == "observe"


def test_completed_result_is_immutable_and_unchanged_run_stays_quiet(clock):
    saved, run = running()
    store.complete_run(1, run["id"], run["lease_token"], report())
    with pytest.raises(store.LeaseLost):
        store.complete_run(1, run["id"], run["lease_token"], report("supports"))
    with store.connection() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE monitoring_runs SET result='{}' WHERE id=?", (run["id"],))
    store.enqueue_run(1, saved["id"])
    next_run = store.claim_run("worker")
    store.complete_run(1, next_run["id"], next_run["lease_token"], {"skipped_unchanged": True})
    assert store.list_notifications(1) == []


def test_worker_model_state_restart_and_credential_rejection():
    store.set_worker_state({"owner": "worker-a", "running": True})
    store.set_model_state(1, {"status": "ready", "model": "configured-model", "fingerprint": "sha256"})
    store.init_db()
    assert store.get_worker_state()["owner"] == "worker-a"
    assert store.get_model_state(1)["status"] == "ready"
    assert store.get_model_state(2) == {}
    with pytest.raises(ValueError, match="凭据"):
        store.set_model_state(1, {"api_key": "never-persist-this"})
    saved, run = running()
    with pytest.raises(ValueError, match="凭据"):
        store.complete_run(1, run["id"], run["lease_token"], {"nested": {"authorization": "secret"}})


@pytest.mark.parametrize("uid", [0, -1, True, "1", 1.2])
def test_user_ids_are_strict(uid):
    with pytest.raises(ValueError):
        store.create_plan(uid, plan())


def test_paused_account_can_verify_changed_credentials_without_resuming_monitoring(clock):
    saved, run = running()
    store.update_config(1, {"enabled": False, "daily_call_limit": 1})
    assert store.reserve_call(1, kind="verify")
    assert store.usage(1)["used"] == 1
    assert not store.get_config(1)["enabled"]
    with pytest.raises(store.BudgetExceeded):
        store.reserve_call(1, kind="verify")
    with pytest.raises(store.PlanUnavailable):
        store.reserve_call(1, run_id=run["id"], kind="verify")
    with pytest.raises(store.PlanUnavailable):
        store.enqueue_run(1, saved["id"])
