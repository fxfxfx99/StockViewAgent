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


def test_inventory_includes_backups_and_relative_categories(monkeypatch, tmp_path):
    monkeypatch.setattr(data_asset_manager, 'settings', SimpleNamespace(data_dir=tmp_path, enable_data_asset_backups=True, data_asset_max_backups=2))
    (tmp_path / 'research/batches').mkdir(parents=True)
    (tmp_path / 'research/batches/sample.parquet').write_bytes(b'evidence')
    (tmp_path / 'issuer_uploads/files').mkdir(parents=True)
    (tmp_path / 'issuer_uploads/files/note.txt').write_text('learning')
    (tmp_path / '_managed/backups').mkdir(parents=True)
    (tmp_path / '_managed/backups/previous.json').write_text('{"old":true}')
    scanned = data_asset_manager.refresh_catalog_from_disk()
    expected = [p for p in tmp_path.rglob('*') if p.is_file()]
    assert scanned['directory_totals']['files'] == len(expected)
    assert scanned['directory_totals']['size_bytes'] == sum(p.stat().st_size for p in expected)
    assert sum(c['size_bytes'] for c in scanned['categories']) == scanned['directory_totals']['size_bytes']
    assert scanned['data_root'] == str(tmp_path)
    catalog = json.loads((tmp_path / '_managed/catalog.json').read_text())
    assert catalog['assets']['research/batches/sample.parquet']['category'] == 'research'
    assert catalog['assets']['issuer_uploads/files/note.txt']['category'] == 'knowledge'
    assert '_managed/backups/previous.json' in catalog['assets']
    assert all(not key.startswith('/') for key in catalog['assets'])


def test_parallel_json_writers_do_not_lose_index_entries(monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setattr(data_asset_manager, 'settings', SimpleNamespace(data_dir=tmp_path, enable_data_asset_backups=False, data_asset_max_backups=0))
    def write(i):
        data_asset_manager.atomic_write_json(tmp_path / f'{i}.json', {'n': i}, kind='test', key=str(i))
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(30)))
    catalog = json.loads((tmp_path / '_managed/catalog.json').read_text())
    assert {f'{i}.json' for i in range(30)} <= catalog['assets'].keys()


def test_inventory_does_not_follow_external_symlinks(monkeypatch, tmp_path):
    root = tmp_path / 'data'
    root.mkdir()
    outside = tmp_path / 'private'
    outside.mkdir()
    (outside / 'key.txt').write_text('not a data asset')
    (root / 'external').symlink_to(outside, target_is_directory=True)
    (root / 'external-file').symlink_to(outside / 'key.txt')
    monkeypatch.setattr(data_asset_manager, 'settings', SimpleNamespace(data_dir=root, enable_data_asset_backups=False, data_asset_max_backups=0))
    inventory = data_asset_manager.scan_data_assets()
    assert not any('external' in item['path'] for item in inventory['items'])
    assert 'not a data asset' not in json.dumps(inventory)
