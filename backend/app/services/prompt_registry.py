"""Versioned local prompts; source upgrades never replace user overrides."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from threading import RLock

from app.config import settings
from app.storage import knowledge_cache_store as store

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
PROMPT_TITLES = {"system_prompt": "系统指令", "news_relevance": "相关性判断",
                 "stock_impact_analysis": "潜在影响分析"}
MAX_PROMPT_LENGTH = 20000
_lock = RLock()
_source_stamp: dict[str, tuple] = {}
_version_cache: dict[str, tuple[tuple, dict]] = {}


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def validate_prompt(name: str, content: str) -> str:
    if name not in PROMPT_TITLES:
        raise ValueError("Unknown analysis prompt")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Analysis prompt is empty")
    if len(content) > MAX_PROMPT_LENGTH:
        raise ValueError(f"Analysis prompt exceeds {MAX_PROMPT_LENGTH} characters")
    return content.strip()


def _stamp(path: Path) -> tuple:
    try:
        stat = path.stat()
        return stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size
    except FileNotFoundError:
        return ()


def _registry_identity() -> tuple:
    try:
        stat = (settings.data_dir / "knowledge_cache" / "registry.sqlite3").stat()
        return stat.st_dev, stat.st_ino
    except FileNotFoundError:
        return ()


def _read(name: str, revision_id: str | None = None) -> dict:
    doc = store.get_document("prompts", name, revision_id=revision_id)
    if doc is None or not isinstance(doc.get("content"), dict):
        raise ValueError("Analysis prompt registry is missing or invalid")
    validate_prompt(name, doc["content"].get("text"))
    return doc


def _import_legacy() -> None:
    if store.get_head("prompts", "migration.legacy"):
        return
    path = settings.data_dir / "prompt_overrides.json"
    overrides, ignored = {}, []
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError("旧 Prompt 配置损坏；请修复 prompt_overrides.json 后迁移，原文件未修改。") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("prompts", {}), dict):
            raise ValueError("旧 Prompt 配置格式无效，原文件未修改。")
        for name, content in raw.get("prompts", {}).items():
            if name not in PROMPT_TITLES:
                ignored.append(str(name)[:128])
                continue
            overrides[name] = validate_prompt(name, content)
    # Validate the whole file before importing any values.
    for name, content in overrides.items():
        active = _read(name)
        if active["content"].get("mode") == "default":
            store.put_document("prompts", name,
                               {"text": content, "mode": "custom", "source_hash": active["content"]["source_hash"]},
                               reason="import_legacy_override", provenance={"source": "prompt_overrides.json"},
                               expected_revision=active["metadata"]["revision_id"])
    store.put_document("prompts", "migration.legacy", {"schema_version": 1, "imported": sorted(overrides),
                       "ignored_unknown_names": ignored, "source_present": path.exists()},
                       reason="legacy_import_completed", provenance={"source": "prompt_overrides.json"})


def ensure_registry() -> None:
    with _lock:
        root = str(settings.data_dir.resolve())
        paths = {name: PROMPTS_DIR / f"{name}.md" for name in PROMPT_TITLES}
        stamp = tuple((name, _stamp(path)) for name, path in paths.items())
        if _source_stamp.get(root) == (stamp, _registry_identity()):
            return
        defaults = {name: validate_prompt(name, path.read_text(encoding="utf-8")) for name, path in paths.items()}
        for name, content in defaults.items():
            source_hash = text_hash(content)
            previous = store.get_document("prompts", f"default.{name}")
            active = store.get_document("prompts", name)
            if active is not None:
                validate_prompt(name, active["content"].get("text"))
            changed = previous is None or previous["content"].get("source_hash") != source_hash
            if changed:
                store.put_document("prompts", f"default.{name}",
                                   {"text": content, "mode": "default", "source_hash": source_hash},
                                   reason="source_default_updated", provenance={"source": f"app/prompts/{name}.md"})
            if active is None or changed and active["content"].get("mode") == "default":
                store.put_document("prompts", name, {"text": content, "mode": "default", "source_hash": source_hash},
                                   reason="default_activated", provenance={"source": f"app/prompts/{name}.md"},
                                   expected_revision=active["metadata"]["revision_id"] if active else "")
        _import_legacy()
        _source_stamp[root] = stamp, _registry_identity()


def get_prompt(name: str) -> dict:
    validate_prompt(name, "check")
    ensure_registry()
    active, default = _read(name), store.get_document("prompts", f"default.{name}")
    return {**active["metadata"], "name": name, "title": PROMPT_TITLES[name],
            "content": active["content"]["text"], "default_content": default["content"]["text"],
            "overridden": active["content"].get("mode") != "default", "mode": active["content"].get("mode"),
            "source_revision": default["metadata"]["revision_id"],
            "source_hash": default["content"]["source_hash"]}


def list_prompts() -> list[dict]:
    return [get_prompt(name) for name in PROMPT_TITLES]


def update_prompt(name: str, content: str, *, actor_id=None, reason="", expected_revision=None) -> dict:
    content = validate_prompt(name, content)
    with _lock:
        ensure_registry()
        default = store.get_document("prompts", f"default.{name}")
        store.put_document("prompts", name, {"text": content, "mode": "custom", "source_hash": default["content"]["source_hash"]},
                           actor_id=actor_id, reason=reason or "user_edit", expected_revision=expected_revision)
        return get_prompt(name)


def reset_prompt(name: str, *, actor_id=None, reason="", expected_revision=None) -> dict:
    validate_prompt(name, "check")
    with _lock:
        ensure_registry()
        default = store.get_document("prompts", f"default.{name}")
        store.put_document("prompts", name, default["content"], actor_id=actor_id,
                           reason=reason or "reset_to_current_default", expected_revision=expected_revision)
        return get_prompt(name)


def activate_prompt(name: str, revision_id: str, *, actor_id=None, reason="", expected_revision=None) -> dict:
    """Restore immutable historical text into a pinned, auditable revision."""
    validate_prompt(name, "check")
    with _lock:
        ensure_registry()
        prior = _read(name, revision_id)
        default = store.get_document("prompts", f"default.{name}")
        store.put_document("prompts", name, {"text": prior["content"]["text"], "mode": "rollback",
                           "source_hash": default["content"]["source_hash"], "restored_from": revision_id},
                           actor_id=actor_id, reason=reason or "rollback", expected_revision=expected_revision,
                           provenance={"restored_from": revision_id})
        return get_prompt(name)


def snapshot_prompts() -> dict:
    with _lock:
        ensure_registry()
        documents = {name: _read(name) for name in PROMPT_TITLES}
        return {"prompts": {name: doc["content"]["text"] for name, doc in documents.items()},
                "versions": {name: {"revision_id": doc["metadata"]["revision_id"],
                                    "content_hash": doc["metadata"]["content_hash"],
                                    "text_hash": text_hash(doc["content"]["text"])}
                             for name, doc in documents.items()}}


def current_versions() -> dict:
    # Only head metadata on archive reuse checks; bodies are immutable and small.
    with _lock:
        ensure_registry()
        root = str(settings.data_dir.resolve())
        # One registry epoch query replaces three independently locked head reads.
        # The epoch includes activation events, so a rollback is visible at once.
        stamp = _registry_identity(), store.cache_epoch("prompts")
        cached = _version_cache.get(root)
        if cached and cached[0] == stamp:
            return cached[1]
        versions = snapshot_prompts()["versions"]
        _version_cache[root] = stamp, versions
        return versions
