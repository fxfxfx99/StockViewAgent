"""Local, user-scoped setup and queue reconciliation; no model or network calls."""
from __future__ import annotations

from datetime import datetime
import json
import os
import re
import time
import uuid

from app.config import settings
from app.security.user_context import current_user_id
from app.services.market_time import SHANGHAI_TZ
from app.storage import research_store as store, users_store, watchlist_store

DEFAULT_SYMBOLS = watchlist_store.DEFAULT_SYMBOLS
REPAIR_COOLDOWN_SECONDS = 3600
_A_SHARE = re.compile(r"^\d{6}\.(SS|SZ|BJ)$")
_ACTIVE = {"queued", "running", "retry"}
_RETRY_RESULTS = {"data_unavailable", "data_required", "configuration_required", "in_progress", "partial", "waiting_collection"}


def _watchlist(user_id):
    path = settings.data_dir / "watchlists" / f"{int(user_id)}.json"
    exists = path.exists()
    try:
        raw = json.loads(path.read_text()) if exists else {}
        values = raw.get("symbols", []) if isinstance(raw, dict) else raw
        if not isinstance(values, list):
            raise ValueError("invalid watchlist")
        symbols = list(dict.fromkeys(str(s).strip().upper().replace(".SH", ".SS") for s in values if str(s).strip()))
        issue = None
    except (OSError, ValueError, TypeError):
        symbols, issue = [], "watchlist_unreadable"
    marker = store.get_state(user_id, "autopilot_initialized", {})
    return {"symbols": symbols, "research_symbols": sorted(s for s in symbols if _A_SHARE.fullmatch(s)),
            "count": len(symbols), "seeded": bool(marker.get("seeded")),
            "explicitly_empty": exists and not symbols and issue is None, "issue": issue}


def initialize_user(user_id):
    """Never overwrite an existing list, including an intentionally empty one."""
    if not settings.enable_research_worker:
        return
    path = settings.data_dir / "watchlists" / f"{int(user_id)}.json"
    if int(user_id) == 1:
        watchlist_store.migrate_legacy_watchlist_for_admin(1)
    seeded = False
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.parent / f".autopilot-{uuid.uuid4().hex}.tmp"
        try:
            temporary.write_text(json.dumps({"symbols": list(DEFAULT_SYMBOLS)}, ensure_ascii=False))
            try:
                # Atomic creation without replacement protects a concurrent clear.
                os.link(temporary, path)
                seeded = True
            except FileExistsError:
                pass
        finally:
            temporary.unlink(missing_ok=True)
    store.compare_and_set_state(user_id, "autopilot_initialized", None, {"at": time.time(), "seeded": seeded})


def llm_configured(user_id):
    token = current_user_id.set(int(user_id))
    try:
        # Match generation's endpoint without a paid probe or an ambient key.
        return bool(settings.kline_llm_key and settings.kline_llm_base and settings.kline_llm_model)
    finally:
        current_user_id.reset(token)


def research_wait_reason(user_id, snapshot_id):
    symbols = _watchlist(user_id)["research_symbols"]
    if not symbols:
        return "waiting_watchlist"
    if not llm_configured(user_id):
        return "configuration_required"
    snapshot = store.get_entity(user_id, "snapshot", snapshot_id) if snapshot_id else None
    if not snapshot:
        return "waiting_collection"
    pool_id = snapshot["payload"].get("pool_version_id")
    pool = store.get_entity(user_id, "pool_version", pool_id) if pool_id else None
    frozen_symbols = (pool or {}).get("payload", {}).get("symbols") or snapshot["payload"].get("symbols", [])
    if set(frozen_symbols) != set(symbols) or not set(symbols) <= set(snapshot["payload"].get("symbols", [])):
        return "waiting_collection"
    return None


def _public_job(job):
    if not job:
        return None
    outcome = (job.get("result") or {}).get("status")
    repairable = job["status"] == "failed" or (job["status"] == "completed" and outcome in _RETRY_RESULTS)
    return {key: job.get(key) for key in ("id", "kind", "status", "attempts", "not_before", "updated_at")} | {
        "outcome": outcome, "retry_at": job["updated_at"] + REPAIR_COOLDOWN_SECONDS if repairable else None}


def _ensure_job(user_id, kind, payload, logical_key, now):
    state_key = f"autopilot_job:{logical_key}"
    pointer = store.get_state(user_id, state_key)
    previous = store.get_job(user_id, pointer) if pointer else None
    if previous and previous["status"] in _ACTIVE:
        return previous
    failed = previous and (previous["status"] == "failed" or
                           (previous["status"] == "completed" and (previous.get("result") or {}).get("status") in _RETRY_RESULTS))
    if previous and not failed:
        return previous
    activated = bool(previous and (previous.get("result") or {}).get("status") == "configuration_required" and llm_configured(user_id))
    if failed and not activated and now - previous["updated_at"] < REPAIR_COOLDOWN_SECONDS:
        return previous
    # Coalesce an active prior-day/manual task with identical scoped inputs.
    for job in store.list_jobs(user_id, limit=100):
        if job["kind"] == kind and job["status"] in _ACTIVE and job["payload"] == payload:
            store.set_state(user_id, state_key, job["id"])
            return job
    suffix = f":activated:{previous['id']}" if activated else f":repair:{int(now // REPAIR_COOLDOWN_SECONDS)}" if failed else ""
    job = store.queue_job(user_id, kind, payload, f"autopilot:{logical_key}{suffix}", max_attempts=3)
    store.set_state(user_id, state_key, job["id"])
    return job


def reconcile(user_id, *, now=None, startup=False):
    """Idempotent local repair: queue bounded work, never execute it in an API."""
    now = datetime.now(SHANGHAI_TZ) if now is None else now.astimezone(SHANGHAI_TZ)
    at = now.timestamp()
    config = store.get_config(user_id)
    if not settings.enable_research_worker or not config["enabled"]:
        return status(user_id) | {"queued_jobs": []}
    initialize_user(user_id)
    watchlist = _watchlist(user_id)
    symbols = watchlist["research_symbols"]
    jobs = {}
    day, week = now.date().isoformat(), now.strftime("%G-W%V")
    # Clearing a watchlist stops new research, but cannot abandon existing paper
    # holdings and their future liquidation or corporate-action accounting.
    from app.services import research_paper
    collect_symbols = symbols or research_paper.required_symbols(user_id)
    if collect_symbols and not watchlist["issue"]:
        pool = store.content_hash(collect_symbols)[:16]
        period = "close" if (now.hour, now.minute) >= (16, 30) else "startup"
        cycle = f"{day}:{period}:{pool}"
        jobs["collect"] = _ensure_job(user_id, "collect", {"symbols": collect_symbols}, f"collect:{cycle}", at)
        collected = jobs["collect"]
        collected_snapshot = (collected.get("result") or {}).get("snapshot_id") if collected["status"] == "completed" else None
        # Existing holdings keep settling while a new collection is retrying.
        snapshot_id = collected_snapshot or store.get_state(user_id, "latest_snapshot")
        snapshot = store.get_entity(user_id, "snapshot", snapshot_id) if snapshot_id else None
        if snapshot:
            jobs["paper"] = _ensure_job(user_id, "paper", {"snapshot_id": snapshot_id}, f"paper:{snapshot_id}", at)
            complete_pool = bool(collected_snapshot and symbols) and research_wait_reason(user_id, snapshot_id) is None
            if complete_pool and not config["paused"] and config["weekly_candidates"] > 0:
                jobs["research"] = _ensure_job(user_id, "research", {"snapshot_id": snapshot_id,
                    "candidate_count": min(60, config["weekly_candidates"])}, f"research:{cycle}", at)
    # Initial data is the dependency for research and paper work. Optional
    # source checks and GitHub discovery must not occupy the worker first.
    jobs["source_maintenance"] = _ensure_job(user_id, "source_maintenance", {}, f"sources:{day}", at)
    if not config["paused"]:
        jobs["github"] = _ensure_job(user_id, "github", {}, f"github:{week}", at)
    prior = store.get_state(user_id, "autopilot_last", {})
    ids = {**prior.get("jobs", {}), **{kind: job["id"] for kind, job in jobs.items()}}
    store.set_state(user_id, "autopilot_last", {"at": at, "jobs": ids})
    return status(user_id) | {"queued_jobs": [_public_job(job) for job in jobs.values() if job["status"] in _ACTIVE]}


def reconcile_all(*, now=None, startup=False):
    if not settings.enable_research_worker:
        return []
    jobs = []
    for user in users_store.list_users():
        jobs.extend(reconcile(user.id, now=now, startup=startup)["queued_jobs"])
    return jobs


def status(user_id):
    config = store.get_config(user_id)
    enabled = settings.enable_research_worker and config["enabled"]
    watchlist = _watchlist(user_id)
    configured = llm_configured(user_id)
    last = store.get_state(user_id, "autopilot_last", {})
    jobs = {kind: _public_job(store.get_job(user_id, last.get("jobs", {}).get(kind, "")))
            for kind in ("collect", "github", "research", "paper", "source_maintenance")}
    heartbeat = store.get_state(0, "worker_heartbeat", {}) or {}
    healthy = heartbeat.get("status") in {"idle", "busy"} and time.time() - heartbeat.get("seen_at", 0) < 150
    worker = {"status": heartbeat.get("status", "offline") if healthy else "offline", "healthy": healthy,
              "seen_at": heartbeat.get("seen_at")}
    sid = store.get_state(user_id, "latest_snapshot")
    record = store.get_entity(user_id, "snapshot", sid) if sid else None
    data = ({"snapshot_id": sid, "strict": record["payload"].get("strict", False),
             "quality": record["payload"].get("quality"), "symbols": record["payload"].get("symbols", []),
             "created_at": record["created_at"]} if record else None)
    if not enabled:
        phase, action = "disabled", "开启本地研究 worker 和研究开关"
    elif watchlist["issue"]:
        phase, action = "attention", "修复无法读取的自选列表；已有文件保持不变"
    elif not watchlist["research_symbols"]:
        phase, action = "waiting_watchlist", "添加 A 股自选标的后自动继续"
    elif config["paused"]:
        phase, action = "paused", "研究已暂停；数据采集和模拟账本维护继续"
    elif not configured:
        phase, action = "waiting_llm", "保存大模型配置后自动开始研究；数据采集继续"
    elif not healthy:
        phase, action = "worker_offline", "等待本地 worker 启动或恢复"
    elif any(job and job["retry_at"] for job in jobs.values()):
        phase, action = "attention", "任务暂时失败；保留已有数据并在冷却结束后自动重试"
    elif jobs["collect"] and jobs["collect"]["status"] in _ACTIVE:
        phase, action = "collecting", "采集完成后自动运行研究和模拟维护"
    elif not data:
        phase, action = "collecting", "等待完整股票池的数据快照；失败采集将按冷却重试"
    elif jobs["research"] and jobs["research"]["status"] in _ACTIVE:
        phase, action = "researching", "在固定预算内生成、验证候选并记录失败证据"
    else:
        phase, action = "observing", "持续积累行情与前向证据；达到门槛后才更新现行策略"
    try:
        from app.services import source_autopilot
        source_health = source_autopilot.summary(user_id)
    except ImportError:
        source_health = None
    return {"enabled": enabled, "paused": config["paused"], "llm_configured": configured,
            "phase": phase, "next_action": action, "watchlist": watchlist, "worker": worker, "jobs": jobs,
            "budget": store.budget_status(user_id), "data": data,
            "source_health": source_health, "last_reconciled_at": last.get("at")}
