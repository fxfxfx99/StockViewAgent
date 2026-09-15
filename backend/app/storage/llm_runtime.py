"""前端保存的大模型配置（OpenAI 兼容），存 backend/data/llm_runtime.json。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_BACKEND = Path(__file__).resolve().parent.parent
_FILE = _BACKEND / "data" / "llm_runtime.json"
_MISSING = object()


def normalize_openai_api_base(url: str) -> str:
    """OpenAI 兼容根路径应为 …/v1；若误填完整 chat 端点则去掉尾部 /chat/completions。"""
    s = (url or "").strip()
    if not s:
        return ""
    s = s.rstrip("/")
    while s.endswith("/chat/completions"):
        s = s[: -len("/chat/completions")].rstrip("/")
    return s


def _mkdir() -> None:
    _FILE.parent.mkdir(parents=True, exist_ok=True)


def load_runtime() -> dict[str, Any]:
    _mkdir()
    if not _FILE.exists():
        return {}
    try:
        return json.loads(_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_runtime_merge(
    api_key: Any = _MISSING,
    api_key_backup: Any = _MISSING,
    api_base: Any = _MISSING,
    model: Any = _MISSING,
    embedding_model: Any = _MISSING,
    embedding_api_base: Any = _MISSING,
) -> dict[str, Any]:
    cur = load_runtime()
    if api_key is not _MISSING:
        cur["api_key"] = api_key if isinstance(api_key, str) else str(api_key)
    if api_key_backup is not _MISSING:
        cur["api_key_backup"] = (
            (api_key_backup or "").strip() if isinstance(api_key_backup, str) else str(api_key_backup or "")
        )
    if api_base is not _MISSING:
        cur["api_base"] = normalize_openai_api_base(api_base if isinstance(api_base, str) else str(api_base or ""))
    if model is not _MISSING:
        cur["model"] = (model or "").strip() if isinstance(model, str) else ""
    if embedding_model is not _MISSING:
        cur["embedding_model"] = (
            (embedding_model or "").strip() if isinstance(embedding_model, str) else ""
        )
    if embedding_api_base is not _MISSING:
        raw_eb = (embedding_api_base or "").strip() if isinstance(embedding_api_base, str) else ""
        cur["embedding_api_base"] = normalize_openai_api_base(raw_eb) if raw_eb else ""
    _mkdir()
    _FILE.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
    return cur


def save_runtime_unified(data: dict[str, Any]) -> dict[str, Any]:
    """合并后的配置经 unify 后写入文件（推荐对外保存入口）。"""
    from app.services.llm_runtime_unify import unify_llm_runtime_fields

    unified = unify_llm_runtime_fields(dict(data))
    _mkdir()
    _FILE.write_text(json.dumps(unified, ensure_ascii=False, indent=2), encoding="utf-8")
    return unified


def apply_llm_patch_and_unify(patch: dict[str, Any]) -> dict[str, Any]:
    """
    将 patch 合并进当前 llm_runtime.json，再经 unify（规范化 Base、补全向量、修正模型名）后写回。
    patch 仅含需要更新的键（与 PUT /settings/llm 一致）。
    """
    cur = load_runtime()
    merged: dict[str, Any] = {**cur}
    for key in ("api_key", "api_key_backup", "api_base", "model", "embedding_model", "embedding_api_base"):
        if key not in patch:
            continue
        val = patch[key]
        if key in ("api_key", "api_key_backup"):
            merged[key] = "" if val is None else str(val)
        else:
            merged[key] = "" if val is None else str(val)
    return save_runtime_unified(merged)
