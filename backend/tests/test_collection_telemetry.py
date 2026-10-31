"""Network acquisition counters verified against the real isolated SQLite ledger."""
import asyncio
import copy
import json
import time
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest

from app.security.user_context import current_user_id
from app.services import runtime_telemetry


def events(operation, uid=1):
    return [row for row in runtime_telemetry.overview(uid, "all")["recent"] if row["operation"] == operation]


def market_bundle():
    return {"kline_source": "eastmoney", "price_basis": "raw", "volume_unit": "shares", "candles": [
        {"t": int(datetime.fromisoformat(day + "T00:00:00+08:00").timestamp()),
         "o": 10, "h": 12, "l": 9, "c": 11, "v": 1000}
        for day in ("2025-01-06", "2025-01-07")
    ]}


def test_market_counts_network_delta_not_merged_history_or_fetch_timestamps(tmp_path, monkeypatch):
    from app.services import kline_pipeline
    from app.services.research_data import ResearchData
    payload = market_bundle()

    async def fetch(*args):
        return copy.deepcopy(payload)

    monkeypatch.setattr(kline_pipeline, "fetch_a_share_kline_with_fallbacks", fetch)
    data = ResearchData(tmp_path)
    asyncio.run(data.fetch_history(7, "600000.SS"))
    assert events("market_history", 7)[0]["records_added"] == 2
    asyncio.run(data.fetch_history(7, "600000.SS"))
    repeated = events("market_history", 7)[0]
    assert repeated["records_received"] == 2 and repeated["records_added"] == 0
    assert repeated["bytes_stored"] > 0  # A new acquisition snapshot really was stored.
    payload["candles"] = [payload["candles"][-1]]
    payload["candles"][0]["c"] = 10.5
    output = asyncio.run(data.fetch_bundle(7, "600000.SS"))
    assert len(output["candles"]) == 2
    revision = events("market_history", 7)[0]
    assert (revision["records_received"], revision["records_added"]) == (1, 1)
    assert events("market_history", 8) == []


def test_market_manual_import_offline_and_local_file_are_not_network(tmp_path, monkeypatch):
    from app.services import kline_pipeline
    from app.services.research_data import ResearchData
    data = ResearchData(tmp_path)
    data.import_snapshot(1, {"600000.SS": market_bundle()}, metadata={})
    asyncio.run(data.fetch_history(1, "600000.SS", offline=True))
    assert events("market_history") == []

    async def local(*args):
        return {**market_bundle(), "kline_source": "free_stockdb_local"}

    monkeypatch.setattr(kline_pipeline, "fetch_a_share_kline_with_fallbacks", local)
    asyncio.run(data.fetch_history(1, "600000.SS"))
    assert events("market_history") == []


def test_market_failed_fetch_with_cache_records_only_failure_and_redacts(tmp_path, monkeypatch):
    from app.services import kline_pipeline
    from app.services.research_data import ResearchData
    from app.storage import kline_bundle_cache
    kline_bundle_cache.save_bundle("600000.SS", "5y", "1d", market_bundle())

    async def fail(*args):
        raise RuntimeError("https://provider.invalid/?api_key=secret-private")

    monkeypatch.setattr(kline_pipeline, "fetch_a_share_kline_with_fallbacks", fail)
    asyncio.run(ResearchData(tmp_path).fetch_history(1, "600000.SS"))
    rows = events("market_history")
    assert len(rows) == 1 and rows[0]["status"] == "failed"
    assert rows[0]["records_received"] == rows[0]["records_added"] == 0
    assert "secret-private" not in json.dumps(rows)


def article(content="公司新增生产订单"):
    return {"title": "某公司生产订单公告", "link": "https://example.org/news/1", "content": content,
            "source": "fixture", "published_ts": int(time.time()) - 3600, "fetched_at": time.time()}


def test_news_store_counts_committed_content_changes_without_emitting_collection():
    from app.storage import news_store
    row = article()
    counts = {}
    assert news_store.upsert_many([row], collection_stats=counts) == 1
    assert counts["records_added"] == 1
    news_store.upsert_many([{**row, "fetched_at": time.time() + 1}], collection_stats=counts)
    assert counts["records_added"] == 0
    revised = {**row, "content": "公司公告修订：原订单终止，原计划全部取消", "source_articles": [], "fetched_at": time.time() + 2}
    news_store.upsert_many([revised], collection_stats=counts)
    assert counts["records_added"] == 1
    assert events("news_archive") == []


def test_news_archive_counts_network_and_excludes_media_cache(monkeypatch):
    from app.services import news_feed_sync as sync
    from app.storage import news_store
    raw = article()

    async def rss(symbols, *, collection_stats, collector=None):
        collection_stats["records_received"] = 2
        return [copy.deepcopy(raw), copy.deepcopy(raw)]

    async def headlines(**kwargs):
        return {"items": [], "records_received": 0}

    async def media(**kwargs):
        return {"items": [{**article(), "title": "只在历史媒体缓存中的旧新闻"}],
                "network_items": [], "records_received": 0}

    monkeypatch.setattr(sync.rss_fetcher, "fetch_news_for_symbols", rss)
    monkeypatch.setattr(sync.cn_market_news, "fetch_cn_headlines", headlines)
    monkeypatch.setattr(sync.china_tech_media_feeds, "fetch_tech_media_feeds", media)
    asyncio.run(sync.sync_all_sources_to_archive(["600000.SS"]))
    first = events("news_archive")[0]
    assert first["shared"] and first["records_received"] == 2 and first["records_added"] == 1
    asyncio.run(sync.sync_all_sources_to_archive(["600000.SS"]))
    assert events("news_archive")[0]["records_added"] == 0
    rows, total = news_store.list_archive(scope="all")
    assert total == 1 and all("旧新闻" not in row["title"] for row in rows)


def test_news_store_rollback_cannot_report_added(monkeypatch):
    from app.storage import news_store
    original = news_store._write_article
    calls = []

    def fail_second(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise OSError("disk full")
        return original(*args, **kwargs)

    monkeypatch.setattr(news_store, "_write_article", fail_second)
    counts = {}
    with pytest.raises(OSError):
        news_store.upsert_many([article(), {**article(), "title": "完全不同的外部公告", "link": "https://example.org/news/2"}], collection_stats=counts)
    assert counts["records_added"] == 0
    assert news_store.list_archive(scope="all")[1] == 0


def test_company_refresh_counts_only_network_sections_and_real_changes(monkeypatch):
    from app.services import company_updates as updates
    monkeypatch.setattr(updates, "_fingerprint", lambda: "fixture")
    now = [int(time.time())]
    monkeypatch.setattr(updates.time, "time", lambda: now[0])
    fresh = {"company": {"name": "公司甲"}, "news": [{"title": "旧缓存新闻"}], "major_events": [],
             "sources": {"company": "东方财富", "news": "本地新闻库", "major_events": "雪球"},
             "section_errors": {"company": None, "news": "offline", "major_events": None},
             "stale_fields": ["news"]}
    monkeypatch.setattr(updates.xueqiu_pipeline, "run_company_bundle", lambda _: copy.deepcopy(fresh))
    updates.get_company_bundle("600000.SS")
    first = events("company_profile")[0]
    assert (first["records_received"], first["records_added"]) == (1, 1)
    assert first["bytes_stored"] > 0 and first["shared"]
    updates.get_company_bundle("600000.SS")
    assert len(events("company_profile")) == 1
    now[0] += 901
    updates.get_company_bundle("600000.SS")
    assert events("company_profile")[0]["records_added"] == 0
    now[0] += 901
    fresh["company"]["name"] = "公司乙"
    updates.get_company_bundle("600000.SS")
    assert events("company_profile")[0]["records_added"] == 1


@pytest.mark.parametrize("target", ["watchlist", "universe"])
def test_f10_refresh_actual_store_dedup_and_actor(target, monkeypatch):
    from app.routers import watchlist
    from app.services import company_universe_service as universe
    module = watchlist if target == "watchlist" else universe
    monkeypatch.setattr(module.company_profile_em, "fetch_company_survey", lambda _: ({"name": "公司甲", "intro": "资料"}, None))
    monkeypatch.setattr(module.a_share_stocks, "lookup_by_yahoo_symbol", lambda _: None)
    async def fetch():
        if target == "watchlist":
            await module._refresh_f10_only(["600000.SS"])
        else:
            await module.refresh_symbols(["600000.SS"], sleep_sec=0)
    token = current_user_id.set(7)
    try:
        asyncio.run(fetch())
        asyncio.run(fetch())
    finally:
        current_user_id.reset(token)
    rows = events("company_profile", 7)
    assert [row["records_added"] for row in rows] == [0, 1]
    assert all(row["records_received"] == 1 and not row["shared"] for row in rows)
    assert events("company_profile", 8) == []


def test_github_network_duplicates_and_changed_card_actual_ledger(monkeypatch):
    from app.services import research_generation as generation
    from app.storage import research_store
    original = httpx.AsyncClient
    body = ["# First source\nPublic methodology"]
    def respond(request):
        if request.url.host == "api.github.com":
            return httpx.Response(200, json=[{"sha": "a" * 40}])
        return httpx.Response(200, text=body[0])
    monkeypatch.setattr(generation.httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    monkeypatch.setattr(generation, "SOURCES", (("microsoft/qlib", "README.md", "实验记录"),))
    asyncio.run(generation.refresh_github_sources(7))
    asyncio.run(generation.refresh_github_sources(7))
    assert [row["records_added"] for row in events("github_source", 7)] == [0, 1]
    body[0] = "# Revised source\nUpdated public methodology"
    asyncio.run(generation.refresh_github_sources(7))
    assert len(research_store.list_entities(7, "knowledge")) == 2
    rows = events("github_source", 7)
    assert rows[0]["records_added"] == 1 and all(row["records_received"] == 1 for row in rows)
    assert rows[0]["bytes_stored"] > 0 and rows[1]["bytes_stored"] == 0


def test_failed_github_network_logs_sanitized_error(monkeypatch):
    from app.services import research_generation as generation
    original = httpx.AsyncClient
    def fail(request):
        raise httpx.ConnectError("secret-key https://user:password@host/?token=private", request=request)
    monkeypatch.setattr(generation.httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(fail), **kwargs))
    monkeypatch.setattr(generation, "SOURCES", (("microsoft/qlib", "README.md", "实验记录"),))
    asyncio.run(generation.refresh_github_sources(7))
    row = events("github_source", 7)[0]
    assert row["status"] == "failed" and row["records_added"] == row["records_received"] == 0
    assert "secret-key" not in json.dumps(row) and "password" not in json.dumps(row)


def test_standalone_media_counts_network_not_retained_cache_and_dedups(monkeypatch):
    from app.services import china_tech_media_feeds as media
    from app.storage import tech_media_store
    raw = {**article(), "id": "fresh-media"}
    tech_media_store.upsert_and_prune([{**raw, "id": "cached-media", "title": "历史缓存"}])
    available = [True]
    async def fetch(*args):
        return [copy.deepcopy(raw)] if available[0] else []
    monkeypatch.setattr(media, "TECH_MEDIA_FEEDS", [("fixture", "https://example.org/rss", "https://example.org")])
    monkeypatch.setattr(media, "_fetch_one", fetch)
    first = asyncio.run(media.fetch_tech_media_feeds())
    assert len(first["items"]) == 2 and len(first["network_items"]) == 1
    assert (events("tech_news")[0]["records_received"], events("tech_news")[0]["records_added"]) == (1, 1)
    asyncio.run(media.fetch_tech_media_feeds())
    assert events("tech_news")[0]["records_added"] == 0
    available[0] = False
    fallback = asyncio.run(media.fetch_tech_media_feeds())
    assert len(fallback["items"]) == 2
    assert events("tech_news")[0]["records_received"] == events("tech_news")[0]["records_added"] == 0
    before = len(events("tech_news"))
    asyncio.run(media.fetch_tech_media_feeds(record_activity=False))
    assert len(events("tech_news")) == before


def test_media_storage_failure_keeps_received_without_claiming_added(monkeypatch):
    from app.services import china_tech_media_feeds as media
    async def fetch(*args):
        return [{**article(), "id": "fresh-media"}]
    def fail(*args, **kwargs):
        raise OSError("disk full private details")
    monkeypatch.setattr(media, "TECH_MEDIA_FEEDS", [("fixture", "https://example.org/rss", "https://example.org")])
    monkeypatch.setattr(media, "_fetch_one", fetch)
    monkeypatch.setattr(media.tech_media_store, "upsert_and_prune", fail)
    with pytest.raises(OSError):
        asyncio.run(media.fetch_tech_media_feeds())
    row = events("tech_news")[0]
    assert row["status"] == "failed" and (row["records_received"], row["records_added"]) == (1, 0)
    assert "private" not in json.dumps(row)


def test_standalone_headlines_count_before_dedup_and_can_delegate_event(monkeypatch):
    from app.services import cn_market_news as headlines
    raw = article()
    async def maintenance():
        return {}
    async def gather(*args, **kwargs):
        return [[raw, copy.deepcopy(raw)]], {"fixture": 2}
    async def attach(items):
        pass
    monkeypatch.setattr(headlines, "run_daily_maintenance_if_due", maintenance)
    monkeypatch.setattr(headlines, "write_override_template", lambda: None)
    monkeypatch.setattr(headlines, "sources_for_fetch", lambda **kwargs: [])
    monkeypatch.setattr(headlines, "_gather_from_rows", gather)
    monkeypatch.setattr(headlines, "_attach_related_entity_labels", attach)
    result = asyncio.run(headlines.fetch_cn_headlines())
    assert len(result["items"]) == 1
    assert events("news_archive")[0]["records_received"] == 2
    asyncio.run(headlines.fetch_cn_headlines(record_activity=False))
    assert len(events("news_archive")) == 1


def test_plain_feed_records_once_and_respects_no_save(monkeypatch):
    from app.routers import news
    raw = article()
    async def fetch(symbols, *, collection_stats):
        collection_stats["records_received"] = 2
        return [copy.deepcopy(raw)]
    monkeypatch.setattr(news.rss_fetcher, "fetch_news_for_symbols", fetch)
    asyncio.run(news.news_feed(SimpleNamespace(id=7), symbols="600000.SS", save=True))
    asyncio.run(news.news_feed(SimpleNamespace(id=7), symbols="600000.SS", save=True))
    asyncio.run(news.news_feed(SimpleNamespace(id=7), symbols="600000.SS", save=False))
    rows = events("news_archive", 7)
    assert [row["records_added"] for row in rows] == [0, 0, 1]
    assert all(row["records_received"] == 2 for row in rows)
