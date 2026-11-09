import asyncio

import httpx
import pytest

from app.services import strategy_knowledge_service as service
from app.storage import strategy_knowledge_store as store


VALUE = "价值投资"
GROWTH = "质量成长"


def source(source_id, tags, *, priority=50, last_checked_at=None):
    return {
        "id": source_id,
        "title": source_id,
        "url": f"https://knowledge.example/{source_id}",
        "source_type": "research_article",
        "strategy_tags": tags,
        "priority": priority,
        "last_checked_at": last_checked_at,
    }


def mock_http(monkeypatch, handler):
    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        service.httpx,
        "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(handler), **kwargs),
    )


def test_scoped_refresh_seeds_fetches_and_synthesizes_only_requested_strategy(monkeypatch):
    monkeypatch.setattr(service, "PUBLIC_SOURCES", [
        source("shared", [VALUE, GROWTH]),
        source("growth_only", [GROWTH], priority=99),
    ])
    monkeypatch.setattr(service, "BOOK_NOTES", [
        {"source_id": "shared", "strategy": VALUE, "title": "value note", "summary": "value"},
        {"source_id": "growth_only", "strategy": GROWTH, "title": "growth note", "summary": "growth"},
    ])
    # Existing knowledge for the other strategy must survive a shared-source fetch.
    store.upsert_source(source("shared", [VALUE, GROWTH]))
    store.upsert_item({
        "source_id": "shared", "strategy": GROWTH, "title": "shared", "summary": "existing growth",
    })
    growth_before = store.query_strategy(GROWTH)
    urls = []

    def handler(request):
        urls.append(str(request.url))
        return httpx.Response(200, text="<article>Value investing and quality cash flow.</article>")

    mock_http(monkeypatch, handler)
    result = asyncio.run(service.refresh_public_sources(strategy=VALUE, limit=10))

    assert result["strategy"] == VALUE
    assert result["sources_checked"] == 1
    assert result["items_upserted"] == 1
    assert result["synthesized_strategies"] == 1
    assert urls == ["https://knowledge.example/shared"]
    assert store.query_strategy(GROWTH) == growth_before
    assert "growth_only" not in {s["id"] for s in store.list_sources()}
    assert {item["title"] for item in store.query_strategy(VALUE)} == {
        "value note", "shared", "价值投资 强化规则摘要",
    }


def test_unknown_strategy_rejected_before_any_storage_or_network(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("invalid strategy must not seed, fetch or start a run")

    monkeypatch.setattr(service, "seed_sources", unexpected)
    monkeypatch.setattr(store, "start_refresh_run", unexpected)
    monkeypatch.setattr(service.httpx, "AsyncClient", unexpected)
    with pytest.raises(ValueError, match="未知策略"):
        asyncio.run(service.refresh_public_sources(strategy="unknown"))


def test_refresh_rotates_oldest_checked_before_priority(monkeypatch):
    monkeypatch.setattr(service, "PUBLIC_SOURCES", [
        source("recent_priority", [VALUE], priority=99, last_checked_at="2026-11-01T00:00:00+00:00"),
        source("old_low_priority", [VALUE], priority=1, last_checked_at="2026-10-01T00:00:00+00:00"),
        source("never_checked", [VALUE], priority=1),
    ])
    monkeypatch.setattr(service, "BOOK_NOTES", [])
    monkeypatch.setattr(store, "_now_iso", lambda: "2026-11-08T00:00:00+00:00")
    urls = []

    def handler(request):
        urls.append(request.url.path)
        return httpx.Response(200, text="Value investing")

    mock_http(monkeypatch, handler)
    for _ in range(3):
        asyncio.run(service.refresh_public_sources(strategy=VALUE, limit=1))

    assert urls == ["/never_checked", "/old_low_priority", "/recent_priority"]


def test_reseeding_unchanged_content_preserves_freshness(monkeypatch):
    times = {"now": "2026-10-01T00:00:00+00:00"}
    monkeypatch.setattr(store, "_now_iso", lambda: times["now"])
    monkeypatch.setattr(service, "PUBLIC_SOURCES", [source("book", [VALUE])])
    note = {"source_id": "book", "strategy": VALUE, "title": "notes", "summary": "original"}
    monkeypatch.setattr(service, "BOOK_NOTES", [note])
    service.seed_sources()
    source_before = store.list_sources()[0]
    item_before = store.query_strategy(VALUE)[0]

    times["now"] = "2026-11-08T00:00:00+00:00"
    service.seed_sources()
    service.query_strategy_knowledge(VALUE)
    service.status()
    assert store.list_sources()[0] == source_before
    assert store.query_strategy(VALUE)[0] == item_before

    # A real content revision advances its modification date, not its first collection date.
    note["summary"] = "revised"
    service.seed_sources()
    updated = store.query_strategy(VALUE)[0]
    assert updated["summary"] == "revised"
    assert updated["updated_at"] == times["now"]
    assert updated["collected_at"] == item_before["collected_at"]


@pytest.mark.parametrize("status_code,body", [(503, "unavailable"), (200, "<script>nothing</script>")])
def test_failed_refresh_retains_last_successful_knowledge(monkeypatch, status_code, body):
    monkeypatch.setattr(service, "PUBLIC_SOURCES", [source("available_before", [VALUE])])
    monkeypatch.setattr(service, "BOOK_NOTES", [])
    times = {"now": "2026-10-01T00:00:00+00:00"}
    monkeypatch.setattr(store, "_now_iso", lambda: times["now"])
    service.seed_sources()
    store.upsert_item({
        "source_id": "available_before", "strategy": VALUE, "title": "available_before",
        "summary": "last successful snapshot", "content_excerpt": "cash flow and value",
    })
    before = store.query_strategy(VALUE)[0]
    times["now"] = "2026-11-08T00:00:00+00:00"
    mock_http(monkeypatch, lambda request: httpx.Response(status_code, text=body))

    result = asyncio.run(service.refresh_public_sources(strategy=VALUE))

    retained = next(i for i in store.query_strategy(VALUE) if i["source_id"] == "available_before")
    assert retained == before
    assert result["sources_checked"] == 1
    assert result["items_upserted"] == 0
    assert result["errors"][0]["source_id"] == "available_before"
    assert result["recent_runs"][0]["status"] == "warning"
    checked = next(s for s in store.list_sources() if s["id"] == "available_before")
    assert checked["status"] == "error"
    assert checked["last_checked_at"] == times["now"]


def test_global_refresh_still_updates_all_source_strategies(monkeypatch):
    monkeypatch.setattr(service, "PUBLIC_SOURCES", [source("shared", [VALUE, GROWTH])])
    monkeypatch.setattr(service, "BOOK_NOTES", [])
    mock_http(monkeypatch, lambda request: httpx.Response(200, text="Value and quality investing"))

    result = asyncio.run(service.refresh_public_sources())

    assert result["items_upserted"] == 2
    assert result["synthesized_strategies"] == len(service._strategy_sources())
    for strategy in (VALUE, GROWTH):
        assert any(item["source_id"] == "shared" for item in store.query_strategy(strategy))
