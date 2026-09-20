import asyncio

import time

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from app.deps.auth import get_current_user
from app.services import a_share_stocks, company_profile_em, issuer_basic_info_universe, issuer_rag_service as irag
from app.services import tushare_service
from app.services import news_watchlist_matcher as nwm
from app.services import watchlist_import
from app.services import watchlist_import_excel
from app.services import watchlist_price_context as price_ctx
from app.services import watchlist_stock_detail as stock_detail
from app.services import issuer_uploads_service as ius
from app.storage import news_store, profile_store, universe_company_store, watchlist_store
from app.storage.users_store import UserRecord

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])


class WatchlistBody(BaseModel):
    symbols: list[str] = Field(default_factory=list, description="股票代码列表，大写")


class ProfilesRefreshBody(BaseModel):
    symbols: list[str] | None = Field(
        default=None,
        description="仅刷新这些代码；默认当前股票列表全部",
    )


def _issuer_uploads_public(sym: str) -> list[dict]:
    out = []
    for x in ius.list_documents(symbol=sym):
        out.append(
            {
                "id": x.get("id"),
                "category": x.get("category"),
                "category_label": x.get("category_label"),
                "original_name": x.get("original_name"),
                "summary": x.get("summary"),
                "size": x.get("size"),
                "created_at": x.get("created_at"),
                "rag_indexed": bool(x.get("rag_indexed")),
                "rag_chunks": int(x.get("rag_chunks") or 0),
                "rag_lexical_only": bool(x.get("rag_lexical_only")),
                "rag_embedding_error": x.get("rag_embedding_error"),
            }
        )
    return out


class ProfileManualBody(BaseModel):
    symbol: str = Field(..., min_length=4, max_length=24)
    name: str | None = None
    org_name: str | None = None
    main_business: str | None = None
    industry: str | None = None
    intro: str | None = None


def _background_tushare_fetch(symbols: list[str]) -> None:
    """股票列表新增标的后：拉取 Tushare stock_basic + daily。"""
    for sym in symbols:
        payload, err = tushare_service.fetch_and_pack_for_yahoo_symbol(sym)
        if payload:
            profile_store.upsert_tushare(sym, payload, err)
        else:
            profile_store.upsert_tushare(sym, {}, err)
        time.sleep(0.15)


async def _refresh_f10_only(ordered: list[str]) -> dict[str, str]:
    """仅拉东方财富 F10 并写入 profile_store（不向量化上传文件）。"""
    errors: dict[str, str] = {}
    for sym in ordered:
        try:
            auto, err = await asyncio.to_thread(company_profile_em.fetch_company_survey, sym)
            lk = a_share_stocks.lookup_by_yahoo_symbol(sym)
            if lk and not (auto.get("name") or "").strip():
                auto["name"] = (lk.get("name") or "").strip()
            profile_store.upsert_auto(sym, auto, err)
            if err:
                errors[sym] = err
        except Exception as e:  # noqa: BLE001
            errors[sym] = f"异常：{e!s}"
        await asyncio.sleep(0.12)
    return errors


def _profile_view(sym: str) -> dict:
    sym = sym.strip().upper()
    entry = profile_store.get_entry(sym)
    merged = profile_store.merge_display(entry)
    lk = a_share_stocks.lookup_by_yahoo_symbol(sym)
    if not merged.get("name") and lk:
        merged["name"] = (lk.get("name") or "").strip()
    merged = universe_company_store.merge_into_display(merged, sym)
    tu = entry.get("tushare") or {}
    tb = tu.get("basic") if isinstance(tu.get("basic"), dict) else {}
    if not merged.get("name") and tb.get("name"):
        merged["name"] = str(tb.get("name") or "").strip()
    if not merged.get("industry") and tb.get("industry"):
        merged["industry"] = str(tb.get("industry") or "").strip()
    manual = entry.get("manual") or {}
    ae = entry.get("auto") or {}
    has_f10 = bool(
        (str(ae.get("org_name") or "")).strip()
        or (str(ae.get("intro") or "")).strip()
        or (str(ae.get("main_business") or "")).strip()
    )
    uni = universe_company_store.get_symbol(sym)
    _basic_keys = ("org_name", "main_business", "industry", "intro")
    filled_from_universe = bool(
        uni
        and not uni.get("fetch_error")
        and not has_f10
        and any(
            (merged.get(k) or "").strip() and not (str(ae.get(k) or "")).strip() for k in _basic_keys
        )
    )
    return {
        "symbol": sym,
        "name": merged.get("name") or "",
        "org_name": merged.get("org_name") or "",
        "main_business": merged.get("main_business") or "",
        "industry": merged.get("industry") or "",
        "intro": merged.get("intro") or "",
        "recent_news": news_store.list_recent_for_symbol(sym, 20),
        "fetch_error": entry.get("fetch_error"),
        "em_fetched_at": int(entry.get("em_fetched_at") or 0),
        "manual_fields": [k for k, v in manual.items() if isinstance(v, str) and v.strip()],
        "company_source": "eastmoney_f10" if has_f10 else ("universe_basic_cache" if filled_from_universe else None),
        "news_source": "local_archive",
        "issuer_uploads": _issuer_uploads_public(sym),
    }


@router.get("/suggest")
async def watchlist_suggest(
    q: str = Query(default="", min_length=1, description="代码或简称，供股票列表搜索补全"),
    limit: int = Query(default=15, ge=1, le=50),
):
    """与 GET /api/stocks/search 相同数据源；语义上强调「补全股票列表标的」。"""
    await asyncio.to_thread(issuer_basic_info_universe.ensure_cached_blocking)
    items = await asyncio.to_thread(a_share_stocks.search_stocks, q, limit)
    return {"items": items}


class ParseImportBody(BaseModel):
    text: str = Field(default="", description="粘贴文本或 CSV 片段")
    merge: bool = Field(default=True, description="true 时与现有股票列表合并去重")


def _save_imported_symbols(
    symbols: list[str],
    details: list[dict],
    background_tasks: BackgroundTasks,
    user_id: int,
):
    """文本和 Excel 导入共用合并、重匹配及资料回填流程。"""
    cur = watchlist_store.load_symbols(user_id)
    previous = set(cur)
    saved = watchlist_store.save_symbols(user_id, [*cur, *symbols])
    added = [symbol for symbol in saved if symbol not in previous]
    nwm.invalidate_match_hints_cache()
    news_store.rematch_all_matched_symbols()
    queued = added if added and tushare_service.is_configured() else []
    if queued:
        background_tasks.add_task(_background_tushare_fetch, added)
    return {
        "symbols": saved,
        "parsed": symbols,
        "details": details,
        "added": added,
        "tushare_backfill_queued": queued,
    }


@router.post("/parse-import")
def parse_watchlist_import(
    body: ParseImportBody,
    background_tasks: BackgroundTasks,
    user: Annotated[UserRecord, Depends(get_current_user)],
):
    """从文本或 CSV 解析 A 股代码，可选合并写入股票列表。"""
    symbols, details = watchlist_import.parse_text_to_symbols(body.text)
    if not body.merge:
        return {"symbols": symbols, "details": details, "saved": None}
    return _save_imported_symbols(symbols, details, background_tasks, user.id)


@router.post("/parse-import-xlsx")
async def parse_watchlist_import_xlsx(
    background_tasks: BackgroundTasks,
    user: Annotated[UserRecord, Depends(get_current_user)],
    file: UploadFile = File(..., description="xlsx，≤2MB；列名含 代码/stock_code 等优先"),
    merge: bool = Form(True),
):
    """从 Excel 解析 A 股代码并可选合并（借鉴 daily import_parser 列别名）。"""
    raw = await file.read()
    try:
        symbols, details = await asyncio.to_thread(watchlist_import_excel.parse_excel_bytes, raw)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"解析失败: {e!s}") from e
    if not merge:
        return {"symbols": symbols, "details": details, "saved": None}
    return _save_imported_symbols(symbols, details, background_tasks, user.id)


@router.get("")
def get_watchlist(user: Annotated[UserRecord, Depends(get_current_user)]):
    return {"symbols": watchlist_store.load_symbols(user.id)}


@router.put("")
def put_watchlist(
    body: WatchlistBody,
    background_tasks: BackgroundTasks,
    user: Annotated[UserRecord, Depends(get_current_user)],
):
    old = set(watchlist_store.load_symbols(user.id))
    saved = watchlist_store.save_symbols(user.id, body.symbols)
    added = [s for s in saved if s not in old]
    nwm.invalidate_match_hints_cache()
    rematched = news_store.rematch_all_matched_symbols()
    if added and tushare_service.is_configured():
        background_tasks.add_task(_background_tushare_fetch, added)
    return {
        "symbols": saved,
        "articles_rematched": rematched,
        "tushare_backfill_queued": added if added and tushare_service.is_configured() else [],
    }


@router.get("/profiles")
def get_watchlist_profiles(user: Annotated[UserRecord, Depends(get_current_user)]):
    symbols = watchlist_store.load_symbols(user.id)
    return {"profiles": {s: _profile_view(s) for s in symbols}}


@router.get("/price-context")
async def get_watchlist_price_context(user: Annotated[UserRecord, Depends(get_current_user)]):
    """实时刷新各列表标的行情；主源失败时自动走统一备用源链。"""
    wl = watchlist_store.load_symbols(user.id)
    ctx = await price_ctx.price_context_for_symbols(wl)
    success_count = sum(1 for item in ctx.values() if not item.get("error"))
    return {
        "symbols": wl,
        "context": ctx,
        "success_count": success_count,
        "error_count": len(ctx) - success_count,
    }


@router.get("/stock-detail")
async def get_watchlist_stock_detail(
    user: Annotated[UserRecord, Depends(get_current_user)],
    symbol: str = Query(..., description="如 600519.SS"),
):
    """单标的展开详情：扩展行情快照、所属板块、本地公司简介、近 20 日 K 线数据（懒加载用）。"""
    sym = symbol.strip().upper()
    wl = watchlist_store.load_symbols(user.id)
    if sym not in wl:
        raise HTTPException(status_code=404, detail="该代码不在股票列表中")
    return await stock_detail.build_stock_detail(sym)


@router.post("/profiles/quick-refresh")
async def quick_refresh_watchlist_profiles(
    user: Annotated[UserRecord, Depends(get_current_user)],
    body: ProfilesRefreshBody | None = None,
):
    """新加标的等场景：只同步 F10 公司资料（秒级～数十秒），不跑本地上传向量化。"""
    wl = watchlist_store.load_symbols(user.id)
    targets = body.symbols if body and body.symbols else wl
    seen: set[str] = set()
    ordered: list[str] = []
    for s in targets:
        t = s.strip().upper()
        if t in wl and t not in seen:
            seen.add(t)
            ordered.append(t)
    errors = await _refresh_f10_only(ordered)
    return {
        "profiles": {s: _profile_view(s) for s in wl},
        "refreshed": ordered,
        "errors": errors,
    }


@router.post("/profiles/refresh")
async def refresh_watchlist_profiles(
    user: Annotated[UserRecord, Depends(get_current_user)],
    body: ProfilesRefreshBody | None = None,
):
    wl = watchlist_store.load_symbols(user.id)
    targets = body.symbols if body and body.symbols else wl
    seen: set[str] = set()
    ordered: list[str] = []
    for s in targets:
        t = s.strip().upper()
        if t in wl and t not in seen:
            seen.add(t)
            ordered.append(t)
    errors = await _refresh_f10_only(ordered)
    # 向量化可能较慢，放线程池避免阻塞事件循环；前端已延长超时与代理时间
    issuer_uploads_sync = await asyncio.to_thread(irag.reindex_all_for_symbols, ordered)
    return {
        "profiles": {s: _profile_view(s) for s in wl},
        "refreshed": ordered,
        "errors": errors,
        "issuer_uploads_sync": issuer_uploads_sync,
    }


@router.patch("/profiles/manual")
def patch_watchlist_profile_manual(
    user: Annotated[UserRecord, Depends(get_current_user)],
    body: ProfileManualBody,
):
    wl = watchlist_store.load_symbols(user.id)
    sym = body.symbol.strip().upper()
    if sym not in wl:
        return {"ok": False, "detail": "该代码不在股票列表中"}
    raw = body.model_dump(exclude_unset=True)
    raw.pop("symbol", None)
    profile_store.patch_manual(sym, raw)
    return {"ok": True, "profile": _profile_view(sym)}
