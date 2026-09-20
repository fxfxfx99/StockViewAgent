from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.deps.auth import get_current_user, require_admin
from app.services import (
    china_tech_media_feeds,
    cn_headline_source_registry,
    cn_market_news,
    impact_analyzer,
    news_feed_sync,
    news_watchlist_matcher,
    research_sources,
    rss_fetcher,
)
from app.storage import news_store, watchlist_store
from app.storage.users_store import UserRecord

# 新闻抓取和解读可能调用账户凭证；开放模式仍由依赖自动使用本地默认账户。
router = APIRouter(prefix="/api/news", tags=["news"], dependencies=[Depends(get_current_user)])


class AnalyzeBody(BaseModel):
    items: list[dict] = Field(default_factory=list, description="与 GET /feed 单项结构一致")
    max_items: int = Field(default=20, ge=1, le=40)
    persist: bool = Field(default=False, description="将分析结果写入本地库对应 id")


class SyncArchiveBody(BaseModel):
    symbols: list[str] | None = Field(default=None, description="若为空则使用当前股票列表")
    lookback_days: int | None = Field(default=None, ge=1, le=30)


class AnalyzeSymbolArchiveBody(BaseModel):
    symbol: str = Field(..., min_length=4, max_length=24)
    limit: int = Field(default=20, ge=1, le=40)
    persist: bool = Field(default=True, description="写回本地库逐股票解读")
    lookback_days: int | None = Field(default=30, ge=1, le=30)


class CustomRssSource(BaseModel):
    id: str = Field(..., min_length=1, max_length=80)
    label: str = Field(..., min_length=1, max_length=120)
    url: str = Field(..., min_length=8, max_length=1000)
    referer: str = Field(default="", max_length=1000)
    priority: int = Field(default=80, ge=1, le=999)


class NewsSourceConfigBody(BaseModel):
    rss: list[CustomRssSource] = Field(default_factory=list, max_length=50)


@router.get("/feed")
async def news_feed(
    user: Annotated[UserRecord, Depends(get_current_user)],
    symbols: str | None = Query(default=None, description="逗号分隔代码；空则用股票列表"),
    save: bool = Query(default=True, description="是否写入本地 SQLite 新闻库"),
):
    wl = watchlist_store.load_symbols(user.id)
    if symbols and symbols.strip():
        sym_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    else:
        sym_list = wl
    if not sym_list:
        sym_list = ["600519.SS"]
    items = await rss_fetcher.fetch_news_for_symbols(sym_list)
    wl_hints, name_hints = news_watchlist_matcher.load_watchlist_match_hints()
    display_items = (
        news_watchlist_matcher.enrich_items_with_matches(items, wl_hints, name_hints) if items else []
    )
    saved = 0
    if save and display_items:
        saved = news_store.upsert_many(display_items)
    st = news_store.stats()
    return {
        "watchlist": wl,
        "symbols_queried": sym_list,
        "items": display_items,
        "saved_to_archive": saved,
        "archive": st,
    }


@router.get("/archive")
def news_archive(
    limit: int = Query(default=40, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    symbol: str | None = Query(default=None, description="按标的筛选相关解读或待解读候选"),
    sort: str = Query(default="importance", pattern="^(importance|latest)$"),
    scope: str = Query(default="related", pattern="^(related|analysis|pending)$"),
    lookback_days: int | None = Query(default=None, ge=1, le=30),
):
    options = {"lookback_days": lookback_days} if lookback_days is not None else {}
    rows, total = news_store.list_archive(
        limit=limit, offset=offset, symbol=symbol, sort=sort, scope=scope, **options
    )
    result = {"total": total, "limit": limit, "offset": offset, "scope": scope, "items": rows}
    if lookback_days is not None:
        result["lookback_days"] = lookback_days
    return result


@router.get("/summary")
def news_summary(user: Annotated[UserRecord, Depends(get_current_user)]):
    return {"items": news_store.summary_for_symbols(watchlist_store.load_symbols(user.id)), "archive": news_store.stats()}


@router.get("/status")
def news_status():
    return {
        "running": False,
        "phase": "idle",
        "scheduler_enabled": False,
        "message": "新闻解读在本页按需触发；抓取与分析共用后端端口，无独立数据源进程。",
    }


@router.post("/sync-archive-feeds")
async def sync_archive_feeds(
    user: Annotated[UserRecord, Depends(get_current_user)],
    body: SyncArchiveBody | None = None,
):
    """合并拉取 RSS + A 股快讯 + 36氪/虎嗅，写入 SQLite，并更新 matched_symbols。"""
    syms = body.symbols if body and body.symbols else watchlist_store.load_symbols(user.id)
    lookback = body.lookback_days if body else None
    return await news_feed_sync.sync_all_sources_to_archive(syms, lookback_days=lookback)


@router.post("/analyze-symbol-archive")
def analyze_symbol_archive(
    body: AnalyzeSymbolArchiveBody,
    user: Annotated[UserRecord, Depends(get_current_user)],
):
    """对某列表内标的：近 30 天已匹配、尚未完成解读的条目批量分析并可选写回。"""
    symbol = body.symbol.strip().upper()
    wl = watchlist_store.load_symbols(user.id)
    if symbol not in wl:
        return {"ok": False, "detail": "该代码不在股票列表中", "analyzed": 0, "results": []}
    pending = news_store.list_pending([symbol], limit=body.limit, max_age_days=body.lookback_days)
    if not pending:
        return {
            "ok": True,
            "symbol": symbol,
            "analyzed": 0,
            "message": "最近关联新闻均已解读或暂无可分析条目",
            "results": [],
        }
    results = []
    analyzed = 0
    for item in pending:
        codes = item.get("analysis_symbols") or [symbol]
        analyses = impact_analyzer.analyze_batch([item], codes, max_age_days=body.lookback_days)
        an = analyses[0] if analyses else {}
        if body.persist and item.get("id"):
            news_store.save_analysis(str(item["id"]), an)
        related = an.get("related_stocks") if isinstance(an, dict) else None
        display = None
        if isinstance(related, list):
            display = next((row for row in related if row.get("stock_code") == symbol), related[0] if related else None)
        results.append({**item, "analysis": display or an})
        analyzed += 1
    return {
        "ok": True,
        "symbol": symbol,
        "analyzed": analyzed,
        "persisted": body.persist,
        "results": results,
    }


@router.post("/analyze")
def analyze_news(body: AnalyzeBody, user: Annotated[UserRecord, Depends(get_current_user)]):
    wl = watchlist_store.load_symbols(user.id)
    items = body.items[: body.max_items]
    if not items:
        return {"watchlist": wl, "results": []}
    analyses = impact_analyzer.analyze_batch(items, wl)
    results = []
    for it, an in zip(items, analyses, strict=True):
        row = {**it, "analysis": an}
        results.append(row)
        if body.persist and it.get("id"):
            news_store.save_analysis(str(it["id"]), an)
    return {"watchlist": wl, "results": results, "persisted": body.persist}


@router.get("/stats")
def news_stats():
    return news_store.stats()


@router.get("/cn-headlines")
async def cn_headlines(
    limit: int = Query(default=40, ge=10, le=120, description="返回条数"),
    major_first: bool = Query(default=True, description="重磅/政策类关键词优先排序"),
):
    """A 股相关：东财多栏目 + 多 RSS 备用；每日自动探测源可用性（见 /cn-headlines/sources）。"""
    return await cn_market_news.fetch_cn_headlines(limit=limit, major_first=major_first)


@router.get("/cn-headlines/sources")
def cn_headlines_sources():
    """内置源清单、探测状态与覆盖配置文件路径（站点变更时改 override JSON）。"""
    return {
        "builtin_sources": cn_headline_source_registry.builtin_source_rows(),
        "maintenance_ledger": cn_headline_source_registry.maintenance_ledger(),
        "daily_check_timezone": "Asia/Shanghai",
        "override_template_note": "首次请求快讯时若不存在会生成 data/cn_headline_sources_override.json 模板，可增删 RSS。",
    }


@router.get("/source-config")
def news_source_config(_: Annotated[UserRecord, Depends(get_current_user)]):
    """控制台使用：查看可扩展 RSS 配置及内置源。"""
    return {
        "custom_rss": cn_headline_source_registry.load_custom_rss_sources(),
        "builtin_sources": cn_headline_source_registry.builtin_source_rows(),
        "storage": "backend/data/cn_headline_sources_override.json",
        "hint": "新增 RSS 会自动进入新闻快讯抓取、归档同步与每日健康探测。",
    }


@router.put("/source-config")
def update_news_source_config(
    body: NewsSourceConfigBody,
    _: Annotated[UserRecord, Depends(require_admin)],
):
    """管理员维护附加 RSS；无需改代码即可扩展新闻来源。"""
    try:
        saved = cn_headline_source_registry.save_custom_rss_sources(
            [row.model_dump() for row in body.rss]
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "custom_rss": saved}


@router.get("/feeds-health")
async def feeds_health():
    """自检：各外部源是否拉到数据；说明爬虫与 SQLite 均在本进程，无独立「数据源端口」。"""
    cn = await cn_market_news.fetch_cn_headlines(limit=12, major_first=True)
    tm = await china_tech_media_feeds.fetch_tech_media_feeds(limit=12, per_feed=20)
    return {
        "architecture": (
            "爬虫与 SQLite 均在 FastAPI 进程内；开发默认后端 8001、前端 5175。"
            "端口冲突时设置 BACKEND_PORT / FRONTEND_PORT。"
        ),
        "cn_headlines": {
            "items_returned": len(cn.get("items") or []),
            "fetch_meta": cn.get("fetch_meta"),
            "warnings": cn.get("warnings"),
        },
        "tech_media": {
            "items_returned": len(tm.get("items") or []),
            "stored_count": tm.get("stored_count"),
            "fetch_meta": tm.get("fetch_meta"),
            "warnings": tm.get("warnings"),
        },
    }


@router.get("/research-sources")
def research_sources_catalog():
    """PDF/Excel 整理的行研与数据入口；含 36氪/虎嗅 RSS 与研究院链接说明。"""
    return research_sources.load_catalog()


@router.get("/tech-media")
async def tech_media_feed(
    limit: int = Query(default=100, ge=1, le=120, description="返回条数（本地最多保留 100 条）"),
    per_feed: int = Query(default=40, ge=8, le=55, description="每个 RSS 单次拉取条数（用于合并入库）"),
):
    """36氪 + 虎嗅 RSS：写入本地表 tech_media_items，仅保留最近 100 条；返回含报道时间与原文链接。"""
    return await china_tech_media_feeds.fetch_tech_media_feeds(limit=limit, per_feed=per_feed)
