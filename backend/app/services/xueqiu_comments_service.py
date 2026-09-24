"""Local snapshots and short enqueue requests; external work runs in the worker."""
from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import date, datetime, timedelta
import re
from threading import Event
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.config import settings
from app.security.user_context import current_user_id
from app.services import xueqiu_http
from app.storage import users_store, xueqiu_comments_store as store

_SH = ZoneInfo("Asia/Shanghai")


def normalize_symbol(symbol: str) -> str:
    symbol = str(symbol).strip().upper().replace(".SH", ".SS")
    if not re.fullmatch(r"[0-9]{6}\.(SS|SZ|BJ)", symbol):
        raise ValueError("仅支持 A 股代码，例如 600519.SS")
    return symbol


def prerequisites(uid: int) -> dict:
    token = current_user_id.set(uid)
    try:
        return {
            "xueqiu_configured": bool(xueqiu_http.effective_xueqiu_cookies()),
            "xueqiu_auto_session": True,
            "llm_configured": bool(settings.any_llm_key_configured),
        }
    finally:
        current_user_id.reset(token)


def can_refresh(configured: dict) -> bool:
    """An automatic session is acquired by the worker, never by a status read."""
    return bool(configured.get("llm_configured") and (
        configured.get("xueqiu_auto_session") or configured.get("xueqiu_configured")
    ))


def get_snapshot(uid: int, symbol: str) -> dict:
    from app.services.xueqiu_comments_scheduler import schedule_info

    symbol = normalize_symbol(symbol)
    state, job = store.read(uid, symbol)
    configured = prerequisites(uid)
    result = state["result"]
    status = state.get("status", "not_started")
    error = state.get("error")
    active = bool(job and job["status"] in {"queued", "running"})
    if active:
        status = job["status"]
    elif not can_refresh(configured):
        status = "blocked"
        error = "请先配置个人 LLM；雪球 Cookie 将由服务自动获取"
    # Old result metadata stays with old items while a new scope is queued/running.
    snapshot = {
        "symbol": symbol,
        "status": status,
        "mode": result.get("mode") or (job["mode"] if job else "latest"),
        "target_date": result.get("target_date") if result else (job["target_date"] if job else None),
        "items": result.get("items") or [],
        "raw_count": result.get("raw_count") or 0,
        "analyzed_count": result.get("analyzed_count") or 0,
        "selected_count": result.get("selected_count") or 0,
        "rejected_count": result.get("rejected_count") or 0,
        "fetched_at": result.get("fetched_at"),
        "updated_at": state.get("updated_at"),
        "last_attempt_at": state.get("last_attempt_at"),
        "error": error,
        "warnings": state.get("warnings") or [],
        "stale": bool(result and (active or status in {"partial", "error", "blocked"})),
        "job": {key: job[key] for key in ("id", "status", "progress", "total", "mode", "target_date")} if job else None,
        "prerequisites": configured,
        "xueqiu_session": xueqiu_http.session_info(),
        "schedule": schedule_info(uid, symbol, configured=configured),
    }
    return snapshot


def enqueue_refresh(uid: int, symbol: str, mode: str = "latest", target_date: str | None = None, source: str = "manual") -> dict:
    symbol = normalize_symbol(symbol)
    if mode not in {"latest", "previous_day"} or source not in {"manual", "scheduled"}:
        raise ValueError("无效刷新范围")
    if mode == "previous_day":
        target_date = date.fromisoformat(str(target_date)).isoformat() if target_date else (datetime.now(_SH).date() - timedelta(days=1)).isoformat()
    else:
        target_date = None
    configured = prerequisites(uid)
    if not can_refresh(configured):
        snapshot = get_snapshot(uid, symbol)
        snapshot.update(status="blocked", error="请先配置个人 LLM；雪球 Cookie 将由服务自动获取", stale=bool(snapshot["items"]))
        return snapshot
    outcome = store.enqueue(uid, symbol, mode=mode, target_date=target_date, source=source)
    snapshot = get_snapshot(uid, symbol)
    if outcome == "limited":
        snapshot.update(status="blocked", error="评论任务或标的数量已达上限，请稍后重试", stale=bool(snapshot["items"]))
    elif outcome == "cooldown":
        snapshot["warnings"] = [*snapshot["warnings"], "刷新间隔至少 60 秒，请稍后重试"]
    return snapshot


def fetch_comments(symbol, target_date=None, limit=200):
    from app.services.xueqiu_comments_pipeline import fetch_comments as fetch
    return fetch(symbol, target_date=target_date, limit=limit)


def select_comments(symbol, items, progress=None):
    from app.services.xueqiu_comments_pipeline import select_comments as select
    return select(symbol, items, progress=progress)


async def process_job(job: dict, owner: str) -> None:
    """Explicit context reaches to_thread and is reset even on cancellation."""
    token = current_user_id.set(job["uid"])
    lost_lease = Event()

    async def keep_lease():
        while True:
            await asyncio.sleep(30)
            try:
                renewed = await asyncio.to_thread(store.heartbeat, job["id"], owner)
            except Exception:
                renewed = False
            if not renewed:
                lost_lease.set()
                return

    def progress(done, total):
        if lost_lease.is_set() or not store.heartbeat(job["id"], owner, progress=done, total=total):
            lost_lease.set()
            raise RuntimeError("comment job ownership lost")

    heartbeat = asyncio.create_task(keep_lease())
    try:
        if not users_store.get_by_id(job["uid"]) or not can_refresh(prerequisites(job["uid"])):
            store.finish(job, owner, {"status": "blocked", "error": "账户不存在或未配置个人 LLM", "retry_when_configured": True})
            return
        fetched = await asyncio.to_thread(fetch_comments, job["symbol"], target_date=job["target_date"], limit=200)
        if lost_lease.is_set():
            return
        items = (fetched.get("items") or [])[:200]
        warnings = list(fetched.get("warnings") or [])
        common = {"raw_count": fetched.get("raw_count") or len(items), "fetched_at": fetched.get("fetched_at")}
        if fetched.get("auth_status") in {"missing", "expired", "verification_required"}:
            warnings.append("本日自动任务不重复尝试被拒绝的会话；可稍后手动刷新，若雪球要求登录或验证，请由管理员完成后配置登录 Cookie")
            result = {**common, "status": "blocked", "error": fetched.get("error") or "雪球自动会话未获访问权限，可能需要登录或验证", "warnings": warnings}
        elif fetched.get("error") and not items:
            result = {**common, "status": "error", "error": fetched["error"], "warnings": warnings}
        elif not items:
            result = {**common, "status": "partial" if fetched.get("partial") else "empty", "items": [], "analyzed_count": 0, "selected_count": 0, "rejected_count": 0, "warnings": warnings}
        else:
            progress(0, len(items))
            selected = await asyncio.to_thread(select_comments, job["symbol"], items, progress=progress)
            warnings += list(selected.get("warnings") or [])
            chosen = (selected.get("items") or [])[:20]
            errors = [str(error) for error in (fetched.get("error"), selected.get("error")) if error]
            partial = bool(fetched.get("partial") or selected.get("partial") or errors)
            analyzed = int(selected.get("analyzed_count") or 0)
            result = {
                **common, "status": "error" if errors and not analyzed and not chosen else "partial" if partial else "ready" if chosen else "empty",
                "items": chosen, "selected_count": len(chosen), "analyzed_count": analyzed,
                "rejected_count": int(selected.get("rejected_count") or 0),
                "model": selected.get("model"), "warnings": warnings, "error": "; ".join(errors) or None,
            }
        if not lost_lease.is_set():
            store.finish(job, owner, result)
    except asyncio.CancelledError:
        lost_lease.set()
        # The thread may still be unwinding a network request. Leave the lease in
        # place so another worker cannot immediately repeat a paid LLM request.
        raise
    except Exception:
        if not lost_lease.is_set():
            store.finish(job, owner, {"status": "error", "error": "评论任务未完成，请稍后重试"})
    finally:
        heartbeat.cancel()
        try:
            with suppress(asyncio.CancelledError):
                await heartbeat
        finally:
            current_user_id.reset(token)


async def worker_loop() -> None:
    owner = uuid4().hex
    while True:
        try:
            job = await asyncio.to_thread(store.claim, owner)
            if job:
                await process_job(job, owner)
            else:
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Do not log provider exceptions: URLs and messages may contain keys.
            await asyncio.sleep(5)
