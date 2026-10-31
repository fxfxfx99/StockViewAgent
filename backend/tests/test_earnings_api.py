from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.config import settings
from app.deps.auth import get_current_user
from app.routers import earnings
from app.services import earnings_analysis
from app.storage import earnings_store as store


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "enable_earnings", True)
    monkeypatch.setattr(earnings_analysis, "get_model_status", lambda uid: {"configured": True, "model": "test"})
    app = FastAPI()
    app.include_router(earnings.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
    with TestClient(app) as client:
        yield client


def seed(**extra):
    row = {"identity_key": "test:one", "symbol": "600519.SS", "title": "2025年年度报告", "report_period": "2025-12-31",
           "announcement_date": "2026-04-17", "published_at": 1776384000, "document_kind": "full_report", **extra}
    store.save_discovery(row["symbol"], {"items": [row]})
    return next(report for report in store.list_reports(row["symbol"]) if report["identity_key"] == row["identity_key"])


def test_get_is_local_and_does_not_enqueue_or_call_models(client, monkeypatch):
    monkeypatch.setattr(store, "enqueue", lambda *a, **kw: pytest.fail("read must not enqueue"))
    monkeypatch.setattr(earnings_analysis, "analyze_document", lambda *a, **kw: pytest.fail("read must not call a model"))
    report = seed()
    data = client.get("/api/news/earnings", params={"symbol": "600519.SH"}).json()
    assert len(data["reports"]) == 1 and data["jobs"] == [] and data["usage"]["used"] == 0
    assert client.get("/api/news/earnings/" + report["id"]).json()["analyses"] == []
    assert client.get("/api/news/earnings", params={"symbol": "https://example.com"}).status_code == 400


def test_model_required_only_for_interpretation_and_summaries_rejected(client, monkeypatch):
    report = seed()
    monkeypatch.setattr(earnings_analysis, "get_model_status", lambda uid: {"configured": False})
    assert client.post("/api/news/earnings/refresh", json={"symbol": "600519.SS"}).status_code == 202
    assert client.post(f"/api/news/earnings/{report['id']}/prepare", json={}).status_code == 202
    assert client.post(f"/api/news/earnings/{report['id']}/analyze", json={}).status_code == 409
    monkeypatch.setattr(earnings_analysis, "get_model_status", lambda uid: {"configured": True, "model": "test"})
    summary = seed(identity_key="summary", document_kind="summary")
    assert client.post(f"/api/news/earnings/{summary['id']}/analyze", json={}).status_code == 409


def test_account_jobs_are_private_and_history_remains_without_model(client, monkeypatch):
    report = seed()
    response = client.post(f"/api/news/earnings/{report['id']}/analyze", json={"focus": "现金流质量"})
    assert response.status_code == 202 and "user_id" not in response.json()
    assert "owner" not in response.json()
    assert client.post(f"/api/news/earnings/{report['id']}/analyze", json={"focus": "资产负债"}).status_code == 409
    client.app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=2)
    assert client.get(f"/api/news/earnings/{report['id']}").json()["jobs"] == []
    assert client.get("/api/news/earnings", params={"symbol": "600519.SS"}).json()["jobs"] == []


def test_missing_disabled_and_invalid_input(client, monkeypatch):
    assert client.post("/api/news/earnings/missing/analyze", json={}).status_code == 404
    assert client.post("/api/news/earnings/refresh", json={"symbol": "600519.SS", "url": "http://127.0.0.1"}).status_code == 422
    report = seed()
    assert client.post(f"/api/news/earnings/{report['id']}/analyze", json={"focus": "x" * 2001}).status_code == 422
    assert client.get(f"/api/news/earnings/{report['id']}/pdf").status_code == 404
    monkeypatch.setattr(settings, "enable_earnings", False)
    assert client.post("/api/news/earnings/refresh", json={"symbol": "600519.SS"}).status_code == 409
    assert client.post(f"/api/news/earnings/{report['id']}/analyze", json={}).status_code == 409
    assert client.get(f"/api/news/earnings/{report['id']}").status_code == 200
