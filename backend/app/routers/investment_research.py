"""Account-owned investment research; the existing /research lab is unchanged."""
import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import Field

from app.config import settings
from app.deps.auth import get_current_user
from app.schemas.investment_research import (
    AdoptBody, DossierCreate, DossierPatch, GenerateBody, MaterialCreate,
    QuestionCreate, QuestionPatch, ReadBody, StrictModel,
)
from app.services import investment_research as service
from app.storage import investment_research_store as store
from app.storage.users_store import UserRecord

router = APIRouter(prefix="/api/investment-research", tags=["investment-research"])
User = Annotated[UserRecord, Depends(get_current_user)]


class MonitoringImport(StrictModel):
    monitor_run_id: str = Field(min_length=1, max_length=80)
    title: str = Field(default="监控变化与证据", min_length=1, max_length=200)


def _call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except store.NotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except store.DailyLimit as exc:
        raise HTTPException(429, str(exc)) from exc
    except store.ResearchConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def _require(value, message):
    if value is None:
        raise HTTPException(404, message)
    return value


def _enabled():
    if not settings.enable_investment_research:
        raise HTTPException(409, "研究后台已关闭，请启用 ENABLE_INVESTMENT_RESEARCH 并重启后端")


@router.get("")
@router.get("/overview")
def overview(user: User, symbol: str | None = Query(default=None, max_length=40)):
    return _call(service.overview, user.id, symbol=symbol)


@router.get("/dossiers")
def dossiers(user: User, symbol: str | None = Query(default=None, max_length=40)):
    from app.schemas.tracking import canonical_symbol
    return {"items": _call(store.list_dossiers, user.id, symbol=_call(canonical_symbol, symbol) if symbol else None)}


@router.post("/dossiers", status_code=201)
def create_dossier(body: DossierCreate, user: User):
    return _call(service.create_dossier, user.id, body.model_dump())


@router.get("/dossiers/{dossier_id}")
def dossier_detail(dossier_id: str, user: User):
    return _call(service.detail, user.id, dossier_id)


@router.patch("/dossiers/{dossier_id}")
def update_dossier(dossier_id: str, body: DossierPatch, user: User):
    return _call(store.update_dossier, user.id, dossier_id, body.model_dump(exclude_unset=True))


@router.post("/dossiers/{dossier_id}/questions", status_code=201)
def create_question(dossier_id: str, body: QuestionCreate, user: User):
    return _call(store.create_question, user.id, dossier_id, body.model_dump())


@router.patch("/questions/{question_id}")
def update_question(question_id: str, body: QuestionPatch, user: User):
    return _call(store.update_question, user.id, question_id, body.model_dump(exclude_unset=True))


@router.delete("/questions/{question_id}")
def delete_question(question_id: str, user: User, revision: int = Query(ge=1)):
    return _call(store.delete_question, user.id, question_id, revision)


@router.post("/dossiers/{dossier_id}/materials", status_code=201)
def create_material(dossier_id: str, body: MaterialCreate, user: User):
    return _call(service.add_material, user.id, dossier_id, body.model_dump())


@router.post("/dossiers/{dossier_id}/monitoring-updates", status_code=201)
def import_monitoring(dossier_id: str, body: MonitoringImport, user: User):
    return _call(service.add_material, user.id, dossier_id, {"kind": "monitoring", **body.model_dump()})


@router.post("/dossiers/{dossier_id}/materials/upload", status_code=201)
async def upload_material(dossier_id: str, user: User, file: UploadFile = File(...), title: str | None = Form(default=None)):
    _require(store.get_dossier(user.id, dossier_id), "研究档案不存在")
    chunks, size = [], 0
    try:
        while chunk := await file.read(256 * 1024):
            size += len(chunk)
            if size > service.MAX_UPLOAD_BYTES:
                raise HTTPException(413, "材料文件不能超过30MiB")
            chunks.append(chunk)
        return await asyncio.to_thread(_call, service.upload_material, user.id, dossier_id,
                                       file.filename, b"".join(chunks), title=title)
    finally:
        await file.close()


@router.get("/materials/{material_id}")
def material_detail(material_id: str, user: User):
    return _require(store.get_material(user.id, material_id), "研究材料不存在")


@router.get("/materials/{material_id}/pdf")
def material_pdf(material_id: str, user: User):
    path = _call(service.pdf_path, user.id, material_id)
    return FileResponse(path, media_type="application/pdf", filename="research-material.pdf",
                        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


@router.post("/materials/{material_id}/prepare", status_code=202)
def prepare_material(material_id: str, user: User):
    _enabled()
    value = _require(store.get_material(user.id, material_id), "研究材料不存在")
    return service.public_job(_call(store.enqueue, user.id, value["dossier_id"], kind="prepare", material_id=material_id))


@router.delete("/materials/{material_id}")
def delete_material(material_id: str, user: User):
    return _call(store.delete_material, user.id, material_id)


@router.post("/dossiers/{dossier_id}/generate", status_code=202)
def generate(dossier_id: str, body: GenerateBody, user: User):
    from app.services import investment_research_engine as engine
    _require(store.get_dossier(user.id, dossier_id), "研究档案不存在")
    _enabled()
    if not engine.model_status(user.id)["configured"]:
        raise HTTPException(409, "请先在设置中配置当前账户的大模型接口，再生成研究报告")
    return service.public_job(_call(store.enqueue, user.id, dossier_id, focus=body.focus))


@router.get("/jobs/{job_id}")
def get_job(job_id: str, user: User):
    return service.public_job(_require(store.get_job(user.id, job_id), "研究任务不存在"))


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, user: User):
    return service.public_job(_call(store.cancel_job, user.id, job_id))


@router.get("/reports/{report_id}")
def report_detail(report_id: str, user: User):
    return _require(store.get_report(user.id, report_id), "研究报告不存在")


@router.post("/reports/{report_id}/adopt")
def adopt_report(report_id: str, body: AdoptBody, user: User):
    return _call(store.adopt_report, user.id, report_id, body.revision)


@router.post("/reports/{report_id}/read")
def read_report(report_id: str, body: ReadBody, user: User):
    return _call(store.mark_report, user.id, report_id, body.is_read)
