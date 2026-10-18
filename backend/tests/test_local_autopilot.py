"""Local bootstrap must never send a paid probe or cross an account boundary."""
from datetime import datetime
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.deps.auth import get_current_user
from app.routers import autopilot, settings_integrations, settings_llm
from app.security.user_context import current_user_id
from app.services import local_autopilot as auto
from app.services.market_time import SHANGHAI_TZ
from app.storage import research_store as store, user_credentials_store as credentials
from app.storage.users_store import UserRecord

USER = 81
NOW = datetime(2026, 10, 19, 17, tzinfo=SHANGHAI_TZ)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def enable_local(monkeypatch):
    monkeypatch.setattr(settings, "enable_research_worker", True)
    monkeypatch.setattr(settings, "auth_required", False)


def list_file(user_id=USER, symbols=()):
    directory = settings.data_dir / "watchlists"
    directory.mkdir(exist_ok=True)
    path = directory / f"{user_id}.json"
    path.write_text(json.dumps({"symbols": list(symbols)}))
    return path


def complete_collection(user_id=USER, *, missing=False):
    job = store.claim_job("test-worker", user_id=user_id, kinds=["collect"])
    assert job
    symbols = job["payload"]["symbols"]
    snapshot = store.put_entity(user_id, "snapshot", {"symbols": symbols[:-1] if missing else symbols,
        "strict": False, "quality": {"eligible": False, "issues": ["historical_availability_missing"]}})
    store.set_state(user_id, "latest_snapshot", snapshot["id"])
    store.complete_job(user_id, job["id"], job["lease_token"],
                       {"status": "partial" if missing else "collected", "snapshot_id": snapshot["id"]})
    return snapshot


def configured(user_id=USER):
    credentials.merge_user_credentials(user_id, {"llm": {
        "api_key": "private-key-never-return", "api_base": "https://api.openai.com/v1", "model": "test-model"}})


def test_first_boot_seeds_five_and_queues_no_paid_work():
    result = auto.reconcile(USER, now=NOW)
    assert result["watchlist"]["symbols"] == list(auto.DEFAULT_SYMBOLS)
    assert result["watchlist"]["seeded"] is True
    assert result["phase"] == "waiting_llm"
    assert {job["kind"] for job in result["queued_jobs"]} == {"collect", "github", "source_maintenance"}
    assert store.budget_status(USER)["used"] == {"calls": 0, "tokens": 0, "candidates": 0}
    again = auto.reconcile(USER, now=NOW)
    assert {job["id"] for job in again["queued_jobs"]} == {job["id"] for job in result["queued_jobs"]}


@pytest.mark.parametrize("symbols", [[], ["600036.SS"]])
def test_existing_and_explicitly_empty_watchlists_are_never_reseeded(symbols):
    path = list_file(symbols=symbols)
    before = path.read_bytes()
    result = auto.reconcile(USER, now=NOW)
    assert path.read_bytes() == before
    assert result["watchlist"]["seeded"] is False
    assert result["watchlist"]["explicitly_empty"] is (not symbols)
    if not symbols:
        assert result["phase"] == "waiting_watchlist"
        assert not any(job["kind"] in {"collect", "research"} for job in result["queued_jobs"])


def test_corrupt_watchlist_is_reported_without_overwriting():
    path = list_file()
    path.write_text("{broken-json")
    result = auto.reconcile(USER, now=NOW)
    assert result["phase"] == "attention"
    assert path.read_text() == "{broken-json"


def test_research_and_paper_wait_for_collection_then_use_exact_snapshot():
    configured()
    before = auto.reconcile(USER, now=NOW)
    assert before["jobs"]["research"] is None and before["jobs"]["paper"] is None
    snapshot = complete_collection()
    result = auto.reconcile(USER, now=NOW)
    assert result["jobs"]["research"]["status"] == "queued"
    for kind in ("research", "paper"):
        job = store.get_job(USER, result["jobs"][kind]["id"])
        assert job["payload"]["snapshot_id"] == snapshot["id"]
    assert result["data"]["strict"] is False  # autopilot never upgrades data quality
    assert store.budget_status(USER)["used"]["calls"] == 0
    assert "private-key-never-return" not in json.dumps(result)


def test_partial_pool_only_allows_paper_maintenance():
    configured()
    auto.reconcile(USER, now=NOW)
    complete_collection(missing=True)
    result = auto.reconcile(USER, now=NOW)
    assert result["jobs"]["paper"]["status"] == "queued"
    assert result["jobs"]["research"] is None


def test_credentials_and_snapshots_are_scoped_even_with_other_ambient_user():
    configured(USER)
    token = current_user_id.set(USER)
    try:
        assert auto.llm_configured(USER)
        assert not auto.llm_configured(USER + 1)
        assert current_user_id.get() == USER
        auto.reconcile(USER, now=NOW)
        snapshot = complete_collection()
        # A foreign ID in a mutable pointer cannot disclose the other record.
        store.set_state(USER + 1, "latest_snapshot", snapshot["id"])
        result = auto.reconcile(USER + 1, now=NOW)
        assert result["data"] is None and not result["llm_configured"]
        assert result["jobs"]["research"] is None
        assert all(store.get_job(USER, j["id"]) is None for j in result["queued_jobs"])
    finally:
        current_user_id.reset(token)


def test_paused_candidates_keep_data_and_source_maintenance():
    configured()
    store.update_config(USER, {"paused": True})
    result = auto.reconcile(USER, now=NOW)
    assert {j["kind"] for j in result["queued_jobs"]} == {"collect", "source_maintenance"}
    assert result["phase"] == "paused"
    complete_collection()
    result = auto.reconcile(USER, now=NOW)
    assert result["jobs"]["paper"] and result["jobs"]["research"] is None
    job = store.claim_job("test-worker", user_id=USER, kinds=["source_maintenance"])
    assert job and job["kind"] == "source_maintenance"


def test_disabled_worker_does_not_initialize_or_queue(monkeypatch):
    monkeypatch.setattr(settings, "enable_research_worker", False)
    result = auto.reconcile(USER, now=NOW)
    assert result["phase"] == "disabled" and result["queued_jobs"] == []
    assert not (settings.data_dir / "watchlists" / f"{USER}.json").exists()
    assert store.list_jobs(USER) == []


def test_failed_tasks_cool_down_and_retry_without_deleting_evidence():
    first = auto.reconcile(USER, now=NOW)["jobs"]["collect"]
    for _ in range(3):
        job = store.claim_job("test-worker", user_id=USER, kinds=["collect"])
        assert job
        store.fail_job(USER, job["id"], job["lease_token"], "temporary provider failure", retry_delay=0)
    failed = store.get_job(USER, first["id"])
    # Keep the same logical cycle while advancing cooldown deterministically.
    logical = f"collect:{NOW.date().isoformat()}:close:{store.content_hash(sorted(auto.DEFAULT_SYMBOLS))[:16]}"
    same = auto._ensure_job(USER, "collect", failed["payload"], logical, failed["updated_at"] + 10)
    assert same["id"] == failed["id"]
    retry = auto._ensure_job(USER, "collect", failed["payload"], logical, failed["updated_at"] + 3601)
    assert retry["id"] != failed["id"] and retry["status"] == "queued"
    assert store.get_job(USER, failed["id"])["status"] == "failed"
    assert auto._ensure_job(USER, "collect", failed["payload"], logical, failed["updated_at"] + 3602)["id"] == retry["id"]


@pytest.mark.parametrize("endpoint", ["/api/settings/llm", "/api/settings/integrations"])
def test_saving_llm_activates_without_restart_or_paid_probe(endpoint):
    auto.reconcile(USER)
    complete_collection()
    app = FastAPI()
    for router in (autopilot.router, settings_llm.router, settings_integrations.router):
        app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: UserRecord(USER, "test", "user", "", {})
    value = {"api_key": "private-key-never-return", "api_base": "https://api.openai.com/v1", "model": "test-model"}
    with TestClient(app) as client:
        response = client.put(endpoint, json={"llm": value} if endpoint.endswith("integrations") else value)
        assert response.status_code == 200
        status = client.get("/api/autopilot/status").json()
        assert status["llm_configured"]
        assert status["jobs"]["research"]["status"] == "queued"
        assert "private-key-never-return" not in json.dumps(status)
        previous = status["jobs"]["research"]["id"]
        assert client.post("/api/autopilot/reconcile").json()["jobs"]["research"]["id"] == previous
    assert store.budget_status(USER)["used"]["calls"] == 0


def test_setup_ready_needs_only_llm_with_public_market_fallback():
    configured()
    result = settings_integrations.setup_status_payload(USER)
    assert result["ready"] is True
    assert result["market_data_configured"] is True
    assert result["market_data_provider"] == "public"


@pytest.mark.anyio
async def test_clearing_pool_after_queue_prevents_stale_automatic_generation(monkeypatch):
    from app import research_worker
    from app.services import research_service
    configured()
    auto.reconcile(USER, now=NOW)
    complete_collection()
    result = auto.reconcile(USER, now=NOW)
    job = store.get_job(USER, result["jobs"]["research"]["id"])
    list_file(symbols=[])
    async def forbidden(*args, **kwargs):
        raise AssertionError("cleared watchlist must not start a queued proposal")
    monkeypatch.setattr(research_service, "run_research", forbidden)
    assert await research_worker.dispatch(job) == {"status": "waiting_watchlist"}


def test_repaired_llm_configuration_resumes_waiting_job_without_cooldown():
    logical = "research:waiting-for-configuration"
    job = auto._ensure_job(USER, "research", {}, logical, NOW.timestamp())
    leased = store.claim_job("worker", user_id=USER, kinds=["research"])
    store.complete_job(USER, leased["id"], leased["lease_token"], {"status": "configuration_required"})
    configured()
    resumed = auto._ensure_job(USER, "research", {}, logical, job["updated_at"] + 1)
    assert resumed["id"] != job["id"] and resumed["status"] == "queued"


def test_clear_does_not_abandon_existing_paper_holdings(monkeypatch):
    from app.services import research_paper
    list_file(symbols=[])
    monkeypatch.setattr(research_paper, "required_symbols", lambda uid: ["600036.SS"])
    result = auto.reconcile(USER, now=NOW)
    assert result["watchlist"]["explicitly_empty"]
    job = store.get_job(USER, result["jobs"]["collect"]["id"])
    assert job["payload"]["symbols"] == ["600036.SS"]
    complete_collection()
    result = auto.reconcile(USER, now=NOW)
    assert result["jobs"]["paper"] and result["jobs"]["research"] is None


def test_autopilot_routes_require_auth_on_authenticated_deployments(monkeypatch):
    monkeypatch.setattr(settings, "auth_required", True)
    app = FastAPI()
    app.include_router(autopilot.router)
    with TestClient(app) as client:
        assert client.get("/api/autopilot/status").status_code == 401
        assert client.post("/api/autopilot/reconcile").status_code == 401


def test_zero_weekly_budget_never_queues_candidate_generation():
    configured()
    store.update_config(USER, {"weekly_candidates": 0})
    auto.reconcile(USER, now=NOW)
    complete_collection()
    result = auto.reconcile(USER, now=NOW)
    assert result["jobs"]["paper"] and result["jobs"]["research"] is None
