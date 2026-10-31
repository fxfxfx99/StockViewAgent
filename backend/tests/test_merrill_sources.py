import asyncio
from datetime import date, datetime, timezone
import math

import httpx
import pytest

from app.services import merrill_sources as sources
from app.storage import merrill_store as store


def test_aggregation_excludes_incomplete_week_and_does_not_fill_gaps():
    rows = [
        {"date": "2026-10-01", "close": 100},
        {"date": "2026-10-02", "close": 101},
        {"date": "2026-10-02", "close": 102},
        {"date": "2026-10-03", "close": 999},  # weekend is not a trading session
        {"date": "2026-10-15", "close": -37.6},
        {"date": "2026-10-16", "close": None},
        {"date": "2026-10-16", "close": float("nan")},
        {"date": "2026-10-19", "close": 50},
        {"date": "2026-10-23", "close": 51},
    ]
    assert sources.aggregate_weekly(rows, today=date(2026, 10, 23)) == [
        {"date": "2026-10-02", "close": 102, "observation_date": "2026-10-02"},
        {"date": "2026-10-16", "close": -37.6, "observation_date": "2026-10-15"},
    ]
    assert sources.aggregate_weekly(rows, today=date(2026, 10, 24))[-1]["close"] == 51
    assert store.completed_friday(date(2026, 10, 19)) == date(2026, 10, 16)


def test_yahoo_uses_exchange_day_and_does_not_silently_mix_adjusted_prices():
    stamp = int(datetime(2026, 10, 1, 23, tzinfo=timezone.utc).timestamp())
    payload = {"chart": {"result": [{"meta": {"exchangeTimezoneName": "Asia/Tokyo"},
                "timestamp": [stamp], "indicators": {"quote": [{"close": [10]}], "adjclose": [{"adjclose": [8]}]}}]}}
    assert sources.parse_yahoo(payload, adjusted=True, kind="etf") == [{"date": "2026-10-02", "close": 8}]
    payload["chart"]["result"][0]["indicators"].pop("adjclose")
    with pytest.raises(sources.SourceUnavailable):
        sources.parse_yahoo(payload, adjusted=True, kind="etf")
    payload["chart"]["result"][0]["indicators"]["quote"][0]["close"] = [-37.6]
    assert sources.parse_yahoo(payload, kind="futures_price")[0]["close"] == -37.6
    assert sources.parse_yahoo(payload, kind="index") == []


def test_fred_missing_values_stay_missing_and_zero_negative_are_real_numbers():
    text = "observation_date,RATE\n2020-01-01,0\n2020-02-01,.\n2020-03-01,-1\n2020-03-01,-2\n2020-04-01,NaN\n2020-05-01,inf\n"
    assert sources.parse_fred_csv(text, "RATE") == [{"date": "2020-01-01", "value": 0}, {"date": "2020-03-01", "value": -2}]
    with pytest.raises(sources.SourceUnavailable):
        sources.parse_fred_csv("<html>Rate limited</html>", "RATE")


def test_china_preserves_yoy_and_excludes_combined_january_february_industry():
    payload = {"success": True, "result": {"data": [
        {"REPORT_DATE": "2026-02-01 00:00:00", "BASE_SAME": 5.9, "NATIONAL_SAME": -0.7},
        {"REPORT_DATE": "2026-03-01 00:00:00", "BASE_SAME": 6.0, "NATIONAL_SAME": 0.0},
    ]}}
    assert sources.parse_china_macro(payload, "CN_INDUSTRY_YOY") == [{"date": "2026-03-01", "value": 6.0}]
    assert sources.parse_china_macro(payload, "CN_CPI_YOY") == [{"date": "2026-02-01", "value": -0.7}, {"date": "2026-03-01", "value": 0}]


def test_official_fallback_parsers_keep_exact_series_and_months():
    text = '"B50001: Total index"\n"B50001" 2026 100.1 100.2 -999 NA\n"B00004" 2026 5 6\n'
    assert sources.parse_federal_reserve_ip(text) == [{"date": "2026-01-01", "value": 100.1}, {"date": "2026-02-01", "value": 100.2}]
    payload = {"status": "REQUEST_SUCCEEDED", "Results": {"series": [{"seriesID": "CUSR0000SA0", "data": [
        {"year": "2026", "period": "M09", "value": "336.298"},
        {"year": "2026", "period": "M13", "value": "333"}]}]}}
    assert sources.parse_bls_cpi(payload) == [{"date": "2026-09-01", "value": 336.298}]


def test_fred_outage_uses_same_official_industrial_series_with_provenance():
    def respond(request):
        if request.url.host == "fred.stlouisfed.org":
            return httpx.Response(503)
        assert request.url.host == "www.federalreserve.gov"
        return httpx.Response(200, text='"B50001" 2025 101 102 103 104 105 106 107 108 109 110 111 112\n')
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await sources.fetch_macro(client, "INDPRO", date(2025, 1, 1), date(2025, 12, 31))
    rows, digest, provenance = asyncio.run(run())
    assert len(rows) == 12 and len(digest) == 64
    assert provenance["source_symbol"] == "B50001"
    assert provenance["source_url"].startswith("https://www.federalreserve.gov/")
    assert provenance["fallback_reason"] == "公共来源HTTP 503"


def test_partial_sync_preserves_old_observations_and_records_error(monkeypatch):
    store.upsert_weekly("gold", [{"date": "2026-10-02", "close": 2000}])
    monkeypatch.setattr(sources, "ASSETS", [store.ASSET_CATALOG[0], next(x for x in store.ASSET_CATALOG if x["id"] == "gold")])
    monkeypatch.setattr(sources, "MACRO_CATALOG", {})
    monkeypatch.setattr(sources, "getproxies", lambda: {})
    async def market(client, asset, start, end):
        if asset["id"] == "gold":
            raise sources.SourceUnavailable("公共来源HTTP 429")
        return [{"date": "2026-10-02", "close": 5000}], "abc"
    monkeypatch.setattr(sources, "fetch_market", market)
    status = asyncio.run(sources.sync_all(full=True))
    assert status["status"] == "partial" and status["series_count"] == 1
    assert status["errors"] == [{"id": "gold", "error": "公共来源HTTP 429"}]
    assert store.get_series(["gold"])[0]["points"][0]["close"] == 2000
    assert next(row for row in store.catalog() if row["id"] == "gold")["last_error"] == "公共来源HTTP 429"


def test_csi300_paginates_same_index_daily_data_without_price_adjustment_substitution():
    requested = []
    def respond(request):
        window = request.url.params["param"]
        requested.append(window)
        _, period, lower, upper, cap, _ = window.split(",")
        assert period == "day" and cap == "640"
        return httpx.Response(200, json={"code": 0, "data": {"sh000300": {"day": [[lower, "100", "101", "102", "99", "1000"]]}}})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await sources.fetch_csi300(client, date(1976, 10, 31), date(2010, 10, 29))
    rows, digest = asyncio.run(run())
    assert len(requested) == 3
    assert [row["date"] for row in rows] == ["2005-01-01", "2007-01-01", "2009-01-01"]
    assert requested[-1].endswith("2010-10-29,640,qfq")
    assert len(digest) == 64


def test_pmi_is_a_monthly_level_not_a_year_over_year_transformation():
    payload = {"success": True, "result": {"data": [
        {"REPORT_DATE": "2026-09-01 00:00:00", "MAKE_INDEX": 50.2, "MAKE_SAME": 0.6024} ]}}
    rows = sources.parse_china_macro(payload, "CN_PMI")
    assert rows == [{"date": "2026-09-01", "value": 50.2}]
    store.save_macro("CN_PMI", rows)
    assert store.get_macro()["CN_PMI"] == rows
