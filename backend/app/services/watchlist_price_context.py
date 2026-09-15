"""股票列表标的：近期日线涨跌幅摘要（东方财富 A 股 K 线）。"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.services import kline_pipeline

_CN = ZoneInfo("Asia/Shanghai")


def _pct_change(last: float, prev: float) -> float | None:
    if prev is None or last is None or prev == 0:
        return None
    return round((last / prev - 1.0) * 100.0, 2)


def _returns_from_candles(candles: list[dict[str, Any]]) -> dict[str, Any]:
    closes = [float(c["c"]) for c in candles if c.get("c") is not None]
    if len(closes) < 2:
        return {"error": "有效收盘价不足"}
    last = closes[-1]
    i5 = max(0, len(closes) - 6)
    i20 = max(0, len(closes) - 21)
    r5 = _pct_change(last, closes[i5])
    r20 = _pct_change(last, closes[i20])
    parts = [
        f"最新收盘约 {last:.4g}",
    ]
    if r5 is not None:
        parts.append(f"近约 5 个交易日累计 {r5:+.2f}%")
    if r20 is not None:
        parts.append(f"近约 20 个交易日累计 {r20:+.2f}%")
    parts.append("走势受多重因素影响，与单条新闻无必然对应；非投资建议。")
    return {
        "last_close": last,
        "return_5d_pct": r5,
        "return_20d_pct": r20,
        "summary_zh": "；".join(parts),
    }


def _yesterday_stats(candles: list[dict[str, Any]]) -> dict[str, Any]:
    """昨日 K 线：收盘、相对前一日涨跌幅、换手率、成交量（股）。

    若最后一根为「当日」日线（与北京时间同日），则「昨日」取倒数第二根；否则最后一根即为最近已收盘交易日。
    """
    out: dict[str, Any] = {
        "yesterday_close": None,
        "yesterday_change_pct": None,
        "yesterday_turnover_rate": None,
        "yesterday_volume": None,
    }
    if not candles:
        return out
    n = len(candles)
    last = candles[-1]
    last_ts = int(last.get("t", 0) or 0)
    try:
        last_d = datetime.fromtimestamp(last_ts, _CN).date()
    except (OSError, ValueError, OverflowError):
        last_d = datetime.now(_CN).date()
    today = datetime.now(_CN).date()
    if n >= 2 and last_d >= today:
        y_idx = n - 2
    else:
        y_idx = n - 1
    y_bar = candles[y_idx]
    prior = candles[y_idx - 1] if y_idx >= 1 else None

    yc = y_bar.get("c")
    if yc is not None:
        try:
            out["yesterday_close"] = round(float(yc), 4)
        except (TypeError, ValueError):
            pass
    if prior is not None and yc is not None and prior.get("c") is not None:
        try:
            out["yesterday_change_pct"] = _pct_change(float(yc), float(prior["c"]))
        except (TypeError, ValueError):
            pass
    tr = y_bar.get("turnover_rate")
    if tr is not None:
        try:
            out["yesterday_turnover_rate"] = round(float(tr), 2)
        except (TypeError, ValueError):
            pass
    vol = y_bar.get("v")
    if vol is not None:
        try:
            out["yesterday_volume"] = int(vol)
        except (TypeError, ValueError):
            pass
    return out


def _candles_20d(candles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """最近至多 20 根日线，供前端本地绘制 K 线（与 fetch 同源）。"""
    if not candles:
        return []
    tail = candles[-20:]
    out: list[dict[str, Any]] = []
    for c in tail:
        out.append(
            {
                "t": int(c.get("t", 0) or 0),
                "o": c.get("o"),
                "h": c.get("h"),
                "l": c.get("l"),
                "c": c.get("c"),
                "v": c.get("v"),
                "amount": c.get("amount"),
                "turnover_rate": c.get("turnover_rate"),
            }
        )
    return out


async def _one_symbol(sym: str) -> tuple[str, dict[str, Any]]:
    s = sym.strip().upper()
    try:
        # 与「行情信息」共用主源 + 备用源链，避免东财短暂断连时整张股票列表全部为空。
        bundle = await kline_pipeline.fetch_a_share_kline_with_fallbacks(s, "3mo", "1d")
        candles = bundle.get("candles") or []
        metrics = bundle.get("metrics") or {}
        base = _returns_from_candles(candles)
        base.update(_yesterday_stats(candles))
        base["candles_20d"] = _candles_20d(candles)
        # 股票列表表格需要与「个股信息」行情卡保持同源的准实时快照口径；
        # 昨日字段仍保留，供需要盘后日线口径的模块继续使用。
        base.update(
            {
                "latest_price": metrics.get("latest_close"),
                "latest_change_pct": metrics.get("change_pct"),
                "latest_open": metrics.get("latest_open"),
                "latest_high": metrics.get("latest_high"),
                "latest_low": metrics.get("latest_low"),
                "previous_close": metrics.get("previous_close"),
                "latest_turnover_rate": metrics.get("latest_turnover_rate"),
                "latest_volume": (
                    int(metrics["snapshot_volume_lots"]) * 100
                    if metrics.get("snapshot_volume_lots") is not None
                    else metrics.get("latest_volume")
                ),
                "latest_amount": metrics.get("snapshot_amount_yuan") or metrics.get("latest_amount"),
                "pe_ttm": metrics.get("pe_ttm"),
                "total_market_cap_yuan": metrics.get("total_market_cap_yuan"),
                "float_market_cap_yuan": metrics.get("float_market_cap_yuan"),
            }
        )
        base["data_source"] = metrics.get("data_source")
        base["kline_source"] = bundle.get("kline_source") or "eastmoney"
        base["refreshed_at"] = datetime.now(_CN).isoformat(timespec="seconds")
        return s, base
    except Exception as e:
        return s, {"error": str(e)[:200]}


async def price_context_for_symbols(symbols: list[str]) -> dict[str, dict[str, Any]]:
    seen: list[str] = []
    for x in symbols:
        t = x.strip().upper()
        if t and t not in seen:
            seen.append(t)
    if not seen:
        return {}
    # 控制并发，避免列表较长时瞬间打满公开行情源并触发限流/断连。
    semaphore = asyncio.Semaphore(3)

    async def limited(s: str) -> tuple[str, dict[str, Any]]:
        async with semaphore:
            return await _one_symbol(s)

    pairs = await asyncio.gather(*[limited(s) for s in seen])
    return {k: v for k, v in pairs}
