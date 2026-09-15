"""价格表现：多区间收益、波动、简单异动。"""
from __future__ import annotations

import math
import statistics
from typing import Any


def _pct_change(candles: list[dict[str, Any]], days_back: int) -> float | None:
    if not candles or len(candles) < 2:
        return None
    last = candles[-1].get("c")
    if last is None or last == 0:
        return None
    idx = max(0, len(candles) - 1 - days_back)
    base = candles[idx].get("c")
    if base is None or base == 0:
        return None
    return round((float(last) - float(base)) / float(base) * 100, 2)


def _daily_returns(candles: list[dict[str, Any]]) -> list[float]:
    out: list[float] = []
    for i in range(1, len(candles)):
        a, b = candles[i - 1].get("c"), candles[i].get("c")
        if a and b and float(a) != 0:
            out.append((float(b) - float(a)) / float(a))
    return out


def analyze_price_action(bundle: dict[str, Any]) -> dict[str, Any]:
    kline = bundle.get("kline") or {}
    candles: list[dict[str, Any]] = list(kline.get("candles") or [])
    metrics = kline.get("metrics") or {}
    sym = bundle.get("primary_symbol") or kline.get("symbol") or ""

    text_parts: list[str] = []
    if not candles:
        text_parts.append("未获取到 K 线数据，无法计算区间收益与波动率。")
        return {
            "symbol": sym,
            "returns_5d": None,
            "returns_1m": None,
            "returns_3m": None,
            "returns_1y": None,
            "volatility_annual_approx": None,
            "latest_close": metrics.get("latest_close"),
            "change_pct_session": metrics.get("change_pct"),
            "narrative": "\n".join(text_parts),
            "raw": {},
        }

    r5 = _pct_change(candles, min(5, len(candles) - 1))
    r21 = _pct_change(candles, min(21, len(candles) - 1))
    r63 = _pct_change(candles, min(63, len(candles) - 1))
    r252 = _pct_change(candles, min(252, len(candles) - 1))

    rets = _daily_returns(candles[-min(60, len(candles)) :])
    vol = None
    if len(rets) >= 5:
        try:
            sd = statistics.stdev(rets)
            vol = round(sd * math.sqrt(252) * 100, 2)
        except statistics.StatisticsError:
            vol = None

    lc = metrics.get("latest_close") or candles[-1].get("c")
    chg = metrics.get("change_pct")

    text_parts.append(
        f"最新收盘价（来自行情摘要）: {lc if lc is not None else '未确认'}；"
        f"最近一根 K 线对应涨跌幅约 {chg if chg is not None else '未确认'}%。"
    )
    text_parts.append(
        f"近似区间收益（按可用交易日回溯）: 约5日 {r5}%，约1月(21日) {r21}%，"
        f"约3月(63日) {r63}%，约1年(252日) {r252}%。"
    )
    if vol is not None:
        text_parts.append(f"近段日收益年化波动率（简化估算）: 约 {vol}%。数据不足时该指标仅供参考。")

    last_v = candles[-1].get("v") or 0
    avg_v = statistics.mean([int(c.get("v") or 0) for c in candles[-20:]]) if len(candles) >= 5 else 0
    if avg_v and last_v > avg_v * 2:
        text_parts.append("成交量相对近 20 日均量明显放大，可能存在事件驱动或情绪集中释放（需结合新闻）。")

    sec = bundle.get("secondary")
    comp_line = ""
    if sec and sec.get("kline"):
        c2 = list((sec.get("kline") or {}).get("candles") or [])
        if c2:
            r21b = _pct_change(c2, min(21, len(c2) - 1))
            comp_line = f"\n对比标的 {sec.get('symbol')}: 约1月收益 {r21b}%。"
            text_parts.append(comp_line.strip())

    return {
        "symbol": sym,
        "returns_5d": r5,
        "returns_1m": r21,
        "returns_3m": r63,
        "returns_1y": r252,
        "volatility_annual_approx": vol,
        "latest_close": lc,
        "change_pct_session": chg,
        "narrative": "\n".join(text_parts),
        "raw": {
            "candle_count": len(candles),
            "kline_source": kline.get("kline_source") or metrics.get("data_source"),
        },
    }
