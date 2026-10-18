"""Pure interpreter for StrategySpecV1; only past and current bars are visible."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from app.schemas.research import ResearchExpr, StrategySpecV1, expression_lookback
from app.services import trading_rules

SHANGHAI = ZoneInfo("Asia/Shanghai")
_COMMUTATIVE = {"add", "mul", "and", "or", "eq"}


def normalize_spec(spec: StrategySpecV1 | dict[str, Any]) -> StrategySpecV1:
    return spec if isinstance(spec, StrategySpecV1) else StrategySpecV1.model_validate(spec)


def canonical_logic(spec: StrategySpecV1 | dict[str, Any]) -> dict[str, Any]:
    """Ignore prose and canonicalize commutative operands for candidate dedup."""
    data = normalize_spec(spec).model_dump(mode="json", exclude={"name", "hypothesis", "parent_version_id", "sources", "applicability", "failure_conditions"}, exclude_none=True)

    def canonical(node: Any) -> Any:
        if isinstance(node, list):
            return [canonical(x) for x in node]
        if not isinstance(node, dict):
            return node
        out = {key: canonical(value) for key, value in node.items()}
        if out.get("op") in _COMMUTATIVE:
            out["args"] = sorted(out["args"], key=lambda x: json.dumps(x, sort_keys=True))
        return out

    return canonical(data)


def strategy_logic_hash(spec: StrategySpecV1 | dict[str, Any]) -> str:
    payload = json.dumps(canonical_logic(spec), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def required_warmup(spec: StrategySpecV1 | dict[str, Any]) -> int:
    value = normalize_spec(spec)
    return max(expression_lookback(x) for x in (value.entry, value.exit, value.rank) if x)


def trade_timestamp(bar: dict[str, Any]) -> int:
    day = bar.get("trade_date") or bar.get("date")
    if day:
        text = str(day)
        parsed = datetime.strptime(text, "%Y%m%d" if len(text) == 8 else "%Y-%m-%d").date()
    else:
        raw = float(bar["t"])
        if not np.isfinite(raw) or raw > 100_000_000_000:
            raise ValueError("bar timestamp must be Unix seconds")
        parsed = datetime.fromtimestamp(raw, SHANGHAI).date()
    return int(datetime.combine(parsed, time(), SHANGHAI).timestamp())


def normalize_bars(bars_by_symbol: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for symbol, bars in bars_by_symbol.items():
        seen: set[int] = set()
        rows = []
        for bar in bars:
            timestamp = trade_timestamp(bar)
            if timestamp in seen:
                raise ValueError(f"duplicate trading date for {symbol}")
            seen.add(timestamp)
            rows.append({**bar, "t": timestamp})
        out[symbol] = sorted(rows, key=lambda x: x["t"])
    return out


def _available_for_decision(bar: dict[str, Any]) -> bool:
    """Never backfill a late publication/revision into a historical signal.

    Missing timing evidence remains exploratory. Explicitly impossible timing
    masks the entire row permanently; finite rolling windows can recover using
    later, independently timely observations without revising an earlier signal.
    """
    day = datetime.fromtimestamp(bar["t"], SHANGHAI).date().isoformat()
    return not trading_rules.daily_bar_availability_issues(bar, day, require_metadata=False)


def _matrices(spec: StrategySpecV1, bars_by_symbol: dict[str, list[dict[str, Any]]]):
    bars = normalize_bars(bars_by_symbol)
    symbols = sorted(spec.universe or tuple(bars))
    if any(symbol not in bars for symbol in symbols):
        raise ValueError("strategy universe contains missing symbols")
    if not 1 <= len(symbols) <= 500:
        raise ValueError("research universe requires 1–500 symbols")
    timestamps = sorted({b["t"] for s in symbols for b in bars[s]})
    slots = {t: i for i, t in enumerate(timestamps)}
    fields = {key: np.full((len(timestamps), len(symbols)), np.nan) for key in ("o", "h", "l", "c", "v", "amount")}
    for col, symbol in enumerate(symbols):
        for bar in bars[symbol]:
            if not _available_for_decision(bar):
                continue
            row = slots[bar["t"]]
            for key, matrix in fields.items():
                value = bar.get(key)
                if value is not None:
                    number = float(value)
                    if np.isfinite(number):
                        matrix[row, col] = number
    return timestamps, symbols, fields


def _shift(value: np.ndarray, count: int) -> np.ndarray:
    out = np.full_like(value, np.nan)
    if count < len(value):
        out[count:] = value[:-count]
    return out


def _rolling(value: np.ndarray, window: int, operation: str) -> np.ndarray:
    rolling = pd.DataFrame(value).rolling(window, min_periods=window)
    if operation == "std":
        return rolling.std(ddof=0).to_numpy()
    return getattr(rolling, operation)().to_numpy()


def _evaluate_expr(expr: ResearchExpr, fields: dict[str, np.ndarray], cache: dict[str, np.ndarray]) -> np.ndarray:
    key = expr.model_dump_json()
    if key in cache:
        return cache[key]
    args = [_evaluate_expr(arg, fields, cache) for arg in expr.args]
    op, window = expr.op, int(expr.window or 1)
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        if op == "field":
            result = fields[expr.field]
        elif op == "const":
            result = np.full_like(fields["c"], expr.value)
        elif op == "volume":
            result = fields["v"]
        elif op == "lag":
            result = _shift(args[0], window)
        elif op == "return":
            result = args[0] / _shift(args[0], window) - 1
        elif op in {"sma", "rolling_mean", "rolling_std", "rolling_min", "rolling_max"}:
            operation = "mean" if op in {"sma", "rolling_mean"} else op.removeprefix("rolling_")
            result = _rolling(args[0], window, operation)
        elif op == "volatility":
            result = _rolling(args[0] / _shift(args[0], 1) - 1, window, "std") * np.sqrt(252)
        elif op == "rsi":
            delta = args[0] - _shift(args[0], 1)
            gains = _rolling(np.maximum(delta, 0), window, "mean")
            losses = _rolling(np.maximum(-delta, 0), window, "mean")
            result = 100 - 100 / (1 + gains / losses)
            result = np.where((losses == 0) & (gains > 0), 100, result)
            result = np.where((losses == 0) & (gains == 0), 50, result)
        elif op == "atr":
            previous = _shift(fields["c"], 1)
            ranges = np.maximum(fields["h"] - fields["l"], np.maximum(abs(fields["h"] - previous), abs(fields["l"] - previous)))
            result = _rolling(ranges, window, "mean")
        elif op == "rank":
            result = pd.DataFrame(args[0]).rank(axis=1, method="average", pct=True).to_numpy()
        elif op in {"add", "sub", "mul", "div"}:
            result = {"add": np.add, "sub": np.subtract, "mul": np.multiply, "div": np.divide}[op](*args)
        elif op == "abs":
            result = np.abs(args[0])
        elif op == "neg":
            result = -args[0]
        elif op in {"gt", "gte", "lt", "lte", "eq"}:
            compare = {"gt": np.greater, "gte": np.greater_equal, "lt": np.less, "lte": np.less_equal, "eq": np.equal}[op]
            result = compare(*args).astype(float)
            result[~np.isfinite(args[0]) | ~np.isfinite(args[1])] = np.nan
        elif op in {"and", "or"}:
            stacked = np.stack(args)
            result = (np.all(stacked == 1, axis=0) if op == "and" else np.any(stacked == 1, axis=0)).astype(float)
            result[~np.all(np.isfinite(stacked), axis=0)] = np.nan
        else:  # not; schema excludes all other operations
            result = 1 - args[0]
    result = np.asarray(result, dtype=float).copy()
    result[~np.isfinite(result)] = np.nan
    cache[key] = result
    return result


def compute_indicators(spec: StrategySpecV1 | dict[str, Any], bars_by_symbol: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    value = normalize_spec(spec)
    timestamps, symbols, fields = _matrices(value, bars_by_symbol)
    cache: dict[str, np.ndarray] = {}
    values = {name: _evaluate_expr(expr, fields, cache) for name, expr in (("entry", value.entry), ("exit", value.exit), ("rank", value.rank)) if expr}
    return {"timestamps": timestamps, "symbols": symbols, "values": values, "close": fields["c"]}


def compute_targets(
    spec: StrategySpecV1 | dict[str, Any],
    bars_by_symbol: dict[str, list[dict[str, Any]]],
    *,
    start_at: int | None = None,
) -> dict[int, dict[str, float]]:
    """Sparse close targets. Absent date means no new rebalance instruction.

    start_at fixes the first decision session for warmup comparisons. Indicators
    can use earlier bars, but simulated selection state starts empty at start_at.
    """
    value = normalize_spec(spec)
    data = compute_indicators(value, bars_by_symbol)
    timestamps, symbols, values = data["timestamps"], data["symbols"], data["values"]
    targets: dict[int, dict[str, float]] = {}
    held: dict[int, int] = {}
    first = next((i for i, t in enumerate(timestamps) if start_at is None or t >= start_at), len(timestamps))
    for i in range(first, len(timestamps)):
        # Unknown data does not trigger an invented exit or a fresh entry.
        expired = {col for col, entered in held.items() if i - entered >= value.holding_days}
        exiting = {col for col in held if "exit" in values and values["exit"][i, col] == 1}
        removed = expired | exiting
        before = set(held)
        for col in removed:
            del held[col]
        rebalance = (i - first) % value.rebalance_days == 0
        if rebalance:
            candidates = [col for col in range(len(symbols)) if col not in held and col not in removed
                          and values["entry"][i, col] == 1 and np.isfinite(data["close"][i, col])
                          and data["close"][i, col] > 0]
            if "rank" in values:
                candidates = [col for col in candidates if np.isfinite(values["rank"][i, col])]
                candidates.sort(key=lambda col: (-values["rank"][i, col], symbols[col]))
            for col in candidates[:max(0, value.max_positions - len(held))]:
                held[col] = i
        if rebalance or before != set(held):
            weight = min(value.max_position_pct / len(held), value.max_symbol_weight) if held else 0
            targets[timestamps[i]] = {symbols[col]: round(weight, 12) for col in sorted(held)}
    return targets


def explain_strategy(spec: StrategySpecV1 | dict[str, Any]) -> dict[str, Any]:
    value = normalize_spec(spec)

    def expr_text(expr: ResearchExpr) -> str:
        if expr.op == "field":
            return str(expr.field)
        if expr.op == "const":
            return str(expr.value)
        content = [expr_text(a) for a in expr.args]
        if expr.window is not None:
            content.append(f"window={expr.window}")
        return f"{expr.op}({', '.join(content)})"

    return {
        "logic_hash": strategy_logic_hash(value), "warmup_sessions": required_warmup(value),
        "entry": expr_text(value.entry), "exit": expr_text(value.exit) if value.exit else None,
        "rank": expr_text(value.rank) if value.rank else None,
        "holding_days": value.holding_days, "rebalance_days": value.rebalance_days,
        "execution": "close decision; next trading session open; long only; equal weights; residual cash",
    }


def built_in_baselines() -> dict[str, StrategySpecV1]:
    close = {"op": "field", "field": "c"}
    def sma(window: int):
        return {"op": "sma", "window": window, "args": [close]}
    common = {"holding_days": 20, "rebalance_days": 5, "max_positions": 30, "max_symbol_weight": 1.0}
    return {
        "ma_trend": StrategySpecV1.model_validate({**common, "name": "MA5/20 baseline",
            "entry": {"op": "gt", "args": [sma(5), sma(20)]}, "exit": {"op": "lte", "args": [sma(5), sma(20)]}}),
    }


def equal_weight_targets(bars_by_symbol: dict[str, list[dict[str, Any]]], *, start_at: int | None = None) -> dict[int, dict[str, float]]:
    """Initial equal weights, held without periodic rebalance; cash uses {}."""
    bars = normalize_bars(bars_by_symbol)
    symbols = sorted(bars)
    if not symbols or any(not rows for rows in bars.values()):
        return {}
    common = set.intersection(*({b["t"] for b in rows if _available_for_decision(b)} for rows in bars.values()))
    timestamps = sorted(t for t in common if start_at is None or t >= start_at)
    return {timestamps[0]: {symbol: 1 / len(symbols) for symbol in symbols}} if timestamps else {}
