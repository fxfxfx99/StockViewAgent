import pytest

from app.services import news_relevance_knowledge as knowledge, news_watchlist_matcher as matcher, prompt_registry, stock_features
from app.storage import integrations_store, profile_store, knowledge_cache_store as store

SYMBOL = "600519.SS"


def test_knowledge_is_stock_scoped_and_changes_apply_to_actual_matching():
    profile_store.patch_manual(SYMBOL, {"name": "特定测试公司", "industry": "半导体"})
    integrations_store.merge_integrations({"secret_token": "never-copy-this", "news": {"match_keywords": {SYMBOL: ["独有产品"]}}})
    document = knowledge.ensure_stock_knowledge(SYMBOL)
    assert document["content"]["aliases"][0]["source"] == "manual_profile"
    assert document["content"]["configured_keywords"][0]["status"] == "candidate"
    assert "never-copy-this" not in str(document)
    assert matcher.article_match_evidence({"title": "半导体行业报告"}, [SYMBOL, "AAPL"]) == {}
    assert set(matcher.article_match_evidence({"title": "独有产品发布"}, [SYMBOL, "AAPL"])) == {SYMBOL}
    integrations_store.merge_integrations({"news": {"match_keywords": {SYMBOL: ["新款产品"]}}})
    assert matcher.article_match_evidence({"title": "独有产品发布"}, [SYMBOL]) == {}
    assert matcher.article_match_evidence({"title": "新款产品发布"}, [SYMBOL])
    assert len(store.history("news_relevance", SYMBOL)) == 2


def test_feature_scopes_require_same_stock_sources_and_historical_version(monkeypatch):
    identity = {"stock_code": SYMBOL, "version_id": "feature-v2", "content_hash": "feature-hash"}
    snapshot = {**identity, "created_at": "2026-10-01T00:00:00Z",
                "evidence_sources": [{"source_id": "valid", "stock_code": SYMBOL, "title": "公司年报"},
                                     {"source_id": "wrong", "stock_code": "AAPL", "title": "另一家公司"}],
                "features": [{"id": "cost", "value": "原材料为铜箔", "status": "reported", "source_ids": ["valid"]},
                             {"id": "wrong", "value": "原油成本", "status": "reported", "source_ids": ["wrong"]}],
                "factor_links": [{"factor_id": "input_cost", "feature_ids": ["cost"], "scope_terms": ["铜箔"], "event_keywords": ["涨价"]},
                                 {"factor_id": "bad", "feature_ids": ["wrong"], "scope_terms": ["原油"]}]}
    monkeypatch.setattr(stock_features, "get_snapshot_identity", lambda symbol: identity)
    monkeypatch.setattr(stock_features, "get_snapshot", lambda symbol: snapshot)
    document = knowledge.ensure_stock_knowledge(SYMBOL)
    scopes = document["content"]["business_scopes"]
    assert len(scopes) == 1 and scopes[0]["term"] == "铜箔" and scopes[0]["status"] == "candidate"
    assert scopes[0]["feature_statuses"]["cost"] == "reported"
    assert matcher.article_match_evidence({"title": "铜箔涨价"}, [SYMBOL], {}) == {}
    documents = {SYMBOL: document}
    assert knowledge.context_from_snapshot(documents, {SYMBOL: {"version_id": "feature-v1", "features": [{"feature_id": "cost"}]}})[SYMBOL]["business_scopes"] == []
    assert knowledge.context_from_snapshot(documents, {SYMBOL: {"version_id": "feature-v2", "features": [{"feature_id": "cost"}]}})[SYMBOL]["business_scopes"] == scopes


def test_logic_signature_changes_for_prompt_keywords_but_never_other_stock_or_secrets(monkeypatch):
    before = knowledge.current_analysis_signature(SYMBOL)
    assert knowledge.analysis_logic_current({}, {"stock_code": SYMBOL, "analysis_logic_signature": before})
    assert not knowledge.analysis_logic_current({}, {"stock_code": SYMBOL})
    integrations_store.merge_integrations({"secret_token": "rotated"})
    assert knowledge.current_analysis_signature(SYMBOL) == before
    integrations_store.merge_integrations({"news": {"match_keywords": {"AAPL": ["iPhone"]}}})
    assert knowledge.current_analysis_signature(SYMBOL) == before
    integrations_store.merge_integrations({"news": {"match_keywords": {SYMBOL: ["公司产品"]}}})
    changed = knowledge.current_analysis_signature(SYMBOL)
    assert changed != before
    prompt_registry.update_prompt("news_relevance", "新的相关性核查规则")
    assert knowledge.current_analysis_signature(SYMBOL) != changed
    # Once synchronized, signature validation may read identities but no large feature bodies.
    monkeypatch.setattr(stock_features, "get_snapshot", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("full snapshot read")))
    assert knowledge.current_analysis_signature(SYMBOL)


def test_corrupt_source_does_not_erase_existing_stock_knowledge(tmp_path):
    integrations_store.merge_integrations({"news": {"match_keywords": {SYMBOL: ["原有产品"]}}})
    previous = knowledge.ensure_stock_knowledge(SYMBOL)
    (tmp_path / "integrations.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError, match="保留已有知识版本"):
        knowledge.ensure_stock_knowledge(SYMBOL)
    assert store.get_head("news_relevance", SYMBOL)["revision_id"] == previous["metadata"]["revision_id"]
    assert store.get_document("news_relevance", SYMBOL)["content"]["configured_keywords"][0]["term"] == "原有产品"


def test_collector_alias_cache_notices_profile_edits_immediately(monkeypatch):
    monkeypatch.setattr(matcher.watchlist_store, "load_all_symbols_union", lambda: [SYMBOL])
    profile_store.patch_manual(SYMBOL, {"name": "公司旧名称"})
    assert matcher.enrich_items_with_matches([{"title": "公司旧名称公告"}])[0]["matched_symbols"] == [SYMBOL]
    profile_store.patch_manual(SYMBOL, {"name": "公司新名称"})
    rows = matcher.enrich_items_with_matches([{"title": "公司旧名称公告"}, {"title": "公司新名称公告"}])
    assert rows[0]["matched_symbols"] == []
    assert rows[1]["matched_symbols"] == [SYMBOL]
