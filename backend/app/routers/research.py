"""Authenticated control plane; background work always goes through the ledger."""
from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.deps.auth import get_current_user
from app.schemas.research import StrategySpecV1
from app.services import research_data, research_service
from app.storage import research_store as store
from app.storage.users_store import UserRecord

router = APIRouter(prefix="/api/research", tags=["research"])
User = Annotated[UserRecord, Depends(get_current_user)]


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["collect", "research", "evaluate", "github", "paper", "rollback"] = "research"
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=160)


class StrategyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    spec: StrategySpecV1


class ImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bundles: dict[str, dict[str, Any]]
    metadata: dict[str, Any]


def _not_found(value):
    if value is None:
        raise HTTPException(404, "当前账户没有此研究记录")
    return value


@router.get("/overview")
def overview(user: User):
    return research_service.overview(user.id)


@router.patch("/config")
def update_config(body: dict[str, Any], user: User):
    try:
        if "initial_cash" in body and store.get_state(user.id, "paper_active"):
            raise ValueError("模拟账户建立后初始资金不可修改")
        return store.update_config(user.id, body)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/runs", status_code=202)
def run(body: RunRequest, user: User):
    payload = body.payload
    allowed = {
        "collect": {"symbols", "offline"}, "research": {"snapshot_id", "goal", "candidate_count"},
        "evaluate": {"strategy_id", "snapshot_id"}, "github": set(), "paper": {"snapshot_id"}, "rollback": {"strategy_id"},
    }
    try:
        if set(payload) - allowed[body.kind]:
            raise ValueError("任务包含不支持的参数")
        for key in ("snapshot_id", "strategy_id"):
            if key in payload and (not isinstance(payload[key], str) or not 1 <= len(payload[key]) <= 200):
                raise ValueError(f"{key} 须为有效记录标识")
        if "offline" in payload and type(payload["offline"]) is not bool:
            raise ValueError("offline 须为布尔值")
        if "snapshot_id" in payload:
            _not_found(store.get_entity(user.id, "snapshot", payload["snapshot_id"]))
        if body.kind == "evaluate":
            _not_found(store.get_entity(user.id, "strategy", payload.get("strategy_id", "")))
        if "symbols" in payload:
            payload["symbols"] = research_service.canonical_symbols(payload["symbols"])
        if "candidate_count" in payload and (type(payload["candidate_count"]) is not int or not 1 <= payload["candidate_count"] <= 60):
            raise ValueError("单次候选数量须介于1和60之间")
        if "goal" in payload and (not isinstance(payload["goal"], str) or len(payload["goal"]) > 2000):
            raise ValueError("研究目标最长2000字符")
        return research_service.enqueue(user.id, body.kind, payload, body.idempotency_key)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/jobs/{job_id}")
def job(job_id: str, user: User):
    return _not_found(store.get_job(user.id, job_id))


@router.post("/strategies", status_code=201)
def create_strategy(body: StrategyRequest, user: User):
    try:
        return research_service.register_strategy(user.id, body.spec.model_dump(mode="json"))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/strategies/{strategy_id}")
def get_strategy(strategy_id: str, user: User):
    return _not_found(store.get_entity(user.id, "strategy", strategy_id))


@router.get("/experiments/{experiment_id}")
def get_experiment(experiment_id: str, user: User):
    return _not_found(store.get_entity(user.id, "experiment", experiment_id))


@router.get("/snapshots/{snapshot_id}")
def get_snapshot(snapshot_id: str, user: User):
    _not_found(store.get_entity(user.id, "snapshot", snapshot_id))
    try:
        return research_data.get_snapshot(user.id, snapshot_id)
    except ValueError as exc:
        raise HTTPException(409, "快照文件不完整或校验失败") from exc


@router.post("/import", status_code=201)
def import_snapshot(body: ImportRequest, user: User):
    try:
        if any(not isinstance(bundle.get("candles"), list) or any(not isinstance(row, dict) for row in bundle["candles"])
               for bundle in body.bundles.values()):
            raise ValueError("每个 bundle 的 candles 必须为日线对象数组")
        if sum(len(b.get("candles", [])) for b in body.bundles.values()) > 1_000_000:
            raise ValueError("单次导入不能超过100万条日线")
        return research_service.import_data(user.id, body.bundles, body.metadata)
    except (ValueError, OSError) as exc:
        raise HTTPException(400, str(exc)) from exc
