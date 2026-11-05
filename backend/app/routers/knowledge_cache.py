"""Authenticated local knowledge management; mutations are administrator-only."""
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app.deps.auth import require_admin
from app.services import knowledge_cache_manager as manager, prompt_registry
from app.storage import knowledge_cache_store as cache
from app.storage.users_store import UserRecord

router = APIRouter(prefix="/api/data-center/knowledge", tags=["data-center"])
Admin = Annotated[UserRecord, Depends(require_admin)]
Kind = Literal["stock_features", "news_relevance", "prompts"]


def checked(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except cache.RevisionConflict as exc:
        raise HTTPException(409, "当前版本已变化，请读取新版本后再保存；本次修改未写入。") from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/overview")
def overview(user: Admin):
    return checked(manager.overview)


@router.get("/documents")
def documents(user: Admin, kind: Kind | None = None, q: str = Query("", max_length=128),
              offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)):
    checked(prompt_registry.ensure_registry)
    return checked(cache.list_documents, kind=kind, q=q, offset=offset, limit=limit)


@router.get("/document")
def document(user: Admin, kind: Kind, key: str = Query(..., min_length=1, max_length=128),
             revision_id: str | None = Query(None, max_length=64), include_diff: bool = True):
    return checked(manager.document, kind, key, revision_id, include_diff=include_diff)


@router.get("/history")
def history(user: Admin, kind: Kind, key: str = Query(..., min_length=1, max_length=128),
            offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)):
    return checked(cache.history_page, kind, key, offset=offset, limit=limit)


@router.get("/events")
def events(user: Admin, kind: Kind | None = None, key: str | None = Query(None, max_length=128),
           offset: int = Query(0, ge=0), limit: int = Query(30, ge=1, le=200)):
    return checked(cache.list_events, kind=kind, key=key, offset=offset, limit=limit)


@router.get("/status")
def status(user: Admin):
    return checked(manager.status)


class RebuildBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    scope: Literal["all", "stocks", "relevance", "prompts"] = "all"
    symbol: str | None = Field(None, min_length=1, max_length=32)


@router.post("/rebuild")
def rebuild(body: RebuildBody, user: Admin):
    return checked(manager.enqueue, body.scope, body.symbol, actor_id=user.id)


@router.get("/prompts")
def prompts(user: Admin):
    return {"items": checked(prompt_registry.list_prompts)}


class MutationBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    reason: str = Field(min_length=1, max_length=400)
    expected_revision: str = Field(min_length=1, max_length=64)


class PromptBody(MutationBody):
    content: str = Field(min_length=1, max_length=20000)


class ActivateBody(MutationBody):
    revision_id: str = Field(min_length=1, max_length=64)


@router.put("/prompts/{name}")
def update_prompt(name: str, body: PromptBody, user: Admin):
    return checked(prompt_registry.update_prompt, name, body.content, reason=body.reason,
                   actor_id=user.id, expected_revision=body.expected_revision)


@router.post("/prompts/{name}/reset")
def reset_prompt(name: str, body: MutationBody, user: Admin):
    return checked(prompt_registry.reset_prompt, name, reason=body.reason,
                   actor_id=user.id, expected_revision=body.expected_revision)


@router.post("/prompts/{name}/activate")
def activate_prompt(name: str, body: ActivateBody, user: Admin):
    return checked(prompt_registry.activate_prompt, name, body.revision_id, reason=body.reason,
                   actor_id=user.id, expected_revision=body.expected_revision)
