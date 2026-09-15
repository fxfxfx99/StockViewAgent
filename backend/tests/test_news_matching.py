"""Stock-scoped matching evidence and editable configuration, using isolated data."""
import pytest

from app.services import news_watchlist_matcher as matcher
from app.storage import integrations_store


def test_search_hint_and_previous_matches_do_not_prove_association():
    item = {"title": "今日天气晴朗", "symbol_hint": "600519.SS", "matched_symbols": ["600519.SS"],
            "source_articles": [{"symbol_hint": "AAPL", "matched_symbols": ["AAPL"]}]}
    assert matcher.article_match_evidence(item, ["600519.SS", "AAPL"], {}) == {}


def test_company_aliases_are_scoped_to_the_correct_stock_and_preserve_evidence():
    item = {"title": "贵州茅台披露半年报", "source": "公司公告"}
    evidence = matcher.article_match_evidence(item, ["600519.SS", "600143.SS"],
                                            {"600519.SS": ["贵州茅台"], "600143.SS": ["金发科技"]})
    assert evidence == {"600519.SS": [{"kind": "company_keyword", "value": "贵州茅台", "source": "公司公告"}]}


def test_industry_words_do_not_propagate_to_all_stocks():
    item = {"title": "央行调整利率，能源、消费和半导体产业政策发布"}
    assert matcher.article_match_evidence(item, ["600519.SS", "600143.SS", "AAPL"], {}) == {}


def test_configured_keywords_are_stock_specific_literal_and_case_insensitive():
    integrations_store.merge_integrations({"news": {"match_keywords": {
        "600143.SS": ["改性塑料", "PET+"], "AAPL": ["iPhone"], "600519.SS": ["白酒消费"],
    }}})
    item = {"title": "改性塑料新订单增长，IPHONE 发布新产品"}
    evidence = matcher.article_match_evidence(item, ["600143.SS", "AAPL", "600519.SS"], {})
    assert set(evidence) == {"600143.SS", "AAPL"}
    assert evidence["600143.SS"] == [{"kind": "configured_keyword", "value": "改性塑料"}]
    assert matcher.article_match_evidence({"title": "PET has demand"}, ["600143.SS"], {}) == {}
    assert matcher.article_match_evidence({"title": "PET+ has demand"}, ["600143.SS"], {})


@pytest.mark.parametrize("code,symbol", [
    ("600519", "600519.SS"), ("sh600519", "600519.SS"), ("600519.SH", "600519.SS"),
    ("1.600519", "600519.SS"), ("0.300302", "300302.SZ"), ("0.920001", "920001.BJ"),
    ("2.830001", "830001.BJ"), ("00700.HK", "0700.HK"), ("HK00700", "0700.HK"),
    ("116.00700", "0700.HK"), ("aapl", "AAPL"), ("105.AAPL", "AAPL"), ("BRK.B", "BRK-B"),
])
def test_provider_codes_normalize_across_markets(code, symbol):
    evidence = matcher.article_match_evidence({"title": "发行人公告", "related_codes": [code]}, [symbol], {})
    assert evidence == {symbol: [{"kind": "provider", "value": code}]}


def test_explicit_exchange_never_falls_back_to_a_different_stock():
    symbols = ["600519.SS", "600519.SZ", "600519.BJ"]
    for item in ({"title": "600519.SZ 公告"}, {"title": "SZ600519 公告"},
                 {"title": "发行人公告", "related_codes": ["0.600519"]}):
        assert matcher.match_article_to_watchlist(item, symbols, {symbol: ["600519"] for symbol in symbols}) == ["600519.SZ"]


@pytest.mark.parametrize("title,symbol", [
    ("RENEWABLE BUSINESS", "NEW"), ("公司收入 1600519 元", "600519.SS"),
    ("公司收入 6005190 元", "600519.SS"), ("公司收入 600519.12 元", "600519.SS"),
    ("编号 ABC600519XYZ", "600519.SS"), ("A regular news story", "A"),
    ("公司收入 700 港元", "0700.HK"),
])
def test_ticker_and_number_boundaries_reject_substrings(title, symbol):
    assert matcher.article_match_evidence({"title": title}, [symbol], {}) == {}


@pytest.mark.parametrize("title,symbol", [
    ("600519 公告", "600519.SS"), ("SH600519 公告", "600519.SS"),
    ("0700.HK 公告", "0700.HK"), ("HK00700 公告", "0700.HK"),
    ("AAPL earnings", "AAPL"), ("BRK.B earnings", "BRK-B"),
    ("$A earnings", "A"), ("NYSE:A earnings", "A"),
])
def test_explicit_codes_are_company_keyword_evidence(title, symbol):
    evidence = matcher.article_match_evidence({"title": title}, [symbol], {})
    assert evidence[symbol][0]["kind"] == "company_keyword"


def test_source_merge_preserves_platform_evidence_without_cross_stock_pollution():
    item = {"title": "跨平台转载的发行人公告", "source": "转载站", "symbol_hint": "MSFT",
            "related_codes": ["1.600519", "116.00700"],
            "source_articles": [
                {"source": "东方财富", "related_codes": ["1.600519"]},
                {"source": "港股资讯", "related_codes": ["116.00700"]},
                {"source": "搜索站", "symbol_hint": "MSFT"},
                {"source": "东方财富", "related_codes": ["1.600519"]},
            ]}
    evidence = matcher.article_match_evidence(item, ["600519.SS", "0700.HK", "MSFT"], {})
    assert set(evidence) == {"600519.SS", "0700.HK"}
    assert evidence["600519.SS"] == [{"kind": "provider", "value": "1.600519", "source": "东方财富"}]


def test_complete_company_names_and_removed_stock_history(monkeypatch):
    full_name = "这是一家名称长度超过二十四个汉字的虚构科技股份有限公司"
    monkeypatch.setattr(matcher.watchlist_store, "load_all_symbols_union", lambda: [])
    monkeypatch.setattr(matcher.profile_store, "get_entry", lambda symbol: {"name": full_name})
    monkeypatch.setattr(matcher.profile_store, "merge_display", lambda entry: entry)
    assert matcher.article_match_evidence({"title": full_name + "发布公告"}, ["600143.SS"])
    assert matcher.article_match_evidence({"title": full_name[:12] + "另一家公司发布公告"}, ["600143.SS"]) == {}


def test_keyword_validation_normalizes_and_merges_equivalent_symbols():
    options = matcher.normalize_match_keywords({"sh600519": ["  白酒消费  ", "ＡＢＣ", "abc"],
                                               "600519.SH": ["白酒消费", "消费  升级"]})
    assert options == {"600519.SS": ["白酒消费", "ABC", "消费 升级"]}


@pytest.mark.parametrize("value", [
    [], {"https://bad.example": ["行业"]}, {"AAPL": "iPhone"}, {"AAPL": [1]},
    {"AAPL": ["A"]}, {"AAPL": ["a" * 41]}, {"AAPL": ["..."]},
    {"AAPL": [f"term{i}" for i in range(21)]},
    {f"{600000+i}.SS": ["行业"] for i in range(101)},
])
def test_invalid_keyword_configuration_is_rejected(value):
    with pytest.raises(ValueError):
        matcher.normalize_match_keywords(value)
