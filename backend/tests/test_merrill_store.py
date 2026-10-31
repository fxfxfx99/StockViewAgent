from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
import pytest

from app.storage import merrill_store as store


def test_catalog_has_twenty_distinct_assets_and_explicit_proxies():
    catalog = store.catalog()
    assert len(catalog) == len({row["id"] for row in catalog}) == 20
    assert all(row["row_count"] == 0 and row["missing_weeks"] > 2500 for row in catalog)
    assert all(row["source_url"].startswith("https://") for row in catalog)
    assert all(row["proxy_note"] for row in catalog if row["is_proxy"])
    dax = next(row for row in catalog if row["id"] == "dax")
    assert not dax["is_proxy"] and dax["price_basis"] == "performance_index"


def test_upsert_is_idempotent_tracks_actual_day_and_does_not_regress_with_older_observation():
    rows = [{"date": "2026-10-02", "close": 9, "observation_date": "2026-10-01"},
            {"date": "2026-10-02", "close": 10, "observation_date": "2026-10-02"}]
    store.upsert_weekly("sp500", rows, requested_from="2026-09-25", requested_to="2026-10-16")
    store.upsert_weekly("sp500", rows)
    store.upsert_weekly("sp500", [{"date": "2026-10-02", "close": 8, "observation_date": "2026-10-01"}])
    assert store.get_series(["sp500"])[0]["points"] == [{"date": "2026-10-02", "close": 10, "observation_date": "2026-10-02"}]
    row = store.catalog()[0]
    assert row["row_count"] == 1 and row["missing_weeks"] == 3 and row["coverage_ratio"] == 0.25
    assert store.get_series(["sp500"], start="2026-10-03")[0]["points"] == []


def test_negative_commodity_values_survive_and_invalid_batch_is_atomic():
    store.upsert_weekly("crude_wti", [{"date": "2020-04-24", "close": -37.63}])
    assert store.get_series(["crude_wti"])[0]["points"][0]["close"] == -37.63
    with pytest.raises(ValueError):
        store.upsert_weekly("crude_wti", [{"date": "2020-05-01", "close": 30}, {"date": "2020-05-08", "close": float("nan")}])
    assert len(store.get_series(["crude_wti"])[0]["points"]) == 1
    with pytest.raises(ValueError):
        store.upsert_weekly("sp500", [{"date": "2026-10-05", "close": 10}])
    next_friday = store.completed_friday() + timedelta(days=7)
    with pytest.raises(ValueError):
        store.upsert_weekly("sp500", [{"date": next_friday.isoformat(), "close": 10}])


def test_monthly_macro_not_expanded_to_weekly_and_analysis_is_scoped():
    store.save_macro("CN_CPI_YOY", [{"date": "2026-08-01", "value": -0.5}, {"date": "2026-09-01", "value": 0}])
    store.save_macro("CN_CPI_YOY", [{"date": "2026-09-01", "value": 0.1}])
    assert store.get_macro()["CN_CPI_YOY"] == [{"date": "2026-08-01", "value": -0.5}, {"date": "2026-09-01", "value": 0.1}]
    store.save_analysis(1, {"text": "美国"}, region="US")
    store.save_analysis(1, {"text": "中国"}, region="CN")
    assert store.get_analysis(1, "US") == {"text": "美国"}
    assert store.get_analysis(1, "CN") == {"text": "中国"}
    assert store.get_analysis(2, "US") is None
    with pytest.raises(ValueError):
        store.get_analysis(0)
    store.set_state("queue", {"status": "pending"})
    assert store.get_state("queue") == {"status": "pending"}


def test_concurrent_upserts_keep_one_observation_per_asset_week():
    store.init_db()
    def write(value):
        store.upsert_weekly("sp500", [{"date": "2026-10-02", "close": value}])
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write, range(1, 9)))
    points = store.get_series(["sp500"])[0]["points"]
    assert len(points) == 1 and points[0]["close"] in range(1, 9)
