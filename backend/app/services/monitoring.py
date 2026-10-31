"""Independent, opt-in observation worker with account-scoped model execution."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import fcntl
import logging
import os
import stat
import time
import uuid

from app.config import settings
from app.security.user_context import current_user_id
from app.storage import monitoring_store as store, users_store

logger = logging.getLogger(__name__)


def worker_status():
    state = store.get_worker_state()
    fresh = time.time() - float(state.get("heartbeat_at") or 0) < 75
    # Never expose another account's plan or active run IDs in shared state.
    return {"enabled": settings.enable_monitoring, "running": bool(settings.enable_monitoring and fresh and state.get("running")),
            "heartbeat_at": state.get("heartbeat_at"), "phase": state.get("phase", "idle")}


@contextmanager
def worker_lock():
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(settings.data_dir / "monitoring-worker.lock",
                         os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("观察监控锁文件无效")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


async def _run(run, owner):
    from app.services import monitoring_engine as engine

    uid, rid, lease = run["user_id"], run["id"], run["lease_token"]
    context = current_user_id.set(uid)
    operation = None

    async def renew():
        while True:
            await asyncio.sleep(20)
            store.renew_run(uid, rid, lease)
            store.set_worker_state({"owner": owner, "running": True, "phase": "observing", "heartbeat_at": time.time()})

    heartbeat = asyncio.create_task(renew())
    try:
        if users_store.get_by_id(uid) is None:
            store.fail_run(uid, rid, lease, "account_unavailable")
            return
        plan = store.get_plan(uid, run["plan_snapshot"]["id"])
        if (not plan or plan["status"] != "active" or plan["revision"] != run["plan_snapshot"]["revision"]
                or not store.get_config(uid)["enabled"]):
            store.fail_run(uid, rid, lease, "plan_changed")
            return
        operation = asyncio.create_task(engine.generate_report(uid, run))
        done, _ = await asyncio.wait({operation, heartbeat}, return_when=asyncio.FIRST_COMPLETED)
        if heartbeat in done:
            # A lost lease must not publish under an expired ownership token.
            await asyncio.gather(asyncio.shield(operation), return_exceptions=True)
            heartbeat.result()
        result = await asyncio.shield(operation)
        store.complete_run(uid, rid, lease, result)
    except asyncio.CancelledError:
        # Keep the process lock while any paid request or writer is in flight.
        if operation is not None:
            await asyncio.gather(asyncio.shield(operation), return_exceptions=True)
        raise
    except Exception as exc:
        code = getattr(exc, "code", None) or ("daily_budget_exhausted" if isinstance(exc, store.BudgetExceeded) else "observation_failed")
        try:
            store.fail_run(uid, rid, lease, code)
        except store.LeaseLost:
            pass
        logger.warning("观察任务未完成 code=%s type=%s", code, type(exc).__name__)
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
        current_user_id.reset(context)


async def run_cycle(*, owner=None):
    if not settings.enable_monitoring:
        return {"status": "disabled", "processed": 0}
    owner = owner or uuid.uuid4().hex
    with worker_lock() as acquired:
        if not acquired:
            return {"status": "another_worker", "processed": 0}
        processed = 0
        try:
            store.recover_interrupted()
            store.set_worker_state({"owner": owner, "running": True, "phase": "scheduling", "heartbeat_at": time.time()})
            store.schedule_due()
            for _ in range(3):
                run = store.claim_run(owner)
                if not run:
                    break
                store.set_worker_state({"owner": owner, "running": True, "phase": "observing", "heartbeat_at": time.time()})
                await _run(run, owner)
                processed += 1
            return {"status": "processed" if processed else "idle", "processed": processed}
        finally:
            store.set_worker_state({"owner": owner, "running": True, "phase": "idle", "heartbeat_at": time.time()})


async def worker_loop():
    owner = uuid.uuid4().hex
    await asyncio.sleep(5)
    try:
        while True:
            try:
                await run_cycle(owner=owner)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("观察监控后台暂不可用 type=%s", type(exc).__name__)
            await asyncio.sleep(15)
    finally:
        state = store.get_worker_state()
        if state.get("owner") == owner:
            store.set_worker_state({"owner": owner, "running": False, "phase": "stopped", "heartbeat_at": time.time()})
