"""Real filesystem/SQLite coverage for durable knowledge and safe projections."""
from concurrent.futures import ThreadPoolExecutor
import gzip
import hashlib
import json
import os
import sqlite3
import subprocess
import sys

import pytest

from app.storage import knowledge_cache_store as store


def test_versioned_documents_have_compressed_deduplicated_objects_and_readable_projections(tmp_path):
    evidence = tmp_path / "stock_features.sqlite3"
    evidence.write_bytes(b"original authority")
    first = store.put_document("stock_features", "600519.SH", {"value": 1, "name": "茅台"}, reason="初始", provenance={"source": "stock_features.sqlite3"})
    root = tmp_path / "knowledge_cache"
    assert first["object_path"].endswith(".json.gz")
    compressed = (root / first["object_path"]).read_bytes()
    payload = gzip.decompress(compressed)
    assert hashlib.sha256(payload).hexdigest() == first["content_hash"]
    assert json.loads(payload)["name"] == "茅台"
    assert json.loads((root / first["current_path"]).read_text())["value"] == 1
    assert json.loads((root / first["manifest_path"]).read_text())["revision_id"] == first["revision_id"]
    same = store.put_document("stock_features", "600519.SH", {"name": "茅台", "value": 1})
    assert same["unchanged"] and same["revision_id"] == first["revision_id"]
    second = store.put_document("stock_features", "600519.SH", {"value": 2})
    third = store.put_document("stock_features", "600519.SH", {"value": 1, "name": "茅台"})
    assert len({first["revision_id"], second["revision_id"], third["revision_id"]}) == 3
    assert len(store.history("stock_features", "600519.SH")) == 3
    assert store.overview()["counts"]["objects"] == 2
    assert store.get_document("stock_features", "600519.SH", second["revision_id"])["content"] == {"value": 2}
    assert evidence.read_bytes() == b"original authority"
    assert (root / "README.md").is_file()


def test_drafts_activation_rollback_and_optimistic_conflicts_are_audited():
    first = store.put_document("prompts", "system_prompt", "v1", format="markdown", expected_revision="")
    epoch = store.cache_epoch("prompts")
    draft = store.put_document("prompts", "system_prompt", "v2", format="markdown", activate=False)
    assert not draft["is_active"]
    assert store.get_document("prompts", "system_prompt")["content"] == "v1"
    assert store.cache_epoch("prompts") == epoch
    store.activate_revision("prompts", "system_prompt", draft["revision_id"], "启用测试", actor_id=3, expected_revision=first["revision_id"])
    assert store.cache_epoch("prompts") != epoch
    with pytest.raises(store.RevisionConflict):
        store.put_document("prompts", "system_prompt", "v3", format="markdown", expected_revision=first["revision_id"])
    store.activate_revision("prompts", "system_prompt", first["revision_id"], "回滚", actor_id=3)
    assert store.get_document("prompts", "system_prompt")["content"] == "v1"
    events = store.list_events(kind="prompts", key="system_prompt")["items"]
    assert events[0]["details"]["previous_revision"] == draft["revision_id"]
    assert events[0]["actor_id"] == "3"
    assert events[0]["reason"] == "回滚"
    assert store.get_head("prompts", "system_prompt")["revision_id"] == first["revision_id"]


def test_projection_edit_never_becomes_content_and_is_repaired(tmp_path):
    result = store.put_document("news_relevance", "600519.SH", {"aliases": ["贵州茅台"]})
    root = tmp_path / "knowledge_cache"
    (root / result["current_path"]).write_text('{"api_key":"untrusted"}')
    (root / result["manifest_path"]).unlink()
    loaded = store.get_document("news_relevance", "600519.SH")
    assert loaded["content"] == {"aliases": ["贵州茅台"]}
    assert loaded["metadata"]["projection_status"] == "repaired"
    assert json.loads((root / result["current_path"]).read_text()) == loaded["content"]
    assert store.get_document("news_relevance", "600519.SH")["metadata"]["projection_status"] == "healthy"


def test_corrupted_immutable_object_is_not_replaced_by_current_file(tmp_path):
    result = store.put_document("news_relevance", "600519.SH", {"aliases": []})
    root = tmp_path / "knowledge_cache"
    (root / result["object_path"]).write_bytes(b"corruption")
    with pytest.raises(store.KnowledgeCacheError, match="权威对象"):
        store.get_document("news_relevance", "600519.SH")
    # Ordinary re-ingestion must not silently overwrite damaged authority.
    with pytest.raises(store.KnowledgeCacheError, match="权威对象"):
        store.put_document("news_relevance", "600519.SH", {"aliases": []})
    assert (root / result["object_path"]).read_bytes() == b"corruption"
    assert store.get_head("news_relevance", "600519.SH")["revision_id"] == result["revision_id"]


@pytest.mark.parametrize("kind,key", [("other", "valid"), ("prompts", "../outside"), ("prompts", "."), ("prompts", "/tmp/a"),
                                      ("prompts", "foo/bar"), ("prompts", "foo\\bar"), ("prompts", "a\x00"), ("prompts", "a" * 129)])
def test_untrusted_document_paths_are_rejected_before_initialization(kind, key, tmp_path):
    with pytest.raises(store.KnowledgeCacheError):
        store.put_document(kind, key, {})
    assert not (tmp_path / "knowledge_cache").exists()


def test_root_and_nested_symlinks_are_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "knowledge_cache"
    root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(store.KnowledgeCacheError):
        store.put_document("prompts", "system_prompt", {})
    assert list(outside.iterdir()) == []
    root.unlink()
    store.overview()
    (root / "current").symlink_to(outside, target_is_directory=True)
    with pytest.raises(store.KnowledgeCacheError):
        store.put_document("prompts", "system_prompt", {})
    assert store.get_head("prompts", "system_prompt") is None
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("hardlink", [False, True])
def test_current_symlink_or_hardlink_cannot_modify_external_file(tmp_path, hardlink):
    first = store.put_document("prompts", "system_prompt", "trusted", format="markdown")
    outside = tmp_path / "secret.txt"
    outside.write_text("private")
    current = tmp_path / "knowledge_cache" / first["current_path"]
    current.unlink()
    if hardlink:
        os.link(outside, current)
    else:
        current.symlink_to(outside)
    loaded = store.get_document("prompts", "system_prompt")
    assert loaded["content"] == "trusted"
    assert loaded["metadata"]["projection_status"] == "repair_failed"
    with pytest.raises(store.KnowledgeCacheError):
        store.put_document("prompts", "system_prompt", "changed", format="markdown")
    assert store.get_head("prompts", "system_prompt")["revision_id"] == first["revision_id"]
    assert outside.read_text() == "private"


@pytest.mark.parametrize("name", ["registry.sqlite3", "registry.sqlite3-wal", "registry.sqlite3-shm", "registry.lock"])
def test_registry_and_sqlite_sidecar_links_are_rejected(tmp_path, name):
    root = tmp_path / "knowledge_cache"
    root.mkdir()
    outside = tmp_path / "external"
    outside.write_text("do not touch")
    (root / name).symlink_to(outside)
    with pytest.raises(store.KnowledgeCacheError):
        store.overview()
    assert outside.read_text() == "do not touch"


def test_failed_projection_write_keeps_previous_head_and_read_repairs_projection(monkeypatch):
    first = store.put_document("prompts", "system_prompt", "old", format="markdown")
    original = store._atomic_write
    def fail_manifest(root, relative, payload):
        if relative.startswith("manifests/"):
            raise OSError("disk full")
        return original(root, relative, payload)
    monkeypatch.setattr(store, "_atomic_write", fail_manifest)
    with pytest.raises(store.KnowledgeCacheError):
        store.put_document("prompts", "system_prompt", "new", format="markdown")
    assert store.get_head("prompts", "system_prompt")["revision_id"] == first["revision_id"]
    assert len(store.history("prompts", "system_prompt")) == 1
    monkeypatch.setattr(store, "_atomic_write", original)
    loaded = store.get_document("prompts", "system_prompt")
    assert loaded["content"] == "old"
    assert loaded["metadata"]["projection_status"] == "repaired"


def test_concurrent_writers_share_lock_and_only_one_expected_head_succeeds():
    first = store.put_document("prompts", "system_prompt", "old", format="markdown")
    def publish(index):
        try:
            return store.put_document("prompts", "system_prompt", str(index), format="markdown", expected_revision=first["revision_id"])
        except store.RevisionConflict:
            return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(publish, range(8)))
    assert sum(result is not None for result in results) == 1
    assert len(store.history("prompts", "system_prompt")) == 2


def test_separate_processes_share_the_same_write_lock(tmp_path):
    first = store.put_document("prompts", "system_prompt", "old", format="markdown")
    script = """
import sys
from app.storage import knowledge_cache_store as store
try:
    store.put_document('prompts','system_prompt',sys.argv[1],format='markdown',expected_revision=sys.argv[2])
except store.RevisionConflict:
    sys.exit(2)
"""
    environment = {**os.environ, "STOCKVIEW_DATA_DIR": str(tmp_path)}
    processes = [subprocess.Popen([sys.executable, "-c", script, str(i), first["revision_id"]], env=environment,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(4)]
    results = []
    for process in processes:
        _, stderr = process.communicate(timeout=15)
        assert process.returncode in (0, 2), stderr.decode()
        results.append(process.returncode)
    assert results.count(0) == 1
    assert len(store.history("prompts", "system_prompt")) == 2


def test_switching_content_format_leaves_one_current_projection_and_rollback_restores_it(tmp_path):
    first = store.put_document("prompts", "system_prompt", {"text": "original"})
    second = store.put_document("prompts", "system_prompt", "new markdown", format="markdown")
    root = tmp_path / "knowledge_cache"
    assert not (root / first["current_path"]).exists()
    assert (root / second["current_path"]).read_text() == "new markdown"
    store.activate_revision("prompts", "system_prompt", first["revision_id"])
    assert not (root / second["current_path"]).exists()
    assert json.loads((root / first["current_path"]).read_text()) == {"text": "original"}


def test_registry_versions_objects_and_audit_rows_are_immutable(tmp_path):
    store.put_document("prompts", "system_prompt", {"text": "test"})
    with sqlite3.connect(tmp_path / "knowledge_cache" / "registry.sqlite3") as conn:
        assert "payload" not in {row[1] for row in conn.execute("PRAGMA table_info(objects)")}
        for table in ("objects", "revisions", "events"):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                conn.execute(f"DELETE FROM {table}")


def test_search_pagination_and_unknown_versions_are_isolated():
    for key in ("alpha_1", "alphaX1", "beta"):
        store.put_document("prompts", key, {"text": key})
    assert store.list_documents(q="alpha_1")["total"] == 1
    page = store.list_documents(kind="prompts", offset=1, limit=1)
    assert page["total"] == 3 and len(page["items"]) == 1
    foreign = store.get_head("prompts", "alpha_1")["revision_id"]
    assert store.get_document("prompts", "beta", foreign) is None
    with pytest.raises(store.KnowledgeCacheError):
        store.activate_revision("prompts", "beta", foreign)
    with pytest.raises(store.KnowledgeCacheError):
        store.record_event("prompts", "beta", "used", revision_id=foreign)
    assert store.get_document("prompts", "unknown") is None


def test_name_search_matches_feature_and_same_key_relevance_without_reading_bodies(monkeypatch):
    store.put_document("stock_features", "600519.SS", {"version": 1}, provenance={"title": "贵州茅台"})
    store.put_document("news_relevance", "600519.SS", {"aliases": []})
    store.put_document("news_relevance", "000001.SZ", {"body_only_name": "贵州茅台"})
    store.put_document("prompts", "system_prompt", {"text": "贵州茅台"})
    monkeypatch.setattr(store, "_payload", lambda *a: pytest.fail("name search must not decompress bodies"))
    result = store.list_documents(q="茅台")
    assert result["total"] == 2
    assert {(row["kind"], row["key"]) for row in result["items"]} == {
        ("stock_features", "600519.SS"), ("news_relevance", "600519.SS")}
    for kind in ("stock_features", "news_relevance"):
        selected = store.list_documents(kind=kind, q="贵州")
        assert selected["total"] == 1 and selected["items"][0]["kind"] == kind
    assert store.list_documents(kind="prompts", q="贵州")["total"] == 0
    assert store.list_documents(q="600519")["total"] == 2


def test_name_search_uses_active_title_and_paginates_both_kinds():
    original = store.put_document("stock_features", "600519.SS", {"version": 1}, provenance={"title": "旧名称"})
    store.put_document("news_relevance", "600519.SS", {"aliases": []})
    store.put_document("stock_features", "600519.SS", {"version": 2}, provenance={"title": "甲集团"})
    store.put_document("stock_features", "000001.SZ", {}, provenance={"title": "乙集团"})
    store.put_document("news_relevance", "000001.SZ", {"aliases": []})
    assert store.list_documents(q="旧名称")["total"] == 0
    first, second = [store.list_documents(q="集团", offset=offset, limit=2) for offset in (0, 2)]
    assert first["total"] == second["total"] == 4
    pairs = [(row["kind"], row["key"]) for page in (first, second) for row in page["items"]]
    assert len(pairs) == len(set(pairs)) == 4
    selected = store.list_documents(kind="news_relevance", q="集团", offset=1, limit=1)
    assert selected["total"] == 2 and len(selected["items"]) == 1
    assert store.list_documents(q="集团", offset=4)["items"] == []
    store.activate_revision("stock_features", "600519.SS", original["revision_id"])
    assert store.list_documents(q="旧名称")["total"] == 2


@pytest.mark.parametrize("query", ["%", "_", "\\", "100%", "名称_100%\\集团"])
def test_name_search_treats_like_metacharacters_as_literal(query):
    store.put_document("stock_features", "600519.SS", {}, provenance={"title": "名称_100%\\集团"})
    store.put_document("news_relevance", "600519.SS", {})
    store.put_document("stock_features", "000001.SZ", {}, provenance={"title": "名称X1000集团"})
    store.put_document("news_relevance", "000001.SZ", {})
    result = store.list_documents(q=query)
    assert result["total"] == 2
    assert {row["key"] for row in result["items"]} == {"600519.SS"}
    with pytest.raises(store.KnowledgeCacheError):
        store.list_documents(q="茅" * 129)


def test_history_pages_and_predecessors_reach_beyond_200_versions():
    revisions = [store.put_document("prompts", "system_prompt", str(i), format="markdown")["revision_id"] for i in range(205)]
    page = store.history_page("prompts", "system_prompt", offset=200, limit=3)
    assert (page["total"], page["offset"], page["limit"]) == (205, 200, 3)
    assert [row["revision_id"] for row in page["items"]] == list(reversed(revisions[:5]))[:3]
    assert store.previous_revision("prompts", "system_prompt", revisions[3])["revision_id"] == revisions[2]
    assert store.previous_revision("prompts", "system_prompt", revisions[0]) is None
    assert store.previous_revision("prompts", "system_prompt", "f" * 32) is None
    assert len(store.history("prompts", "system_prompt")) == 20
    assert store.history_page("prompts", "system_prompt", offset=500)["items"] == []


def test_previous_revision_does_not_use_another_document_or_kind():
    first = store.put_document("prompts", "alpha", {"value": 1})
    foreign = store.put_document("prompts", "beta", {"value": 2})
    other_kind = store.put_document("news_relevance", "alpha", {"value": 3})
    latest = store.put_document("prompts", "alpha", {"value": 4})
    assert store.previous_revision("prompts", "alpha", latest["revision_id"])["revision_id"] == first["revision_id"]
    assert store.previous_revision("prompts", "alpha", foreign["revision_id"]) is None
    assert store.previous_revision("prompts", "alpha", other_kind["revision_id"]) is None


def test_overview_does_not_walk_other_data_or_read_account_secrets(tmp_path, monkeypatch):
    (tmp_path / "accounts.json").write_text('{"api_key":"secret"}')
    store.put_document("news_relevance", "AAPL", {"name": "Apple"})
    monkeypatch.setattr(os, "walk", lambda *a, **k: pytest.fail("full directory scan"))
    result = store.overview()
    assert result["counts"] == {"documents": 1, "revisions": 1, "objects": 1, "events": 1}
    assert "secret" not in json.dumps(result)
    assert result["bytes"]["objects"] > 0
    assert result["automatic_deletion"] is False
