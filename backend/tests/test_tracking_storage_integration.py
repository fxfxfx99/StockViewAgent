"""Research notes survive the same protected-data lifecycle as other evidence."""
from app.config import settings
from app.storage import cache_lifecycle, data_asset_manager, data_portability, tracking_store


def test_tracking_history_and_feedback_survive_verified_directory_copy(tmp_path, monkeypatch):
    source, destination = tmp_path / "original", tmp_path / "backup"
    source.mkdir()
    active = [source]
    monkeypatch.setattr(type(settings), "data_dir", property(lambda self: active[0]))
    thesis = tracking_store.create_thesis(7, {"symbol": "600000.SS", "reason": "核对毛利率改善是否持续"})
    revised = tracking_store.update_thesis(7, thesis["id"], {
        "revision": 1, "invalidation_conditions": "新订单未能转化为利润率改善",
    })
    tracking_store.put_changes(7, [{"id": "fixture-revision", "kind": "news", "symbol": "600000.SS",
                                  "thesis_ids": [thesis["id"]], "title": "测试资料修订", "priority": 50}])
    tracking_store.update_change(7, "fixture-revision", {"is_read": True, "feedback": "useful"})
    history = tracking_store.list_timeline(7, thesis["id"])

    result = data_portability.migrate_data_directory(source, destination)
    assert result["source_preserved"] and result["sqlite_databases"] == 1
    active[0] = destination
    assert tracking_store.get_thesis(7, thesis["id"]) == revised
    assert tracking_store.list_timeline(7, thesis["id"]) == history
    change = tracking_store.get_change(7, "fixture-revision")
    assert change["is_read"] and change["feedback"] == "useful"
    assert tracking_store.get_thesis(8, thesis["id"]) is None
    assert tracking_store.list_changes(8) == []
    assert data_portability.verify_data_directory(destination)["ok"]


def test_tracking_database_is_classified_and_protected(tmp_path):
    tracking_store.create_thesis(7, {"symbol": "600000.SS", "reason": "跟踪公开证据"})
    path = tmp_path / "tracking.sqlite3"
    assert data_asset_manager._category(path.name) == "research"
    assert data_asset_manager._infer_asset_meta(path.name, path)["source"] == "tracking_store"
    asset = cache_lifecycle.describe_asset(path.name, path.stat())
    assert asset["storage_role"] == "protected_data" and asset["protected"]
    plan = cache_lifecycle.preview_cleanup([path.name])
    assert plan["total_files"] == 0 and path.exists()
