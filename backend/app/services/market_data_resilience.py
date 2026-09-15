"""公开行情源韧性层：实时源失败时自动切换最近成功缓存，并补齐备用源缺失字段。"""
from __future__ import annotations

import asyncio
from typing import Any

from app.services import eastmoney_market, free_stockdb_http
from app.storage import market_source_cache


def _err_text(exc: BaseException) -> str:
    text = str(exc).strip()
    return text or exc.__class__.__name__


async def fetch_quote_snapshot_resilient(symbol: str) -> dict[str, Any]:
    """东财实时快照；失败时返回最近成功缓存，带 `served_from_cache/cache_warning`。"""
    sym = symbol.strip().upper()
    try:
        payload = await asyncio.wait_for(eastmoney_market.fetch_extended_quote_snapshot(sym), timeout=18.0)
        market_source_cache.save_payload("quote_snapshot", sym, payload, source="eastmoney_push2")
        market_source_cache.record_health("eastmoney_quote_snapshot", ok=True)
        return {**payload, "served_from_cache": False, "source": "eastmoney_push2"}
    except Exception as exc:  # noqa: BLE001
        err = _err_text(exc)
        market_source_cache.record_health("eastmoney_quote_snapshot", ok=False, detail=err)
        cached = market_source_cache.load_payload("quote_snapshot", sym)
        if cached and isinstance(cached.get("payload"), dict):
            return {
                **cached["payload"],
                "served_from_cache": True,
                "cache_saved_at": cached.get("saved_at"),
                "cache_warning": err[:500],
                "source": cached.get("source") or "local_cache",
            }
        raise


async def fetch_stock_blocks_resilient(secid: str, symbol: str) -> list[dict[str, Any]]:
    """东财所属板块；失败时返回最近成功缓存。"""
    sym = symbol.strip().upper()
    try:
        payload = await asyncio.wait_for(eastmoney_market.fetch_stock_blocks(secid), timeout=18.0)
        market_source_cache.save_payload("stock_blocks", sym, payload, source="eastmoney_slist")
        market_source_cache.record_health("eastmoney_stock_blocks", ok=True)
        return payload
    except Exception as exc:  # noqa: BLE001
        err = _err_text(exc)
        market_source_cache.record_health("eastmoney_stock_blocks", ok=False, detail=err)
        cached = market_source_cache.load_payload("stock_blocks", sym)
        if cached and isinstance(cached.get("payload"), list):
            rows: list[dict[str, Any]] = []
            for row in cached["payload"]:
                if isinstance(row, dict):
                    out = dict(row)
                    out.setdefault("_served_from_cache", True)
                    out.setdefault("_cache_saved_at", cached.get("saved_at"))
                    out.setdefault("_cache_warning", err[:500])
                    rows.append(out)
            return rows
        raise


def enrich_metrics_with_quote_snapshot(bundle: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    """用快照补齐腾讯/新浪等备用 K 线源没有的指标，不覆盖已存在且可信的 K 线数据。"""
    if not snapshot:
        return bundle
    out = dict(bundle)
    metrics = dict(out.get("metrics") or {})
    fill_map = {
        "name": "name",
        "previous_close": "prev_close",
        "latest_open": "open",
        "latest_high": "high",
        "latest_low": "low",
        "latest_turnover_rate": "turnover_rate_pct",
        "pe_ttm": "pe_ttm",
        "total_market_cap_yuan": "total_market_cap_yuan",
        "float_market_cap_yuan": "float_market_cap_yuan",
        "shares_outstanding": "shares_total",
        "limit_up": "limit_up",
        "limit_down": "limit_down",
        "volume_ratio": "volume_ratio",
        "inner_lots": "inner_lots",
        "outer_lots": "outer_lots",
        "snapshot_volume_lots": "volume_lots",
        "snapshot_amount_yuan": "amount_yuan",
    }
    for metric_key, snapshot_key in fill_map.items():
        val = snapshot.get(snapshot_key)
        if val is not None and val != "" and metrics.get(metric_key) in (None, "", 0):
            metrics[metric_key] = val

    amount = snapshot.get("amount_yuan")
    if amount is not None and metrics.get("latest_amount") in (None, "", 0, 0.0):
        metrics["latest_amount"] = amount
    volume_lots = snapshot.get("volume_lots")
    if volume_lots is not None and metrics.get("latest_volume") in (None, "", 0, 0.0):
        try:
            metrics["latest_volume"] = int(float(volume_lots) * 100)
        except (TypeError, ValueError):
            pass

    # 备用源常缺总股本，拿到总股本后可回填日线换手率。
    shares = snapshot.get("shares_total")
    if shares:
        try:
            shares_f = float(shares)
            for row in out.get("candles") or []:
                if row.get("turnover_rate") is None and row.get("v"):
                    row["turnover_rate"] = round(float(row["v"]) / shares_f * 100.0, 4)
            last = (out.get("candles") or [])[-1] if out.get("candles") else None
            if last and metrics.get("latest_turnover_rate") in (None, "") and last.get("turnover_rate") is not None:
                metrics["latest_turnover_rate"] = last.get("turnover_rate")
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    extras: list[str] = list(metrics.get("data_source_notes") or [])
    if snapshot.get("served_from_cache"):
        extras.append(f"快照指标使用本地缓存（{snapshot.get('cache_saved_at') or '未知时间'}）")
    else:
        extras.append("快照指标来自东方财富 push2")
    metrics["data_source_notes"] = list(dict.fromkeys(x for x in extras if x))
    metrics["quote_snapshot_source"] = snapshot.get("source") or "eastmoney_push2"
    metrics["quote_snapshot_cached"] = bool(snapshot.get("served_from_cache"))
    if snapshot.get("cache_warning"):
        metrics["quote_snapshot_warning"] = snapshot.get("cache_warning")

    note = metrics.get("fallback_note")
    if note and "快照指标" not in note:
        metrics["fallback_note"] = f"{note} 快照类指标已自动补齐。"
    out["metrics"] = metrics
    return out


async def enrich_kline_bundle_resilient(bundle: dict[str, Any], symbol: str) -> dict[str, Any]:
    """尽力补齐 K 线 bundle 的快照字段；补齐失败不让 K 线主数据失败。"""
    try:
        snapshot = await fetch_quote_snapshot_resilient(symbol)
    except Exception as exc:  # noqa: BLE001
        metrics = dict(bundle.get("metrics") or {})
        metrics["quote_snapshot_warning"] = _err_text(exc)[:500]
        market_source_cache.record_health("kline_quote_enrichment", ok=False, detail=_err_text(exc))
        return {**bundle, "metrics": metrics}
    market_source_cache.record_health("kline_quote_enrichment", ok=True)
    return enrich_metrics_with_quote_snapshot(bundle, snapshot)


def market_source_health() -> dict[str, Any]:
    items = dict(market_source_cache.load_health() or {})
    items["free_stockdb_local"] = free_stockdb_http.health_check()
    return items
