from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.config import settings
from app.deps.auth import get_current_user
from app.routers import investment_research as router
from app.services import investment_research as service, investment_research_engine as engine
from app.storage import investment_research_store as store, monitoring_store


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "enable_investment_research", True)
    monkeypatch.setattr(engine, "model_status", lambda uid: {"configured": True, "model": "fake", "error_code": None})
    app = FastAPI()
    app.include_router(router.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
    with TestClient(app) as client:
        yield client


def create(client, **changes):
    response = client.post("/api/investment-research/dossiers", json={"title": "现金流研究", "target_type": "stock",
        "target_code": "600519.SS", "objective": "核对收入与回款", **changes})
    assert response.status_code == 201, response.text
    return response.json()


def test_crud_account_filter_and_input_validation(client):
    dossier = create(client)
    path = "/api/investment-research/dossiers/" + dossier["id"]
    assert client.get(path).json()["dossier"]["revision"] == 1
    question = client.post(path + "/questions", json={"text": "增长来自哪里？"}).json()
    assert client.patch("/api/investment-research/questions/" + question["id"], json={"revision": 1, "answer": "待核实"}).status_code == 200
    assert client.patch("/api/investment-research/questions/" + question["id"], json={"revision": 1, "answer": "过期"}).status_code == 409
    assert client.get("/api/investment-research?symbol=600519.sh").json()["dossiers"][0]["id"] == dossier["id"]
    assert client.get("/api/investment-research?symbol=000001.SZ").json()["dossiers"] == []
    assert client.post(path + "/materials", json={"kind": "note", "title": "来源", "text": "x", "user_id": 2}).status_code == 422
    assert client.post(path + "/materials", json={"kind": "note", "title": "来源", "text": "x", "source_url": "javascript:alert(1)"}).status_code == 422
    client.app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=2)
    assert client.get(path).status_code == 404
    assert client.post(path + "/questions", json={"text": "越权"}).status_code == 404
    assert client.get("/api/investment-research").json()["dossiers"] == []


def test_draft_reading_does_not_call_model_and_gate_only_applies_to_generation(client, monkeypatch):
    async def unexpected(*args, **kwargs):
        pytest.fail("page reads and edits must not call a model")
    monkeypatch.setattr(engine, "analyze_research", unexpected)
    monkeypatch.setattr(engine, "model_status", lambda uid: {"configured": False, "model": None})
    dossier = create(client)
    path = "/api/investment-research/dossiers/" + dossier["id"]
    assert client.get(path).status_code == 200
    assert client.post(path + "/generate", json={}).status_code == 409
    assert client.post(path + "/materials", json={"title": "人工判断", "text": "尚待验证"}).status_code == 201
    monkeypatch.setattr(engine, "model_status", lambda uid: {"configured": True, "model": "fake"})
    assert client.post(path + "/generate", json={}).status_code == 202
    assert store.usage(1)["used"] == 0


def test_upload_private_access_bounds_and_pending_pdf(client, monkeypatch):
    dossier = create(client)
    path = "/api/investment-research/dossiers/" + dossier["id"] + "/materials/upload"
    text = client.post(path, files={"file": ("notes.md", "收入仍需核对".encode(), "text/markdown")})
    assert text.status_code == 201
    text_id = text.json()["id"]
    assert client.get("/api/investment-research/materials/" + text_id).json()["text"] == "收入仍需核对"
    invalid = client.post(path, files={"file": ("image.pdf", b"<html>not pdf</html>", "application/pdf")})
    assert invalid.status_code == 400
    pdf = client.post(path, files={"file": ("private.pdf", b"%PDF-1.4\nprivate sample", "application/pdf")})
    assert pdf.status_code == 201 and pdf.json()["status"] == "pending"
    identifier = pdf.json()["id"]
    assert client.get("/api/investment-research/materials/" + identifier + "/pdf").content.startswith(b"%PDF-")
    assert client.post("/api/investment-research/dossiers/" + dossier["id"] + "/generate", json={}).status_code == 409
    monkeypatch.setattr(service, "MAX_UPLOAD_BYTES", 8)
    assert client.post(path, files={"file": ("big.txt", b"x"*9)}).status_code == 413
    client.app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=2)
    assert client.get("/api/investment-research/materials/" + identifier + "/pdf").status_code == 404
    assert client.get("/api/investment-research/materials/" + text_id).status_code == 404


def monitor_run(uid=1, symbol="600519.SS"):
    plan = monitoring_store.create_plan(uid, {"title": "观察", "target_type": "stock", "target_code": symbol, "goal": "持续观察"})
    monitoring_store.enqueue_run(uid, plan["id"])
    job = monitoring_store.claim_run("monitor-owner")
    result = {"plan_snapshot": plan, "report": {"summary": "注意回款变化", "assessment": "attention", "conditions": [], "risks": [], "next_steps": []},
              "evidence": [{"id": "frozen-source", "title": "原文", "excerpt": "证据片段", "factual_support": True}], "coverage": [], "input_hash": "old"}
    monitoring_store.complete_run(uid, job["id"], job["lease_token"], result)
    return job["id"]


def test_monitor_import_uses_authoritative_account_run_and_deduplicates(client):
    rid = monitor_run()
    dossier = create(client, monitor_run_id=rid)
    path = "/api/investment-research/dossiers/" + dossier["id"]
    original = client.get(path).json()["materials"][0]
    body = {"kind": "monitoring", "monitor_run_id": rid, "title": "变化"}
    repeated = client.post(path + "/materials", json=body)
    assert repeated.status_code == 201 and repeated.json()["id"] == original["id"]
    assert repeated.json()["deduplicated"]
    assert client.get(path).json()["dossier"]["revision"] == dossier["revision"]
    saved = client.get("/api/investment-research/materials/" + original["id"]).json()
    assert saved["monitoring_snapshot"]["evidence"][0]["id"] == "frozen-source"
    assert client.post(path + "/materials", json={**body, "text": "伪造"}).status_code == 422
    wrong = create(client, target_code="000001.SZ")
    assert client.post("/api/investment-research/dossiers/"+wrong["id"]+"/monitoring-updates", json={"monitor_run_id": rid}).status_code == 409
    client.app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=2)
    foreign = client.post("/api/investment-research/dossiers", json={"title": "越权导入", "target_type": "stock", "target_code": "600519.SS", "objective": "目标", "monitor_run_id": rid})
    assert foreign.status_code == 404


def test_disabled_worker_preserves_drafts_and_reports(client, monkeypatch):
    dossier = create(client)
    path = "/api/investment-research/dossiers/" + dossier["id"]
    monkeypatch.setattr(settings, "enable_investment_research", False)
    assert client.get(path).status_code == 200
    assert client.post(path + "/generate", json={}).status_code == 409
    assert client.patch(path, json={"revision": 1, "thesis": "个人判断仍可保存"}).status_code == 200
    assert client.get("/api/investment-research").json()["worker"]["enabled"] is False
