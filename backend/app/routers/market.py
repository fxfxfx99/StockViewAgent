import asyncio
import csv
import io
import re
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from app.services import (
    kline_pipeline,
    kline_sources_registry,
    market_data_resilience,
    market_extra_http,
    market_fund_flow_service,
    market_source_policy,
    mytt_compute,
)
from app.storage import kline_bundle_cache, market_history_cache

router = APIRouter(prefix="/api/market", tags=["market"])

_A_SHARE_ONLY = re.compile(r"^\d{6}\.(SS|SH|SZ|BJ)$", re.I)

_RANGES = frozenset({"1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max"})
_INTERVALS = frozenset({"1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h", "1d", "5d", "1wk", "1mo"})


@router.get("/kline/{symbol}")
async def get_kline(
    symbol: str,
    range_param: str = Query(
        default="1y",
        alias="range",
        description="东方财富 K 线区间：1d,5d,1mo,3mo,6mo,1y,2y,5y,10y,ytd,max（仅 A 股）",
    ),
    interval: str = Query(
        default="1d",
        description="K 线周期：1m…1h, 1d, 1wk, 1mo；失败时按链兜底：日类→腾讯→Baostock→新浪→Pytdx；分钟→腾讯mkline→Baostock(5/15/30/60)→Pytdx",
    ),
):
    sym = symbol.strip().upper()
    if not _A_SHARE_ONLY.match(sym):
        raise HTTPException(
            status_code=400,
            detail="仅支持 A 股：6 位代码 + .SS/.SH/.SZ/.BJ（如 600519.SS）；主源东方财富，失败时腾讯/新浪链式兜底",
        )
    if range_param not in _RANGES:
        raise HTTPException(status_code=400, detail=f"range 必须是 {_RANGES} 之一")
    if interval not in _INTERVALS:
        raise HTTPException(status_code=400, detail=f"interval 必须是 {_INTERVALS} 之一")

    try:
        bundle = await kline_pipeline.fetch_a_share_kline_with_fallbacks(sym, range_param, interval)
        candles = bundle.get("candles") or []
        bundle["data_as_of"] = kline_bundle_cache.last_bar_date_str(candles)
        bundle["served_from_cache"] = False
        bundle["cache_stale"] = False
        try:
            kline_bundle_cache.save_bundle(sym, range_param, interval, bundle)
        except OSError:
            pass
        return bundle
    except ValueError as e:
        err_s = str(e)
        ent = kline_bundle_cache.load_bundle(sym, range_param, interval)
        if ent:
            return kline_bundle_cache.merge_stale_response(ent, fetch_error=err_s)
        raise HTTPException(status_code=404, detail=err_s) from e
    except RuntimeError as e:
        err_s = str(e)
        ent = kline_bundle_cache.load_bundle(sym, range_param, interval)
        if ent:
            return kline_bundle_cache.merge_stale_response(ent, fetch_error=err_s)
        raise HTTPException(status_code=502, detail=err_s) from e


@router.get("/mytt/catalog")
def mytt_catalog():
    """MyTT 指标 id 清单（与 GET /api/market/mytt/{symbol} 的 series 参数对应）。"""
    return {
        "items": mytt_compute.MYTT_CATALOG,
        "max_series_per_request": mytt_compute.MYTT_MAX_SERIES_PER_REQUEST,
        "source": "https://github.com/mpquant/MyTT",
    }


@router.get("/mytt/{symbol}")
async def mytt_for_symbol(
    symbol: str,
    range_param: str = Query(
        default="1y",
        alias="range",
        description="与 /kline 相同 range",
    ),
    interval: str = Query(default="1d", description="与 /kline 相同 interval"),
    series: str = Query(
        default="ma5,macd,boll",
        description="逗号分隔指标 id，见 /api/market/mytt/catalog",
    ),
):
    """先走与 /kline 相同的东财→腾讯→新浪/分钟 mkline 链路，再对 OHLCV 计算 MyTT 指标序列。"""
    sym = symbol.strip().upper()
    if not _A_SHARE_ONLY.match(sym):
        raise HTTPException(status_code=400, detail="仅支持 A 股代码格式")
    if range_param not in _RANGES:
        raise HTTPException(status_code=400, detail=f"range 必须是 {_RANGES} 之一")
    if interval not in _INTERVALS:
        raise HTTPException(status_code=400, detail=f"interval 必须是 {_INTERVALS} 之一")

    names = mytt_compute.parse_series_param(series)
    if not names:
        raise HTTPException(status_code=400, detail="请至少指定一个有效 series，如 ma5,macd")

    try:
        bundle = await kline_pipeline.fetch_a_share_kline_with_fallbacks(sym, range_param, interval)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    candles = bundle.get("candles") or []
    computed, unknown = mytt_compute.compute_series(candles, names)
    return {
        "symbol": bundle.get("symbol"),
        "range": bundle.get("range"),
        "interval": bundle.get("interval"),
        "interval_effective": bundle.get("interval_effective"),
        "kline_source": bundle.get("kline_source"),
        "tencent_mkline_slot": bundle.get("tencent_mkline_slot"),
        "bar_count": len(candles),
        "unknown_series": unknown or None,
        "series": computed,
    }


@router.get("/sources")
def market_sources():
    """文档用：说明数据来自哪些公开 URL / SDK。"""
    return {
        "kline_registry": kline_sources_registry.KLINE_REGISTRY,
        "kline_docs_file": "backend/docs/KLINE_DATA_SOURCES.md",
        "kline_ohlcv": "https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.600519&klt=101&…",
        "quote_snapshot": "https://push2.eastmoney.com/api/qt/stock/get?secid=1.600519&fields=f43,f116,f162,…",
        "kline_fallback_tencent": "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get（腾讯前复权日/周/月）",
        "kline_fallback_baidu": "https://finance.pae.baidu.com/selfselect/getstockquotation（百度股市通 quotation_kline_ab，adata 同源思路）",
        "kline_fallback_free_stockdb": "http://127.0.0.1:7899/?cmd=get&t=日k:600519:20260701<20260715（可选 free-stockdb 本地 HTTP，需单独启动 Windows 服务）",
        "kline_fallback_sina": "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData（新浪日/周/月）",
        "kline_fallback_tencent_minute": "https://ifzq.gtimg.cn/appstock/app/kline/mkline（腾讯分钟 m1/m5/m15/m30/m60）",
        "kline_fallback_baostock": "http://www.baostock.com — pip 包 baostock，query_history_k_data_plus（日/周/月 + 5/15/30/60 分钟）",
        "kline_fallback_pytdx": "https://pytdx-docs.readthedocs.io/zh-cn/latest/ — pip 包 pytdx，连接公网行情前置（北交所部分代码可能不可用）",
        "mytt_indicators": "GET /api/market/mytt/{symbol}?series=ma5,macd,boll — MyTT 指标序列（github.com/mpquant/MyTT）",
        "sentiment_extras": "东财 datacenter-web / push2、同花顺 dq.10jqka.com.cn 公开接口，见 /api/market/sentiment/*",
        "market_fund_flow_dashboard": "GET /api/market/fund-flow/dashboard — 行业单日/五日/二十日/趋势逆转 + 机构资金代理指标；主源东财板块资金流，备用本地缓存/北向/两融/同花顺热度。",
        "xueqiu_bundle": "GET /api/xueqiu/bundle?symbol=600519.SS — 可选雪球补充（讨论/摘要等），非主 K 线；需 Cookie；见 backend/docs/XUEQIU_OPTIONAL.md 与 xueqiu_http 限频",
        "notes": [
            "A 股 K 线统一经 kline_pipeline：默认日/周/月链为 东财→腾讯→百度→free-stockdb本地HTTP(可选)→Baostock→新浪→Pytdx；默认分钟链为 东财→腾讯mkline→Baostock(仅5/15/30/60)→Pytdx。",
            "free-stockdb / Baostock / Pytdx 为可选依赖，未启用、未启动或未安装时自动跳过对应环节。",
            "股票列表刷新默认 东财→百度→新浪；某一公开源断连时拒绝覆盖本地旧文件，继续使用现有缓存。",
            "分钟线：腾讯 ifzq mkline（2m→m1 槽，90m/1h→m60）；东财/腾讯/pytdx 成交量多按手×100 转股；新浪日线 volume 实测为股数；百度成交量沿用股市通返回口径。",
            "MyTT 技术分析：GET /api/market/mytt/catalog 与 /api/market/mytt/{symbol}。",
            "总市值、流通市值、市盈率(动)、换手率等来自 stock/get。",
            "标的格式：6 位代码 + .SS / .SZ / .BJ。",
            "可选 HTTP 代理：ADATA_PROXY_* 或 integrations.json adata_proxy_*（与历史配置键兼容）。",
            "可选数据源顺序：MARKET_KLINE_DAILY_CHAIN、MARKET_KLINE_MINUTE_CHAIN、MARKET_STOCK_UNIVERSE_CHAIN，或 integrations.json 对应小写键。",
        ],
        "source_policy": market_source_policy.snapshot(),
    }


@router.get("/source-health")
def market_source_health():
    """最近一次行情相关外部数据源健康状态；失败时服务会优先使用备用源或本地缓存。"""
    return {
        "items": market_data_resilience.market_source_health(),
        "source_policy": market_source_policy.snapshot(),
        "fallback_policy": [
            "K 线：默认日/周/月为 东方财富 → 腾讯 → 百度 → free-stockdb本地HTTP(可选) → Baostock → 新浪 → Pytdx；备用 K 线成功后再补东财快照字段。",
            "K 线：默认分钟为 东方财富 → 腾讯 mkline → Baostock → Pytdx。",
            "股票列表：东方财富 clist → 百度股市通 marketrank → 新浪 hs_a；返回数量异常时拒绝覆盖本地缓存。",
            "行情快照：东方财富 push2；失败时使用最近成功缓存补齐市值、PE、涨跌停、量比等字段。",
            "所属板块：东方财富 slist；失败时使用最近成功缓存。",
            "资金流：东方财富 fflow；失败时使用后端持久化历史缓存，无缓存时返回空集而非 502。",
            "市场资金流向：行业板块主源东方财富板块资金流；20日缺少直连字段时使用缓存/10日代理并标注；机构分类仅使用公开代理口径和置信度。",
        ],
    }


@router.get("/fund-flow/dashboard")
async def market_fund_flow_dashboard(limit: int = Query(30, ge=10, le=100)):
    """市场资金流：行业多周期对比、趋势逆转与机构类型代理指标。"""
    return await market_fund_flow_service.get_market_fund_flow_dashboard(limit=limit)


@router.get("/sentiment/north-flow")
async def sentiment_north_flow(
    mode: str = Query("history", description="history=历史日度; current=最新分时汇总"),
    start_date: str | None = Query(None, description="history 时可选 YYYY-MM-DD 起始"),
):
    try:
        if mode == "current":
            rows = await market_extra_http.fetch_north_flow_current()
            return {"mode": "current", "items": rows}
        rows = await market_extra_http.fetch_north_flow_history(start_date=start_date)
        cached = market_history_cache.merge_save_series("north_flow", "history", rows)
        return {"mode": "history", "items": cached["items"], "start_date": start_date, "cache_saved_at": cached["saved_at"], "served_from_cache": False}
    except Exception as e:
        cached = market_history_cache.load_series("north_flow", "history") if mode == "history" else None
        if cached:
            return {"mode": mode, "items": cached["items"], "start_date": start_date, "cache_saved_at": cached["saved_at"], "served_from_cache": True, "cache_warning": str(e)[:500]}
        raise HTTPException(status_code=502, detail=f"北向资金接口异常: {e!s}") from e


@router.get("/history/export")
def export_market_history(symbol: str = Query("600519.SS")):
    """从后端持久化缓存导出北向、两融和指定股票资金流历史（长表 CSV）。"""
    sym = symbol.strip().upper()
    if not _A_SHARE_ONLY.match(sym):
        raise HTTPException(status_code=400, detail="仅支持 A 股代码格式")
    datasets = [
        ("north_flow", "", market_history_cache.load_series("north_flow", "history"), ("net_hgt", "net_sgt", "net_tgt")),
        ("margin", "", market_history_cache.load_series("margin", "market"), ("rzye", "rqye", "rzrqye")),
        ("stock_capital_flow", sym, market_history_cache.load_series("stock_capital_flow", sym), ("main_net_inflow", "lg_net_inflow", "max_net_inflow")),
    ]
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(("dataset", "symbol", "trade_date", "metric", "value"))
    for name, dataset_symbol, cached, metrics in datasets:
        for row in (cached or {}).get("items") or []:
            for metric in metrics:
                if row.get(metric) is not None:
                    writer.writerow((name, dataset_symbol, row.get("trade_date") or "", metric, row[metric]))
    filename = f"market-history-{sym}-{datetime.now().date().isoformat()}.csv"
    return Response(
        content="\ufeff" + output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/sentiment/hot")
async def sentiment_hot():
    """东财人气 100 + 同花顺热股 100 + 同花顺热门概念 20（并行）。"""
    errors: dict[str, str] = {}

    async def _safe(name: str, coro):
        try:
            return await coro
        except Exception as e:
            errors[name] = str(e)
            return []

    east_pop, ths_hot, ths_concept = await asyncio.gather(
        _safe("east_pop_100", market_extra_http.fetch_hot_pop_east()),
        _safe("ths_hot_100", market_extra_http.fetch_hot_rank_ths()),
        _safe("ths_hot_concept_20", market_extra_http.fetch_hot_concept_ths()),
    )
    return {
        "east_pop_100": east_pop,
        "ths_hot_100": ths_hot,
        "ths_hot_concept_20": ths_concept,
        "errors": errors or None,
    }


@router.get("/sentiment/margin")
async def sentiment_margin(
    start_date: str | None = Query(None, description="可选 YYYY-MM-DD，仅保留该日之后的记录"),
):
    try:
        rows = await market_extra_http.fetch_securities_margin(start_date=start_date)
        cached = market_history_cache.merge_save_series("margin", "market", rows)
        return {"items": cached["items"], "start_date": start_date, "cache_saved_at": cached["saved_at"], "served_from_cache": False}
    except Exception as e:
        cached = market_history_cache.load_series("margin", "market")
        if cached:
            return {"items": cached["items"], "start_date": start_date, "cache_saved_at": cached["saved_at"], "served_from_cache": True, "cache_warning": str(e)[:500]}
        raise HTTPException(status_code=502, detail=f"融资融券接口异常: {e!s}") from e


@router.get("/stock/{symbol}/adata/capital-flow")
async def stock_adata_capital_flow(
    symbol: str,
    start_date: str | None = Query(None),
):
    sym = symbol.strip().upper()
    if not _A_SHARE_ONLY.match(sym):
        raise HTTPException(status_code=400, detail="仅支持 A 股代码格式")
    try:
        rows = await market_extra_http.fetch_stock_capital_flow(sym, start_date=start_date)
        cached = market_history_cache.merge_save_series("stock_capital_flow", sym, rows)
        return {"symbol": sym, "items": cached["items"], "cache_saved_at": cached["saved_at"], "served_from_cache": False}
    except Exception as e:
        cached = market_history_cache.load_series("stock_capital_flow", sym)
        if cached:
            return {"symbol": sym, "items": cached["items"], "cache_saved_at": cached["saved_at"], "served_from_cache": True, "cache_warning": str(e)[:500]}
        return {
            "symbol": sym,
            "items": [],
            "cache_saved_at": None,
            "served_from_cache": False,
            "source_warning": f"资金流向接口异常: {e!s}"[:500],
        }


@router.get("/stock/{symbol}/adata/concepts")
async def stock_adata_concepts(symbol: str):
    sym = symbol.strip().upper()
    if not _A_SHARE_ONLY.match(sym):
        raise HTTPException(status_code=400, detail="仅支持 A 股代码格式")
    try:
        rows = await market_extra_http.fetch_stock_concepts_east(sym)
        return {"symbol": sym, "items": rows}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"概念板块接口异常: {e!s}") from e


@router.get("/etf/{fund_code}/adata/current")
async def etf_adata_current(fund_code: str):
    code = (fund_code or "").strip()
    if not code.isdigit() or len(code) != 6:
        raise HTTPException(status_code=400, detail="ETF 代码须为 6 位数字")
    try:
        rows = await market_extra_http.fetch_etf_current(code)
        return {"fund_code": code, "items": rows}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"ETF 行情接口异常: {e!s}") from e
