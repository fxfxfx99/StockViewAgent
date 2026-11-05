import json
import sqlite3
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from app.services import stock_feature_engine as engine


AS_OF = "2026-06-30T23:59:59+08:00"


def record(**values):
    return {"updated_at": "2026-04-01T12:00:00+08:00", "report_date": "2025-12-31", "source_path": "issuer.csv", **values}


def bars(start="2026-01-01", count=140, slope=1):
    first = date.fromisoformat(start)
    days = [first + timedelta(days=i) for i in range(250)]
    days = [day for day in days if day.weekday() < 5][:count]
    return [{"date": day.isoformat(), "close": 100 + i * slope} for i, day in enumerate(days)]


def test_specific_quotes_and_conditional_mechanisms_are_traceable():
    out = engine.build_from_inputs("SH600519", fundamentals={
        "basic_info": record(short_name="示例股份", main_business="公司生产新能源电池。", industry_name_c="电气机械"),
        "mda": record(mana_disc_anal="公司原材料包括碳酸锂，采购成本受市场报价影响。公司海外客户主要位于欧洲，出口以美元结算。公司有浮动利率借款。"),
        "financial": record(raw_json=json.dumps({"毛利率": "23.5%", "FI_T1.F010101A": "1.23"})),
    }, as_of=AS_OF)
    assert out["stock_code"] == "600519.SS"
    assert out["name"] == "示例股份"
    assert out["stats"]["model_calls"] == 0
    ids = {source["id"] for source in out["evidence_sources"]}
    for feature in out["features"]:
        assert set(feature["source_ids"]) <= ids
        assert feature["evidence"][0]["quote"]
    links = {link["id"]: link for link in out["factor_links"]}
    assert {"input_cost", "customer_demand", "fx_trade", "funding_cost"} <= links.keys()
    assert "碳酸锂" in str(links["input_cost"]["evidence"])
    for link in links.values():
        assert link["direction"]["price"] == "unknown"
        assert link["status"] == "conditional"
        assert link["conditions"] and link["counter_factors"] and link["watch_points"]
        assert link["missing"]
    finance = {f["label"]: f for f in out["features"] if f["category"] == "financial"}
    assert finance["毛利率"]["value"] == 23.5
    assert finance["毛利率"]["unit"] == "%"
    assert finance["FI_T1.F010101A"]["unit"] is None
    assert finance["FI_T1.F010101A"]["status"] == "candidate"


def test_industry_tag_and_denial_do_not_invent_exposure():
    out = engine.build_from_inputs("600519", fundamentals={"basic_info": record(industry_name_c="有色金属"),
        "mda": record(mana_disc_anal="公司不涉及海外业务。公司不存在浮动利率借款。")}, as_of=AS_OF)
    assert not out["factor_links"]
    assert out["coverage"]["status"] == "partial"


def test_historical_cutoff_is_observation_not_accounting_period():
    out = engine.build_from_inputs("600519", fundamentals={"basic_info": record(updated_at="2026-07-01T01:00:00+08:00", main_business="截止日后修订"),
        "mda": {"report_date": "2025-12-31", "mana_disc_anal": "无可核验观测时间的客户描述"},
        "financial": record(raw={"published_at": "2026-07-02T00:00:00+08:00", "毛利率": "90%"})}, as_of=AS_OF)
    assert not out["features"]
    assert out["coverage"]["status"] == "empty"
    assert len(out["coverage"]["missing"]) >= 3
    # Current snapshots can display legacy evidence, explicitly lacking availability.
    current = engine.build_from_inputs("600519", fundamentals={"basic_info": {"main_business": "生产设备"}})
    assert current["features"]
    assert current["evidence_sources"][0]["availability"] == "unknown"
    assert not current["evidence_sources"][0]["historical_availability_verified"]


def test_latest_eligible_disclosure_and_stable_single_value_identity():
    first = engine.build_from_inputs("600519", fundamentals={"financial": record(raw={"毛利率": "20%"})}, as_of=AS_OF)
    second = engine.build_from_inputs("600519", fundamentals={"financial": [record(raw={"毛利率": "21%"}),
        record(report_date="2026-06-30", updated_at="2026-07-02T00:00:00+08:00", raw={"毛利率": "80%"})]}, as_of=AS_OF)
    assert first["features"][0]["id"] == second["features"][0]["id"]
    assert second["features"][0]["value"] == 21
    assert first["features"][0]["source_ids"] != second["features"][0]["source_ids"]


def test_market_statistics_use_complete_past_bars_and_exact_benchmark_endpoints():
    prices = bars(count=140)
    out = engine.build_from_inputs("600519", candles=prices, benchmark_candles=prices,
        as_of="2026-03-31T14:00:00+08:00", market_source={"benchmark_symbol": "000300.SS", "adjustment": "qfq"})
    stats = out["stats"]["market_behavior"]
    assert stats["window_end"] == "2026-03-30"
    assert stats["sample_size"] == 62
    assert stats["beta"] == pytest.approx(1)
    assert stats["beta_sample_size"] == 62
    expected = ((162 / 142) - 1) * 100
    assert stats["return_20d_pct"] == pytest.approx(expected, abs=0.0001)
    assert stats["volatility_60d_pct"] > 0
    assert stats["max_drawdown_120d_pct"] == 0


def test_event_responses_report_benchmark_and_future_windows_are_excluded():
    prices = bars(count=140)
    events = [{"id": "past", "title": "公司海外客户订单增长", "published_at": "2026-03-10T12:00:00+08:00", "observed_at": "2026-03-10T13:00:00+08:00"},
              {"id": "future", "title": "未来事件", "published_at": "2026-09-10T12:00:00+08:00"}]
    out = engine.build_from_inputs("600519", fundamentals={"mda": record(mana_disc_anal="公司海外客户主要位于欧洲，出口以美元结算。")},
        candles=prices, benchmark_candles=prices, events=events, as_of="2026-03-20", market_source={"benchmark_symbol": "000300.SS"})
    reactions = out["stats"]["event_reactions"]
    assert reactions["sample_size"] == 1
    assert {row["window_sessions"] for row in reactions["items"]} == {1, 5}
    assert all(row["excess_return_pct"] == 0 for row in reactions["items"])
    assert all(row["window_end"] <= "2026-03-20" for row in reactions["items"])
    assert reactions["excluded_events"] == 1
    # No benchmark is explicitly unknown rather than replaced by zero.
    no_benchmark = engine.build_from_inputs("600519", candles=prices, events=events, as_of="2026-03-20")
    assert all(row["excess_return_pct"] is None for row in no_benchmark["stats"]["event_reactions"]["items"])


def test_late_market_cache_is_not_a_historical_input():
    out = engine.build_from_inputs("600519", candles=bars(), as_of=AS_OF,
        market_source={"saved_at": "2026-07-02T00:00:00+08:00"})
    assert out["stats"]["market_behavior"]["sample_size"] == 0
    assert any("行情缓存晚于" in reason for reason in out["coverage"]["missing"])


def test_local_builder_readonly_uses_revision_and_explicit_stock_news(monkeypatch, tmp_path):
    db = tmp_path / "company_fundamentals.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE company_record_revisions(id INTEGER, dataset TEXT, symbol TEXT, logical_key TEXT, report_date TEXT, raw_json TEXT, observed_at TEXT)")
        conn.executemany("INSERT INTO company_record_revisions VALUES(?,?,?,?,?,?,?)", [
            (1, "basic_info", "600519", "basic:2025", "2025-12-31", json.dumps({"MAINBUSSINESS": "旧版本主营产品", "ShortName": "旧名称"}), "2026-01-01T00:00:00+00:00"),
            (2, "basic_info", "600519", "basic:2025", "2025-12-31", json.dumps({"MAINBUSSINESS": "未来修订产品", "ShortName": "未来名称"}), "2026-08-01T00:00:00+00:00")])
    news_path = tmp_path / "news.db"
    with sqlite3.connect(news_path) as conn:
        conn.execute("CREATE TABLE articles(id TEXT, title TEXT, symbol_hint TEXT, related_codes TEXT, published_ts REAL, saved_at REAL)")
        epoch = engine._cutoff("2026-03-01").timestamp()
        conn.executemany("INSERT INTO articles VALUES(?,?,?,?,?,?)", [
            ("direct", "直接关联新闻", "600519.SS", "[]", epoch, epoch),
            ("other", "其他公司的新闻", "000001.SZ", '["000001.SZ"]', epoch, epoch)])
    cache = tmp_path / "kline_bundle_cache"
    cache.mkdir()
    (cache / "600519.SS_1y_1d.json").write_text(json.dumps({"saved_at": "2026-06-01T00:00:00+00:00", "payload": {"candles": bars(count=90)}}))
    monkeypatch.setattr(engine, "settings", SimpleNamespace(data_dir=tmp_path, database_path=news_path))
    before = {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    out = engine.build_snapshot("SH600519", as_of=AS_OF)
    after = {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert before == after
    assert out["name"] == "旧名称"
    assert "未来修订" not in str(out)
    reactions = out["stats"]["event_reactions"]
    assert reactions["sample_size"] == 1
    assert {row["event_id"] for row in reactions["items"]} == {"direct"}


def test_no_files_creates_no_assets_and_empty_coverage(monkeypatch, tmp_path):
    empty = tmp_path / "does-not-exist"
    monkeypatch.setattr(engine, "settings", SimpleNamespace(data_dir=empty, database_path=empty / "news.db"))
    result = engine.build_snapshot("000001.SZ")
    assert not empty.exists()
    assert result["coverage"]["status"] == "empty"
    assert result["stats"]["market_behavior"]["beta"] is None


def test_scope_limits_commodity_and_geography_event_matches():
    out = engine.build_from_inputs("600519", fundamentals={"mda": record(mana_disc_anal="公司主要采购原材料碳酸锂。公司海外客户位于欧洲，以美元结算。")},
        candles=bars(), events=[{"id": "oil", "title": "国际原油价格涨价", "published_at": "2026-04-10T12:00:00+08:00", "observed_at": "2026-04-10T13:00:00+08:00"},
        {"id": "lithium", "title": "碳酸锂采购价格上涨", "published_at": "2026-04-10T12:00:00+08:00", "observed_at": "2026-04-10T13:00:00+08:00"}], as_of=AS_OF)
    factors = {link["id"]: link for link in out["factor_links"]}
    assert factors["input_cost"]["scope_terms"] == ["碳酸锂"]
    assert "油价" not in factors["input_cost"]["event_keywords"]
    assert set(factors["fx_trade"]["scope_terms"]) == {"美元", "欧洲"}
    rows = out["stats"]["event_reactions"]["items"]
    assert all("input_cost" not in row["factor_ids"] for row in rows if row["event_id"] == "oil")
    assert all("input_cost" in row["factor_ids"] for row in rows if row["event_id"] == "lithium")


def test_local_universe_company_updates_and_dedicated_index_cache(monkeypatch, tmp_path):
    observed = engine._cutoff("2026-04-01").timestamp()
    (tmp_path / "company_basic_universe.json").write_text(json.dumps({"symbols": {"600519.SS": {
        "auto": {"name": "全市场缓存名称", "main_business": "公司采购原材料碳酸锂。"}, "em_fetched_at": observed}}}))
    updates = tmp_path / "company_updates"
    updates.mkdir()
    (updates / "600519.SS.json").write_text(json.dumps({"bundle": {"company": {"intro": "海外客户位于欧洲，以美元结算。"},
        "section_fetched_at": {"company": observed}, "sources": {"company": "雪球"}}}))
    cache = tmp_path / "kline_bundle_cache"
    cache.mkdir()
    payload = {"saved_at": "2026-06-01T00:00:00+00:00", "payload": {"candles": bars(count=90)}}
    for symbol in ("600519.SS", "INDEX_HS300"):
        (cache / f"{symbol}_1y_1d.json").write_text(json.dumps(payload))
    monkeypatch.setattr(engine, "settings", SimpleNamespace(data_dir=tmp_path, database_path=tmp_path / "news.db"))
    snapshot = engine.build_snapshot("600519", as_of=AS_OF)
    assert {source["kind"] for source in snapshot["evidence_sources"]} >= {"universe_company", "company_update"}
    assert snapshot["stats"]["market_behavior"]["benchmark"] == "INDEX_HS300"
    assert snapshot["stats"]["market_behavior"]["beta"] == 1
    assert all(source["observed_at"] for source in snapshot["evidence_sources"])
    assert {link["id"] for link in snapshot["factor_links"]} >= {"input_cost", "fx_trade"}


@pytest.mark.parametrize("relative_path,error_code", [
    ("company_fundamentals.db", "fundamentals_read_failed"),
    ("news.db", "events_read_failed"),
    ("watchlist_profiles.json", "profiles_json_read_failed"),
    ("company_basic_universe.json", "company_universe_json_read_failed"),
    ("company_updates/600519.SS.json", "company_update_json_read_failed"),
    ("kline_bundle_cache/600519.SS_1y_1d.json", "stock_market_json_read_failed"),
    ("kline_bundle_cache/INDEX_HS300_1y_1d.json", "benchmark_market_json_read_failed"),
])
def test_corrupt_existing_source_blocks_refresh_publication(monkeypatch, tmp_path, relative_path, error_code):
    path = tmp_path / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"broken-source-secret-must-not-be-exposed")
    monkeypatch.setattr(engine, "settings", SimpleNamespace(data_dir=tmp_path, database_path=tmp_path / "news.db"))
    snapshot = engine.build_snapshot("600519")
    assert snapshot["coverage"]["source_errors"] == [error_code]
    assert "broken-source-secret" not in json.dumps(snapshot)
    assert path.read_bytes() == b"broken-source-secret-must-not-be-exposed"


def test_invalid_json_root_is_failure_but_missing_sources_are_only_gaps(monkeypatch, tmp_path):
    monkeypatch.setattr(engine, "settings", SimpleNamespace(data_dir=tmp_path, database_path=tmp_path / "news.db"))
    assert engine.build_snapshot("600519")["coverage"]["source_errors"] == []
    (tmp_path / "watchlist_profiles.json").write_text("[]")
    assert engine.build_snapshot("600519")["coverage"]["source_errors"] == ["profiles_json_read_failed"]


def test_existing_json_io_failure_is_not_a_missing_source(monkeypatch, tmp_path):
    path = tmp_path / "watchlist_profiles.json"
    path.write_text("{}")
    def denied(*args):
        raise PermissionError("sensitive path")
    monkeypatch.setattr(engine, "_read_json_cached", denied)
    monkeypatch.setattr(engine, "settings", SimpleNamespace(data_dir=tmp_path, database_path=tmp_path / "news.db"))
    snapshot = engine.build_snapshot("600519")
    assert snapshot["coverage"]["source_errors"] == ["profiles_json_read_failed"]
    assert "sensitive path" not in json.dumps(snapshot)


def test_corrupt_json_inside_sqlite_source_is_reported(monkeypatch, tmp_path):
    with sqlite3.connect(tmp_path / "company_fundamentals.db") as conn:
        conn.execute("CREATE TABLE company_basic_records(symbol TEXT, report_date TEXT, raw_json TEXT)")
        conn.execute("INSERT INTO company_basic_records VALUES('600519','2025-12-31','{broken')")
    monkeypatch.setattr(engine, "settings", SimpleNamespace(data_dir=tmp_path, database_path=tmp_path / "news.db"))
    snapshot = engine.build_snapshot("600519")
    assert snapshot["coverage"]["source_errors"] == ["fundamentals_read_failed"]
