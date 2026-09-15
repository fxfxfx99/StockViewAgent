import json
from types import SimpleNamespace

from app.storage import data_asset_manager, kline_bundle_cache


def test_atomic_write_json_updates_catalog_and_keeps_backups(monkeypatch, tmp_path):
    monkeypatch.setattr(
        data_asset_manager,
        "settings",
        SimpleNamespace(data_dir=tmp_path, enable_data_asset_backups=True, data_asset_max_backups=2),
    )
    target = tmp_path / "watchlists" / "1.json"

    data_asset_manager.atomic_write_json(
        target,
        {"symbols": ["600519.SS"]},
        kind="watchlist",
        key="1",
        source="test",
        item_count=1,
    )
    data_asset_manager.atomic_write_json(
        target,
        {"symbols": ["300302.SZ"]},
        kind="watchlist",
        key="1",
        source="test",
        item_count=1,
    )

    assert json.loads(target.read_text(encoding="utf-8"))["symbols"] == ["300302.SZ"]
    catalog = json.loads((tmp_path / "_managed" / "catalog.json").read_text(encoding="utf-8"))
    asset = catalog["assets"]["watchlists/1.json"]
    assert asset["kind"] == "watchlist"
    assert asset["item_count"] == 1
    assert len(asset["backups"]) == 1
    assert (tmp_path / asset["backups"][0]).exists()


def test_kline_cache_registers_data_asset(monkeypatch, tmp_path):
    monkeypatch.setattr(
        data_asset_manager,
        "settings",
        SimpleNamespace(data_dir=tmp_path, enable_data_asset_backups=True, data_asset_max_backups=2),
    )
    monkeypatch.setattr(kline_bundle_cache, "settings", data_asset_manager.settings)

    kline_bundle_cache.save_bundle(
        "300302.SZ",
        "1y",
        "1d",
        {"kline_source": "test_source", "candles": [{"t": 1, "c": 10}]},
    )

    catalog = json.loads((tmp_path / "_managed" / "catalog.json").read_text(encoding="utf-8"))
    asset = catalog["assets"]["kline_bundle_cache/300302.SZ_1y_1d.json"]
    assert asset["kind"] == "kline_bundle_cache"
    assert asset["source"] == "test_source"
    assert asset["item_count"] == 1
