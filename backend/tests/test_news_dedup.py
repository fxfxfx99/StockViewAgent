"""Syndication identity and provenance contracts, using local fixtures only."""
from copy import deepcopy

import pytest

from app.schemas.news import normalize_news
from app.services import cn_market_news, news_watchlist_matcher, rss_fetcher
from app.services.news_dedup import canonical_url, deduplicate_news, merge_news, same_event


BASE = 1788868800  # 2026-09-08 12:00:00 UTC


def article(title="京东物流将在5年内采购300万台机器人", **updates):
    return {"title": title, "url": "https://publisher.example/article/one", "source": "来源甲",
            "published_ts": BASE, "summary": "公开发布的新闻摘要。", **updates}


def test_same_article_url_collapses_changed_publisher_suffix_and_tracking():
    left = article("同有科技：向特定对象发行A股股票申请获深交所受理 - 新浪财经",
                   url="https://news.google.com/rss/articles/token?oc=5&utm_source=feed")
    right = article("同有科技：向特定对象发行A股股票申请获深交所受理 - 新浪网",
                    url="https://news.google.com/rss/articles/token")
    assert same_event(left, right)
    assert len(deduplicate_news([left, right])) == 1


def test_canonical_url_preserves_identity_query_and_original_links():
    original = "https://NEWS.example:443/story?id=12&utm_source=feed&lang=zh&fbclid=click#top"
    assert canonical_url(original) == "https://news.example/story?id=12&lang=zh"
    assert canonical_url("https://news.example/story?id=13") != canonical_url(original)
    assert canonical_url("https://news.example/story?source=original&id=12") == "https://news.example/story?id=12&source=original"
    normalized = normalize_news(article(url=original))
    assert "utm_source=feed" in normalized["url"]
    assert "utm_source=feed" in normalized["source_articles"][0]["url"]


@pytest.mark.parametrize("url", ["", "#", "javascript:alert(1)", "https://[broken", "https://news.example:bad/story",
                                      "https://news.example/", "https://news.example/news/",
                                      "https://so.eastmoney.com/news/s?keyword=新闻", "https://www.bing.com/news/search?q=stock",
                                      "https://news.google.com/rss/search?q=AAPL"])
def test_non_article_links_cannot_identify_an_event(url):
    assert canonical_url(url) == ""
    assert not same_event(article("甲公司公布年度利润增长方案", url=url),
                          article("乙公司董事会同意重组相关计划", url=url))


def test_near_copy_can_cross_midnight_with_same_subject_numbers_and_direction():
    left = article(published_ts=BASE + 11 * 3600)
    right = article("京东物流：5年内采购300万台机器人", url="https://another.example/a/2",
                    published_ts=BASE + 13 * 3600)
    assert same_event(left, right)
    assert not same_event(left, {**right, "published_ts": BASE + 60 * 3600})


def test_known_media_suffix_is_ignored_but_arbitrary_headline_ending_is_not():
    left = article("测试公司在本年度第三季度完成重要设备采购计划 - 新浪财经")
    right = article("测试公司在本年度第三季度完成重要设备采购计划 - 东方财富网",
                    url="https://another.example/story")
    assert same_event(left, right)
    assert not same_event(article("测试公司在本年度第三季度完成重要设备采购计划 - 不会交付"),
                          article("测试公司在本年度第三季度完成重要设备采购计划 - 已经交付",
                                  url="https://another.example/story"))


@pytest.mark.parametrize("left_title,right_title", [
    ("测试公司宣布5年内采购300万台机器人项目", "测试公司宣布5年内采购500万台机器人项目"),
    ("测试公司本年度上半年净利润同比增长1.2%", "测试公司本年度上半年净利润同比增长12%"),
    ("测试公司本年度营业收入300亿元净利润30亿元", "测试公司本年度营业收入30亿元净利润300亿元"),
    ("测试公司本年度上半年净利润达到300万元", "测试公司本年度上半年净利润达到300亿元"),
    ("测试公司本年度上半年净利润增幅为-30%", "测试公司本年度上半年净利润增幅为30%"),
    ("测试公司本年度上半年净利润同比增长30%", "测试公司本年度上半年净利润同比下降30%"),
    ("测试公司本年度上半年净利润由亏转盈达到30亿元", "测试公司本年度上半年净利润由盈转亏达到30亿元"),
    ("测试公司已经取得本年度生产许可证批准文件", "测试公司尚未取得本年度生产许可证批准文件"),
    ("测试甲公司宣布开展新一代存储设备采购项目", "测试乙公司宣布开展新一代存储设备采购项目"),
    ("山东航空股份有限公司宣布签署一批大型飞机采购项目", "山东航空集团有限公司宣布签署一批大型飞机采购项目"),
])
def test_changed_numbers_direction_negation_or_subject_are_distinct(left_title, right_title):
    assert not same_event(article(left_title), article(right_title, url="https://another.example/story"))


@pytest.mark.parametrize("same_url", [True, False])
def test_repeated_daily_report_is_not_yesterdays_article(same_url):
    left = article("每日公司新闻汇总与重要公告一览")
    right = article(left["title"], published_ts=BASE + 86400,
                    url=left["url"] if same_url else "https://another.example/daily/next")
    assert not same_event(left, right)
    assert normalize_news(left)["id"] != normalize_news(right)["id"]


def test_undated_different_urls_and_short_paraphrases_are_not_guessed():
    assert not same_event(article(published_ts=None), article(published_ts=None, url="https://another.example/a"))
    assert not same_event(article("公司发布财报"), article("公司公布财报", url="https://another.example/a"))


def test_report_and_separately_published_summary_keep_distinct_documents():
    title = "上海先进高新科技股份有限公司:2026年半年度报告"
    report = article(title, url="https://data.eastmoney.com/notices/detail/600001/AN1.html")
    summary = article(title + "摘要", url="https://data.eastmoney.com/notices/detail/600001/AN2.html")
    assert not same_event(report, summary)
    assert len(deduplicate_news([report, summary])) == 2
    # A second platform's copy of the same summary still merges normally.
    assert same_event(summary, {**summary, "url": "https://other.example/filing-summary"})


def test_merge_preserves_independent_provider_matches_without_promoting_search_hints():
    left = normalize_news(article(related_codes=["1.600519"], symbol_hint="AAPL"))
    right = normalize_news(article(source="来源乙", url="https://another.example/story",
                                   related_codes=["0.300302"], symbol_hint="MSFT"))
    merged = merge_news(left, right)
    assert merged["id"] == left["id"]
    assert merged["related_codes"] == ["1.600519", "0.300302"]
    assert merged["matched_symbols"] == []
    assert [source["symbol_hint"] for source in merged["source_articles"]] == ["AAPL", "MSFT"]
    assert [source["related_codes"] for source in merged["source_articles"]] == [["1.600519"], ["0.300302"]]
    assert len(merge_news(merged, right)["source_articles"]) == 2


def test_longer_material_selects_its_whole_citation_and_retains_canonical_id():
    left = normalize_news(article("公司完成重要采购项目", related_codes=["1.600519"]))
    right = normalize_news(article("公司完成重要采购项目 - 新浪财经", source="来源乙",
                                   url="https://another.example/story", published_ts=BASE + 60,
                                   content="另一来源独立提供的完整正文与事实。" * 10, related_codes=["0.300302"]))
    before = deepcopy(left)
    merged = merge_news(left, right)
    assert left == before
    assert merged["id"] == left["id"]
    for field in ("title", "source", "url", "published_ts", "content", "summary"):
        assert merged[field] == right[field]
    assert merged["source_articles"][0]["title"] == left["title"]
    assert merged["source_articles"][1]["title"] == right["title"]
    again = normalize_news(merged)
    assert again["id"] == left["id"]
    assert again == normalize_news(again)
    assert len(again["source_articles"]) == 2
    assert again["source_articles"][1]["related_codes"] == ["0.300302"]


def test_newer_shorter_full_body_at_same_url_is_a_correction():
    left = normalize_news(article(content="最初发布的较长正文。" * 20, fetched_at=BASE))
    right = normalize_news(article(content="更正后的简短完整正文。", summary="更正摘要。", fetched_at=BASE + 60,
                                   url=left["url"] + "?utm_source=feed"))
    merged = merge_news(left, right)
    assert merged["id"] == left["id"]
    for field in ("title", "source", "url", "summary", "content", "published_ts"):
        assert merged[field] == right[field]
    assert normalize_news(merged) == normalize_news(normalize_news(merged))


@pytest.mark.parametrize("updates", [
    {"url": "https://another.example/a"},
    {"fetched_at": BASE - 60},
    {"content": ""},
])
def test_shorter_material_does_not_replace_full_body_without_newer_same_url(updates):
    left = normalize_news(article(content="原始完整正文。" * 20, fetched_at=BASE))
    right = normalize_news(article(**{"content": "较短的新稿。", "fetched_at": BASE + 60, **updates}))
    merged = merge_news(left, right)
    assert merged["content"] == left["content"] and merged["url"] == left["url"]


def test_same_source_entry_keeps_corrected_body_without_duplicate_provenance():
    left = normalize_news(article(content="最初发布的较长正文。" * 20, fetched_at=BASE))
    right = normalize_news(article(content="更正正文。", fetched_at=BASE + 60))
    merged = normalize_news(merge_news(left, right))
    assert len(merged["source_articles"]) == 1
    assert merged["source_articles"][0]["content"] == right["content"]
    assert merged["source_articles"][0]["fetched_at"] == BASE + 60
    assert merged == normalize_news(merged)


def test_older_long_article_cannot_roll_back_newer_short_correction():
    current = normalize_news(article(content="更正后的完整正文。", fetched_at=BASE + 60))
    old = normalize_news(article(content="过时但较长的旧正文。" * 20, fetched_at=BASE))
    merged = normalize_news(merge_news(current, old))
    assert merged["content"] == current["content"]
    assert merged["source_articles"][0]["content"] == current["content"]
    assert merged["source_articles"][0]["fetched_at"] == BASE + 60


def test_sequential_duplicate_merge_keeps_latest_fetch_for_version_ordering():
    original = normalize_news(article(content="第一版较长正文。" * 30, fetched_at=BASE))
    corrected = normalize_news(article(content="第三版更正正文。", fetched_at=BASE + 120))
    delayed = normalize_news(article(content="第二版旧正文。" * 20, fetched_at=BASE + 60))
    merged = merge_news(merge_news(original, corrected), delayed)
    assert merged["content"] == corrected["content"]
    assert merged["fetched_at"] == BASE + 120


def test_secondary_source_text_remains_available_for_stock_keyword_rematching():
    left = normalize_news(article("企业重要业务调整事项正式公告", summary="厦门钨业披露新增产能计划。", fetched_at=BASE))
    right = normalize_news(article(left["title"], content="其他相关背景事实。" * 20,
                                   url="https://another.example/story", source="来源乙", fetched_at=BASE + 60))
    merged = normalize_news(merge_news(left, right))
    assert merged["content"] == right["content"] and "厦门钨业" not in merged["summary"]
    assert merged["source_articles"][0]["summary"] == left["summary"]
    assert merged["source_articles"][1]["content"] == right["content"]
    evidence = news_watchlist_matcher.article_match_evidence(merged, ["600549.SS"], {"600549.SS": ["厦门钨业"]})
    assert "600549.SS" in evidence
    assert any(hit["value"] == "厦门钨业" and hit.get("source") == "来源甲" for hit in evidence["600549.SS"])


def test_source_excerpt_limits_and_cleaning_match_primary_material():
    row = normalize_news(article(summary="<b>摘</b>" * 9000, content="<b>文</b>" * 17000))
    source = row["source_articles"][0]
    assert source["summary"] == row["summary"] and len(source["summary"]) <= 8000
    assert source["content"] == row["content"] and len(source["content"]) <= 16000
    assert "<" not in source["summary"] + source["content"]
    assert row == normalize_news(row)


def test_legacy_source_metadata_is_filled_only_from_its_exact_primary_article():
    primary = article(summary="主来源摘要中的公司关键词。", content="主来源全文。", fetched_at=BASE)
    primary_source = {key: primary[key] for key in ("title", "url", "source", "published_ts")}
    row = normalize_news({**primary, "source_articles": [
        {**primary_source, "related_codes": ["1.600519"]},
        {**primary_source, "source": "另一来源", "related_codes": ["0.300302"]},
    ]})
    own, another = row["source_articles"]
    assert own["summary"] == primary["summary"] and own["content"] == primary["content"]
    assert own["related_codes"] == ["1.600519"]
    assert another["summary"] == another["content"] == ""
    assert row == normalize_news(row)


def test_normalizer_creates_source_once_and_never_treats_a_string_as_a_list():
    normalized = normalize_news(article(related_codes="1.600519", matched_symbols="AAPL", fetched_at=BASE))
    assert normalized["related_codes"] == normalized["matched_symbols"] == []
    assert len(normalized["source_articles"]) == 1
    assert normalized == normalize_news(normalized)


def test_collector_merge_retains_longest_source_and_all_platform_codes():
    left = article(related_codes=["1.600519"])
    right = article(source="来源乙", url="https://another.example/story", related_codes=["0.300302"],
                    content="完整正文。" * 50)
    merged = cn_market_news._merge_groups([[left], [right]], major_first=False)
    assert len(merged) == 1
    assert merged[0]["source"] == "来源乙" and merged[0]["content"] == right["content"]
    assert len(merged[0]["source_articles"]) == 2
    assert merged[0]["related_codes"] == ["1.600519", "0.300302"]


def test_rss_search_symbol_is_only_a_hint():
    rows = rss_fetcher._parse_feed(b'<rss version="2.0"><channel><item><title>Unrelated</title><link>https://news.example/a</link></item></channel></rss>', "Google search", "AAPL")
    assert rows[0]["symbol_hint"] == "AAPL"
    assert not rows[0].get("matched_symbols")
