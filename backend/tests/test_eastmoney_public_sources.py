"""公共公司资料有界请求、严格错误语义及上海时间解析。"""
import asyncio

import httpx
import pytest

from app.services import company_profile_em as cp
from app.services import eastmoney_stock_news as news
from app.services import news_fetch_http


@pytest.fixture
def http_mock(monkeypatch):
    calls = []
    sync_client = httpx.Client
    async_client = httpx.AsyncClient

    async def no_sleep(_):
        return None

    monkeypatch.setattr(news_fetch_http.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(cp.time, "sleep", lambda _: None)

    def install(handler):
        def record(request):
            calls.append(request)
            return handler(request)

        transport = httpx.MockTransport(record)
        monkeypatch.setattr(httpx, "Client", lambda **kwargs: sync_client(transport=transport, **kwargs))
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: async_client(transport=transport, **kwargs))
        return calls

    return install


@pytest.mark.parametrize("raw, expected", [
    ("2024-01-01", 1704038400),
    ("2024-01-01 08:00", 1704067200),
    ("2024-01-01 08:00:59", 1704067259),
    ("2024-01-01 08:00:59:123", 1704067259),
    ("2024-01-01T08:00:59+08:00", 1704067259),
    ("2024-01-01T00:00:59Z", 1704067259),
    ("not a date", None),
])
def test_news_time_preserves_seconds_in_shanghai(raw, expected):
    assert news._parse_display_time(raw) == (raw, expected)


def test_company_bounded_request_success(http_mock):
    calls = http_mock(lambda _: httpx.Response(200, json={"jbzl": [{"ORG_NAME": "测试公司", "SECURITY_NAME_ABBR": "测试"}]}))
    profile, error = cp.fetch_company_survey("600519.SS", timeout_sec=10, max_retries=0)
    assert error is None
    assert profile["org_name"] == "测试公司"
    assert len(calls) == 1
    assert all(value == 10 for value in calls[0].extensions["timeout"].values())


@pytest.mark.parametrize("max_retries, expected_calls", [(None, 4), (0, 1), (1, 2)])
def test_company_retry_limit(http_mock, max_retries, expected_calls):
    def handler(request):
        raise httpx.ConnectError("offline", request=request)

    calls = http_mock(handler)
    _, error = cp.fetch_company_survey("600519.SS", max_retries=max_retries)
    assert "offline" in error
    assert len(calls) == expected_calls
    assert calls[0].extensions["timeout"]["read"] == 40
    assert calls[0].extensions["timeout"]["connect"] == 18


@pytest.mark.parametrize("payload", [[], {"jbzl": "invalid"}, {"jbzl": []}, {"jbzl": [1]}])
def test_company_malformed_data_returns_error(http_mock, payload):
    http_mock(lambda _: httpx.Response(200, json=payload))
    _, error = cp.fetch_company_survey("600519.SS", timeout_sec=10, max_retries=0)
    assert error


def test_news_bounded_request_keeps_and_sorts_announcement_times(http_mock):
    calls = http_mock(lambda _: httpx.Response(200, json={"gsgg": [
        {"title": "前一日公告", "art_code": "one", "display_time": "2024-01-01 08:00:00"},
        {"title": "后一日公告", "art_code": "two", "display_time": "2024-01-02 08:00:59"},
    ], "gszx": {"data": {"items": []}}}))
    rows = asyncio.run(news.fetch_symbol_news_items("600519.SS", strict=True, timeout_sec=10, max_retries=0))
    assert [row["title"] for row in rows] == ["后一日公告", "前一日公告"]
    assert rows[1]["published_ts"] == 1704067200
    assert len(calls) == 1
    assert all(value == 10 for value in calls[0].extensions["timeout"].values())


@pytest.mark.parametrize("payload", [{"gsgg": []}, {"gszx": []}, {"gszx": {"data": {"items": []}}}])
def test_strict_news_empty_valid_lists_are_success(http_mock, payload):
    http_mock(lambda _: httpx.Response(200, json=payload))
    assert asyncio.run(news.fetch_symbol_news_items("600519.SS", strict=True, max_retries=0)) == []


@pytest.mark.parametrize("payload", [[], {}, {"error": "denied"}, {"gsgg": "invalid", "gszx": {"data": []}}])
def test_strict_news_malformed_structure_raises(http_mock, payload):
    http_mock(lambda _: httpx.Response(200, json=payload))
    with pytest.raises(ValueError):
        asyncio.run(news.fetch_symbol_news_items("600519.SS", strict=True, max_retries=0))
    assert asyncio.run(news.fetch_symbol_news_items("600519.SS", max_retries=0)) == []


def test_strict_news_non_json_raises(http_mock):
    http_mock(lambda _: httpx.Response(200, text="<html>blocked</html>"))
    with pytest.raises(ValueError, match="JSON"):
        asyncio.run(news.fetch_symbol_news_items("600519.SS", strict=True, max_retries=0))
    assert asyncio.run(news.fetch_symbol_news_items("600519.SS", max_retries=0)) == []


@pytest.mark.parametrize("max_retries, expected_calls", [(None, 4), (0, 1), (1, 2)])
def test_news_network_failure_and_retry_limit(http_mock, max_retries, expected_calls):
    def handler(request):
        raise httpx.ConnectError("offline", request=request)

    calls = http_mock(handler)
    with pytest.raises(RuntimeError, match="请求失败"):
        asyncio.run(news.fetch_symbol_news_items("600519.SS", strict=True, max_retries=max_retries))
    assert len(calls) == expected_calls
    assert calls[0].extensions["timeout"]["read"] == 38
    assert calls[0].extensions["timeout"]["connect"] == 15
    calls.clear()
    assert asyncio.run(news.fetch_symbol_news_items("600519.SS", max_retries=max_retries)) == []
    assert len(calls) == expected_calls
