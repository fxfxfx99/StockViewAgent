"""Company-specific event matching, temporal isolation and schema enforcement."""
import copy
from types import SimpleNamespace

import pytest

from app.schemas.analysis import ImpactResult
from app.services import analysis_context, impact_analyzer

NOW = 1788926400
SYMBOL = "600519.SS"
STOCK = {"stock_code": SYMBOL, "stock_name": "测试公司", "industry": "制造业", "main_business": "出口设备"}
ARTICLE = {"id": "event", "title": "人民币升值影响出口订单", "content": "人民币升值，企业出口设备订单仍待确认。", "published_ts": NOW}


def snapshot(**updates):
    return {"version_id": "week-one", "stock_code": SYMBOL, "as_of": "2026-09-08T04:00:00Z",
            "created_at": "2026-09-08T04:00:00Z", "coverage": {"supported": 1},
            "features": [{"feature_id": "export_business", "label": "出口设备业务", "value": "公司生产出口设备，未披露海外收入比例。",
                          "evidence_source_ids": ["annual-report"]}],
            "factor_links": [{"factor_id": "fx_exposure", "feature_ids": ["export_business"],
                              "event_keywords": ["人民币升值", "汇率"], "operating_metric": "出口收入及汇兑损益",
                              "conditions": ["需验证结算币种与套期保值安排"]}],
            "evidence_sources": [{"source_id": "annual-report", "stock_code": SYMBOL, "title": "公司年报",
                                  "text": "公司生产出口设备，未披露海外收入比例。", "url": "https://example.org/report",
                                  "source": "年报", "observed_at": "2026-09-08T03:00:00Z"}], **updates}


@pytest.fixture
def saved(monkeypatch):
    from app import services

    calls = []
    state = {"snapshot": snapshot()}

    def get_snapshot(symbol, as_of=None):
        calls.append((symbol, as_of))
        return copy.deepcopy(state["snapshot"])

    monkeypatch.setattr(services, "stock_features", SimpleNamespace(get_snapshot=get_snapshot, get_snapshot_identity=get_snapshot), raising=False)
    monkeypatch.setattr(analysis_context, "time", SimpleNamespace(time=lambda: NOW))
    return state, calls


def context():
    profiles, sources = analysis_context._stock_feature_context(ARTICLE, [STOCK], NOW)
    return profiles[SYMBOL], sources


def test_event_factor_match_requires_this_companys_evidenced_exposure(saved):
    profile, sources = context()
    assert profile["version_id"] == "week-one"
    factor = profile["matched_factors"][0]
    assert factor["factor_id"] == "fx_exposure" and factor["matched_terms"] == ["人民币升值"]
    assert factor["feature_ids"] == ["export_business"]
    assert factor["evidence_source_ids"] == ["F1_1"]
    assert sources[0]["stock_code"] == SYMBOL and sources[0]["feature_version"] == "week-one"
    assert saved[1] == [(SYMBOL, "2026-09-09T04:00:00Z")]


@pytest.mark.parametrize("updates,status", [
    ({"stock_code": "AAPL"}, "invalid"),
    ({"as_of": "2026-09-10T04:00:00Z"}, "future_version"),
    ({"version_id": ""}, "invalid"),
])
def test_wrong_stock_and_future_library_are_never_used(saved, updates, status):
    saved[0]["snapshot"].update(updates)
    profile, sources = context()
    assert profile["status"] == status
    assert not profile["features"] and not sources


def test_unknown_event_time_does_not_fetch_latest_library(saved):
    profiles, sources = analysis_context._stock_feature_context(ARTICLE, [STOCK], None)
    assert profiles[SYMBOL]["status"] == "unknown_event_time" and not sources
    assert saved[1] == []


def test_missing_historical_snapshot_remains_missing_without_latest_fallback(saved):
    saved[0]["snapshot"] = None
    profile, sources = context()
    assert profile["status"] == "missing" and not sources
    assert all(as_of is not None for _, as_of in saved[1])
    assert "不得用当前资料" in profile["limitations"][0]


@pytest.mark.parametrize("source_change", [{"stock_code": "AAPL"}, {"observed_at": "2026-09-10T00:00:00Z"}, {"source_id": "different"}])
def test_feature_without_visible_own_source_cannot_match(saved, source_change):
    saved[0]["snapshot"]["evidence_sources"][0].update(source_change)
    profile, sources = context()
    assert profile["status"] == "no_evidence"
    assert not profile["features"] and not profile["matched_factors"] and not sources


def test_irrelevant_event_is_not_assigned_a_generic_company_factor(saved):
    profiles, sources = analysis_context._stock_feature_context({**ARTICLE, "title": "召开股东大会", "content": "公司披露会议时间。"}, [STOCK], NOW)
    assert profiles[SYMBOL]["features"] and sources
    assert profiles[SYMBOL]["matched_factors"] == []
    assert "未命中" in profiles[SYMBOL]["limitations"][0]


def test_latin_keywords_do_not_match_inside_another_word(saved):
    saved[0]["snapshot"]["factor_links"][0]["event_keywords"] = ["AI"]
    profiles, _ = analysis_context._stock_feature_context({**ARTICLE, "title": "retail sales", "content": "retail growth"}, [STOCK], NOW)
    assert not profiles[SYMBOL]["matched_factors"]


def test_reuse_signature_tracks_visible_version_and_preserves_legacy_without_library(saved):
    signature = analysis_context.feature_context_signature(ARTICLE, [SYMBOL])[SYMBOL]
    prior = {"stock_code": SYMBOL, "stock_feature_signature": signature}
    assert analysis_context.analysis_features_current(ARTICLE, prior)
    assert not analysis_context.analysis_features_current(ARTICLE, {"stock_code": SYMBOL})
    saved[0]["snapshot"]["version_id"] = "week-one-revised"
    assert not analysis_context.analysis_features_current(ARTICLE, prior)
    saved[0]["snapshot"] = None
    assert analysis_context.analysis_features_current(ARTICLE, {"stock_code": SYMBOL})
    assert not analysis_context.analysis_features_current(ARTICLE, prior)


def feature_impact():
    return {"factor_id": "fx_exposure", "feature_ids": ["export_business"], "feature_version": "week-one",
            "exposure": "公司已披露出口设备业务，但海外收入占比未知。",
            "operating_impact": "人民币升值若使外币收入折算减少，可能压低收入和毛利率。",
            "valuation_impact": "利润预期下修可能影响估值，缺少一致预期及价格数据，无法判断已定价程度。",
            "conditions": ["需确认实际结算币种与定价调整。"], "offsets": ["若存在外币采购或套保，影响可能抵消。"],
            "watch_points": ["观察下一期披露的海外收入和汇兑损益。"], "evidence_source_ids": ["F1_1"]}


def validation_payload():
    profile, sources = context()
    payload = {"context": {"stock_features": {SYMBOL: profile}, "evidence_sources": [
        {"source_id": "N0", "title": ARTICLE["title"], "text": ARTICLE["content"]}, *sources]}}
    row = {"stock_code": SYMBOL, "feature_impacts": [feature_impact()],
           "evidence": [{"source_id": "N0", "quote": "人民币升值"}, {"source_id": "F1_1", "quote": "公司生产出口设备"}]}
    return row, payload


def test_validated_transmission_has_real_factor_feature_version_and_sources(saved):
    row, payload = validation_payload()
    impact_analyzer._validate_evidence([row], payload)
    impact_analyzer._validate_feature_impacts([row], payload)


@pytest.mark.parametrize("field,value,error", [
    ("factor_id", "generic-industry", "unknown_or_duplicate_factor"),
    ("feature_version", "future-week", "wrong_version"),
    ("feature_ids", ["other-stock-export"], "wrong_stock_feature"),
    ("evidence_source_ids", ["N0"], "unsupported_source"),
])
def test_fabricated_transmission_rejected_by_code_not_only_prompt(saved, field, value, error):
    row, payload = validation_payload()
    row["feature_impacts"][0][field] = value
    with pytest.raises(ValueError, match=error):
        impact_analyzer._validate_feature_impacts([row], payload)


def test_missing_feature_path_and_cross_stock_evidence_are_rejected(saved):
    row, payload = validation_payload()
    row["feature_impacts"] = []
    with pytest.raises(ValueError, match="matched_factor_required"):
        impact_analyzer._validate_feature_impacts([row], payload)
    payload["context"]["evidence_sources"][1]["stock_code"] = "AAPL"
    with pytest.raises(ValueError, match="wrong_stock_feature"):
        impact_analyzer._validate_evidence([row], payload)


def test_each_feature_needs_its_own_cited_support(saved):
    row, payload = validation_payload()
    row["evidence"] = row["evidence"][:1]
    with pytest.raises(ValueError, match="unsupported_source"):
        impact_analyzer._validate_feature_impacts([row], payload)


def test_unmatched_feature_context_limits_confidence_without_fabricating_direction(saved):
    profiles, _ = analysis_context._stock_feature_context({**ARTICLE, "title": "公司公告", "content": "变更办公地点。"}, [STOCK], NOW)
    result = {"confidence": 95, "relevance_type": "macro", "impact_direction": "bearish"}
    impact_analyzer._apply_quality_limit(result, STOCK, {"stock_features": profiles, "quality": {}})
    assert result["confidence"] == 50 and result["impact_direction"] == "bearish"
    assert any("未命中" in note for note in result["quality_notes"])


def test_feature_path_schema_remains_optional_for_legacy_inputs():
    assert "feature_impacts" not in ImpactResult.model_json_schema()["required"]


def test_created_after_event_snapshot_is_rejected_even_with_backdated_asof(saved):
    saved[0]["snapshot"]["created_at"] = "2026-09-10T04:00:00Z"
    profile, sources = context()
    assert profile["status"] == "future_version" and not sources


def test_actual_engine_output_produces_traceable_event_channels(saved):
    from app.services import stock_feature_engine

    built = stock_feature_engine.build_from_inputs(SYMBOL, fundamentals={"basic_info": {
        "main_business": "公司从事出口设备的生产与销售，海外业务以美元结算。",
        "industry_name_c": "设备制造", "observed_at": "2026-09-08T03:00:00Z",
    }}, as_of="2026-09-08T04:00:00Z")
    saved[0]["snapshot"] = {**built, "version_id": "engine-week-one", "created_at": "2026-09-08T04:00:00Z"}
    profiles, sources = analysis_context._stock_feature_context({**ARTICLE, "content": "美元兑人民币下跌，出口设备订单待确认。"}, [STOCK], NOW)
    profile = profiles[SYMBOL]
    assert any(factor["factor_id"] == "fx_trade" for factor in profile["matched_factors"])
    source_ids = {source["source_id"] for source in sources}
    for factor in profile["matched_factors"]:
        assert set(factor["evidence_source_ids"]) <= source_ids
        assert all(ref["source_id"] in source_ids for ref in factor["evidence"])
    assert any("候选" in note for note in profile["limitations"])


def test_long_source_keeps_event_specific_quotes_and_bounds_factor_features(saved):
    row = saved[0]["snapshot"]
    row["evidence_sources"][0]["text"] = "本期其他经营披露。" * 1000 + "公司出口设备以美元结算。"
    row["features"][0]["evidence"] = [{"source_id": "annual-report", "quote": "公司出口设备以美元结算。"}]
    for index in range(30):
        row["features"].append({**row["features"][0], "feature_id": f"export_{index}"})
        row["factor_links"][0]["feature_ids"].append(f"export_{index}")
    profile, sources = context()
    assert "公司出口设备以美元结算。" in sources[0]["text"]
    assert len(sources[0]["text"]) <= 6000
    assert len(profile["features"]) <= 24
    assert set(profile["matched_factors"][0]["feature_ids"]) <= {f["feature_id"] for f in profile["features"]}


def test_model_must_repair_missing_structured_company_channel(saved, monkeypatch):
    row, payload = validation_payload()
    valid = {**row, "sentiment": "negative", "impact_direction": "bearish", "impact_strength": 35,
             "time_horizon": "medium_term", "confidence": 65,
             "reasoning": "人民币升值可能通过公司出口业务影响收入折算和利润，但需确认结算币种与套期保值后才能判断净影响。",
             "event_summary": "本条新闻披露人民币升值，出口订单仍待确认。", "event_type": "macro",
             "impact_channels": ["人民币升值改变跨境业务的收入折算条件。", "已披露出口业务可能出现收入和利润预期调整。"],
             "realization_conditions": ["确认美元收入与对冲情况。"], "expectation_gap": "未提供市场一致预期，无法判断是否已经定价。",
             "confidence_reason": "公司披露出口设备业务，但尚缺少币种和净暴露规模。", "counter_arguments": ["自然对冲可能抵消。"],
             "watch_points": ["观察海外收入和汇兑损益。"], "invalidation_conditions": ["若公司出口不涉及外币结算，需重估传导。"],
             "key_factors": ["汇率与出口业务。"], "risk_points": ["未披露净外币敞口。"], "facts": ["新闻披露人民币升值。"],
             "inferences": ["收入折算可能变化。"], "uncertainties": ["净敞口未知。"]}
    invalid = {**valid, "feature_impacts": []}
    responses = iter([([invalid], None), ([valid], None)])
    calls = []
    def complete(messages, **kwargs):
        calls.append(copy.deepcopy(messages))
        return next(responses)
    monkeypatch.setattr(impact_analyzer, "settings", SimpleNamespace(analysis_integrity_retries=1))
    monkeypatch.setattr(impact_analyzer.llm_client, "chat_completion_json_array", complete)
    result, error = impact_analyzer._validated_call("stock_impact_analysis", payload, ImpactResult, [SYMBOL])
    assert error is None and result[0]["feature_impacts"][0]["factor_id"] == "fx_exposure"
    assert "feature_impacts:matched_factor_required" in calls[1][0]["content"]


def test_disclosure_date_does_not_override_precise_prior_observation(saved):
    saved[0]["snapshot"]["evidence_sources"][0].update(published_at="2026-09-09", observed_at="2026-09-09T03:00:00Z")
    profile, sources = context()
    assert profile["matched_factors"] and sources
    saved[0]["snapshot"]["evidence_sources"][0]["published_at"] = "2026-09-10"
    assert not context()[0]["matched_factors"]


def test_matching_exposure_is_selected_before_large_financial_field_batch(saved):
    saved[0]["snapshot"]["features"] = [
        {"feature_id": f"coded_{index}", "value": index, "evidence_source_ids": ["annual-report"]}
        for index in range(120)
    ] + saved[0]["snapshot"]["features"]
    profile, _ = context()
    assert profile["matched_factors"][0]["feature_ids"] == ["export_business"]


def test_specific_input_exposure_rejects_other_commodity_price_news(saved):
    row = saved[0]["snapshot"]
    row["features"][0]["value"] = "公司原材料采购主要为电解铜。"
    row["factor_links"][0].update(scope_terms=["电解铜"], event_keywords=["涨价", "原材料", "电解铜"])
    profiles, _ = analysis_context._stock_feature_context({**ARTICLE, "title": "原油涨价", "content": "原油价格上涨。"}, [STOCK], NOW)
    assert not profiles[SYMBOL]["matched_factors"]
    profiles, _ = analysis_context._stock_feature_context({**ARTICLE, "title": "电解铜涨价", "content": "电解铜原材料价格上涨。"}, [STOCK], NOW)
    assert profiles[SYMBOL]["matched_factors"][0]["matched_scope_terms"] == ["电解铜"]


def test_scoped_factor_only_selects_features_supporting_event_object(saved):
    row = saved[0]["snapshot"]
    row["features"][0]["value"] = "公司向欧洲出口设备。"
    row["features"].append({**row["features"][0], "feature_id": "usd_export", "value": "公司出口业务以美元结算。"})
    row["factor_links"][0].update(scope_terms=["欧洲", "美元"], feature_ids=["export_business", "usd_export"], event_keywords=["美元", "汇率"])
    profiles, _ = analysis_context._stock_feature_context({**ARTICLE, "title": "美元汇率变动", "content": "美元汇率下降。"}, [STOCK], NOW)
    factor = profiles[SYMBOL]["matched_factors"][0]
    assert factor["feature_ids"] == ["usd_export"] and factor["matched_scope_terms"] == ["美元"]


def test_unchanged_weekly_check_refreshes_freshness_without_invalidating_facts(saved):
    saved[0]["snapshot"]["as_of"] = "2026-08-01T04:00:00Z"
    signature = analysis_context.feature_context_signature(ARTICLE, [SYMBOL])[SYMBOL]
    assert context()[0]["age_days"] > 8
    saved[0]["snapshot"]["last_checked_at"] = "2026-09-08T04:00:00Z"
    profile, _ = context()
    assert profile["age_days"] == 1
    assert not any("超过一周" in note for note in profile["limitations"])
    assert profile["signature"] == signature
    assert analysis_context.analysis_features_current(ARTICLE, {"stock_code": SYMBOL, "stock_feature_signature": signature})


def test_future_weekly_check_cannot_refresh_historical_evidence(saved):
    saved[0]["snapshot"]["last_checked_at"] = "2026-09-10T04:00:00Z"
    profile, sources = context()
    assert profile["status"] == "future_version" and not sources


def test_archive_cache_check_reads_identity_without_loading_large_snapshot(saved, monkeypatch):
    from app import services

    row = saved[0]["snapshot"]
    row["content_hash"] = "stored-content-digest"
    signature = analysis_context.feature_context_signature(ARTICLE, [SYMBOL])[SYMBOL]
    identity = {key: row[key] for key in ("stock_code", "version_id", "as_of", "created_at", "content_hash")}
    calls = []
    def full_snapshot(*args, **kwargs):
        raise AssertionError("Archive cache validation must not parse the full snapshot")
    def read_identity(symbol, as_of=None):
        calls.append((symbol, as_of))
        return identity.copy()
    monkeypatch.setattr(services.stock_features, "get_snapshot", full_snapshot)
    monkeypatch.setattr(services.stock_features, "get_snapshot_identity", read_identity)
    prior = {"stock_code": SYMBOL, "stock_feature_signature": signature}
    for _ in range(20):
        assert analysis_context.analysis_features_current(ARTICLE, prior)
    assert len(calls) == 20 and all(cutoff == "2026-09-09T04:00:00Z" for _, cutoff in calls)
    identity["content_hash"] = "changed-content-digest"
    assert not analysis_context.analysis_features_current(ARTICLE, prior)
