"""公司面板在雪球拒绝会话时使用有明确来源的公开数据。"""
from unittest.mock import Mock

import pytest

from app.services import company_public_sources as public
from app.services import xueqiu_http, xueqiu_pipeline as pipeline

SYMBOL = "600519.SS"
COMPANY = {"name": "公开简称", "org_name": "公开公司", "intro": "公开简介", "industry": "公开行业"}
PUBLIC_ITEMS = [
    {"title": "公司公告", "source": "东方财富公告", "link": "https://example.com/notice", "published_ts": 300, "fetched_at": 70},
    {"title": "公告新闻解读", "source": "东方财富资讯", "link": "https://example.com/news", "published_ts": 400, "fetched_at": 100},
]


@pytest.fixture(autouse=True)
def no_real_requests(monkeypatch):
    monkeypatch.setattr(pipeline.time, "time", lambda: 9000)
    monkeypatch.setattr(xueqiu_http, "effective_xueqiu_cookies", lambda: "")
    monkeypatch.setattr(xueqiu_http, "request_json", Mock(side_effect=AssertionError("unexpected network request")))
    monkeypatch.setattr(pipeline, "stage_fetch_company_f10", Mock(return_value=(None, xueqiu_http.XueqiuSessionError(False))))
    monkeypatch.setattr(pipeline, "stage_fetch_major_events", Mock(return_value=([], None)))
    monkeypatch.setattr(pipeline, "stage_fetch_stock_timeline", Mock(return_value=([], None)))
    monkeypatch.setattr(public, "fetch_news", Mock(return_value=(PUBLIC_ITEMS, "东方财富", 8000, None)))


@pytest.mark.parametrize("configured,status", [(False, "missing"), (True, "expired")])
def test_rejected_session_stops_other_xueqiu_requests_and_uses_public_sources(monkeypatch, configured, status):
    monkeypatch.setattr(xueqiu_http, "effective_xueqiu_cookies", lambda: "synthetic-cookie" if configured else "")
    monkeypatch.setattr(pipeline, "stage_fetch_company_f10", Mock(return_value=(None, xueqiu_http.XueqiuSessionError(configured))))
    monkeypatch.setattr(public, "fetch_company", Mock(return_value=(COMPANY, "东方财富", 7000, None)))

    result = pipeline.run_company_bundle(SYMBOL)

    pipeline.stage_fetch_major_events.assert_not_called()
    pipeline.stage_fetch_stock_timeline.assert_not_called()
    assert result["auth_status"] == status
    assert result["cookies_configured"] is configured
    assert result["company"] == COMPANY
    assert result["sources"] == {"company": "东方财富", "major_events": "东方财富", "news": "东方财富"}
    assert result["section_fetched_at"] == {"company": 7000, "major_events": 8000, "news": 8000}
    assert result["errors"] == []
    assert result["stale_fields"] == []
    assert [item["url"] for item in result["major_events"]] == ["https://example.com/notice"]
    assert [item["url"] for item in result["news"]] == ["https://example.com/news"]
    assert result["major_events"][0]["source_label"] == "东方财富公告"
    assert result["news"][0]["source_label"] == "东方财富资讯"


def test_session_rejected_on_events_keeps_successful_company_and_skips_timeline(monkeypatch):
    monkeypatch.setattr(pipeline, "stage_fetch_company_f10", Mock(return_value=({"data": {"company": {"org_name_cn": "雪球公司"}}}, None)))
    monkeypatch.setattr(pipeline, "stage_fetch_major_events", Mock(return_value=([], xueqiu_http.XueqiuSessionError(False))))
    monkeypatch.setattr(public, "fetch_company", Mock(side_effect=AssertionError("company already available")))
    result = pipeline.run_company_bundle(SYMBOL)
    pipeline.stage_fetch_stock_timeline.assert_not_called()
    assert result["company"]["org_name"] == "雪球公司"
    assert result["sources"]["company"] == "雪球"
    assert result["auth_status"] == "missing"
    assert result["news"][0]["source_label"] == "东方财富资讯"


@pytest.mark.parametrize("configured,status", [(False, "anonymous"), (True, "connected")])
def test_successful_empty_xueqiu_feeds_are_valid_and_do_not_trigger_public_fallback(monkeypatch, configured, status):
    monkeypatch.setattr(xueqiu_http, "effective_xueqiu_cookies", lambda: "synthetic-cookie" if configured else "")
    monkeypatch.setattr(pipeline, "stage_fetch_company_f10", Mock(return_value=({"data": {"company": {"org_name_cn": "雪球公司"}}}, None)))
    monkeypatch.setattr(public, "fetch_company", Mock(side_effect=AssertionError("unexpected fallback")))
    result = pipeline.run_company_bundle(SYMBOL)
    public.fetch_news.assert_not_called()
    assert result["auth_status"] == status
    assert result["news"] == result["major_events"] == []
    assert result["errors"] == []
    assert result["section_fetched_at"] == {"company": 9000, "major_events": 9000, "news": 9000}


def test_empty_public_feeds_are_successful_checks_not_missing_data_errors(monkeypatch):
    monkeypatch.setattr(public, "fetch_company", Mock(return_value=(COMPANY, "东方财富", 7000, None)))
    monkeypatch.setattr(public, "fetch_news", Mock(return_value=([], "东方财富", 8000, None)))
    result = pipeline.run_company_bundle(SYMBOL)
    assert result["news"] == result["major_events"] == []
    assert result["errors"] == result["stale_fields"] == []
    assert result["section_fetched_at"]["news"] == 8000


def test_failed_public_refresh_keeps_old_times_and_marks_only_retained_sections_stale(monkeypatch):
    monkeypatch.setattr(public, "fetch_company", Mock(return_value=(COMPANY, "本地公司资料", None, "公司更新失败")))
    monkeypatch.setattr(public, "fetch_news", Mock(return_value=(PUBLIC_ITEMS, "本地新闻库", 100, "资讯更新失败")))
    result = pipeline.run_company_bundle(SYMBOL)
    # 较新的新闻不能将旧公告的采集时间抬高。
    assert result["section_fetched_at"] == {"company": None, "major_events": 70, "news": 100}
    assert set(result["stale_fields"]) == {"company", "major_events", "news"}
    assert result["sources"]["company"] == "本地公司资料"
    assert result["sources"]["news"] == "本地新闻库"
    assert result["errors"] == ["公司更新失败", "资讯更新失败"]


def test_shared_company_bundle_excludes_private_manual_profile_fields(monkeypatch):
    monkeypatch.setattr(public.company_profile_em, "fetch_company_survey", Mock(return_value=({}, "offline")))
    monkeypatch.setattr(public.profile_store, "get_entry", lambda _: {
        "auto": {"name": "公开简称"}, "manual": {"name": "私人简称", "intro": "私人备注"}, "em_fetched_at": 100,
    })
    monkeypatch.setattr(public.universe_company_store, "get_symbol", lambda _: None)
    result = pipeline.run_company_bundle(SYMBOL)
    assert result["company"] == {"name": "公开简称"}
    assert "私人" not in str(result)
    assert result["section_fetched_at"]["company"] == 100
