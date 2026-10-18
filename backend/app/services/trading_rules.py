"""Deterministic A-share daily execution rules; no I/O or wall-clock defaults.

Rule references (effective dates are part of the model, not today's settings):
https://www.sse.com.cn/aboutus/mediacenter/hotandd/c/c_20230827_5725662.shtml
https://jrj.wuhan.gov.cn/ynzx_57/xwzx/202204/t20220429_1964160.shtml
https://edu.sse.com.cn/tib/qa/c/4866268.shtml
https://www.szse.cn/aboutus/trends/news/t20200821_580924.html
https://www.bse.cn/uploads/6/file/public/202509/20250916143055_4sxt0kkec4.pdf

Exchange status, ex-right prices and actual auction liquidity must come from
the historical dataset. Inferred limits are explicitly lower-quality research.
"""
from __future__ import annotations

import math
import hashlib
import json
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
from zoneinfo import ZoneInfo

RULES_VERSION = "cn-equity-daily-2026.3"
_SH = ZoneInfo("Asia/Shanghai")


def finite(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def trade_date(value: Any) -> str:
    if isinstance(value, str) and len(value) >= 10 and value[4] == "-":
        return datetime.strptime(value[:10], "%Y-%m-%d").date().isoformat()
    return datetime.fromtimestamp(float(value), _SH).date().isoformat()


def price_round(value: float, decimals: int = 2) -> float:
    return float(Decimal(str(value)).quantize(Decimal(10) ** -decimals, rounding=ROUND_HALF_UP))


def daily_bar_availability_issues(bar: dict[str, Any], date: str, *, require_metadata: bool = False) -> list[str]:
    """Final daily OHLCV must exist by the 16:30 Shanghai close decision.

    A later revision may be stored for exploration but cannot be backdated into
    a historical decision. Missing declarations remain a data-layer blocker;
    legacy pure replay callers may omit them, but explicit claims are checked.
    """
    day = datetime.fromisoformat(date).replace(tzinfo=_SH)
    close_at = day.replace(hour=15).timestamp()
    decision_at = day.replace(hour=16, minute=30).timestamp()
    issues: set[str] = set()
    times: dict[str, float] = {}
    for key in ("source_time", "available_at"):
        if key == "available_at" and bar.get("availability_inferred") is True:
            if require_metadata:
                issues.add("historical_availability_missing")
            else:
                # Acquisition time is useful for archival as-of reads, but is
                # not an explicit historical arrival claim. Exploration may
                # still use the row; strict data certification always rejects it.
                continue
        value = bar.get(key)
        if value is None:
            if require_metadata:
                issues.add("historical_availability_missing")
            continue
        try:
            if isinstance(value, bool):
                raise ValueError()
            if isinstance(value, str) and ("T" in value or " " in value):
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError()
                timestamp = parsed.timestamp()
            else:
                timestamp = float(value)
            if not math.isfinite(timestamp):
                raise ValueError()
        except (TypeError, ValueError, OverflowError):
            issues.add("invalid_historical_availability")
            continue
        times[key] = timestamp
        if timestamp < close_at:
            issues.add("source_time_before_bar_close" if key == "source_time" else "daily_bar_available_before_close")
        if timestamp > decision_at:
            issues.add("availability_after_decision_time")
    if {"source_time", "available_at"} <= times.keys() and times["available_at"] < times["source_time"]:
        issues.add("invalid_availability_order")
    return sorted(issues)


def parse_corporate_actions(bar: dict[str, Any], date: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Validate the ex-date's cash/split announcements, as known before 09:30.

    cash_dividend is gross cash per PRE-action share; split_ratio is the new /
    old share count. A list represents same-ex-date announcements: dividends
    add per original share and split ratios multiply. Omitted neutral fields
    are not guessed. Unknown actions must never silently become no-action days.
    """
    raw = bar["corporate_action"] if "corporate_action" in bar else bar.get("corporate_actions")
    if raw is None or raw is False or raw == []:
        return [], []
    values = raw if isinstance(raw, list) else [raw]
    normalized: dict[str, dict[str, Any]] = {}
    issues: set[str] = set()
    open_at = datetime.fromisoformat(date).replace(hour=9, minute=30, tzinfo=_SH).timestamp()
    for value in values:
        if not isinstance(value, dict) or not {"cash_dividend", "split_ratio", "available_at"} <= value.keys():
            issues.add("corporate_action_fields_missing")
            continue
        if any(isinstance(value[key], bool) for key in ("cash_dividend", "split_ratio", "available_at")):
            issues.add("corporate_action_invalid_values")
            continue
        if value.get("type") not in (None, "cash_dividend", "split", "cash_and_split", "dividend", "bonus_shares"):
            issues.add("corporate_action_type_unsupported")
            continue
        if value.get("ex_date") and str(value["ex_date"])[:10] != date:
            issues.add("corporate_action_ex_date_mismatch")
            continue
        try:
            cash, ratio = float(value["cash_dividend"]), float(value["split_ratio"])
            raw_time = value["available_at"]
            if isinstance(raw_time, str) and ("T" in raw_time or " " in raw_time):
                parsed = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
                available_at = (parsed if parsed.tzinfo else parsed.replace(tzinfo=_SH)).timestamp()
            else:
                available_at = float(raw_time)
        except (ValueError, TypeError, OverflowError):
            issues.add("corporate_action_invalid_values")
            continue
        if not all(math.isfinite(x) for x in (cash, ratio, available_at)) or cash < 0 or ratio <= 0 or (cash == 0 and ratio == 1):
            issues.add("corporate_action_invalid_values")
            continue
        if available_at >= open_at:
            issues.add("corporate_action_not_known_before_open")
            continue
        action = {"cash_dividend": cash, "split_ratio": ratio, "available_at": available_at}
        if value.get("id") is not None:
            action["id"] = str(value["id"])
        fingerprint = hashlib.sha256(json.dumps({"ex_date": date, **action}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        normalized[fingerprint] = {**action, "fingerprint": fingerprint}
    if issues:
        return [], ["unmodeled_corporate_action", *sorted(issues)]
    return [normalized[key] for key in sorted(normalized)], []


def board_for(symbol: str, bar: dict[str, Any] | None = None) -> str:
    declared = str((bar or {}).get("board") or "").lower()
    if declared in {"star", "chinext", "beijing", "main"}:
        return declared
    if symbol.endswith(".BJ"):
        return "beijing"
    if symbol.startswith(("688", "689")):
        return "star"
    if symbol.startswith(("300", "301")):
        return "chinext"
    return "main"


def order_quantity(symbol: str, requested: float, *, side: str = "buy", held: int = 0,
                   bar: dict[str, Any] | None = None) -> int:
    """One aggregate limit order; small residual holdings can be fully sold."""
    board = board_for(symbol, bar)
    minimum, step = (200, 1) if board == "star" else (100, 1) if board == "beijing" else (100, 100)
    cap = 100_000 if board == "star" else 300_000 if board == "chinext" else 1_000_000
    quantity = max(0, min(int(requested), cap))
    if side == "sell" and 0 < held <= cap and quantity >= held and (held < minimum or held % step):
        return held
    return quantity // step * step if quantity >= minimum else 0


def fees(symbol: str, price: float, shares: int, side: str, cost: dict[str, Any], date: str) -> dict[str, float]:
    if date < "2015-08-01" and (cost.get("stamp_tax_rate") is None or cost.get("transfer_fee_rate") is None):
        raise ValueError("2015-08-01 前回测须显式提供历史印花税及过户费率")
    notional = price * shares
    stamp_rate = cost.get("stamp_tax_rate")
    transfer_rate = cost.get("transfer_fee_rate")
    if stamp_rate is None:
        stamp_rate = 0.0005 if date >= "2023-08-28" else 0.001
    if transfer_rate is None:
        transfer_rate = 0.00001 if date >= "2022-04-29" else 0.000025 if symbol.endswith(".BJ") else 0.00002
    multiplier = finite(cost.get("fee_multiplier"), 1.0)
    commission = max(finite(cost.get("min_commission"), 5.0), notional * finite(cost.get("commission_rate"), 0.0003))
    parts = {
        "commission": commission,
        "stamp_tax": notional * float(stamp_rate) if side == "sell" else 0.0,
        "transfer_fee": notional * float(transfer_rate),
        "flow_fee": finite(cost.get("flow_fee")),
    }
    parts = {key: round(value * multiplier, 4) for key, value in parts.items()}
    return {**parts, "total": round(sum(parts.values()), 4)}


def price_limits(symbol: str, bar: dict[str, Any], previous: dict[str, Any] | None, date: str) -> tuple[float | None, float | None, bool]:
    if bar.get("no_price_limit") is True:
        return None, None, False
    if finite(bar.get("limit_up")) > 0 and finite(bar.get("limit_down")) > 0:
        return float(bar["limit_up"]), float(bar["limit_down"]), False
    # A pre-open ex-right reference is preferable to yesterday's raw close.
    reference = finite(bar.get("pre_close")) or finite((previous or {}).get("execution_c")) or finite((previous or {}).get("c"))
    if reference <= 0:
        return None, None, True
    board = board_for(symbol, bar)
    ratio = 0.3 if board == "beijing" else 0.2 if board == "star" or (board == "chinext" and date >= "2020-08-24") else 0.05 if bar.get("st") or bar.get("is_st") else 0.1
    return price_round(reference * (1 + ratio)), price_round(reference * (1 - ratio)), True


def execution_block_reason(symbol: str, bar: dict[str, Any] | None, previous: dict[str, Any] | None,
                           side: str, price: float, date: str) -> str | None:
    if bar is None:
        return "missing_bar"
    if bar.get("suspended") is True or bar.get("tradable") is False or bar.get("trade_status") in {"suspended", "delisted"}:
        return "not_tradable"
    if parse_corporate_actions(bar, date)[1]:
        return "unmodeled_corporate_action"
    if str(bar.get("price_basis") or "").lower() in {"qfq", "hfq", "adjusted", "forward", "backward"} and not bar.get("execution_o"):
        return "adjusted_price_without_raw_execution_price"
    if price <= 0:
        return "invalid_open"
    upper, lower, _ = price_limits(symbol, bar, previous, date)
    # With daily bars, fills at the limiting side cannot be established; refuse.
    if side == "buy" and upper is not None and price >= upper - 1e-8:
        return "limit_up"
    if side == "sell" and lower is not None and price <= lower + 1e-8:
        return "limit_down"
    return None
