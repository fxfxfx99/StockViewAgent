"""Personal model-assisted observation, alerts and review decisions."""
from __future__ import annotations

from typing import Annotated, Literal
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from app.config import settings
from app.deps.auth import get_current_user
from app.routers.settings_llm import LLMUpdate, save_llm_settings
from app.schemas.monitoring import ConfigPatch, HandlingCreate, NotificationPatch, PlanCreate, PlanPatch, PlanReview, SHANGHAI
from app.schemas.tracking import canonical_symbol
from app.security.user_context import current_user_id
from app.services import monitoring as service, monitoring_engine as engine
from app.storage import monitoring_store as store
from app.storage.users_store import UserRecord

router = APIRouter(prefix="/api/monitoring", tags=["monitoring"])
User = Annotated[UserRecord, Depends(get_current_user)]


def _error(exc):
    if isinstance(exc, store.RevisionConflict):
        return HTTPException(409, "计划已更新，请重新读取后再保存；当前输入可以保留")
    if isinstance(exc, store.BudgetExceeded):
        return HTTPException(429, "今日模型调用额度已用完，请调整额度或明日再试")
    if isinstance(exc, engine.MonitoringError):
        return HTTPException(409 if exc.code in {"account_required", "configuration_required", "verification_required", "configuration_changed", "plan_changed", "plan_unavailable", "verification_in_progress", "verification_cooldown"} else 502, str(exc))
    if isinstance(exc, store.PlanUnavailable):
        return HTTPException(409, str(exc))
    return HTTPException(400, str(exc))


def _call(function, *args, **kwargs):
    try:
        value = function(*args, **kwargs)
    except (ValueError, store.BudgetExceeded, store.PlanUnavailable) as exc:
        raise _error(exc) from exc
    if value is None:
        raise HTTPException(404, "当前账户没有此观察记录")
    return value


def _plan(uid, identifier):
    return _call(store.get_plan, uid, identifier)


def _ready(uid, *, require_worker=False):
    status = engine.model_status(uid)
    if not status["configured"]:
        raise HTTPException(409, "请先配置大模型接口，再使用观察、监控与AI复盘")
    if not status["verified"]:
        raise HTTPException(409, "请先测试当前大模型连接，验证通过后再启用")
    if require_worker and not settings.enable_monitoring:
        raise HTTPException(409, "观察监控服务尚未开启，请启用 ENABLE_MONITORING 并重启后端")


@router.get("/overview")
def overview(user: User, symbol: str | None = None):
    usage = store.usage(user.id)
    plans = store.list_plans(user.id)
    if symbol is not None:
        try:
            symbol = canonical_symbol(symbol)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        plans = [plan for plan in plans if plan.get("target_code") == symbol or symbol in plan.get("target_symbols", [])]
    ids = {plan["id"] for plan in plans}
    return {"model": engine.model_status(user.id), "config": store.get_config(user.id),
            "usage": {**usage, "calls_used": usage["used"], "daily_call_limit": usage["limit"]},
            "worker": service.worker_status(), "plans": plans,
            "notifications": [row for row in store.list_notifications(user.id) if symbol is None or row["plan_id"] in ids]}


@router.put("/model")
def save_model(body: LLMUpdate, user: User):
    # A dedicated save path never starts the separate research autopilot.
    context = current_user_id.set(user.id)
    try:
        saved = save_llm_settings(body, user, activate_research=False)
        return {**saved, "monitoring": engine.model_status(user.id)}
    finally:
        current_user_id.reset(context)


@router.post("/model/verify")
async def verify_model(user: User):
    try:
        return await engine.verify_model(user.id)
    except (engine.MonitoringError, store.BudgetExceeded, store.PlanUnavailable) as exc:
        raise _error(exc) from exc


@router.patch("/config")
def configure(body: ConfigPatch, user: User):
    values = body.model_dump(exclude_unset=True)
    if values.get("enabled") is True:
        _ready(user.id, require_worker=True)
    return _call(store.update_config, user.id, values)


@router.post("/plans", status_code=201)
def create_plan(body: PlanCreate, user: User):
    _ready(user.id, require_worker=body.status == "active")
    if body.research_link is not None:
        from app.storage import investment_research_store
        link = body.research_link
        report = investment_research_store.get_report(user.id, link.report_id)
        if not report or report["dossier_id"] != link.dossier_id:
            raise HTTPException(404, "当前账户没有此研究报告")
        result = report["result"]
        observations = result["report"].get("next_observations") or []
        if link.observation_index >= len(observations):
            raise HTTPException(422, "研究报告中没有此下一步观察")
        snapshot = result["dossier_snapshot"]
        if investment_research_store.target_signature(body.model_dump(mode="json")) != investment_research_store.target_signature(snapshot):
            raise HTTPException(422, "观察对象须与来源研究一致；更换对象请独立创建计划")
    return _call(store.create_plan, user.id, body.model_dump(mode="json"))


@router.get("/plans/{plan_id}")
def detail(plan_id: str, user: User):
    plan = _plan(user.id, plan_id)
    return {"plan": plan, "runs": store.list_runs(user.id, plan_id=plan_id),
            "reviews": store.list_reviews(user.id, plan_id), "handling_history": store.list_handling(user.id, plan_id)}


@router.patch("/plans/{plan_id}")
def update_plan(plan_id: str, body: PlanPatch, user: User):
    plan = _plan(user.id, plan_id)
    patch = body.model_dump(mode="json", exclude_unset=True)
    if patch.get("status", plan["status"]) == "active":
        _ready(user.id)
    return _call(store.update_plan, user.id, plan_id, patch)


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["observe", "review"] = "observe"


@router.post("/plans/{plan_id}/runs", status_code=202)
def enqueue(plan_id: str, body: RunRequest, user: User):
    _plan(user.id, plan_id)
    _ready(user.id, require_worker=True)
    return _call(store.enqueue_run, user.id, plan_id, kind=body.kind, trigger="manual")


@router.post("/plans/{plan_id}/reviews")
def review(plan_id: str, body: PlanReview, user: User):
    _plan(user.id, plan_id)
    return _call(store.review_plan, user.id, plan_id, body.model_dump(mode="json", exclude_none=True))


@router.patch("/notifications/{notification_id}")
def mark_read(notification_id: str, body: NotificationPatch, user: User):
    return _call(store.mark_notification, user.id, notification_id, read=body.is_read)


@router.get("/runs/{run_id}/context")
def run_context(run_id: str, user: User):
    run = _call(store.get_run, user.id, run_id)
    plan = run["plan_snapshot"]
    public_run = {key: value for key, value in run.items() if key not in {"user_id", "lease_owner", "lease_token", "lease_until"}}
    return {"run": public_run, "plan_snapshot": plan, "evidence": (run.get("result") or {}).get("evidence", []),
            "coverage": (run.get("result") or {}).get("coverage", []),
            "target_symbols": plan.get("target_symbols") or ([plan["target_code"]] if plan.get("target_type") == "stock" else []),
            "trigger_date": datetime.fromtimestamp(run.get("finished_at") or run["queued_at"], SHANGHAI).date().isoformat(),
            "trigger_date_kind": "monitor_check_date", "research_link": plan.get("research_link")}


@router.post("/notifications/{notification_id}/handling")
def handle_notification(notification_id: str, body: HandlingCreate, user: User):
    if body.dossier_id:
        from app.storage import investment_research_store
        try:
            if investment_research_store.get_dossier(user.id, body.dossier_id) is None:
                raise HTTPException(404, "当前账户没有此研究档案")
        except investment_research_store.NotFound as exc:
            raise HTTPException(404, "当前账户没有此研究档案") from exc
    return _call(store.record_handling, user.id, notification_id, body.model_dump(mode="json"))
