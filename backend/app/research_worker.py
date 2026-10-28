"""Single-writer local research worker: python -m app.research_worker.

SQLite leases fence attempts; an OS lock additionally keeps two launchers from
running expensive research in parallel. API processes never own this scheduler.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import fcntl
import logging
import os
import signal
import time
import uuid

from app.config import settings
from app.security.user_context import current_user_id
from app.storage import research_store as store, users_store

logger = logging.getLogger(__name__)
LEASE_RENEW_INTERVAL = 20


@contextmanager
def worker_lock():
    path = settings.data_dir / "research-worker.lock"
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("已有研究 worker 正在运行") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def schedule_due(now=None, *, startup=False):
    from app.services import local_autopilot
    return local_autopilot.reconcile_all(now=now, startup=startup)


async def dispatch(job):
    uid, kind, payload = job["user_id"], job["kind"], job["payload"]
    if kind == "watch_review":
        from app.services import tracking_service
        return await asyncio.to_thread(tracking_service.refresh, uid)
    from app.services import decision_signal_service, research_generation, research_paper, research_service
    if kind == "collect":
        return await research_service.collect_data(uid, payload)
    if kind == "research":
        if job.get("idempotency_key", "").startswith("autopilot:"):
            from app.services import local_autopilot
            waiting = local_autopilot.research_wait_reason(uid, payload.get("snapshot_id"))
            if waiting:
                return {"status": waiting}
        return await research_service.run_research(uid, payload)
    if kind == "evaluate":
        return await research_service.evaluate_strategy(uid, payload)
    if kind == "github":
        return await research_generation.refresh_github_sources(uid)
    if kind == "source_maintenance":
        from app.services import source_autopilot
        return await source_autopilot.maintain(uid)
    if kind == "signals":
        return await decision_signal_service.evaluate(uid)
    if kind == "paper":
        snapshot_id = payload.get("snapshot_id") or store.get_state(uid, "latest_snapshot")
        if not snapshot_id:
            return {"status": "data_required"}
        return await asyncio.to_thread(research_paper.update_paper, uid, snapshot_id)
    if kind == "rollback":
        return await asyncio.to_thread(research_paper.rollback, uid, {**payload, "job_id": job["id"]})
    raise ValueError("未知任务类型")


async def run_one(worker_id):
    job = store.claim_job(worker_id, lease_seconds=120)
    if not job:
        return False
    uid, jid, token = job["user_id"], job["id"], job["lease_token"]
    context_token = current_user_id.set(uid)
    finished = asyncio.Event()

    async def renew():
        while not finished.is_set():
            try:
                await asyncio.wait_for(finished.wait(), timeout=LEASE_RENEW_INTERVAL)
            except asyncio.TimeoutError:
                store.renew_job(uid, jid, token, lease_seconds=120)
                store.set_state(0, "worker_heartbeat", {"worker_id": worker_id, "pid": os.getpid(), "seen_at": time.time(), "status": "busy"})

    heartbeat = asyncio.create_task(renew())
    operation = asyncio.create_task(dispatch(job))
    try:
        done, _ = await asyncio.wait({operation, heartbeat}, return_when=asyncio.FIRST_COMPLETED)
        if heartbeat in done and heartbeat.exception():
            # Cancelling a to_thread wrapper does not stop its writer thread.
            # Keep the OS writer lock until the operation has fully drained;
            # never lease another job alongside an orphaned paper/holdout write.
            await asyncio.gather(asyncio.shield(operation), return_exceptions=True)
            raise heartbeat.exception()
        result = await asyncio.shield(operation)
        store.complete_job(uid, jid, token, result)
        if settings.enable_research_worker:
            from app.services import local_autopilot
            local_autopilot.reconcile(uid)
    except store.LeaseLost:
        logger.warning("研究任务租约失效；等待当前操作收尾后停止 worker")
        raise
    except asyncio.CancelledError:
        await asyncio.gather(asyncio.shield(operation), return_exceptions=True)
        raise
    except Exception as exc:
        label = f"研究任务失败（{type(exc).__name__}）；已保留历史结果"
        logger.warning(label)
        try:
            store.fail_job(uid, jid, token, label, retry_delay=min(300, 30 * 2 ** job["attempts"]))
        except store.LeaseLost:
            pass
    finally:
        finished.set()
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
        current_user_id.reset(context_token)
    return True


async def serve(*, once=False, stop=None):
    stop = stop or asyncio.Event()
    worker_id = f"local:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    last_schedule = 0.0
    from app.storage.data_portability import data_directory_lock
    from app.storage.storage_bootstrap import initialize_local_storage
    with data_directory_lock(settings.data_dir), worker_lock():
        initialize_local_storage()
        users_store.init_db()
        while not stop.is_set():
            active = settings.enable_research_worker
            store.set_state(0, "worker_heartbeat", {"worker_id": worker_id, "pid": os.getpid(), "seen_at": time.time(), "status": "idle" if active else "disabled"})
            if active and time.monotonic() - last_schedule >= 60:
                schedule_due(startup=last_schedule == 0)
                last_schedule = time.monotonic()
            worked = await run_one(worker_id) if active else False
            if once:
                break
            if not worked:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass
        store.set_state(0, "worker_heartbeat", {"worker_id": worker_id, "pid": os.getpid(), "seen_at": time.time(), "status": "stopped"})


def main():
    parser = argparse.ArgumentParser(description="StockViewAgent 持久化研究 worker")
    parser.add_argument("--once", action="store_true", help="调度并最多执行一个待处理任务后退出")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    async def run():
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await serve(once=args.once, stop=stop)
    try:
        asyncio.run(run())
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
