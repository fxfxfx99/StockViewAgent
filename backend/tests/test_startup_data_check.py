import asyncio
import time

from app.services import startup_data_check


def test_refresh_stock_list_skips_when_fresh(monkeypatch):
    monkeypatch.setattr(
        startup_data_check.a_share_stocks,
        "meta",
        lambda: {"count": 10, "updated_at": int(time.time())},
    )

    def fail_refresh():
        raise AssertionError("fresh stock list should not refresh")

    monkeypatch.setattr(startup_data_check.a_share_stocks, "fetch_and_save", fail_refresh)

    out = asyncio.run(startup_data_check._refresh_stock_list_if_stale())

    assert out["status"] == "skipped"
    assert out["reason"] == "fresh"
    assert out["count"] == 10


def test_refresh_stock_list_runs_when_stale(monkeypatch):
    monkeypatch.setattr(
        startup_data_check.a_share_stocks,
        "meta",
        lambda: {"count": 10, "updated_at": 1},
    )
    monkeypatch.setattr(
        startup_data_check.a_share_stocks,
        "fetch_and_save",
        lambda: {"count": 11, "updated_at": 2},
    )

    out = asyncio.run(startup_data_check._refresh_stock_list_if_stale())

    assert out["status"] == "ok"
    assert out["reason"] == "refreshed"
    assert out["count"] == 11
