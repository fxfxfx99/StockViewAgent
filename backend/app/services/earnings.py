"""Durable on-demand filing discovery, PDF preparation and personal interpretation."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import stat
import uuid

from app.config import settings
from app.schemas.tracking import canonical_symbol
from app.security.user_context import current_user_id
from app.storage import earnings_store as store, users_store

logger = logging.getLogger(__name__)


class EarningsError(ValueError):
    pass


def normalize_symbol(symbol):
    return canonical_symbol(symbol)


def public_job(job):
    return {key: value for key, value in job.items() if key not in {"user_id", "owner", "lease_until"}}


def overview(uid, symbol):
    from app.services import earnings_analysis
    symbol = normalize_symbol(symbol)
    reports = []
    for row in store.list_reports(symbol):
        document = store.get_document(row.get("document_sha"))
        reports.append({**row, "pdf_url": row.get("pdf_url") or (document or {}).get("pdf_url"), "document": document})
    return {"symbol": symbol, "reports": reports, "jobs": [public_job(row) for row in store.list_jobs(uid, symbol=symbol)],
            "source_status": store.source_status(symbol), "model": earnings_analysis.get_model_status(uid),
            "usage": store.usage(uid), "worker_enabled": settings.enable_earnings}


def report_detail(uid, report_id):
    report = store.get_report(report_id)
    if report is None:
        return None
    document = store.get_document(report.get("document_sha"))
    return {"report": {**report, "pdf_url": report.get("pdf_url") or (document or {}).get("pdf_url")}, "document": document,
            "analyses": store.list_analyses(uid, report_id),
            "jobs": [public_job(row) for row in store.list_jobs(uid, report_id=report_id)]}


def _path(sha, suffix):
    if not re.fullmatch(r"[a-f0-9]{64}", str(sha)) or suffix not in {"pdf", "json"}:
        raise EarningsError("财报文件标识无效")
    folder = settings.data_dir / "earnings" / ("files" if suffix == "pdf" else "text")
    for part in (folder.parent, folder):
        if part.is_symlink():
            raise EarningsError("财报存储目录无效")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{sha}.{suffix}"
    if path.is_symlink() or path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
        raise EarningsError("财报文件路径无效")
    return path


def pdf_path(report_id):
    report = store.get_report(report_id)
    if not report or not report.get("document_sha"):
        return None
    path = _path(report["document_sha"], "pdf")
    return path if path.is_file() else None


def _atomic_write(path: Path, data: bytes):
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as output:
            os.chmod(temporary, 0o600)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _cached_extraction(report):
    sha = report.get("document_sha")
    if not sha:
        return None
    pdf, text = _path(sha, "pdf"), _path(sha, "json")
    if not pdf.is_file() or not text.is_file() or pdf.stat().st_size > 30 * 1024 * 1024 or text.stat().st_size > 12 * 1024 * 1024:
        return None
    try:
        if hashlib.sha256(pdf.read_bytes()).hexdigest() != sha:
            return None
        extraction = json.loads(text.read_text(encoding="utf-8"))
        if extraction.get("sha256") == sha and extraction.get("status") in {"ready", "partial", "no_text", "encrypted"}:
            return extraction
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return None


async def _prepare(job, owner):
    from app.services import earnings_analysis, earnings_sources
    report = store.get_report(job["report_id"])
    if not report:
        raise EarningsError("该财报记录不存在，请重新查找")
    extraction = await asyncio.to_thread(_cached_extraction, report)
    if extraction:
        return report, extraction
    store.heartbeat(job["user_id"], job["id"], owner, phase="downloading")
    downloaded = await earnings_sources.download_report_pdf(report)
    content = downloaded["content"]
    original_url = downloaded.get("url")
    sha = hashlib.sha256(content).hexdigest()
    if downloaded.get("sha256") != sha or len(content) > 30 * 1024 * 1024 or not content.startswith(b"%PDF-"):
        raise EarningsError("财报附件校验失败，请重新查找官方财报")
    path = _path(sha, "pdf")
    await asyncio.to_thread(_atomic_write, path, content)
    del content, downloaded
    store.heartbeat(job["user_id"], job["id"], owner, phase="extracting")
    extraction = await asyncio.to_thread(earnings_analysis.extract_pdf, path)
    if extraction.get("sha256") != sha:
        raise EarningsError("财报解析结果与原文不一致")
    extraction["pdf_url"] = original_url
    await asyncio.to_thread(_atomic_write, _path(sha, "json"), json.dumps(extraction, ensure_ascii=False, allow_nan=False).encode())
    store.save_document(report["id"], sha, extraction)
    return {**report, "document_sha": sha}, extraction


_TEXT_ERRORS = {"no_text": "PDF缺少可提取文字，可能是扫描件；请打开原文核对，本次未调用模型",
                "encrypted": "PDF受密码保护，无法解析，本次未调用模型", "invalid_pdf": "PDF格式损坏或无法读取，本次未调用模型",
                "too_large": "财报超过解析上限，请打开原文阅读，本次未调用模型", "timeout": "财报解析超时，原文件已保留，本次未调用模型"}


async def _execute(job, owner):
    from app.services import earnings_analysis, earnings_sources
    uid = job["user_id"]
    if users_store.get_by_id(uid) is None:
        raise EarningsError("账户不存在，财报任务未执行")
    if job["kind"] == "refresh":
        store.heartbeat(uid, job["id"], owner, phase="discovering")
        result = await earnings_sources.discover_reports(job["symbol"])
        state = store.save_discovery(job["symbol"], result)
        if not result.get("items") and state["sources"] and all(source.get("status") not in {"ok", "success", "ready", "empty"} for source in state["sources"]):
            raise EarningsError("财报来源暂不可用，已保留此前记录；请稍后重试")
        return {"result": {"received": len(result.get("items", []))}}
    report, extraction = await _prepare(job, owner)
    if extraction["status"] not in {"ready", "partial"} or not extraction.get("text_pages"):
        raise EarningsError(_TEXT_ERRORS.get(extraction["status"], "财报可用文字不足，请核对原文，本次未调用模型"))
    if job["kind"] == "prepare":
        return {"result": {"document_sha": extraction["sha256"], "extraction_status": extraction["status"]}}
    if report.get("document_kind") != "full_report" or report.get("is_interpretable") is False:
        raise EarningsError("请选择正式财报全文，摘要、预告与更正公告不能代替完整财报")
    cache_key = earnings_analysis.analysis_cache_key(uid, extraction["sha256"], job["focus"])
    cached = store.cached_analysis(uid, report["id"], cache_key)
    if cached:
        return {"result": {"analysis_id": cached["id"], "reused": True}}
    store.heartbeat(uid, job["id"], owner, phase="analyzing")

    def before_dispatch():
        if earnings_analysis.analysis_cache_key(uid, extraction["sha256"], job["focus"]) != cache_key:
            raise EarningsError("模型配置已改变，请使用新配置重新提交解读")
        store.reserve_call(uid, job["id"], owner)

    result = await earnings_analysis.analyze_document(uid, report, extraction, focus=job["focus"], before_dispatch=before_dispatch)
    return {"analysis": {"document_sha": extraction["sha256"], "cache_key": cache_key, "result": result}}


@contextmanager
def worker_lock():
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(settings.data_dir / "earnings-worker.lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise EarningsError("财报后台锁无效")
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

    heartbeat = asyncio.create_task(renew())
    try:
        operation = asyncio.create_task(_execute(job, owner))
        done, _ = await asyncio.wait({operation, heartbeat}, return_when=asyncio.FIRST_COMPLETED)
        if heartbeat in done:
            await asyncio.gather(asyncio.shield(operation), return_exceptions=True)
            heartbeat.result()
        result = await asyncio.shield(operation)
        store.finish(uid, identifier, owner, **result)
    except asyncio.CancelledError:
        # Retain the process lock until downloads, parser and paid HTTP unwind.
        if operation is not None:
            await asyncio.gather(asyncio.shield(operation), return_exceptions=True)
        raise
    except Exception as exc:
        safe = str(exc) if isinstance(exc, (EarningsError, store.DailyLimit, store.EarningsConflict)) else None
        if not safe and type(exc).__module__ in {"app.services.earnings_analysis", "app.services.earnings_sources"}:
            safe = str(exc)
        try:
            store.finish(uid, identifier, owner, error=safe or "财报任务未完成，请稍后重试；已有文件和解读继续保留")
        except store.LeaseLost:
            pass
        logger.warning("财报任务未完成 kind=%s error_type=%s", job["kind"], type(exc).__name__)
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
        current_user_id.reset(context)


async def run_cycle():
    if not settings.enable_earnings:
        return 0
    with worker_lock() as acquired:
        if not acquired:
            return 0
        store.recover_interrupted()
        owner = uuid.uuid4().hex
        job = store.claim(owner)
        if job is None:
            return 0
        await process_job(job, owner)
        return 1


async def worker_loop():
    await asyncio.sleep(3)
    while True:
        try:
            processed = await run_cycle()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("财报后台暂不可用 type=%s", type(exc).__name__)
            processed = 0
        await asyncio.sleep(1 if processed else 5)
