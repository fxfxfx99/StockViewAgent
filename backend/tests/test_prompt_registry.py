import json

import pytest

from app.services import prompt_loader, prompt_registry as prompts
from app.storage import knowledge_cache_store as store


@pytest.fixture
def source(tmp_path, monkeypatch):
    folder = tmp_path / "source-prompts"
    folder.mkdir()
    for name in prompts.PROMPT_TITLES:
        (folder / f"{name}.md").write_text(f"默认 {name}", encoding="utf-8")
    monkeypatch.setattr(prompts, "PROMPTS_DIR", folder)
    return folder


def test_defaults_custom_edits_source_upgrades_and_reset_are_versioned(source):
    initial = prompts.get_prompt("news_relevance")
    custom = prompts.update_prompt("news_relevance", "逐股查证，不要泛化。", actor_id="admin-1",
                                   expected_revision=initial["revision_id"])
    (source / "news_relevance.md").write_text("新版默认相关性规则", encoding="utf-8")
    current = prompts.get_prompt("news_relevance")
    assert current["revision_id"] == custom["revision_id"]
    assert current["content"] == "逐股查证，不要泛化。"
    assert current["default_content"] == "新版默认相关性规则"
    assert current["source_revision"] != initial["source_revision"]
    reset = prompts.reset_prompt("news_relevance", actor_id="admin-1", expected_revision=current["revision_id"])
    assert reset["content"] == "新版默认相关性规则" and not reset["overridden"]
    assert store.history("prompts", "news_relevance")[0]["actor_id"] == "admin-1"
    assert len(store.history("prompts", "news_relevance")) == 3


def test_source_updates_follow_default_and_rollback_pins_historical_text(source):
    initial = prompts.get_prompt("system_prompt")
    (source / "system_prompt.md").write_text("第二版系统指令", encoding="utf-8")
    assert prompt_loader.load_prompt("system_prompt") == "第二版系统指令"
    restored = prompts.activate_prompt("system_prompt", initial["revision_id"], actor_id="admin-2")
    assert restored["mode"] == "rollback" and restored["content"] == initial["content"]
    (source / "system_prompt.md").write_text("第三版系统指令", encoding="utf-8")
    assert prompt_loader.load_prompt("system_prompt") == initial["content"]
    assert store.get_document("prompts", "system_prompt")["content"]["restored_from"] == initial["revision_id"]


def test_legacy_migration_is_whitelisted_and_never_persists_unrelated_values(source, tmp_path):
    original = {"prompts": {"news_relevance": "原有自定义", "unrecognized": "secret-value"}, "token": "hidden-token"}
    path = tmp_path / "prompt_overrides.json"
    path.write_text(json.dumps(original), encoding="utf-8")
    assert prompt_loader.load_prompt("news_relevance") == "原有自定义"
    migration = store.get_document("prompts", "migration.legacy")["content"]
    assert migration["imported"] == ["news_relevance"]
    assert migration["ignored_unknown_names"] == ["unrecognized"]
    assert "secret-value" not in str(migration) and "hidden-token" not in str(migration)
    assert json.loads(path.read_text()) == original
    path.write_text("damaged after one-time import", encoding="utf-8")
    assert prompt_loader.load_prompt("news_relevance") == "原有自定义"


def test_corrupt_legacy_file_cannot_silently_discard_customization(source, tmp_path):
    path = tmp_path / "prompt_overrides.json"
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError, match="配置损坏"):
        prompt_loader.load_prompt("system_prompt")
    assert path.read_text() == "{broken"
    assert store.get_head("prompts", "migration.legacy") is None


def test_unknown_empty_oversized_and_conflicting_updates_leave_head_unchanged(source):
    old = prompts.get_prompt("news_relevance")
    for name, text in [("../secrets", "x"), ("news_relevance", " "), ("news_relevance", "x" * 20001)]:
        with pytest.raises(ValueError):
            prompts.update_prompt(name, text)
    new = prompts.update_prompt("news_relevance", "更新后的有效提示词")
    with pytest.raises(store.RevisionConflict):
        prompts.update_prompt("news_relevance", "基于旧版本的编辑", expected_revision=old["revision_id"])
    assert prompts.get_prompt("news_relevance")["revision_id"] == new["revision_id"]


def test_corrupt_authoritative_prompt_is_not_overwritten_by_source_sync(source, tmp_path):
    active = prompts.get_prompt("news_relevance")
    (tmp_path / "knowledge_cache" / active["object_path"]).write_bytes(b"broken")
    (source / "news_relevance.md").write_text("新的默认", encoding="utf-8")
    with pytest.raises(store.KnowledgeCacheError):
        prompt_loader.load_prompt("news_relevance")
    assert store.get_head("prompts", "news_relevance")["revision_id"] == active["revision_id"]
