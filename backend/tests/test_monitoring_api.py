from datetime import timedelta
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.config import settings
from app.deps.auth import get_current_user
from app.routers import monitoring
from app.schemas.tracking import today
from app.services import monitoring_engine
from app.storage import monitoring_store as store


def plan_data(**extra):
    return {"title": "利率变化观察", "target_type": "topic", "target_code": "", "keywords": ["利率", "Fed"],
            "goal": "观察利率变化对成长行业的影响", "watch_conditions": "利率出现明确下降", "invalidation_conditions": "通胀持续加速",
            "interval_minutes": 360, "review_at": str(today()+timedelta(days=7)), **extra}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "enable_monitoring", True)
    app = FastAPI()
    app.include_router(monitoring.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
    monkeypatch.setattr(monitoring_engine, "model_status", lambda uid: {"configured": True, "verified": True, "model": "test-model", "checked_at": 1, "error": None})
    with TestClient(app) as client:
        yield client


def test_creation_requires_configured_and_verified_model(client, monkeypatch):
    for configured in (False, True):
        monkeypatch.setattr(monitoring_engine, "model_status", lambda uid: {"configured": configured, "verified": False})
        assert client.post("/api/monitoring/plans", json=plan_data()).status_code == 409
    assert store.list_plans(1) == []


def test_account_isolation_revision_and_manual_review(client, monkeypatch):
    created = client.post("/api/monitoring/plans", json=plan_data())
    assert created.status_code == 201
    plan = created.json()
    path = "/api/monitoring/plans/"+plan["id"]
    assert client.get(path).json()["plan"]["revision"] == 1
    assert client.patch(path, json={"revision": 1, "title": "新的观察目标"}).status_code == 200
    assert client.patch(path, json={"revision": 1, "title": "过期修改"}).status_code == 409
    assert client.post(path+"/runs", json={"kind": "observe"}).status_code == 202
    monkeypatch.setattr(monitoring_engine, "model_status", lambda uid: {"configured": False, "verified": False})
    assert client.post(path+"/runs", json={"kind": "review"}).status_code == 409
    assert client.patch(path, json={"revision": 2, "status": "paused"}).status_code == 200
    # Historical review and closure do not need to make a model request.
    assert client.post(path+"/reviews", json={"revision": 3, "action": "close", "reason": "观察结束", "evidence_assessment": "uncertain"}).status_code == 200
    client.app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=2)
    assert client.get(path).status_code == 404
    assert client.patch(path, json={"revision": 4, "status": "paused"}).status_code == 404
    assert client.post(path+"/runs", json={"kind": "observe"}).status_code == 404
    assert client.get("/api/monitoring/overview").json()["plans"] == []


def test_read_only_status_does_not_verify_or_call_a_model(client, monkeypatch):
    async def unexpected(*args):
        pytest.fail("reading the page must never call a model")
    monkeypatch.setattr(monitoring_engine, "verify_model", unexpected)
    monkeypatch.setattr(monitoring_engine, "generate_report", unexpected)
    store.set_worker_state({"running": True, "heartbeat_at": 0, "user_id": 99, "run_id": "private", "phase": "observing"})
    status = client.get("/api/monitoring/overview").json()
    assert status["usage"]["calls_used"] == 0
    assert status["usage"]["daily_call_limit"] == 12
    assert "user_id" not in status["worker"] and "run_id" not in status["worker"]
    assert status["worker"]["running"] is False


def test_model_save_does_not_start_research(client, monkeypatch):
    from app.services import local_autopilot
    from app.storage import users_store
    users_store.init_db()
    monkeypatch.setattr(settings, "enable_research_worker", True)
    monkeypatch.setattr(local_autopilot, "reconcile", lambda *args: pytest.fail("monitor setup must not enable research"))
    result = client.put("/api/monitoring/model", json={"api_key": "test-private-monitor-key", "api_base": "https://api.openai.com/v1", "model": "test-model"})
    assert result.status_code == 200
    assert "test-private-monitor-key" not in result.text


def test_explicit_verification_and_invalid_requests(client, monkeypatch):
    calls = []
    async def verify(uid):
        calls.append(uid)
        return {"configured": True, "verified": True, "model": "test"}
    monkeypatch.setattr(monitoring_engine, "verify_model", verify)
    assert client.post("/api/monitoring/model/verify").json()["verified"] is True
    assert calls == [1]
    assert client.post("/api/monitoring/plans", json=plan_data(user_id=2)).status_code == 422
    assert client.post("/api/monitoring/plans", json=plan_data(interval_minutes=1)).status_code == 422
    assert client.patch("/api/monitoring/config", json={"daily_call_limit": 0}).status_code == 422


def test_disabled_service_prevents_new_execution_but_keeps_history(client, monkeypatch):
    plan = store.create_plan(1, plan_data())
    monkeypatch.setattr(settings, "enable_monitoring", False)
    assert client.post("/api/monitoring/plans", json=plan_data()).status_code == 409
    assert client.post(f"/api/monitoring/plans/{plan['id']}/runs", json={"kind": "observe"}).status_code == 409
    assert client.get(f"/api/monitoring/plans/{plan['id']}").status_code == 200
    assert client.patch(f"/api/monitoring/plans/{plan['id']}", json={"revision": 1, "status": "paused"}).status_code == 200


def test_busy_observation_does_not_accept_a_different_review_request(client):
    plan = client.post("/api/monitoring/plans", json=plan_data()).json()
    path = f"/api/monitoring/plans/{plan['id']}/runs"
    observation = client.post(path, json={"kind": "observe"})
    assert observation.status_code == 202
    review = client.post(path, json={"kind": "review"})
    assert review.status_code == 409
    assert client.post(path, json={"kind": "observe"}).json()["id"] == observation.json()["id"]
    assert len(store.list_runs(1, plan_id=plan["id"])) == 1


def test_stock_list_crud_and_run_keep_account_membership(client):
    response = client.post("/api/monitoring/plans", json=plan_data(
        title="自选股观察", target_type="stock_list", target_symbols=["600519.SS", "000001.SZ", "600519.sh"]))
    assert response.status_code == 201
    created = response.json()
    assert created["target_symbols"] == ["600519.SS", "000001.SZ"]
    path = f"/api/monitoring/plans/{created['id']}"
    queued = client.post(path + "/runs", json={"kind": "review"})
    assert queued.status_code == 202
    assert queued.json()["plan_snapshot"]["target_symbols"] == created["target_symbols"]
    assert client.patch(path, json={"revision": 1, "target_symbols": ["300750.SZ"]}).status_code == 200
    detail = client.get(path).json()
    assert detail["plan"]["target_symbols"] == ["300750.SZ"]
    assert detail["runs"][0]["status"] == "cancelled"
    client.app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=2)
    assert client.get(path).status_code == 404
    assert client.patch(path, json={"revision": 2, "target_symbols": ["600000.SS"]}).status_code == 404
    assert store.usage(1)["used"] == store.usage(2)["used"] == 0


@pytest.mark.parametrize("extra", [
    {"target_symbols": []}, {"target_symbols": ["AAPL"]}, {"target_symbols": ["600519.SS"] * 21},
    {"target_symbols": ["600519.SS"], "target_code": "600519.SS"}, {"target_symbols": None},
    {"target_type": "stock", "target_code": "600519.SS", "target_symbols": ["000001.SZ"]},
])
def test_stock_list_rejects_empty_invalid_or_mixed_targets(client, extra):
    response = client.post("/api/monitoring/plans", json=plan_data(**{"target_type": "stock_list", **extra}))
    assert response.status_code == 422
    assert store.list_plans(1) == []
