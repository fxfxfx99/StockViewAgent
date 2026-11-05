"""Archive freshness checks stay local and bounded after source synchronization."""
import json

from app.services import news_relevance_knowledge as knowledge, prompt_registry, stock_features
from app.storage import knowledge_cache_store as store


def test_repeated_signature_checks_read_no_prompt_or_feature_document_bodies(tmp_path, monkeypatch):
    (tmp_path / "watchlist_profiles.json").write_text(json.dumps({
        f"{index:06}.SS": {"manual": {"main_business": "示例公司材料" * 200}} for index in range(1000)
    }), encoding="utf-8")
    signature = knowledge.current_analysis_signature("600519.SS")

    def prohibited(*args, **kwargs):
        raise AssertionError("hot freshness check must not reread document bodies")

    monkeypatch.setattr(store, "get_document", prohibited)
    monkeypatch.setattr(stock_features, "get_snapshot", prohibited)
    monkeypatch.setattr(knowledge, "_validate_source_json", prohibited)
    for _ in range(20):
        assert knowledge.current_analysis_signature("600519.SS") == signature


def test_prompt_activation_is_visible_without_waiting_for_cache_expiry():
    first = prompt_registry.get_prompt("news_relevance")
    initial_signature = knowledge.current_analysis_signature("600519.SS")
    edited = prompt_registry.update_prompt("news_relevance", "新的核查规则")
    changed_signature = knowledge.current_analysis_signature("600519.SS")
    assert changed_signature != initial_signature
    prompt_registry.activate_prompt("news_relevance", first["revision_id"], expected_revision=edited["revision_id"])
    restored_signature = knowledge.current_analysis_signature("600519.SS")
    assert restored_signature not in {initial_signature, changed_signature}
    # Restoring creates a new audited revision; its actual prompt text equals the old version.
    assert prompt_registry.get_prompt("news_relevance")["content"] == first["content"]
