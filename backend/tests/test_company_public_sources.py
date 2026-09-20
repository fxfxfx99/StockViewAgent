"""公开公司数据的来源、时间戳与手动资料隔离回归。"""
from unittest.mock import AsyncMock, Mock

import pytest

from app.services import company_public_sources as public


@pytest.fixture(autouse=True)
def no_real_sources(monkeypatch):
    monkeypatch.setattr(public.time, "time", lambda: 9000)
    monkeypatch.setattr(public.company_profile_em, "fetch_company_survey", Mock(return_value=({}, "offline")))
    monkeypatch.setattr(public.eastmoney_stock_news, "fetch_symbol_news_items", AsyncMock(side_effect=RuntimeError("offline")))
    monkeypatch.setattr(public.profile_store, "get_entry", Mock(return_value={}))
    monkeypatch.setattr(public.universe_company_store, "get_symbol", Mock(return_value={}))
    monkeypatch.setattr(public.news_store, "list_archive", Mock(return_value=([], 0)))


def test_live_company_source_and_time_are_used_without_loading_manual_values(monkeypatch):
    company = {"name": "公开简称", "intro": "公开简介"}
    survey = Mock(return_value=(company, None))
    monkeypatch.setattr(public.company_profile_em, "fetch_company_survey", survey)

    assert public.fetch_company("600519.SS") == (company, "东方财富", 9000, None)
    survey.assert_called_once_with("600519.SS", timeout_sec=10, max_retries=0)
    public.profile_store.get_entry.assert_not_called()


def test_company_failure_preserves_freshest_automatic_cache_and_its_time(monkeypatch):
    monkeypatch.setattr(public.profile_store, "get_entry", lambda _: {
        "auto": {"intro": "较旧公开内容"}, "manual": {"intro": "私人覆盖"}, "em_fetched_at": 300,
    })
    monkeypatch.setattr(public.universe_company_store, "get_symbol", lambda _: {
        "auto": {"intro": "较新公开内容"}, "em_fetched_at": 500,
    })

    company, source, fetched_at, error = public.fetch_company("600519.SS")
    assert company == {"intro": "较新公开内容"}
    assert source == "本地公司资料"
    assert fetched_at == 500
    assert error
    assert "私人覆盖" not in str(company)


def test_manual_only_company_is_not_shared_as_public_data(monkeypatch):
    monkeypatch.setattr(public.profile_store, "get_entry", lambda _: {
        "auto": {}, "manual": {"name": "私人名称", "intro": "私人备注"}, "em_fetched_at": 8000,
    })
    company, _, fetched_at, error = public.fetch_company("600519.SS")
    assert company is None
    assert fetched_at is None
    assert error


def test_cached_company_without_known_acquisition_time_stays_unknown(monkeypatch):
    monkeypatch.setattr(public.profile_store, "get_entry", lambda _: {"auto": {"intro": "公开旧资料"}})
    company, _, fetched_at, error = public.fetch_company("600519.SS")
    assert company == {"intro": "公开旧资料"}
    assert fetched_at is None
    assert error


@pytest.mark.parametrize("items", [[], [{"title": "公开新闻", "source": "东方财富资讯"}]])
def test_successful_news_including_empty_result_is_not_replaced_with_old_archive(monkeypatch, items):
    fetch = AsyncMock(return_value=items)
    monkeypatch.setattr(public.eastmoney_stock_news, "fetch_symbol_news_items", fetch)
    assert public.fetch_news("600519.SS", "公开简称") == (items, "东方财富", 9000, None)
    fetch.assert_awaited_once_with(
        "600519.SS", limit=30, stock_name="公开简称", strict=True, timeout_sec=10, max_retries=0,
    )
    public.news_store.list_archive.assert_not_called()


def test_failed_news_refresh_retains_archive_acquisition_time(monkeypatch):
    rows = [{"title": "旧新闻", "fetched_at": 200}, {"title": "较新新闻", "fetched_at": 400}]
    monkeypatch.setattr(public.news_store, "list_archive", Mock(return_value=(rows, 2)))
    items, source, fetched_at, error = public.fetch_news("600519.SS", None)
    assert items == rows
    assert source == "本地新闻库"
    assert fetched_at == 400
    assert error


def test_article_about_an_announcement_is_not_itself_a_company_announcement():
    article = public.as_feed_item({
        "title": "投资者解读公司公告", "source": "东方财富资讯", "link": "https://example.com/article",
        "published_ts": 120, "summary": "新闻摘要",
    })
    announcement = public.as_feed_item({"title": "临时公告", "source": "东方财富公告", "link": "https://example.com/notice"})
    assert article["event_type"] == ""
    assert article["source_label"] == "东方财富资讯"
    assert article["url"] == "https://example.com/article"
    assert article["created_at"] == 120
    assert announcement["event_type"] == "公司公告"
    assert announcement["source_label"] == "东方财富公告"
