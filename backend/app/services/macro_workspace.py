"""Market workspace adapters: public indices plus opt-in licensed macro series."""
from __future__ import annotations

import asyncio
from datetime import datetime

from app.config import settings
from app.services import eastmoney_market, kline_pipeline, macro_tushare_service
from app.services.market_time import SHANGHAI_TZ
from app.storage import data_collection_store

RANGES = frozenset({"3mo", "6mo", "1y", "2y", "5y"})
INTERVALS = frozenset({"1d", "1wk", "1mo"})
PUBLIC_SERIES = {"sh": "SH_COMP", "sz": "SZ_COMP", "cyb": "CYB"}


def indices():
    return [{"key": key, "label": row[1], "region": "CN", "requires_token": False}
            for key, row in eastmoney_market.MAJOR_INDEX_META.items()]


def catalog():
    configured = bool(settings.effective_tushare_token)
    return {"items": [{**row, "requires_token": row["id"] not in PUBLIC_SERIES,
                       "available": row["id"] in PUBLIC_SERIES or configured,
                       "provider_hint": "公开指数来源" if row["id"] in PUBLIC_SERIES else "Tushare（需相应接口权限）"}
                      for row in macro_tushare_service.catalog()],
            "tushare_configured": configured,
            "index_basic_doc": "https://tushare.pro/document/2?doc_id=94",
            "token_hint": "A股大盘指数使用公开来源；黄金、离岸人民币、布伦特及申万行业按当前账户的 Tushare 权限读取。"}


async def index_kline(index_key: str, range_param="1y", interval="1d", *, refresh=False):
    if range_param not in RANGES or interval not in INTERVALS:
        raise ValueError("区间支持3mo/6mo/1y/2y/5y，周期支持1d/1wk/1mo")
    return await kline_pipeline.fetch_major_index_kline_with_fallbacks(
        index_key, range_param, interval, force_refresh=refresh, public_only=True)


async def macro_kline(series_id: str, range_param="1y"):
    if range_param not in RANGES:
        raise ValueError("区间支持3mo/6mo/1y/2y/5y")
    sid = series_id.strip().lower()
    meta = next((row for row in macro_tushare_service.catalog() if row["id"] == sid), None)
    if meta is None:
        raise ValueError("未知宏观序列，请从目录选择")
    if sid in PUBLIC_SERIES:
        bundle = await index_kline(PUBLIC_SERIES[sid], range_param)
        return {**bundle, "series_id": sid, "name": meta["name"], "short": meta.get("short"),
                "ts_code": meta["ts_code"], "provider_hint": "公开指数来源"}
    return await asyncio.to_thread(macro_tushare_service.fetch_macro_kline, sid, range_param)


async def collect_market_baseline():
    """Refresh five public indices without a stock pool, user key, or LLM call.

    Only validated live bars enter the durable data-center record store. Failed
    sources keep their old chart cache and report partial coverage explicitly.
    """
    from app.services.data_collection import durable_call

    semaphore = asyncio.Semaphore(2)
    source_urls = {
        "eastmoney": "https://quote.eastmoney.com/center/",
        "tencent_index_fallback": "https://stockapp.finance.qq.com/",
        "sina_index_fallback": "https://finance.sina.com.cn/stock/",
        "yahoo_stooq_fallback": "https://finance.yahoo.com/",
    }

    async def collect(item):
        async with semaphore:
            try:
                bundle = await index_kline(item["key"], "1y", "1d", refresh=True)
                if bundle.get("cache_stale"):
                    return {"key": item["key"], "status": "stale", "data_as_of": bundle.get("data_as_of")}
                rows = []
                for bar in bundle.get("candles") or []:
                    period = datetime.fromtimestamp(bar["t"], SHANGHAI_TZ).date().isoformat()
                    rows.append({"source_id": "public_market", "source": bundle.get("kline_source") or "公开指数行情",
                                 "upstream_id": f"{item['key']}:{period}", "title": f"{item['label']} 日线行情",
                                 "source_url": bundle.get("source_url") or source_urls.get(bundle.get("kline_source"), "https://quote.eastmoney.com/center/"),
                                 "category": "market", "symbols": [item["key"]], "region": "CN", "period": period,
                                 "published_at": None, "value": bar["c"], "unit": "指数点位",
                                 "text": f"{item['label']} 开盘{bar['o']} 最高{bar['h']} 最低{bar['l']} 收盘{bar['c']}",
                                 "metadata": {**bar, "index_key": item["key"], "range": "1y", "interval": "1d",
                                              "provider": bundle.get("kline_source"),
                                              "provider_detail": (bundle.get("metrics") or {}).get("data_source")}})
                if not rows:
                    return {"key": item["key"], "status": "failed", "error_code": "empty_source"}
                result = await durable_call(data_collection_store.ingest, "public_market", "market", rows)
                return {"key": item["key"], "status": "success", "data_as_of": bundle.get("data_as_of"), **result}
            except asyncio.CancelledError:
                raise
            except Exception:
                return {"key": item["key"], "status": "failed", "error_code": "source_unavailable"}

    results = await asyncio.gather(*(collect(item) for item in indices()))
    await durable_call(data_collection_store.set_state, "public_market_components", results)
    successes = sum(result["status"] == "success" for result in results)
    return {"status": "success" if successes == len(results) else "partial" if successes else "failed",
            "records_received": sum(result.get("records_received", 0) for result in results),
            "records_added": sum(result.get("records_added", 0) for result in results),
            "error_code": None if successes == len(results) else "partial_source_failure",
            "components": results}
