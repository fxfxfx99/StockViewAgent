import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.deps.auth import get_current_user
from app.routers.transaction_agent import router
from app.services import strategy_workspace_service as svc
from app.services.transaction_agent_service import STRATEGIES
from app.storage import strategy_knowledge_store as shared
from app.storage import strategy_workspace_store as store
from app.storage.users_store import UserRecord


STRATEGY = "value_investing"


def _clock(monkeypatch, iso):
    monkeypatch.setattr(store, "_now_iso", lambda: iso)


def _save(user_id=1, strategy_id=STRATEGY, **overrides):
    return svc.save_document(user_id, strategy_id, {
        "title": "现金流复盘", "content": "价值投资关注经营现金流与安全边际。", **overrides,
    })


def _client(user_id=1, role="user"):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: UserRecord(user_id, f"user{user_id}", role, "", {})
    return TestClient(app)


def test_all_strategies_have_distinct_profiles_and_non_fetching_default_workspaces(monkeypatch):
    from app.services import strategy_knowledge_service

    monkeypatch.setattr(strategy_knowledge_service, "seed_sources", lambda: pytest.fail("read must not seed"))
    workspaces = [svc.get_workspace(1, strategy["id"]) for strategy in STRATEGIES]
    assert len(workspaces) == 16
    assert len({workspace["profile"]["lens"] for workspace in workspaces}) == 16
    for workspace in workspaces:
        assert workspace["profile"]["principles"]
        assert workspace["profile"]["suitable_for"]
        assert workspace["profile"]["not_suitable_for"]
        assert workspace["profile"]["evidence_focus"]
        assert workspace["profile"]["update_policy"]
        assert workspace["documents"] == []
        assert workspace["builtin_documents"][0]["origin"] == "methodology"
    assert not store.db_path().exists()
    assert not (svc.settings.data_dir / "strategy_knowledge.db").exists()


def test_private_document_crud_is_persistent_and_isolated_by_user_and_strategy():
    workspace = _save(tags=["现金流", "现金流", " 估值 "])
    doc = workspace["documents"][0]
    assert doc["tags"] == ["现金流", "估值"]
    assert doc["version"] == 1
    assert svc.get_workspace(2, STRATEGY)["documents"] == []
    assert svc.get_workspace(1, "quality_growth")["documents"] == []
    # A fresh SQLite connection reads the persisted data.
    assert store.list_documents(1, STRATEGY)[0]["id"] == doc["id"]
    for uid, strategy in [(2, STRATEGY), (1, "quality_growth")]:
        with pytest.raises(KeyError):
            svc.save_document(uid, strategy, {"title": "unauthorized", "content": "no"}, document_id=doc["id"])
        with pytest.raises(KeyError):
            svc.delete_document(uid, strategy, doc["id"])
    changed = svc.save_document(1, STRATEGY, {"title": "更新", "content": "新现金流反证"}, document_id=doc["id"])
    assert changed["documents"][0]["version"] == 2
    assert changed["knowledge_version"] != workspace["knowledge_version"]
    removed = svc.delete_document(1, STRATEGY, doc["id"])
    assert removed["documents"] == []
    with store.connect() as conn:
        revisions = conn.execute("SELECT version, deleted FROM strategy_document_revisions ORDER BY version").fetchall()
    assert [(row["version"], row["deleted"]) for row in revisions] == [(1, 0), (2, 0), (3, 1)]


def test_config_scope_validation_and_identical_save_stability():
    result = svc.save_config(1, STRATEGY, {"top_k": 2, "include_builtin": False, "max_age_days": 90})
    assert result["rag_config"]["version"] == 1
    assert svc.get_workspace(2, STRATEGY)["rag_config"]["top_k"] == 5
    assert svc.get_workspace(1, "quality_growth")["rag_config"]["top_k"] == 5
    again = svc.save_config(1, STRATEGY, {"top_k": 2, "include_builtin": False, "max_age_days": 90})
    assert again["knowledge_version"] == result["knowledge_version"]
    for payload in [{"top_k": 0}, {"top_k": 21}, {"top_k": True}, {"max_age_days": 0},
                    {"max_age_days": 3651}, {"enabled": "yes"}, {"unknown": True}]:
        with pytest.raises(ValidationError):
            svc.save_config(1, STRATEGY, payload)


@pytest.mark.parametrize("payload", [
    {"title": " ", "content": "valid"}, {"title": "valid", "content": " "},
    {"title": "x" * 161, "content": "valid"},
    {"title": "valid", "content": "x" * 20001},
    {"title": "valid", "content": "valid", "source_url": "javascript:alert(1)"},
    {"title": "valid", "content": "valid", "source_url": "https://user:pass@example.com"},
    {"title": "valid", "content": "valid", "published_at": "2024-2-01"},
    {"title": "valid", "content": "valid", "tags": ["x" * 41]},
])
def test_document_input_rejects_invalid_or_unsafe_fields(payload):
    with pytest.raises(ValidationError):
        svc.save_document(1, STRATEGY, payload)


def test_historical_retrieval_uses_known_revision_before_edit_delete_and_never_backdates(monkeypatch):
    _clock(monkeypatch, "2024-01-05T01:00:00+00:00")
    svc.save_config(1, STRATEGY, {"include_builtin": False})
    first = _save(content="原始现金流证据", published_at="2023-01-01")["documents"][0]
    _clock(monkeypatch, "2024-02-05T01:00:00+00:00")
    svc.save_document(1, STRATEGY, {"title": "新版", "content": "后来的现金流反证", "published_at": "2023-01-01"}, document_id=first["id"])
    _save(title="后来导入的旧报告", content="未来才知晓的现金流", published_at="2023-01-01")
    historical = svc.retrieve_strategy_knowledge(1, STRATEGY, query="现金流", as_of="2024-01-31")
    assert [doc["content"] for doc in historical["documents"]] == ["原始现金流证据"]
    assert historical["documents"][0]["version"] == 1
    _clock(monkeypatch, "2024-03-05T01:00:00+00:00")
    svc.delete_document(1, STRATEGY, first["id"])
    replay = svc.retrieve_strategy_knowledge(1, STRATEGY, query="现金流", as_of="2024-01-31")
    assert replay["documents"] == historical["documents"]
    assert replay["metadata"]["knowledge_version"] == historical["metadata"]["knowledge_version"]
    current = svc.retrieve_strategy_knowledge(1, STRATEGY, query="现金流", as_of="2024-04-01")
    assert [doc["title"] for doc in current["documents"]] == ["后来导入的旧报告"]
    before_created = svc.retrieve_strategy_knowledge(1, STRATEGY, query="现金流", as_of="2024-01-01")
    assert not [doc for doc in before_created["documents"] if doc["origin"] == "personal"]


def test_historical_cutoff_is_end_of_shanghai_day(monkeypatch):
    _clock(monkeypatch, "2024-01-01T15:59:59+00:00")
    _save(title="截止日内", content="日内现金流")
    _clock(monkeypatch, "2024-01-01T16:00:00+00:00")
    _save(title="上海次日", content="次日现金流")
    result = svc.retrieve_strategy_knowledge(1, STRATEGY, query="现金流", as_of="2024-01-01")
    assert [doc["title"] for doc in result["documents"] if doc["origin"] == "personal"] == ["截止日内"]


def test_retrieval_scoping_relevance_top_k_disable_and_background_default(monkeypatch):
    _clock(monkeypatch, "2024-01-01T00:00:00+00:00")
    svc.save_config(1, STRATEGY, {"include_builtin": False, "top_k": 1})
    _save(title="支付行业", content="支付收入增长和客户留存")
    _save(title="芯片订单", content="芯片订单持续改善")
    _save(2, title="别人的支付行业", content="支付收入")
    _save(1, "quality_growth", title="其他策略支付", content="支付收入")
    result = svc.retrieve_strategy_knowledge(1, STRATEGY, query="支付收入")
    assert len(result["documents"]) == 1
    assert result["documents"][0]["title"] == "支付行业"
    assert "支付" in result["documents"][0]["matched_terms"]
    assert svc.retrieve_strategy_knowledge(1, STRATEGY, query="unrelatedticker")["documents"] == []
    assert all(doc["origin"] != "personal" for doc in svc.retrieve_strategy_knowledge(None, STRATEGY)["documents"])
    svc.save_config(1, STRATEGY, {"enabled": False})
    assert svc.retrieve_strategy_knowledge(1, STRATEGY)["documents"] == []
    assert svc.retrieve_strategy_knowledge(None, STRATEGY)["config"]["enabled"] is True


def test_freshness_uses_publication_date_and_config_is_versioned(monkeypatch):
    _clock(monkeypatch, "2024-01-10T00:00:00+00:00")
    svc.save_config(1, STRATEGY, {"include_builtin": False, "max_age_days": 30})
    _save(title="旧报告", content="支付研究", published_at="2023-01-01")
    _save(title="新报告", content="支付研究", published_at="2024-01-09")
    _save(title="未来发布日期", content="支付研究", published_at="2024-03-01")
    result = svc.retrieve_strategy_knowledge(1, STRATEGY, query="支付", as_of="2024-01-20")
    assert [doc["title"] for doc in result["documents"]] == ["新报告"]
    assert result["metadata"]["stale_count"] == 1
    assert result["metadata"]["excluded_after_cutoff_count"] == 1
    _clock(monkeypatch, "2024-02-10T00:00:00+00:00")
    svc.save_config(1, STRATEGY, {"include_builtin": False, "enabled": False})
    assert svc.retrieve_strategy_knowledge(1, STRATEGY, as_of="2024-01-20")["config"]["version"] == 1
    assert svc.retrieve_strategy_knowledge(1, STRATEGY, as_of="2024-02-20")["documents"] == []


def test_shared_records_are_strategy_scoped_and_future_edit_not_replayed(monkeypatch):
    monkeypatch.setattr(shared, "_now_iso", lambda: "2024-01-01T00:00:00+00:00")
    shared.upsert_source({"id": "test-source", "title": "Source", "source_type": "web_article"})
    for strategy in ["价值投资", "质量成长"]:
        shared.upsert_item({"source_id": "test-source", "strategy": strategy, "title": strategy, "summary": "现金流证据"})
    first = svc.retrieve_strategy_knowledge(1, STRATEGY, query="现金流", as_of="2024-01-15")
    assert [doc["title"] for doc in first["documents"] if doc["origin"] == "builtin"] == ["价值投资"]
    # Failed checks are source bookkeeping, never new evidence or freshness.
    monkeypatch.setattr(shared, "_now_iso", lambda: "2024-03-01T00:00:00+00:00")
    shared.mark_source_checked("test-source", ok=False, error="unavailable")
    second = svc.retrieve_strategy_knowledge(1, STRATEGY, query="现金流", as_of="2024-01-15")
    assert second["metadata"]["knowledge_version"] == first["metadata"]["knowledge_version"]
    shared.upsert_item({"source_id": "test-source", "strategy": "价值投资", "title": "价值投资", "summary": "后来的反证"})
    replay = svc.retrieve_strategy_knowledge(1, STRATEGY, query="现金流", as_of="2024-01-15")
    assert not [doc for doc in replay["documents"] if doc["origin"] == "builtin"]
    assert replay["metadata"]["excluded_after_cutoff_count"] == 1


def test_prefer_recent_ranks_source_date_and_fingerprint_does_not_change_between_reads(monkeypatch):
    _clock(monkeypatch, "2024-01-01T00:00:00+00:00")
    svc.save_config(1, STRATEGY, {"include_builtin": False, "top_k": 1, "prefer_recent": True})
    _save(title="较新报告", content="现金流", published_at="2023-12-15")
    _clock(monkeypatch, "2024-01-10T00:00:00+00:00")
    _save(title="刚导入旧报告", content="现金流", published_at="2020-01-01")
    result = svc.retrieve_strategy_knowledge(1, STRATEGY, query="现金流")
    assert result["documents"][0]["title"] == "较新报告"
    assert result["metadata"]["knowledge_version"] == svc.retrieve_strategy_knowledge(1, STRATEGY, query="现金流")["metadata"]["knowledge_version"]


def test_api_auth_validation_crud_isolation_and_admin_refresh(monkeypatch):
    path = f"/api/transaction-agent/strategies/{STRATEGY}"
    anonymous_app = FastAPI()
    anonymous_app.include_router(router)
    monkeypatch.setattr(svc.settings, "auth_required", True)
    assert TestClient(anonymous_app).get(path).status_code == 401
    client = _client()
    assert client.get(path).status_code == 200
    assert client.get("/api/transaction-agent/strategies/not-a-strategy").status_code == 404
    assert client.put(path + "/rag", json={"top_k": 100}).status_code == 422
    response = client.post(path + "/knowledge", json={"title": "我的笔记", "content": "现金流观点"})
    assert response.status_code == 201
    doc = response.json()["documents"][0]
    assert _client(2).get(path).json()["documents"] == []
    assert _client(2).delete(path + "/knowledge/" + doc["id"]).status_code == 404
    assert client.put(path + "/knowledge/" + doc["id"], json={"title": "已更新", "content": "新反证"}).status_code == 200
    assert client.post(path + "/retrieve", json={"query": "现金流", "as_of": "not-date"}).status_code == 422
    assert client.delete(path + "/knowledge/" + doc["id"]).json()["documents"] == []
    assert client.post(path + "/refresh-public").status_code == 403

    async def refresh(**kwargs):
        assert kwargs == {"limit": 3, "strategy": "价值投资"}
        return {"sources_checked": 3, "items_upserted": 2, "errors": [{"source_id": "unavailable", "error": "offline"}]}

    monkeypatch.setattr("app.services.strategy_knowledge_service.refresh_public_sources", refresh)
    refreshed = _client(role="admin").post(path + "/refresh-public?limit=3")
    assert refreshed.status_code == 200
    assert refreshed.json()["refresh_result"]["sources_checked"] == 3
    assert refreshed.json()["profile"]["id"] == STRATEGY
