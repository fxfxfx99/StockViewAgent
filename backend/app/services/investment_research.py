"""Explicitly requested personal research, with a durable independent worker."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import fcntl
import hashlib
import logging
import os
from pathlib import Path
import re
import stat
import time
import uuid

from app.config import settings
from app.schemas.investment_research import DossierCreate, MaterialCreate
from app.schemas.tracking import canonical_symbol
from app.security.user_context import current_user_id
from app.storage import investment_research_store as store, monitoring_store, users_store

logger = logging.getLogger(__name__)
MAX_UPLOAD_BYTES = 30 * 1024 * 1024


def public_job(job):
    return {key: value for key, value in job.items() if key not in {"user_id", "owner", "lease_until", "snapshot"}}


def worker_status():
    state = store.get_worker_state()
    fresh = time.time() - float(state.get("heartbeat_at") or 0) < 75
    return {"enabled": settings.enable_investment_research,
            "running": bool(settings.enable_investment_research and fresh and state.get("running")),
            "heartbeat_at": state.get("heartbeat_at"), "phase": state.get("phase", "idle")}


def overview(uid, *, symbol=None):
    from app.services import investment_research_engine as engine
    symbol = canonical_symbol(symbol) if symbol else None
    return {"dossiers": store.list_dossiers(uid, symbol=symbol), "usage": store.usage(uid),
            "model": engine.model_status(uid), "worker": worker_status()}


def detail(uid, identifier):
    dossier = store.get_dossier(uid, identifier)
    if dossier is None:
        raise store.NotFound("研究档案不存在")
    return {"dossier": dossier, "questions": store.list_questions(uid, identifier),
            "materials": store.list_materials(uid, identifier), "reports": store.list_reports(uid, identifier),
            "jobs": [public_job(row) for row in store.list_jobs(uid, identifier)],
            "adoptions": store.list_adoptions(uid, identifier)}


def _monitor_material(uid, identifier, dossier, *, title="监控变化与证据"):
    run = monitoring_store.get_run(uid, identifier)
    if not run or run.get("status") != "succeeded" or not isinstance(run.get("result"), dict):
        raise store.NotFound("可导入的已完成监控任务不存在")
    result = run["result"]
    snapshot = result.get("plan_snapshot") or run.get("plan_snapshot") or {}
    if store.target_signature(snapshot) != store.target_signature(dossier):
        raise store.ResearchConflict("监控对象与研究档案不一致，请为该对象创建对应档案")
    frozen = {key: result.get(key) for key in ("report", "evidence", "coverage", "generated_at", "input_hash", "model")}
    frozen["plan_snapshot"] = snapshot
    return {"kind": "monitoring", "title": title, "text": "", "monitor_run_id": identifier,
            "monitoring_snapshot": frozen, "source_url": "", "published_at": result.get("generated_at"),
            "status": "ready", "provenance": "monitoring_opinion_with_frozen_sources"}


def create_dossier(uid, payload):
    value = DossierCreate.model_validate(payload).model_dump()
    material = _monitor_material(uid, value["monitor_run_id"], value) if value.get("monitor_run_id") else None
    return store.create_dossier(uid, value, monitoring_material=material)


def add_material(uid, dossier_id, payload):
    value = MaterialCreate.model_validate(payload).model_dump()
    dossier = store.get_dossier(uid, dossier_id)
    if dossier is None:
        raise store.NotFound("研究档案不存在")
    if value["kind"] == "monitoring":
        value = _monitor_material(uid, value["monitor_run_id"], dossier, title=value["title"])
    else:
        value["provenance"] = "user_supplied_unverified"
    return store.add_material(uid, dossier_id, value)


def _file_path(uid, sha):
    if type(uid) is not int or uid < 1 or not re.fullmatch(r"[a-f0-9]{64}", str(sha)):
        raise ValueError("材料文件标识无效")
    folder = settings.data_dir / "investment_research" / str(uid)
    for part in (folder.parent, folder):
        if part.is_symlink():
            raise ValueError("研究材料存储目录无效")
        part.mkdir(parents=True, exist_ok=True)
        if not part.is_dir():
            raise ValueError("研究材料存储目录无效")
    path = folder / f"{sha}.pdf"
    if path.is_symlink() or path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
        raise ValueError("研究材料文件路径无效")
    return path


def _save_pdf(uid, sha, content):
    path = _file_path(uid, sha)
    if path.exists():
        if path.stat().st_size != len(content) or hashlib.sha256(path.read_bytes()).hexdigest() != sha:
            raise ValueError("研究材料原文件校验失败")
        return
    temporary = path.with_name(f".{sha}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            os.chmod(temporary, 0o600)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def upload_material(uid, dossier_id, filename, content, *, title=None):
    if not isinstance(content, bytes) or not content or len(content) > MAX_UPLOAD_BYTES:
        raise ValueError("材料文件须为非空文件，最大30MiB")
    dossier = store.get_dossier(uid, dossier_id)
    if dossier is None:
        raise store.NotFound("研究档案不存在")
    if dossier["status"] != "active":
        raise store.ResearchConflict("档案已归档，请恢复后上传材料")
    filename = Path(filename or "material").name
    title = (title or filename).strip()
    if not title or len(title) > 200:
        raise ValueError("材料标题须为1–200个字符")
    extension = Path(filename).suffix.lower()
    if extension in {".txt", ".md"}:
        try:
            decoded = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ValueError("文字材料须使用UTF-8编码") from None
        if "\x00" in decoded:
            raise ValueError("文字材料包含无效字符")
        return add_material(uid, dossier_id, {"kind": "text", "title": title, "text": decoded})
    if extension != ".pdf" or not content.startswith(b"%PDF-"):
        raise ValueError("仅支持TXT、Markdown和PDF文件")
    if not settings.enable_investment_research:
        raise store.ResearchConflict("研究后台已关闭，无法解析PDF")
    sha = hashlib.sha256(content).hexdigest()
    _save_pdf(uid, sha, content)
    material = store.add_material(uid, dossier_id, {"kind": "pdf", "title": title, "sha256": sha,
                                                   "status": "pending", "byte_size": len(content),
                                                   "provenance": "user_supplied_unverified"})
    try:
        job = store.enqueue(uid, dossier_id, kind="prepare", material_id=material["id"])
    except Exception:
        # Do not leave a material waiting forever if the bounded queue is full.
        store.delete_material(uid, material["id"])
        raise
    return {**material, "job": public_job(job)}


def pdf_path(uid, material_id):
    material = store.get_material(uid, material_id)
    if not material or material["kind"] != "pdf":
        raise store.NotFound("PDF材料不存在")
    path = _file_path(uid, material["sha256"])
    if not path.is_file():
        raise store.NotFound("PDF原文件不存在")
    return path


async def _execute(job, owner):
    from app.services import earnings_analysis, investment_research_engine as engine
    uid, identifier = job["user_id"], job["id"]
    if users_store.get_by_id(uid) is None:
        raise store.ResearchConflict("账户不存在，研究任务未执行")
    dossier = store.get_dossier(uid, job["dossier_id"])
    if not dossier or dossier["status"] != "active":
        raise store.ResearchConflict("档案已归档或不存在")
    if job["kind"] == "prepare":
        material = store.get_material(uid, job["material_id"])
        if material is None:
            raise store.NotFound("材料已移除")
        path = pdf_path(uid, material["id"])
        store.heartbeat(uid, identifier, owner, phase="extracting")
        extraction = await asyncio.to_thread(earnings_analysis.extract_pdf, path)
        return {"extraction": extraction}
    snapshot = job["snapshot"]
    if dossier["revision"] != snapshot["dossier"]["revision"]:
        raise store.ResearchConflict("档案已经更新，本次未调用模型")
    materials = [store.get_material(uid, mid, include_deleted=True) for mid in snapshot["material_ids"]]
    if any(material is None or material["dossier_id"] != dossier["id"] for material in materials):
        raise store.ResearchConflict("研究材料快照不可用")
    adopted = store.get_report(uid, snapshot["dossier"].get("adopted_report_id"))
    previous = adopted["result"] if adopted and store.target_signature(adopted["result"]["dossier_snapshot"]) == store.target_signature(dossier) else None
    store.heartbeat(uid, identifier, owner, phase="researching")
    return await engine.analyze_research(uid, snapshot["dossier"], questions=snapshot["questions"],
                                        materials=materials, previous=previous, focus=job["focus"],
                                        before_dispatch=lambda: store.reserve_call(uid, identifier, owner),
                                        find_cached=lambda key: store.cached_report(uid, dossier["id"], key))


@contextmanager
def worker_lock():
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(settings.data_dir / "investment-research-worker.lock",
                         os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("研究后台锁文件无效")
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


async def process_job(job, owner):
    uid, identifier = job["user_id"], job["id"]
    context = current_user_id.set(uid)
    operation = None

    async def renew():
        while True:
            await asyncio.sleep(20)
            store.heartbeat(uid, identifier, owner)
            store.set_worker_state({"owner": owner, "running": True, "phase": "processing", "heartbeat_at": time.time()})

    heartbeat = asyncio.create_task(renew())
    try:
        operation = asyncio.create_task(_execute(job, owner))
        done, _ = await asyncio.wait({operation, heartbeat}, return_when=asyncio.FIRST_COMPLETED)
        if heartbeat in done:
            await asyncio.gather(asyncio.shield(operation), return_exceptions=True)
            heartbeat.result()
        result = await asyncio.shield(operation)
        store.finish(uid, identifier, owner, result)
    except asyncio.CancelledError:
        # Drain both parser threads and paid requests while retaining the OS lock.
        if operation is not None:
            await asyncio.gather(asyncio.shield(operation), return_exceptions=True)
        raise
    except Exception as exc:
        code = getattr(exc, "code", None) or ("daily_budget_exhausted" if isinstance(exc, store.DailyLimit)
                else "dossier_changed" if isinstance(exc, store.ResearchConflict) else "research_failed")
        try:
            store.fail(uid, identifier, owner, code)
        except store.LeaseLost:
            pass
        logger.warning("研究任务未完成 code=%s type=%s", code, type(exc).__name__)
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
        current_user_id.reset(context)


async def run_cycle(*, owner=None):
    if not settings.enable_investment_research:
        return {"status": "disabled", "processed": 0}
    owner = owner or uuid.uuid4().hex
    with worker_lock() as held:
        if not held:
            return {"status": "another_worker", "processed": 0}
        processed = 0
        try:
            store.recover_interrupted()
            store.set_worker_state({"owner": owner, "running": True, "phase": "processing", "heartbeat_at": time.time()})
            for _ in range(3):
                job = store.claim(owner)
                if job is None:
                    break
                await process_job(job, owner)
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
                logger.warning("研究后台暂不可用 type=%s", type(exc).__name__)
            await asyncio.sleep(5)
    finally:
        state = store.get_worker_state()
        if state.get("owner") == owner:
            store.set_worker_state({"owner": owner, "running": False, "phase": "stopped", "heartbeat_at": time.time()})
