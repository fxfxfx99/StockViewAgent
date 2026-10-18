import asyncio
from concurrent.futures import ThreadPoolExecutor
from email.utils import formatdate
import time

import httpx
import pytest

from app.security.user_context import current_user_id
from app.services import source_autopilot as svc
from app.storage.research_store import ResearchStore


def bars():
    return {"candles": [{"t": 1700000000, "o": 10, "h": 12, "l": 9, "c": 11, "v": 1000}]}


def feed(now):
    date = formatdate(now - 3600, usegmt=True)
    return (f'<rss version="2.0"><channel><title>Finance</title>'
            f'<item><title>A</title><link>https://finance.sina.com.cn/a</link><pubDate>{date}</pubDate></item>'
            f'<item><title>B</title><link>https://finance.sina.com.cn/b</link><pubDate>{date}</pubDate></item>'
            '</channel></rss>').encode()


def test_routing_quarantine_recovery_and_account_isolation():
    chain = ["eastmoney", "tencent", "sina"]
    assert svc.ordered_sources("daily", chain, user_id=1, now=10000) == chain
    svc.record_outcome("daily", "tencent", ok=True, user_id=1, now=10000)
    assert svc.ordered_sources("daily", chain, user_id=1, now=10001)[0] == "tencent"
    for _ in range(3):
        result = svc.record_outcome("daily", "tencent", ok=False, user_id=1, now=10010)
    assert "tencent" not in svc.ordered_sources("daily", chain, user_id=1, now=10011)
    assert svc.ordered_sources("daily", chain, user_id=2, now=10011) == chain
    assert svc.ordered_sources("minute", chain, user_id=1, now=10011) == chain
    assert svc.ordered_sources("daily", chain, user_id=1, now=result["retry_at"])[0] == "tencent"
    svc.record_outcome("daily", "tencent", ok=True, user_id=1, now=result["retry_at"])
    assert svc.ordered_sources("daily", ["sina"], user_id=1) == ["sina"]  # cannot enable a removed source
    changes = ResearchStore().list_entities(1, "source_change")
    assert {r["payload"]["action"] for r in changes} >= {"route_updated", "cooldown", "recovered"}


def test_concurrent_health_updates_are_atomic_and_errors_redacted():
    def fail(_):
        svc.record_outcome("daily", "eastmoney", ok=False, user_id=7, reason="api_key=SECRET")
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(fail, range(12)))
    state = ResearchStore().get_state(7, svc._HEALTH_KEY)
    assert state["daily:eastmoney"]["failures"] == 12
    assert "SECRET" not in str(svc.summary(7))


@pytest.mark.parametrize("rows", [[], [{"t": 1}], [dict(t=1,o=10,h=9,l=8,c=10,v=1)],
                                [dict(t=1,o=10,h=10,l=10,c=10,v=float('nan'))]])
def test_reject_invalid_market_response(rows):
    with pytest.raises(ValueError):
        svc.validate_kline_bundle({"candles": rows})


def test_pipeline_rejects_broken_success_and_uses_healthy_fallback(monkeypatch):
    from app.services import kline_pipeline as pipeline
    monkeypatch.setattr(pipeline.market_source_policy, "kline_daily_chain", lambda: ["eastmoney", "tencent"])
    async def broken(*_):
        return {"candles": []}
    async def enrich(out, *args):
        return out
    monkeypatch.setattr(pipeline.eastmoney_market, "fetch_kline_bundle", broken)
    monkeypatch.setattr(pipeline.market_extra_http, "fetch_kline_bundle_secondary_sync", lambda *_: bars())
    monkeypatch.setattr(pipeline, "_attach_source_and_enrich", enrich)
    result = asyncio.run(pipeline.fetch_a_share_kline_with_fallbacks("600519.SS", "1mo", "1d"))
    assert result == bars()
    state = ResearchStore().get_state(1, svc._HEALTH_KEY)
    assert state["daily:eastmoney"]["ok"] is False
    assert state["daily:tencent"]["ok"] is True


def test_feed_discovery_rejects_foreign_private_and_script_urls():
    links = ["/feed.xml", "https://sina.com.cn.evil.test/rss", "http://127.0.0.1/rss",
             "javascript:alert(1)", "https://finance.sina.com.cn:8000/rss", "https://key@finance.sina.com.cn/rss"]
    html = ''.join(f'<link rel="alternate" type="application/rss+xml" href="{url}">' for url in links)
    assert svc.discover_feed_links(html.encode(), "https://finance.sina.com.cn/") == ["https://finance.sina.com.cn/feed.xml"]


def test_feed_needs_recent_timestamp_and_valid_articles():
    now = time.time()
    assert svc.validate_feed(feed(now), now=now) == 2
    for document in (b'<html><h1>news</h1></html>', feed(now - 60 * 86400)):
        with pytest.raises(ValueError):
            svc.validate_feed(document, now=now)


def test_dns_and_redirect_checks_block_local_network(monkeypatch):
    async def private_lookup(*args, **kwargs):
        return [(None, None, None, None, ("127.0.0.1", 443))]
    monkeypatch.setattr(svc.asyncio, "to_thread", private_lookup)
    with pytest.raises(ValueError, match="本地网络"):
        asyncio.run(svc._public_dns("https://finance.sina.com.cn/"))
    async def dns_ok(*args):
        pass
    monkeypatch.setattr(svc, "_public_dns", dns_ok)
    requested = []
    def transport(request):
        requested.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/secrets"})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            await svc._fetch_document(client, "https://finance.sina.com.cn/")
    with pytest.raises(ValueError):
        asyncio.run(run())
    assert requested == ["https://finance.sina.com.cn/"]


def test_discovery_validates_persists_and_enters_actual_news_registry(monkeypatch):
    from app.services import cn_headline_source_registry as registry
    now = time.time()
    homepage = "https://finance.sina.com.cn/"
    calls = []
    monkeypatch.setattr(svc, "_HOMES", (homepage,))
    async def fetch(client, url):
        calls.append(url)
        if url == homepage:
            return b'<link rel="alternate" type="application/rss+xml" href="/fresh.xml">', url
        return feed(now), url
    monkeypatch.setattr(svc, "_fetch_document", fetch)
    result = asyncio.run(svc._maintain_news(now))
    assert result["active_count"] == 1
    assert any(r.get("url", "").endswith("/fresh.xml") for r in registry.sources_for_fetch())
    assert len(ResearchStore().list_entities(0, "source_definition")) == 1
    asyncio.run(svc._maintain_news(now + 1))
    assert len(calls) == 2  # one global daily update, even across accounts
    async def offline(*_):
        raise OSError("offline")
    monkeypatch.setattr(svc, "_fetch_document", offline)
    for day in range(1, 4):
        asyncio.run(svc._maintain_news(now + day * 86400))
    assert svc.discovered_news_sources() == []
    assert len(ResearchStore().list_entities(0, "source_definition")) == 1  # evidence retained
    assert any(r["payload"]["action"] == "news_quarantined" for r in ResearchStore().list_entities(0, "source_change"))


def test_canaries_are_bounded_and_scoped_without_model(monkeypatch):
    calls = []
    async def probe(source):
        calls.append(source)
        return bars()
    async def news():
        return {"active_count": 0}
    monkeypatch.setattr(svc, "_probe_market", probe)
    monkeypatch.setattr(svc, "_maintain_news", news)
    result = asyncio.run(svc.maintain(8))
    assert len(calls) == 2
    assert result["status"] == "checked"
    assert len(svc.summary(8)["sources"]) == 2
    assert svc.summary(9)["sources"] == []
