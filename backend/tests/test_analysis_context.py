"""Real archive selection and input quality, using only isolated fixture data."""
from types import SimpleNamespace

import pytest

from app.services import analysis_context
from app.storage import news_store


NOW = 1788926400
SYMBOL = "600519.SS"
STOCK = {"stock_code": SYMBOL, "stock_name": "测试公司", "org_name": "测试公司股份有限公司",
         "industry": "食品制造", "main_business": "生产和销售食品"}


@pytest.fixture
def archive(tmp_path, monkeypatch):
    monkeypatch.setattr(news_store, "settings", SimpleNamespace(data_dir=tmp_path, database_path=tmp_path / "news.db"))
    monkeypatch.setattr(analysis_context, "time", SimpleNamespace(time=lambda: NOW))
    news_store.init_db()


def article(title="本次公司公告", **updates):
    result = {"title": title, "summary": "公司公告提供的原始摘要。", "published_ts": NOW - 3600,
            "source": "合成测试来源", "link": f"https://news.example.org/{title}",
            "matched_symbols": [SYMBOL], "related_codes": updates.get("matched_symbols", [SYMBOL]), **updates}
    published = result["published_ts"]
    result.setdefault("fetched_at", published + 60 if isinstance(published, (int, float)) else NOW)
    return result


def test_history_requires_stock_link_and_prior_publication_within_seven_days(archive):
    rows = [article("本次公告"), article("此前公告", published_ts=NOW - 7200),
            article("之后公告", published_ts=NOW - 1800), article("未来公告", published_ts=NOW + 1800),
            article("过期公告", published_ts=NOW - 9 * 86400), article("日期不详", published_ts=None),
            article("其他公司", published_ts=NOW - 7200, matched_symbols=["AAPL"])]
    news_store.upsert_many(rows)
    context = analysis_context.build_analysis_context(rows[0], [STOCK])
    assert [source["title"] for source in context["news_background"]] == ["此前公告"]
    prior = context["news_background"][0]
    assert prior["text"] == rows[1]["summary"] and prior["url"] == rows[1]["link"]
    assert prior["article_id"] == rows[1]["id"] and prior["published_at"]
    assert prior in context["evidence_sources"] and prior["source_id"] == "H1"


def test_historical_analysis_only_selects_source_news_never_reuses_generated_claims(archive):
    current = article()
    hints = article("来源标签匹配", matched_symbols=[], symbol_hint=SYMBOL, published_ts=NOW - 7200)
    accepted = article("历史模型识别", matched_symbols=[], published_ts=NOW - 7300)
    weak = article("低相关分析", matched_symbols=[], published_ts=NOW - 7400)
    failed = article("失败分析", matched_symbols=[], published_ts=NOW - 7500)
    news_store.upsert_many([current, hints, accepted, weak, failed])
    for row, status, score in [(accepted, "analyzed", 60), (weak, "analyzed", 59), (failed, "error", 99)]:
        news_store.save_analysis(row["id"], {"stock_code": SYMBOL, "status": status,
                                           "relevance_score": score, "reasoning": "不应当作为原始证据的模型意见"})
    context = analysis_context.build_analysis_context(current, [STOCK])
    assert {source["title"] for source in context["news_background"]} == {"历史模型识别"}
    assert all(source["text"] == accepted["summary"] for source in context["news_background"])
    assert "不应当作为原始证据" not in str(context)


def test_history_is_bounded_and_deduplicates_current_title_and_url(archive):
    current = article()
    duplicate_url = article("同一来源的不同标题", link=current["link"], published_ts=NOW - 7200)
    duplicate_title = article(current["title"], link="https://news.example.org/duplicate", published_ts=NOW - 86400)
    rows = [current, duplicate_url, duplicate_title,
            *[article(f"历史公告 {index}", content="保留的正文" * 600, published_ts=NOW - 2 * 86400 - index)
              for index in range(6)]]
    news_store.upsert_many(rows)
    raw = news_store.analysis_background([SYMBOL], exclude_id=current["id"], before_ts=current["published_ts"], limit=999)
    assert len(raw) == 4 and current["id"] not in {row["id"] for row in raw}
    context = analysis_context.build_analysis_context(current, [STOCK])
    assert all(row["title"].startswith("历史公告") for row in context["news_background"])
    assert all(len(row["text"]) <= 1800 for row in context["news_background"])
    assert len(context["news_background"]) <= 4


@pytest.mark.parametrize("content,summary,kind,cap", [
    ("", "", "title_only", 40), ("", "简短摘要", "summary", 60),
    ("", "完整摘要材料" * 30, "summary", 80), ("简短正文", "", "full_text", 60),
    ("完整来源正文" * 30, "摘要", "full_text", 100),
])
def test_quality_uses_actual_available_text(archive, content, summary, kind, cap):
    item = article(content=content, summary=summary)
    context = analysis_context.build_analysis_context(item, [STOCK])
    assert context["quality"]["text_kind"] == kind and context["quality"]["confidence_cap"] == cap
    source = context["evidence_sources"][0]
    assert source["source_id"] == "N0" and source["text"] == (content or summary)
    assert source["title"] == item["title"]


@pytest.mark.parametrize("summary", ["公司披露经营数据", "公司披露经营数据 合成测试来源"])
def test_repeated_headline_is_not_treated_as_substantive_summary(archive, summary):
    item = article("公司披露经营数据 - 合成测试来源", summary=summary)
    context = analysis_context.build_analysis_context(item, [STOCK])
    assert context["quality"]["text_kind"] == "title_only" and context["quality"]["confidence_cap"] == 40
    assert context["evidence_sources"][0]["text"] == item["summary"]  # Preserve the exact source material.


@pytest.mark.parametrize("published", [None, "bad", float("nan"), float("inf"), NOW + 3600, NOW + 3 * 86400])
def test_unverifiable_date_is_explicit_and_never_reads_future_background(archive, published):
    current = article(content="完整来源正文" * 30, published_ts=published)
    future = article("尚未发布的消息", published_ts=NOW + 3600)
    news_store.upsert_many([future])
    context = analysis_context.build_analysis_context(current, [STOCK])
    assert not context["quality"]["publication_date_known"] and context["quality"]["confidence_cap"] == 55
    assert context["evidence_sources"][0]["published_at"] is None
    assert not context["news_background"]
    assert any("发布时间" in note for note in context["quality"]["limitations"])


def test_company_profile_contains_only_saved_fields_and_reports_missing_context(archive):
    blank = {"stock_code": "AAPL", "stock_name": "", "industry": "", "main_business": ""}
    context = analysis_context.build_analysis_context(article(), [STOCK, blank])
    profiles = [source for source in context["evidence_sources"] if source["kind"] == "company_profile"]
    assert profiles[0]["stock_code"] == SYMBOL and "main_business: 生产和销售食品" in profiles[0]["text"]
    assert profiles[1]["text"] == "stock_code: AAPL" and not profiles[1]["url"]
    assert any("AAPL" in note for note in context["quality"]["limitations"])


def test_history_failure_preserves_current_evidence_and_explains_limitation(archive, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("synthetic storage failure")
    monkeypatch.setattr(news_store, "analysis_background", fail)
    context = analysis_context.build_analysis_context(article(), [STOCK])
    assert not context["news_background"] and context["evidence_sources"][0]["source_id"] == "N0"
    assert any("历史背景暂不可读取" in note for note in context["quality"]["limitations"])
    assert "synthetic storage failure" not in str(context)


def test_major_event_attention_is_a_hint_not_additional_evidence_or_confidence(archive):
    item = article("测试公司：关于终止重大资产重组的公告", summary="公司宣布终止本次重组，后续经营影响仍需核实。")
    context = analysis_context.build_analysis_context(item, [STOCK])
    assert context["attention"]["level"] == "major"
    assert context["quality"]["confidence_cap"] == 60
    assert {row["source_id"] for row in context["evidence_sources"]} == {"N0", "P1"}
    assert context["evidence_sources"][0]["text"] == item["summary"]
    ordinary = article("测试公司：股东会召开通知", summary="关于召开股东会的时间与地点。",
                       attention={"level": "major", "rank": 2})
    assert analysis_context.build_analysis_context(ordinary, [STOCK])["attention"]["level"] == "normal"


def test_later_body_update_cannot_enter_an_earlier_events_background(archive):
    current = article()
    previous = article("先前披露订单", published_ts=NOW - 7200, content="公司披露订单。")
    news_store.upsert_many([current, previous])
    assert len(analysis_context.build_analysis_context(current, [STOCK])["news_background"]) == 1
    # A repeat fetch of unchanged text retains the original observation time.
    news_store.upsert_many([{**previous, "fetched_at": NOW}])
    assert len(analysis_context.build_analysis_context(current, [STOCK])["news_background"]) == 1
    # The publisher keeps the old publication date but adds a later development.
    news_store.upsert_many([{**previous, "content": "公司披露订单。最新更新：公司宣布订单已被取消。", "fetched_at": NOW}])
    assert not analysis_context.build_analysis_context(current, [STOCK])["news_background"]
    # It is valid evidence for a subsequent event after we observed the update.
    later = news_store.analysis_background([SYMBOL], exclude_id=current["id"], before_ts=NOW + 60)
    assert later[0]["content_observed_at"] == NOW and "订单已被取消" in later[0]["content"]


def test_retroactively_collected_article_is_not_assumed_known_before_observation(archive):
    current = article()
    news_store.upsert_many([article("事后补采", published_ts=NOW - 7200, fetched_at=NOW)])
    assert not analysis_context.build_analysis_context(current, [STOCK])["news_background"]
