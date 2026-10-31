"""Financial reports are public; model interpretations and jobs are account-owned."""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from app.config import settings
from app.deps.auth import get_current_user
from app.services import earnings as service
from app.storage import earnings_store as store
from app.storage.users_store import UserRecord

router = APIRouter(prefix="/api/news/earnings", tags=["news"])
User = Annotated[UserRecord, Depends(get_current_user)]


class RefreshBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(min_length=1, max_length=24)


class AnalyzeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    focus: str = Field(default="", max_length=2000)


def _call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except store.DailyLimit as exc:
        raise HTTPException(429, str(exc)) from exc
    except store.EarningsConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def _report(identifier):
    report = store.get_report(identifier)
    if report is None:
        raise HTTPException(404, "未找到该财报，请先查找当前股票的财报")
    return report


def _worker():
    if not settings.enable_earnings:
        raise HTTPException(409, "财报处理后台已关闭，请启用 ENABLE_EARNINGS 并重启后端")


@router.get("")
def overview(user: User, symbol: str = Query(min_length=1, max_length=24)):
    return _call(service.overview, user.id, symbol)


@router.post("/refresh", status_code=202)
def refresh(body: RefreshBody, user: User):
    _worker()
    symbol = _call(service.normalize_symbol, body.symbol)
    return service.public_job(_call(store.enqueue, user.id, symbol, "refresh"))


@router.get("/{report_id}")
def detail(report_id: str, user: User):
    _report(report_id)
    return service.report_detail(user.id, report_id)


@router.post("/{report_id}/prepare", status_code=202)
def prepare(report_id: str, user: User):
    report = _report(report_id)
    _worker()
    return service.public_job(_call(store.enqueue, user.id, report["symbol"], "prepare", report_id=report_id))


@router.post("/{report_id}/analyze", status_code=202)
def analyze(report_id: str, body: AnalyzeBody, user: User):
    from app.services import earnings_analysis
    report = _report(report_id)
    _worker()
    if report.get("document_kind") != "full_report" or report.get("is_interpretable") is False:
        raise HTTPException(409, "请选择正式财报全文，摘要或业绩预告不能替代财报")
    if not earnings_analysis.get_model_status(user.id)["configured"]:
        raise HTTPException(409, "请先在设置中配置当前账户的大模型接口，再解读财报")
    return service.public_job(_call(store.enqueue, user.id, report["symbol"], "analyze", report_id=report_id, focus=body.focus))


@router.get("/{report_id}/pdf")
def download(report_id: str, user: User):
    report = _report(report_id)
    path = _call(service.pdf_path, report_id)
    if path is None:
        raise HTTPException(404, "财报原文件尚未下载，请先下载解析")
    return FileResponse(path, media_type="application/pdf", filename=f"{report['symbol']}-{report.get('report_period') or report_id}.pdf",
                        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})
