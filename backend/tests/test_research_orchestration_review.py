"""Regression checks for restart recovery and bounded research evidence."""
from datetime import datetime, timedelta

import pytest

from app.schemas.research import StrategySpecV1
from app.services import research_service as service
from app.services.market_time import SHANGHAI_TZ
from app.storage import research_store as store

USER = 82
SYMBOLS = ["600000.SS"]


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def snapshot(monkeypatch):
    pool = store.put_entity(USER, "pool_version", {"symbols": SYMBOLS})
    result = {"id": "review-snapshot", "payload": {"pool_version_id": pool["id"],
              "symbols": SYMBOLS, "strict": True, "quality": {"eligible": True}}}
    def read(user_id, snapshot_id):
        assert user_id == USER and snapshot_id == result["id"]
        return result
    monkeypatch.setattr(service.research_data, "get_snapshot", read)
    monkeypatch.setattr(service, "snapshot_bars", lambda *args: {})
    monkeypatch.setattr(service.evaluation, "build_research_splits", lambda *args, **kwargs: {
        "eligible": True, "issues": [], "folds": [], "development": {"start": 10, "end": 100},
        "holdout": {"start": 101, "end": 200} if kwargs["include_holdout"] else None})
    store.set_state(USER, "latest_snapshot", result["id"])
    return result


def campaign(snapshot, *, age_weeks=0, code_hash=None, user_id=USER):
    week = (datetime.now(SHANGHAI_TZ) - timedelta(weeks=age_weeks)).strftime("%G-W%V")
    entity = store.put_entity(user_id, "campaign", {
        "week": week, "snapshot_id": snapshot["id"], "pool_version_id": snapshot["payload"]["pool_version_id"],
        "symbols": SYMBOLS, "needs_holdout": True, "goal": "research",
        "splits": {"eligible": True, "issues": [], "folds": [], "development": {"start": 10, "end": 100},
                   "holdout": {"start": 101, "end": 200}},
        "code_hash": service._code_digest() if code_hash is None else code_hash,
        "financial_config": {"initial_cash": 100000, "max_symbol_weight": .2, "max_position_pct": 1.0},
    })
    store.set_state(user_id, f"campaign_progress:{entity['id']}",
                    {"attempt": 1, "experiment_ids": ["existing-evidence"], "status": "generating"})
    return entity


def test_existing_campaign_repairs_missing_progress(snapshot):
    first = service._campaign(USER, snapshot, "original")
    store.set_state(USER, f"campaign_progress:{first['id']}", None)
    recovered = service._campaign(USER, snapshot, "retry")
    assert recovered == first
    assert store.get_state(USER, f"campaign_progress:{first['id']}") == {
        "attempt": 0, "experiment_ids": [], "status": "generating"}


def test_campaign_is_not_published_before_progress_exists(snapshot, monkeypatch):
    original = store.compare_and_set_state
    def checked(user_id, key, expected, value):
        if key.startswith("campaign:"):
            assert store.get_state(user_id, f"campaign_progress:{value}") is not None
        return original(user_id, key, expected, value)
    monkeypatch.setattr(store, "compare_and_set_state", checked)
    service._campaign(USER, snapshot, "publish")


@pytest.mark.anyio
async def test_weekly_budget_ceiling_freezes_existing_evidence(snapshot, monkeypatch):
    current = campaign(snapshot)
    monkeypatch.setattr(service, "_campaign", lambda *args: current)
    store.update_config(USER, {"weekly_candidates": 2})
    store.reserve_budget(USER, "manual-and-generated", candidates=2)
    async def exhausted(*args, **kwargs):
        raise store.BudgetExceeded("week exhausted")
    monkeypatch.setattr(service.generation, "generate_candidate", exhausted)
    calls = []
    def freeze(user_id, selected, evidence):
        calls.append((user_id, selected["id"], evidence))
        return {"status": "shadow_started"}
    monkeypatch.setattr(service, "freeze_best_candidate", freeze)
    result = await service.run_research(USER)
    assert result["status"] == "closed"
    assert result["decision"]["status"] == "shadow_started"
    assert calls == [(USER, current["id"], ["existing-evidence"])]
    assert result["attempt"] == 1  # capacity was used by another valid workload


@pytest.mark.anyio
async def test_daily_budget_ceiling_preserves_resumable_campaign(snapshot, monkeypatch):
    current = campaign(snapshot)
    monkeypatch.setattr(service, "_campaign", lambda *args: current)
    store.update_config(USER, {"daily_calls": 1})
    store.reserve_budget(USER, "today", calls=1)
    async def exhausted(*args, **kwargs):
        raise store.BudgetExceeded("day exhausted")
    monkeypatch.setattr(service.generation, "generate_candidate", exhausted)
    def forbidden(*args):
        raise AssertionError("daily pause must not freeze a partial search")
    monkeypatch.setattr(service, "freeze_best_candidate", forbidden)
    result = await service.run_research(USER)
    assert result["status"] == "budget_exhausted"
    assert store.get_state(USER, f"campaign_progress:{current['id']}")["status"] == "generating"


@pytest.mark.anyio
async def test_week_rollover_finishes_old_search_before_new_holdout_policy(snapshot, monkeypatch):
    old = campaign(snapshot, age_weeks=1)
    calls = []
    def freeze(user_id, selected, evidence):
        calls.append(selected["id"])
        store.set_state(user_id, "holdout_access", {"campaign_id": selected["id"]})
        return {"status": "holdout_rejected"}
    monkeypatch.setattr(service, "freeze_best_candidate", freeze)
    async def no_model(*args, **kwargs):
        return {"status": "configuration_required"}
    monkeypatch.setattr(service.generation, "generate_candidate", no_model)
    result = await service.run_research(USER)
    new = store.get_entity(USER, "campaign", result["campaign_id"])
    assert calls == [old["id"]]
    assert store.get_state(USER, f"campaign_progress:{old['id']}")["status"] == "closed"
    assert new["payload"]["needs_holdout"] is False
    assert new["payload"]["splits"]["holdout"] is None
    await service.run_research(USER)
    assert calls == [old["id"]]


@pytest.mark.anyio
async def test_rollover_closes_obsolete_engine_without_freeze_and_respects_owner(snapshot, monkeypatch):
    old = campaign(snapshot, age_weeks=1, code_hash="obsolete")
    other_user = campaign(snapshot, age_weeks=1, user_id=USER + 1)
    def forbidden(*args):
        raise AssertionError("obsolete or another user's evidence cannot freeze")
    monkeypatch.setattr(service, "freeze_best_candidate", forbidden)
    await service._close_previous_campaigns(USER, snapshot)
    progress = store.get_state(USER, f"campaign_progress:{old['id']}")
    assert progress["status"] == "closed" and progress["decision"]["status"] == "engine_changed"
    assert store.get_state(USER + 1, f"campaign_progress:{other_user['id']}")["status"] == "generating"
    assert store.get_state(USER, "holdout_access") is None


def test_feedback_excludes_metrics_outside_current_development_period():
    spec = StrategySpecV1.model_validate({"name": "feedback", "universe": SYMBOLS,
        "applicability": "daily", "failure_conditions": ["sideways"],
        "hypothesis": "test", "entry": {"op": "gt", "args": [{"op": "field", "field": "c"}, {"op": "const", "value": 0}]}})
    record = service.register_strategy(USER, spec.model_dump(mode="json"))
    future = store.put_entity(USER, "experiment", {"strategy_id": record["id"], "status": "evaluated",
        "report": {"metrics": {"marker": "FUTURE_METRICS"}, "issues": ["FUTURE_ISSUE"],
                   "splits": {"development": {"start": 10, "end": 200}}}})
    known = store.put_entity(USER, "experiment", {"strategy_id": record["id"], "status": "evaluated",
        "report": {"metrics": {"marker": "KNOWN_METRICS"}, "splits": {"development": {"start": 10, "end": 100}}}})
    missing = store.put_entity(USER, "experiment", {"strategy_id": record["id"], "status": "evaluated",
        "report": {"metrics": {"marker": "UNKNOWN_PERIOD"}}})
    feedback = service._feedback(USER, [future["id"], known["id"], missing["id"]], SYMBOLS,
                                 development={"start": 10, "end": 100})
    assert "KNOWN_METRICS" in str(feedback)
    assert "FUTURE" not in str(feedback) and "UNKNOWN_PERIOD" not in str(feedback)


@pytest.mark.anyio
async def test_failed_proposals_enter_next_attempt_feedback(snapshot, monkeypatch):
    current = campaign(snapshot)
    store.set_state(USER, f"campaign_progress:{current['id']}", {"attempt": 0, "experiment_ids": [], "status": "generating"})
    monkeypatch.setattr(service, "_campaign", lambda *args: current)
    contexts = []
    async def generated(user_id, context, **kwargs):
        contexts.append(context)
        return {"status": "invalid", "error": "unsupported expression"} if len(contexts) == 1 else {"status": "configuration_required"}
    monkeypatch.setattr(service.generation, "generate_candidate", generated)
    result = await service.run_research(USER, {"candidate_count": 2})
    assert result["status"] == "configuration_required"
    assert len(contexts) == 2
    failure = contexts[1]["validation_feedback"][0]
    assert failure["status"] == "failed" and failure["error"] == "unsupported expression"
    experiments = store.list_entities(USER, "experiment")
    assert len(experiments) == 1 and experiments[0]["payload"]["proposal_status"] == "invalid"
    assert store.list_entities(USER + 1, "experiment") == []


def test_rejected_outside_pool_proposal_teaches_its_original_campaign(snapshot):
    current = campaign(snapshot)
    failed = service._failed_attempt(USER, current, "wrong-pool", {
        "status": "generated", "spec": {"universe": ["000001.SZ"], "hypothesis": "outside-pool hypothesis"}},
        "候选包含池外标的")
    feedback = service._feedback(USER, [failed["id"]], SYMBOLS, development={"start": 10, "end": 100})
    assert feedback[0]["error"] == "候选包含池外标的"
    assert feedback[0]["hypothesis"] == "outside-pool hypothesis"
    assert service._feedback(USER, [failed["id"]], ["000001.SZ"], development={"start": 10, "end": 100}) == []
