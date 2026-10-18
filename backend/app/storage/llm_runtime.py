"""前端保存的大模型配置，跟随当前数据目录；旧位置由统一迁移器处理。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any

from app.config import settings

_LEGACY_FILE = Path(__file__).resolve().parent.parent / "data" / "llm_runtime.json"
# Explicit overrides remain useful to isolated callers/tests. The default must
# be resolved per operation, never pinned to a checkout at module-import time.
_FILE: Path | None = None
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


def _mkdir() -> Path:
    path = Path(_FILE) if _FILE is not None else settings.data_dir / "llm_runtime.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def load_runtime() -> dict[str, Any]:
    path = _mkdir()
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _write_runtime(value: dict[str, Any]) -> None:
    """Replace complete credentials atomically; interrupted writes retain old keys."""
    path = _mkdir()
    if path.exists():
        try:
            if not isinstance(json.loads(path.read_text(encoding="utf-8")), dict):
                raise ValueError("invalid configuration")
        except (OSError, ValueError):
            raise ValueError("现有模型配置无法读取，保留原文件；请先修复或恢复备份") from None
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".llm-runtime-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


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
    _write_runtime(cur)
    return cur


def save_runtime_unified(data: dict[str, Any]) -> dict[str, Any]:
    """合并后的配置经 unify 后写入文件（推荐对外保存入口）。"""
    from app.services.llm_runtime_unify import unify_llm_runtime_fields

    unified = unify_llm_runtime_fields(dict(data))
    _write_runtime(unified)
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
