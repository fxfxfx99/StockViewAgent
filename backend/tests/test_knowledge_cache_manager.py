"""Local maintenance projections and bounded worker behavior without providers."""
import asyncio
import json

import pytest

from app.config import settings
from app.services import knowledge_cache_manager as manager
from app.storage import knowledge_cache_jobs as jobs, knowledge_cache_store as cache


SYMBOL = "600519.SS"


def snapshot():
    return {"schema_version": 1, "stock_code": SYMBOL, "name": "贵州茅台", "version_id": "source-version-1",
            "content_hash": "source-hash", "as_of": "2026-11-04T00:00:00+00:00", "created_at": "2026-11-04T00:00:00+00:00",
            "coverage": {"status": "partial"},
            "features": [{"id": "business", "label": "主营业务", "value": "白酒", "source_ids": ["source-1"],
                          "source_text": "duplicated long prose", "evidence": ["duplicated evidence"]}],
            "factor_links": [{"factor": "白酒需求", "direction": "positive", "evidence": "duplicated source text"}],
            "evidence_sources": [{"source_id": "source-1", "title": "年报", "url": "https://example.org/report", "text": "证据" * 1000}],
            "stats": {"market_behavior": {"volatility": 0.2}, "event_reactions": {"count": 23, "items": [{"many": "prices"}]}}}


def test_compact_stock_sync_preserves_values_and_source_identity_without_large_repeated_data(monkeypatch):
    original = snapshot()
    monkeypatch.setattr(manager.stock_features, "get_snapshot", lambda symbol: original)
    monkeypatch.setattr(manager.stock_features, "refresh", lambda *a, **k: pytest.fail("existing source should not refresh"))
    result = manager.sync_stock("SH600519", reason="手动同步", actor_id=4)
    loaded = cache.get_document("stock_features", SYMBOL)
    content = loaded["content"]
    assert content["features"][0]["value"] == "白酒"
    assert content["features"][0]["source_ids"] == ["source-1"]
    assert "source_text" not in content["features"][0]
    assert "evidence" not in content["factor_links"][0]
    assert len(content["evidence_sources"][0]["excerpt"]) == 600
    assert "items" not in content["stats"]["event_reactions"]
    assert content["source_record"]["version_id"] == original["version_id"]
    assert result["provenance"]["source"] == "stock_features.sqlite3"
    assert result["provenance"]["source_version"] == original["version_id"]
    assert original["stats"]["event_reactions"]["items"]  # Does not mutate source evidence.
    assert manager.sync_stock(SYMBOL)["revision_id"] == result["revision_id"]


def test_missing_feature_source_uses_refresh_and_failure_does_not_replace_old_document(monkeypatch):
    first = cache.put_document("stock_features", SYMBOL, {"existing": "valid"})
    monkeypatch.setattr(manager.stock_features, "get_snapshot", lambda symbol: None)
    monkeypatch.setattr(manager.stock_features, "refresh", lambda symbol, **kwargs: {"snapshot": None})
    with pytest.raises(ValueError, match="尚无"):
        manager.sync_stock(SYMBOL)
    assert cache.get_head("stock_features", SYMBOL)["revision_id"] == first["revision_id"]
    def refresh(symbol, **kwargs):
        assert kwargs == {"sync_knowledge": False}
        return {"snapshot": snapshot()}
    monkeypatch.setattr(manager.stock_features, "refresh", refresh)
    assert manager.sync_stock(SYMBOL)["revision_id"] != first["revision_id"]


def test_manager_validates_scope_and_normalizes_requested_stock(monkeypatch):
    monkeypatch.setattr(manager, "_symbols", lambda: [SYMBOL, "000001.SZ"])
    result = manager.enqueue("all", "SH600519", actor_id=9)
    assert result["job"]["symbol"] == SYMBOL and result["job"]["total"] == 2
    claimed = jobs.claim()
    assert [item["symbol"] for item in jobs.pending(claimed)] == ["__prompts__", SYMBOL]
    with pytest.raises(ValueError):
        manager.enqueue("invalid")
    with pytest.raises(ValueError, match="全局"):
        manager.enqueue("prompts", SYMBOL)
    with pytest.raises(ValueError):
        manager.enqueue("stocks", "../../private")


def test_run_batch_waits_for_resources_without_claiming_or_processing(monkeypatch):
    queued = jobs.enqueue("stocks", SYMBOL, [SYMBOL])
    monkeypatch.setattr(manager.stock_features, "_resource_ready", lambda: False)
    monkeypatch.setattr(manager, "_process", lambda *a: pytest.fail("must not process under pressure"))
    result = asyncio.run(manager.run_batch(limit=2))
    assert result == {"status": "resource_wait", "processed": 0}
    state = jobs.status()["active_job"]
    assert state["id"] == queued["job"]["id"] and state["status"] == "queued"
    assert state["started_at"] is None


def test_run_batch_progress_resumes_and_failure_keeps_previous_valid_head(monkeypatch):
    first = cache.put_document("stock_features", SYMBOL, {"stable": True})
    symbols = ["000001.SZ", SYMBOL, "AAPL", "MSFT", "TSLA"]
    jobs.enqueue("stocks", None, symbols, reason="批量测试")
    monkeypatch.setattr(manager.stock_features, "_resource_ready", lambda: True)
    processed = []
    def sync(symbol, **kwargs):
        processed.append(symbol)
        if symbol == SYMBOL:
            raise RuntimeError("private token must not reach audit")
        return cache.put_document("stock_features", symbol, {"symbol": symbol})
    monkeypatch.setattr(manager, "sync_stock", sync)
    first_batch = asyncio.run(manager.run_batch(limit=2))
    assert first_batch["processed"] == 2
    state = jobs.status()["active_job"]
    assert (state["status"], state["completed"], state["failed"]) == ("queued", 2, 1)
    assert cache.get_head("stock_features", SYMBOL)["revision_id"] == first["revision_id"]
    second_batch = asyncio.run(manager.run_batch(limit=2))
    third_batch = asyncio.run(manager.run_batch(limit=2))
    assert second_batch["processed"] == 2 and third_batch["processed"] == 1
    assert processed == symbols
    assert jobs.status()["active_job"] is None
    assert jobs.status()["recent_jobs"][0]["status"] == "partial"
    audit = cache.list_events("stock_features", SYMBOL)
    assert audit["items"][0]["event_type"] == "sync_failed"
    assert "private token" not in json.dumps(audit)
    assert asyncio.run(manager.run_batch()) == {"status": "idle", "processed": 0}


def test_run_batch_releases_lease_when_processing_is_interrupted(monkeypatch):
    jobs.enqueue("stocks", SYMBOL, [SYMBOL])
    monkeypatch.setattr(manager.stock_features, "_resource_ready", lambda: True)
    def interrupted(*args):
        raise KeyboardInterrupt()
    monkeypatch.setattr(manager, "_process", interrupted)
    # durable_call deliberately propagates BaseException after draining its thread.
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(manager.run_batch(limit=1))
    assert jobs.status()["active_job"]["status"] == "queued"
    assert jobs.status()["active_job"]["completed"] == 0


def test_document_diff_uses_true_preceding_version_even_when_older_than_200():
    revisions = [cache.put_document("prompts", "system_prompt", str(i), format="markdown")["revision_id"] for i in range(205)]
    loaded = manager.document("prompts", "system_prompt", revisions[2])
    assert loaded["diff"]["from_revision"] == revisions[1]
    assert loaded["diff"]["to_revision"] == revisions[2]
    assert "-1" in loaded["diff"]["text"] and "+2" in loaded["diff"]["text"]
    with pytest.raises(FileNotFoundError):
        manager.document("prompts", "unknown")


def test_expired_or_replaced_owner_cannot_publish_and_new_owner_resumes(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(jobs.time, "time", lambda: clock[0])
    original = cache.put_document("stock_features", SYMBOL, {"original": True})
    jobs.enqueue("stocks", SYMBOL, [SYMBOL], actor_id=4)
    old = jobs.claim()
    item = jobs.pending(old)[0]
    writes = []
    def sync(symbol, **kwargs):
        writes.append(symbol)
        return cache.put_document("stock_features", symbol, {"new": True})
    monkeypatch.setattr(manager, "sync_stock", sync)
    clock[0] = 1601.0
    assert not manager._process(old, item)  # Expired even before a new claim.
    assert writes == []
    newer = jobs.claim()
    assert not manager._process(old, item)  # No resurrection after reassignment.
    assert cache.get_head("stock_features", SYMBOL)["revision_id"] == original["revision_id"]
    assert manager._process(newer, item)
    jobs.release(newer)
    assert writes == [SYMBOL]
    assert cache.get_document("stock_features", SYMBOL)["content"] == {"new": True}
    assert jobs.status()["recent_jobs"][0]["status"] == "completed"


def test_all_scope_rechecks_owner_between_stock_and_relevance_publications(monkeypatch):
    from app.services import news_relevance_knowledge
    clock = [1000.0]
    monkeypatch.setattr(jobs.time, "time", lambda: clock[0])
    jobs.enqueue("all", SYMBOL, [SYMBOL], actor_id=8)
    first = jobs.claim()
    item = jobs.pending(first)[0]
    newer = []
    def lose_lease_during_stock(*args, **kwargs):
        clock[0] = 1601.0
        newer.append(jobs.claim())
    relevance_calls = []
    monkeypatch.setattr(manager, "sync_stock", lose_lease_during_stock)
    monkeypatch.setattr(news_relevance_knowledge, "ensure_stock_knowledge", lambda symbol, **kwargs: relevance_calls.append((symbol, kwargs)))
    assert not manager._process(first, item)
    assert relevance_calls == []
    assert jobs.status()["active_job"]["completed"] == 0
    monkeypatch.setattr(manager, "sync_stock", lambda *a, **k: None)
    assert manager._process(newer[0], item)
    assert relevance_calls == [(SYMBOL, {"force": True, "actor_id": 8})]
    jobs.release(newer[0])
    assert jobs.status()["recent_jobs"][0]["status"] == "completed"
