"""Compatibility reader for the locally versioned analysis prompt registry."""
from __future__ import annotations

from contextvars import ContextVar

from app.config import settings
from app.services import prompt_registry
from app.services.prompt_registry import MAX_PROMPT_LENGTH, PROMPTS_DIR, PROMPT_TITLES

_pinned: ContextVar[dict | None] = ContextVar("analysis_prompt_snapshot", default=None)
_ALLOWED = set(PROMPT_TITLES)


def _overrides_path():
    return settings.data_dir / "prompt_overrides.json"


def load_prompt(name: str) -> str:
    prompt_registry.validate_prompt(name, "check")
    pinned = _pinned.get()
    if pinned is not None:
        return pinned["prompts"][name]
    return prompt_registry.get_prompt(name)["content"]


def snapshot_prompts() -> dict:
    """Pin all model stages to one immutable set, preserving the public loader."""
    snapshot = prompt_registry.snapshot_prompts()
    token = _pinned.set(snapshot)
    try:
        for name in PROMPT_TITLES:
            text = load_prompt(name)
            if text != snapshot["prompts"][name]:
                snapshot["prompts"][name] = text
                snapshot["versions"][name] = {"revision_id": None, "content_hash": prompt_registry.text_hash(text),
                                              "text_hash": prompt_registry.text_hash(text)}
        return snapshot
    finally:
        _pinned.reset(token)
