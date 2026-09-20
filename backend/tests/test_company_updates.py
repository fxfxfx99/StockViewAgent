import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app.services import company_updates as updates
from app.storage import company_updates_store

SYMBOL = "600519.SS"


def bundle(**patch):
    return {
        "ok": True,
        "yahoo_symbol": SYMBOL,
        "company": {"name": "贵州茅台", "intro": "公司资料"},
        "major_events": [{"title": "业绩公告"}],
        "news": [{"title": "最新资讯"}],
        "errors": [],
        "section_errors": {"company": None, "major_events": None, "news": None},
        "sources": {"company": "公开资料", "major_events": "雪球", "news": "雪球"},
        **patch,
    }


@pytest.fixture
def runtime(monkeypatch):
    now = [10_000]
    cookies = [""]
    monkeypatch.setattr(updates.time, "time", lambda: now[0])
    monkeypatch.setattr(updates.xueqiu_http, "effective_xueqiu_cookies", lambda: cookies[0])
    monkeypatch.setattr(updates, "refresh_interval_sec", lambda: 900)
    return now, cookies


def test_cache_survives_reload_and_expires(runtime, monkeypatch):
    now, _ = runtime
    calls = []
    monkeypatch.setattr(updates.xueqiu_pipeline, "run_company_bundle", lambda sym: calls.append(sym) or bundle())
    first = updates.get_company_bundle(SYMBOL)
    assert first["cached"] is False
    assert first["status"] == "ready"
    assert first["fetched_at"] == 10_000
    assert first["next_refresh_at"] == 10_900
    assert company_updates_store.load(SYMBOL)["bundle"]["company"]["name"] == "贵州茅台"
    now[0] += 899
    cached = updates.get_company_bundle(SYMBOL)
    assert cached["cached"] is True
    # 返回对象不能修改磁盘中的缓存或后续响应。
    cached["company"]["name"] = "changed"
    assert updates.get_company_bundle(SYMBOL)["company"]["name"] == "贵州茅台"
    now[0] += 1
    assert updates.get_company_bundle(SYMBOL)["cached"] is False
    assert calls == [SYMBOL, SYMBOL]


def test_failure_preserves_last_successful_sections_and_sources(runtime, monkeypatch):
    now, _ = runtime
    responses = iter([
        bundle(),
        bundle(
            company=None,
            major_events=[],
            news=[{"title": "新公开资讯"}],
            errors=["来源不可用"],
            section_errors={"company": "来源不可用", "major_events": "来源不可用", "news": None},
            sources={"company": None, "major_events": None, "news": "本地新闻归档"},
        ),
    ])
    monkeypatch.setattr(updates.xueqiu_pipeline, "run_company_bundle", lambda sym: next(responses))
    updates.get_company_bundle(SYMBOL)
    now[0] += 900
    result = updates.get_company_bundle(SYMBOL)
    assert result["company"]["name"] == "贵州茅台"
    assert result["major_events"] == [{"title": "业绩公告"}]
    assert result["news"] == [{"title": "新公开资讯"}]
    assert result["sources"] == {"company": "公开资料", "major_events": "雪球", "news": "本地新闻归档"}
    assert result["stale_fields"] == ["company", "major_events"]
    assert result["section_fetched_at"] == {"company": 10_000, "major_events": 10_000, "news": 10_900}
    assert result["status"] == "partial"


def test_successful_empty_list_clears_old_news(runtime, monkeypatch):
    now, _ = runtime
    responses = iter([bundle(), bundle(news=[])])
    monkeypatch.setattr(updates.xueqiu_pipeline, "run_company_bundle", lambda sym: next(responses))
    updates.get_company_bundle(SYMBOL)
    now[0] += 900
    result = updates.get_company_bundle(SYMBOL)
    assert result["news"] == []
    assert result["stale_fields"] == []
    assert result["status"] == "ready"


def test_public_cache_keeps_its_real_age_across_refreshes(runtime, monkeypatch):
    now, _ = runtime
    monkeypatch.setattr(
        updates.xueqiu_pipeline,
        "run_company_bundle",
        lambda sym: bundle(
            major_events=[],
            news=[],
            auth_status="missing",
            section_fetched_at={"company": 2_000, "major_events": None, "news": None},
            stale_fields=["company"],
        ),
    )
    for expected_attempt in (10_000, 10_900):
        result = updates.get_company_bundle(SYMBOL)
        assert result["section_fetched_at"] == {"company": 2_000, "major_events": None, "news": None}
        assert result["fetched_at"] == 2_000
        assert result["last_attempt_at"] == expected_attempt
        assert result["stale_fields"] == ["company"]
        assert result["status"] == "stale"
        now[0] += 900


def test_unknown_source_age_is_not_replaced_by_refresh_time(runtime, monkeypatch):
    now, _ = runtime
    responses = iter([
        bundle(
            major_events=[],
            news=[],
            section_fetched_at={field: None for field in updates._SECTIONS},
            stale_fields=["company"],
        ),
        bundle(
            company=None,
            major_events=[],
            news=[],
            section_errors={field: "来源不可用" for field in updates._SECTIONS},
            section_fetched_at={field: None for field in updates._SECTIONS},
        ),
    ])
    monkeypatch.setattr(updates.xueqiu_pipeline, "run_company_bundle", lambda sym: next(responses))
    first = updates.get_company_bundle(SYMBOL)
    assert first["fetched_at"] is None
    assert first["section_fetched_at"]["company"] is None
    now[0] += 900
    retained = updates.get_company_bundle(SYMBOL)
    assert retained["company"]["name"] == "贵州茅台"
    assert retained["section_fetched_at"]["company"] is None
    assert retained["fetched_at"] is None
    assert retained["status"] == "stale"


@pytest.mark.parametrize("fallback_timestamp", [2_000, None])
def test_failed_source_does_not_replace_newer_success_with_older_public_cache(runtime, monkeypatch, fallback_timestamp):
    now, _ = runtime
    responses = iter([
        bundle(sources={"company": "雪球", "major_events": "雪球", "news": "雪球"}),
        bundle(
            company={"name": "旧版公开资料"},
            section_fetched_at={"company": fallback_timestamp},
            section_errors={"company": "公开来源暂不可用", "major_events": None, "news": None},
            stale_fields=["company"],
            sources={"company": "本地公司资料", "major_events": "雪球", "news": "雪球"},
        ),
    ])
    monkeypatch.setattr(updates.xueqiu_pipeline, "run_company_bundle", lambda sym: next(responses))
    updates.get_company_bundle(SYMBOL)
    now[0] += 900
    result = updates.get_company_bundle(SYMBOL)
    assert result["company"]["name"] == "贵州茅台"
    assert result["sources"]["company"] == "雪球"
    assert result["section_fetched_at"]["company"] == 10_000
    assert result["stale_fields"] == ["company"]
    assert result["status"] == "partial"


def test_public_online_success_is_usable_without_xueqiu_auth(runtime, monkeypatch):
    monkeypatch.setattr(
        updates.xueqiu_pipeline,
        "run_company_bundle",
        lambda sym: bundle(auth_status="missing", errors=["雪球未登录，已使用公开来源"]),
    )
    result = updates.get_company_bundle(SYMBOL)
    assert result["ok"] is True
    assert result["status"] == "ready"
    assert result["auth_status"] == "missing"


def test_exception_keeps_previous_result_and_retries_after_ttl(runtime, monkeypatch):
    now, _ = runtime
    calls = []

    def fetch(sym):
        calls.append(sym)
        if len(calls) == 1:
            return bundle()
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(updates.xueqiu_pipeline, "run_company_bundle", fetch)
    updates.get_company_bundle(SYMBOL)
    now[0] += 900
    result = updates.get_company_bundle(SYMBOL)
    assert result["ok"] is True
    assert result["status"] == "stale"
    assert result["fetched_at"] == 10_000
    assert result["last_attempt_at"] == 10_900
    assert result["next_refresh_at"] == 11_800
    assert "provider unavailable" not in json.dumps(result)
    assert updates.get_company_bundle(SYMBOL)["cached"] is True
    assert len(calls) == 2


def test_cookie_change_invalidates_cache_without_persisting_secret(runtime, monkeypatch, tmp_path):
    _, cookies = runtime
    calls = []
    monkeypatch.setattr(updates.xueqiu_pipeline, "run_company_bundle", lambda sym: calls.append(sym) or bundle())
    updates.get_company_bundle(SYMBOL)
    cookies[0] = "xq_a_token=private-cookie; u=user-secret"
    assert updates.get_company_bundle(SYMBOL)["cached"] is False
    assert len(calls) == 2
    stored = (tmp_path / "company_updates" / f"{SYMBOL}.json").read_text()
    assert "private-cookie" not in stored
    assert "user-secret" not in stored
    assert "fingerprint" in stored


def test_force_refresh_has_a_minimum_interval(runtime, monkeypatch):
    now, _ = runtime
    calls = []
    monkeypatch.setattr(updates.xueqiu_pipeline, "run_company_bundle", lambda sym: calls.append(sym) or bundle())
    updates.get_company_bundle(SYMBOL)
    now[0] += 29
    assert updates.get_company_bundle(SYMBOL, force=True)["cached"] is True
    now[0] += 1
    assert updates.get_company_bundle(SYMBOL, force=True)["cached"] is False
    assert len(calls) == 2


def test_concurrent_requests_share_one_fetch(runtime, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def fetch(sym):
        calls.append(sym)
        entered.set()
        assert release.wait(3)
        return bundle()

    monkeypatch.setattr(updates.xueqiu_pipeline, "run_company_bundle", fetch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(updates.get_company_bundle, SYMBOL)
        assert entered.wait(3)
        second = pool.submit(updates.get_company_bundle, SYMBOL, True)
        release.set()
        assert first.result()["cached"] is False
        assert second.result()["cached"] is True
    assert calls == [SYMBOL]


def test_unavailable_result_is_negatively_cached(runtime, monkeypatch):
    calls = []
    monkeypatch.setattr(
        updates.xueqiu_pipeline,
        "run_company_bundle",
        lambda sym: calls.append(sym) or bundle(
            company=None,
            major_events=[],
            news=[],
            errors=["来源不可用"],
            section_errors={field: "来源不可用" for field in updates._SECTIONS},
        ),
    )
    result = updates.get_company_bundle(SYMBOL)
    assert result["status"] == "unavailable"
    assert result["fetched_at"] is None
    assert result["ok"] is False
    assert updates.get_company_bundle(SYMBOL)["cached"] is True
    assert len(calls) == 1


def test_background_refreshes_union_only_and_uses_cache(runtime, monkeypatch):
    calls = []
    monkeypatch.setattr(updates.watchlist_store, "load_all_symbols_union", lambda: [SYMBOL, "000001.SZ", "AAPL"])
    monkeypatch.setattr(updates.xueqiu_pipeline, "run_company_bundle", lambda sym: calls.append(sym) or bundle(yahoo_symbol=sym))

    async def no_wait(seconds):
        pass

    monkeypatch.setattr(updates.asyncio, "sleep", no_wait)
    asyncio.run(updates.refresh_watchlist_once())
    asyncio.run(updates.refresh_watchlist_once())
    assert calls == [SYMBOL, "000001.SZ"]


def test_background_loop_can_be_cancelled_and_restarted(monkeypatch):
    calls = []
    real_sleep = asyncio.sleep

    async def short_sleep(seconds):
        await real_sleep(0)

    async def refresh():
        calls.append("refresh")
        await real_sleep(3600)

    monkeypatch.setattr(updates.asyncio, "sleep", short_sleep)
    monkeypatch.setattr(updates, "refresh_watchlist_once", refresh)
    monkeypatch.setattr(updates, "settings", SimpleNamespace(company_auto_refresh_enabled=True))

    async def run():
        for _ in range(2):
            task = asyncio.create_task(updates.company_updates_loop())
            await real_sleep(0)
            await real_sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run())
    assert calls == ["refresh", "refresh"]


def test_corrupt_cache_is_ignored_and_path_traversal_rejected(tmp_path):
    directory = tmp_path / "company_updates"
    directory.mkdir()
    (directory / f"{SYMBOL}.json").write_text("not json")
    assert company_updates_store.load(SYMBOL) == {}
    with pytest.raises(ValueError):
        company_updates_store.save("../secrets", {})
