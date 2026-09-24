"""强制登录的共享部署仅允许内置公网 LLM 接口，避免用户配置触发 SSRF。"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from fastapi import HTTPException

from app.config import settings
from app.services.llm_provider_presets import PROVIDER_PRESETS
from app.storage.llm_runtime import normalize_openai_api_base

_BASE_FIELDS = ("api_base", "fallback_api_base", "embedding_api_base")
_PUBLIC_BASES = frozenset(
    normalize_openai_api_base(str(preset["api_base"]))
    for preset in PROVIDER_PRESETS
    if str(preset["api_base"]).startswith("https://")
)


def validate_llm_endpoints(data: dict[str, Any]) -> None:
    """本地模式支持自建网关；共享模式只接受规范化后精确匹配的 HTTPS Base。"""
    if not settings.auth_required:
        return
    for field in _BASE_FIELDS:
        raw = str(data.get(field) or "").strip()
        if not raw:
            continue
        allowed = False
        try:
            parsed = urlsplit(raw)
            allowed = (
                not any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in raw)
                and parsed.scheme == "https"
                and parsed.username is None
                and parsed.password is None
                and "?" not in raw
                and "#" not in raw
                and normalize_openai_api_base(raw) in _PUBLIC_BASES
            )
        except ValueError:
            pass
        if not allowed:
            raise HTTPException(
                status_code=400,
                detail=f"公网部署的 {field} 仅支持控制台内置的 HTTPS 提供商地址",
            )
