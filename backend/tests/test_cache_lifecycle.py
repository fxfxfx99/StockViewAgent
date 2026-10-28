from datetime import datetime, timezone
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.storage import cache_lifecycle as lifecycle, data_asset_manager as assets

NOW = 1792800000.0
DAY = 86400


@pytest.fixture
def root(monkeypatch, tmp_path):
    monkeypatch.setattr(assets, "settings", SimpleNamespace(data_dir=tmp_path, enable_data_asset_backups=True, data_asset_max_backups=2))
    monkeypatch.setattr(lifecycle.time, "time", lambda: NOW)
    return tmp_path


def cache(root, path="market_source_cache/indices/main.json", *, age=20, saved_age=None, payload=None):
    file = root / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps({"saved_at": datetime.fromtimestamp(NOW - (age if saved_age is None else saved_age) * DAY, timezone.utc).isoformat(),
                                "payload": payload or {"price": 3}}))
    os.utime(file, (NOW - age * DAY, NOW - age * DAY))
    return file


def test_cleanup_is_previewed_reversible_and_refreshes_index(root):
    original = cache(root)
    before = original.read_bytes()
    plan = lifecycle.preview_cleanup()
    assert plan["total_files"] == 1
    assert original.exists()
    assert "sha256" not in plan["items"][0]
    result = lifecycle.execute_cleanup(plan["plan_id"])
    assert result["moved_files"] == 1 and result["disk_space_freed_bytes"] == 0
    assert not original.exists()
    assert (root / result["items"][0]["quarantine_path"]).read_bytes() == before
    index = json.loads((root / "_managed/catalog.json").read_text())
    assert str(original.relative_to(root)) not in index["assets"]
    assert lifecycle.list_quarantine()["total"] == 1
    restored = lifecycle.restore_quarantine(result["batch_id"])
    assert restored["restored_files"] == 1
    assert original.read_bytes() == before
    assert lifecycle.restore_quarantine(result["batch_id"])["restored_files"] == 0


def test_replaying_plan_is_idempotent(root):
    cache(root)
    plan = lifecycle.preview_cleanup()
    first = lifecycle.execute_cleanup(plan["plan_id"])
    second = lifecycle.execute_cleanup(plan["plan_id"])
    assert first == second
    assert lifecycle.list_quarantine()["total"] == 1


@pytest.mark.parametrize("path", ["news.db", "news.db-wal", "economic_events.sqlite3", "data_center.sqlite3", "tracking.sqlite3", "tracking.sqlite3-wal",
                                  "datasets/macro/cpi.json", "research/raw/evidence.json", "issuer_uploads/raw.json",
                                  "watchlists/1.json", "integrations.json", "market_history_cache/fx/history.json",
                                  "arbitrary_cache/password.json", "market_source_cache/_health/market_sources.json",
                                  "market_source_cache/snapshot/data.db", "market_source_cache/snapshot/token.json"])
def test_durable_assets_and_unrecognized_cache_directories_cannot_move(root, path):
    original = cache(root, path)
    plan = lifecycle.preview_cleanup([path])
    assert plan["total_files"] == 0 and plan["skipped"]
    assert original.exists()


@pytest.mark.parametrize("path", ["../outside.json", "/tmp/outside.json", "a/../b.json", "a//b.json", "./a.json", "C:/a.json", "a\\b.json", "a\x00b"])
def test_unsafe_paths_rejected(root, path):
    with pytest.raises(lifecycle.CacheLifecycleError):
        lifecycle.preview_cleanup([path])


def test_symlink_and_hardlink_files_are_refused(root, tmp_path):
    outside = root / "private.json"
    outside.write_text("secret")
    folder = root / "market_source_cache"
    folder.mkdir()
    (folder / "alias.json").symlink_to(outside)
    os.link(outside, folder / "hard.json")
    plan = lifecycle.preview_cleanup(["market_source_cache/alias.json", "market_source_cache/hard.json"])
    assert plan["total_files"] == 0 and len(plan["skipped"]) == 2
    assert outside.read_text() == "secret"


def test_symlink_parent_is_never_followed(root):
    outside = root / "outside"
    outside.mkdir()
    cache(root, "outside/old.json")
    (root / "market_source_cache").symlink_to(outside, target_is_directory=True)
    plan = lifecycle.preview_cleanup(["market_source_cache/old.json"])
    assert plan["total_files"] == 0
    assert (outside / "old.json").exists()


def test_symlink_management_directory_refused_without_external_writes(root):
    outside = root / "outside"
    outside.mkdir()
    (root / "_managed").symlink_to(outside, target_is_directory=True)
    with pytest.raises((ValueError, OSError)):
        lifecycle.preview_cleanup()
    assert list(outside.iterdir()) == []


def test_cache_changed_after_preview_is_not_moved(root):
    original = cache(root)
    plan = lifecycle.preview_cleanup()
    original.write_text('{"saved_at":"2026-01-01T00:00:00Z","payload":{"new":true}}')
    os.utime(original, (NOW - 20 * DAY, NOW - 20 * DAY))
    result = lifecycle.execute_cleanup(plan["plan_id"])
    assert result["moved_files"] == 0
    assert "已更新" in result["items"][0]["error"]
    assert json.loads(original.read_text())["payload"]["new"]


def test_cache_parent_replaced_after_preview_cannot_escape(root):
    original = cache(root)
    plan = lifecycle.preview_cleanup()
    source = original.parent
    moved = source.with_name("previous")
    source.rename(moved)
    source.symlink_to(moved, target_is_directory=True)
    result = lifecycle.execute_cleanup(plan["plan_id"])
    assert result["moved_files"] == 0
    assert (moved / "main.json").exists()


def test_restore_does_not_overwrite_new_cache(root):
    original = cache(root)
    result = lifecycle.execute_cleanup(lifecycle.preview_cleanup()["plan_id"])
    original.write_text("newly fetched data")
    restored = lifecycle.restore_quarantine(result["batch_id"])
    assert restored["restored_files"] == 0
    assert "已有文件" in restored["items"][0]["restore_error"]
    assert original.read_text() == "newly fetched data"
    assert (root / result["items"][0]["quarantine_path"]).exists()


def test_restore_detects_quarantine_corruption(root):
    cache(root)
    result = lifecycle.execute_cleanup(lifecycle.preview_cleanup()["plan_id"])
    quarantined = root / result["items"][0]["quarantine_path"]
    quarantined.write_text("modified")
    restored = lifecycle.restore_quarantine(result["batch_id"])
    assert restored["restored_files"] == 0
    assert "校验失败" in restored["items"][0]["restore_error"]
    assert quarantined.exists()


def test_expired_preview_cannot_execute(root):
    original = cache(root)
    plan = lifecycle.preview_cleanup()
    with pytest.raises(lifecycle.CacheLifecycleError, match="过期"):
        lifecycle.execute_cleanup(plan["plan_id"], now=NOW + 1801)
    assert original.exists()


def test_policy_change_revalidated_at_execution(root):
    original = cache(root)
    plan = lifecycle.preview_cleanup()
    lifecycle.update_policy({"ttl_days": {"market_source_cache": 30}})
    result = lifecycle.execute_cleanup(plan["plan_id"])
    assert result["moved_files"] == 0 and original.exists()


@pytest.mark.parametrize("patch", [{"enabled": 1}, {"unknown": 4}, {"ttl_days": None}, {"ttl_days": {"research": 1}},
                                   {"ttl_days": {"market_source_cache": 0}}, {"ttl_days": {"market_source_cache": True}},
                                   {"budget_bytes": 10}, {"max_batch_files": 0}, {"max_batch_bytes": -1}])
def test_invalid_policy_rejected(root, patch):
    with pytest.raises(lifecycle.CacheLifecycleError):
        lifecycle.update_policy(patch)
    assert lifecycle.get_policy()["enabled"] is False


def test_partial_policy_updates_preserve_other_rules(root):
    lifecycle.update_policy({"enabled": True})
    result = lifecycle.update_policy({"ttl_days": {"market_source_cache": 15}})
    assert result["enabled"] is True
    assert result["ttl_days"] == {"market_source_cache": 15, "kline_bundle_cache": 90, "tushare_daily_cache": 180}


def test_maintenance_requires_opt_in_and_never_purges_quarantine(root):
    original = cache(root)
    assert lifecycle.run_maintenance()["status"] == "disabled"
    assert original.exists()
    lifecycle.update_policy({"enabled": True})
    result = lifecycle.run_maintenance()
    assert result["status"] == "completed" and result["moved_files"] == 1
    assert lifecycle.run_maintenance()["status"] == "no_expired_cache"
    assert (root / result["items"][0]["quarantine_path"]).exists()


def test_batch_limit_leaves_other_expired_files_available_for_next_run(root):
    cache(root, "market_source_cache/snapshot/a.json", age=30)
    cache(root, "market_source_cache/snapshot/b.json", age=20)
    lifecycle.update_policy({"max_batch_files": 1})
    plan = lifecycle.preview_cleanup()
    assert plan["total_files"] == 1
    assert plan["items"][0]["path"].endswith("a.json")
    assert len(plan["skipped"]) == 1
    lifecycle.execute_cleanup(plan["plan_id"])
    assert lifecycle.preview_cleanup()["total_files"] == 1


def test_budget_pressure_never_evicts_fresh_cache(root):
    original = cache(root, age=1)
    with original.open("ab") as handle:
        handle.truncate(65 * 1024 ** 2)
    os.utime(original, (NOW, NOW))
    lifecycle.update_policy({"budget_bytes": 64 * 1024 ** 2})
    plan = lifecycle.preview_cleanup()
    assert plan["over_budget_bytes"] == 1024 ** 2
    assert plan["total_files"] == 0 and original.exists()


@pytest.mark.parametrize("body", ["SQLite format 3\u0000database", "not json", '{"token":"secret"}', '{"payload":{},"saved_at":"invalid"}'])
def test_unrecognized_payload_is_preserved(root, body):
    original = cache(root)
    original.write_text(body)
    os.utime(original, (NOW - 20 * DAY, NOW - 20 * DAY))
    assert lifecycle.preview_cleanup()["total_files"] == 0
    assert original.exists()


def test_saved_at_prevents_misclassification_of_preserved_old_mtime(root):
    cache(root, age=20, saved_age=1)
    plan = lifecycle.preview_cleanup()
    assert plan["total_files"] == 0 and "保存时间" in plan["skipped"][0]["reason"]


def test_restore_unknown_or_tampered_paths_is_rejected(root):
    cache(root)
    batch = lifecycle.execute_cleanup(lifecycle.preview_cleanup()["plan_id"])
    with pytest.raises(lifecycle.CacheLifecycleError):
        lifecycle.restore_quarantine(batch["batch_id"], ["market_source_cache/missing.json"])
    manifest = root / f"_managed/quarantine/{batch['batch_id']}/manifest.json"
    payload = json.loads(manifest.read_text())
    payload["items"][0]["path"] = "../outside.json"
    manifest.write_text(json.dumps(payload))
    with pytest.raises(lifecycle.CacheLifecycleError):
        lifecycle.restore_quarantine(batch["batch_id"])


def test_inventory_pagination_filters_and_durable_classification(root):
    cache(root, "market_source_cache/snapshot/a.json")
    cache(root, "market_source_cache/snapshot/b.json", age=1)
    cache(root, "datasets/fx/rates.json")
    assets.register_existing_asset(root / "market_source_cache/snapshot/a.json", kind="market_source_cache", key="a", source="test_source")
    result = assets.query_data_assets(lifecycle="expired", source="test_source", q="a.json")
    assert result["total"] == 1 and result["items"][0]["protected"] is False
    assert "test_source" in result["sources"] and "data_center" in result["sources"]
    assert result["summary"]["expired_bytes"] == (root / "market_source_cache/snapshot/a.json").stat().st_size
    assert result["summary"]["cache_bytes"] > result["summary"]["expired_bytes"]
    protected = assets.query_data_assets(q="datasets/fx", lifecycle="protected")
    assert protected["items"][0]["data_domain"] == "fx"
    page = assets.query_data_assets(category="cache", limit=1, offset=1, sort="path", descending=False)
    assert page["total"] == 2 and page["items"][0]["path"].endswith("b.json")


@pytest.mark.parametrize("kwargs", [{"sort": "__class__"}, {"category": "missing"}, {"lifecycle": "delete"},
                                    {"limit": 0}, {"limit": 501}, {"offset": -1}, {"q": "a" * 201}])
def test_inventory_invalid_filters_rejected(root, kwargs):
    with pytest.raises(ValueError):
        assets.query_data_assets(**kwargs)


def test_operations_have_private_permissions(root):
    cache(root)
    plan = lifecycle.preview_cleanup()
    path = root / f"_managed/cache/plans/{plan['plan_id']}.json"
    assert path.stat().st_mode & 0o077 == 0
    assert path.parent.stat().st_mode & 0o077 == 0


def test_manual_purge_removes_only_unchanged_quarantined_files(root):
    original = cache(root)
    size = original.stat().st_size
    batch = lifecycle.execute_cleanup(lifecycle.preview_cleanup()["plan_id"])
    result = lifecycle.purge_quarantine(batch["batch_id"])
    assert result["purged_files"] == 1 and result["freed_bytes"] == size
    assert not result["skipped"]
    assert not (root / batch["items"][0]["quarantine_path"]).exists()
    assert lifecycle.purge_quarantine(batch["batch_id"])["purged_files"] == 0
    assert lifecycle.restore_quarantine(batch["batch_id"])["restored_files"] == 0
    assert lifecycle.list_quarantine()["items"][0]["items"][0]["status"] == "purged"


def test_manual_purge_preserves_recreated_original_cache(root):
    original = cache(root)
    batch = lifecycle.execute_cleanup(lifecycle.preview_cleanup()["plan_id"])
    original.write_text("new data")
    assert lifecycle.purge_quarantine(batch["batch_id"])["purged_files"] == 1
    assert original.read_text() == "new data"


def test_manual_purge_skips_restored_items(root):
    original = cache(root)
    batch = lifecycle.execute_cleanup(lifecycle.preview_cleanup()["plan_id"])
    lifecycle.restore_quarantine(batch["batch_id"])
    result = lifecycle.purge_quarantine(batch["batch_id"])
    assert result["purged_files"] == 0 and result["freed_bytes"] == 0 and len(result["skipped"]) == 1
    assert original.exists()


def test_manual_purge_refuses_changed_or_hardlinked_quarantine(root):
    cache(root)
    batch = lifecycle.execute_cleanup(lifecycle.preview_cleanup()["plan_id"])
    quarantined = root / batch["items"][0]["quarantine_path"]
    quarantined.write_text("new contents")
    result = lifecycle.purge_quarantine(batch["batch_id"])
    assert result["purged_files"] == 0 and "变化" in result["skipped"][0]["reason"]
    os.link(quarantined, root / "hardlinked.json")
    result = lifecycle.purge_quarantine(batch["batch_id"])
    assert result["purged_files"] == 0 and "硬链接" in result["skipped"][0]["reason"]
    assert quarantined.exists()


@pytest.mark.parametrize("value", ["../bad", "", "/tmp/test", "A" * 32, "1" * 33])
def test_manual_purge_rejects_non_batch_locators(root, value):
    with pytest.raises(lifecycle.CacheLifecycleError):
        lifecycle.purge_quarantine(value)
