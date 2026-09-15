from typing import Annotated

from fastapi import APIRouter, Depends, Query

from pydantic import BaseModel, Field

from app.config import settings
from app.deps.auth import get_current_user
from app.services.llm_provider_presets import infer_from_api_base, public_presets_list
from app.services.llm_runtime_unify import unify_llm_runtime_fields
from app.storage.user_credentials_store import load_user_credentials, merge_user_credentials
from app.storage.users_store import UserRecord

router = APIRouter(prefix="/api/settings", tags=["settings"])


class LLMUpdate(BaseModel):
    api_key: str | None = Field(default=None, description="不传则不修改；空字符串清除页面保存的密钥")
    api_key_backup: str | None = Field(
        default=None,
        description="备用 API Key；不传则不修改；空字符串清除。与主 Key 共用同一 API Base / Model。",
    )
    fallback_api_key: str | None = Field(default=None, description="不同提供商的故障切换 API Key")
    fallback_api_base: str | None = Field(default=None, description="故障切换提供商的 OpenAI 兼容 API 根")
    fallback_model: str | None = Field(default=None, description="故障切换提供商的模型 ID")
    api_base: str | None = Field(default=None, description="不传则不修改")
    model: str | None = Field(default=None, description="不传则不修改")
    embedding_model: str | None = Field(
        default=None,
        description="RAG 向量模型（OpenAI 兼容 /embeddings），不传则不修改",
    )
    embedding_api_base: str | None = Field(
        default=None,
        description="RAG 专用 API 根（将请求 {根}/embeddings）；不传则不修改，空字符串表示清除并与大模型 Base 相同",
    )


def _key_source(user_id: int) -> str:
    rt = load_user_credentials(user_id).get("llm", {})
    if (rt.get("api_key") or "").strip() or (rt.get("api_key_backup") or "").strip():
        return "personal"
    return "none"


def llm_public_dict(user_id: int) -> dict:
    """不返回完整密钥；供 GET /llm 与 integrations 聚合。"""
    src = _key_source(user_id)
    rt = load_user_credentials(user_id).get("llm", {})
    k = settings.kline_llm_key
    kb = settings.kline_llm_key_backup
    suffix = None
    if src in ("personal", "env") and len(k) >= 6:
        suffix = k[-4:]
    suffix_backup = None
    if kb and len(kb) >= 6:
        suffix_backup = kb[-4:]
    return {
        "api_base": settings.kline_llm_base,
        "model": settings.kline_llm_model,
        "embedding_model": (rt.get("embedding_model") or "").strip() or "text-embedding-3-small",
        "embedding_api_base": (rt.get("embedding_api_base") or "").strip(),
        "embedding_effective_base": settings.kline_embedding_api_base,
        "key_source": src,
        "key_suffix": suffix,
        "key_backup_suffix": suffix_backup,
        "key_configured": settings.any_llm_key_configured,
        "key_backup_configured": bool(kb),
        "fallback_configured": bool(
            settings.kline_llm_fallback_key
            and settings.kline_llm_fallback_base
            and settings.kline_llm_fallback_model
        ),
        "fallback_api_base": settings.kline_llm_fallback_base,
        "fallback_model": settings.kline_llm_fallback_model,
    }


@router.get("/llm")
def get_llm_public(user: Annotated[UserRecord, Depends(get_current_user)]):
    return llm_public_dict(user.id)


@router.get("/llm/presets")
def get_llm_presets(_: Annotated[UserRecord, Depends(get_current_user)]):
    """常用 OpenAI 兼容厂商的默认 Base / 模型，与控制台快捷选择一致。"""
    return {"providers": public_presets_list()}


@router.get("/llm/infer")
def get_llm_infer(
    _: Annotated[UserRecord, Depends(get_current_user)],
    api_base: str = Query("", description="用户输入的 API Base（可含误填的 /chat/completions）"),
):
    """根据 URL 推断提供商并返回推荐对话模型、向量模型与可选模型列表。"""
    r = infer_from_api_base(api_base)
    if not r:
        return {"ok": False, "message": "无法从该地址推断，请选择上方预设或手动填写模型名"}
    return {"ok": True, **r}


@router.put("/llm")
def put_llm(body: LLMUpdate, user: Annotated[UserRecord, Depends(get_current_user)]):
    patch = body.model_dump(exclude_unset=True)
    if not patch:
        return llm_public_dict(user.id)
    kw: dict = dict(load_user_credentials(user.id).get("llm", {}))
    if "api_key" in patch:
        v = patch["api_key"]
        kw["api_key"] = "" if v is None else str(v)
    if "api_key_backup" in patch:
        v = patch["api_key_backup"]
        kw["api_key_backup"] = "" if v is None else str(v)
    if "fallback_api_key" in patch:
        v = patch["fallback_api_key"]
        kw["fallback_api_key"] = "" if v is None else str(v)
    if "fallback_api_base" in patch:
        kw["fallback_api_base"] = patch["fallback_api_base"] or ""
    if "fallback_model" in patch:
        kw["fallback_model"] = patch["fallback_model"] or ""
    if "api_base" in patch:
        kw["api_base"] = patch["api_base"] or ""
    if "model" in patch:
        kw["model"] = patch["model"] or ""
    if "embedding_model" in patch:
        kw["embedding_model"] = patch["embedding_model"] or ""
    if "embedding_api_base" in patch:
        kw["embedding_api_base"] = patch["embedding_api_base"] or ""
    merge_user_credentials(user.id, {"llm": unify_llm_runtime_fields(kw)})
    return llm_public_dict(user.id)
