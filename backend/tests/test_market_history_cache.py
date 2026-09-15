from types import SimpleNamespace

from app.storage import market_history_cache
from app.storage import data_asset_manager


def test_market_history_cache_merges_dates_and_keeps_latest(monkeypatch, tmp_path):
    monkeypatch.setattr(market_history_cache, "_path", lambda _kind, _key: tmp_path / "series.json")
    monkeypatch.setattr(
        data_asset_manager,
        "settings",
        SimpleNamespace(data_dir=tmp_path, enable_data_asset_backups=True, data_asset_max_backups=2),
    )
    market_history_cache.merge_save_series(
        "margin", "market", [{"trade_date": "2026-07-01", "rqye": 1}]
    )
    saved = market_history_cache.merge_save_series(
        "margin",
        "market",
        [
            {"trade_date": "2026-07-01", "rqye": 2},
            {"trade_date": "2026-07-02", "rqye": 3},
        ],
    )
    assert [row["trade_date"] for row in saved["items"]] == ["2026-07-02", "2026-07-01"]
    assert saved["items"][1]["rqye"] == 2
