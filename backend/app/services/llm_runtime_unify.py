"""LLM 运行时配置统一归一化：保存时由后端集中补全 Base / 模型 / 向量，避免前后端分叉。"""
from __future__ import annotations

import re
from typing import Any

from app.services.llm_provider_presets import infer_from_api_base
from app.storage.llm_runtime import normalize_openai_api_base


def _norm_model_key(s: str) -> str:
    return re.sub(r"[\s_\-]+", "", (s or "").lower())


def _is_kimi_marketing_name(raw: str) -> bool:
    """营销名/口语（如 kimi 2.5）与 API model id 不一致时的识别。"""
    r = (raw or "").strip()
    if not r:
        return False
    nk = _norm_model_key(r)
    if nk in ("kimi25", "kimik25", "kimik2.5", "kimi2.5"):
        return True
    rl = r.lower()
    return "kimi" in rl and "2.5" in r


def resolve_chat_model_id(model_raw: str, inf: dict[str, Any] | None) -> str:
    """空则使用推断默认；常见营销名映射为合法 API id。"""
    r = (model_raw or "").strip()
    if _is_kimi_marketing_name(r):
        return "kimi-k2-turbo-preview"

    if not r:
        if inf:
            return str(inf.get("model") or "").strip()
        return ""

    if not inf:
        return r

    pid = str(inf.get("preset_id") or "")
    chat_models = list(inf.get("chat_models") or [])
    if r in chat_models:
        return r

    nk = _norm_model_key(r)
    if pid == "kimi":
        aliases = {
            "moonshotv132k": "moonshot-v1-32k",
            "moonshotv18k": "moonshot-v1-8k",
            "moonshotv1128k": "moonshot-v1-128k",
        }
        if nk in aliases:
            return aliases[nk]

    return r


def unify_llm_runtime_fields(data: dict[str, Any]) -> dict[str, str]:
    """
    保存前统一处理：规范化 Base、按 Base 推断并补全默认向量模型、修正非法 model 文案。
    返回写入 llm_runtime.json 的字段。
    """
    api_key = str(data.get("api_key") or "")
    api_key_backup = str(data.get("api_key_backup") or "")
    api_base = normalize_openai_api_base(str(data.get("api_base") or ""))
    fallback_api_key = str(data.get("fallback_api_key") or "").strip()
    fallback_api_base = normalize_openai_api_base(str(data.get("fallback_api_base") or ""))

    model_in = str(data.get("model") or "").strip()
    emb_in = str(data.get("embedding_model") or "").strip()
    emb_base_in = str(data.get("embedding_api_base") or "").strip()

    inf = infer_from_api_base(api_base) if api_base else None

    model = resolve_chat_model_id(model_in, inf)
    fallback_inf = infer_from_api_base(fallback_api_base) if fallback_api_base else None
    fallback_model = resolve_chat_model_id(str(data.get("fallback_model") or "").strip(), fallback_inf)
    embedding_model = emb_in
    if not embedding_model and inf:
        embedding_model = str(inf.get("embedding_model") or "").strip()
    if not embedding_model:
        embedding_model = "text-embedding-3-small"

    embedding_api_base = normalize_openai_api_base(emb_base_in) if emb_base_in else ""
    if not embedding_api_base and inf:
        eb = str(inf.get("embedding_api_base") or "").strip()
        embedding_api_base = normalize_openai_api_base(eb) if eb else ""

    return {
        "api_key": api_key,
        "api_key_backup": api_key_backup.strip(),
        "api_base": api_base,
        "model": model,
        "embedding_model": embedding_model,
        "embedding_api_base": embedding_api_base,
        "fallback_api_key": fallback_api_key,
        "fallback_api_base": fallback_api_base,
        "fallback_model": fallback_model,
    }
