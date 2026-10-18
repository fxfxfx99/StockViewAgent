"""Deterministic research protocol and promotion gates; never reads/writes state.

The caller owns the holdout lock and must freeze a candidate before calling
evaluate_holdout. evaluate_candidate never evaluates holdout bars.
"""
from __future__ import annotations

import calendar
import copy
import math
from statistics import median
from datetime import date, datetime, time, timedelta
from typing import Any

import numpy as np

from app.schemas.research import StrategySpecV1
from app.services.research_strategy import (
    SHANGHAI, built_in_baselines, compute_indicators, compute_targets,
    equal_weight_targets, normalize_bars, normalize_spec, required_warmup,
    strategy_logic_hash,
)

PROTOCOL_VERSION = "research-evaluation-v1"


def _month_shift(day: date, months: int) -> date:
    total = day.year * 12 + day.month - 1 + months
    year, month = divmod(total, 12)
    return date(year, month + 1, min(day.day, calendar.monthrange(year, month + 1)[1]))


def _timestamp(day: date) -> int:
    return int(datetime.combine(day, time(), SHANGHAI).timestamp())


def _days(bars: dict[str, list[dict[str, Any]]]) -> list[int]:
    return sorted({int(b["t"]) for rows in bars.values() for b in rows})


def build_research_splits(
    bars_by_symbol: dict[str, list[dict[str, Any]]], holding_days: int = 20, *, include_holdout: bool = True,
) -> dict[str, Any]:
    """24-month training / 6-month validation, stepping 6 months; final 6 held out.

    Purge uses actual available sessions, never calendar-day subtraction. The
    tail of each validation window is excluded too, so its maximum holding
    period cannot overlap the following window or the final holdout.
    """
    if not 5 <= holding_days <= 20:
        raise ValueError("holding_days must be 5–20")
    bars = normalize_bars(bars_by_symbol)
    times = _days(bars)
    result: dict[str, Any] = {"protocol_version": PROTOCOL_VERSION, "eligible": False, "issues": [],
                              "folds": [], "development": None, "holdout": None, "purge_sessions": holding_days}
    if not times:
        result["issues"].append("no_history")
        return result
    first = datetime.fromtimestamp(times[0], SHANGHAI).date()
    last = datetime.fromtimestamp(times[-1], SHANGHAI).date()
    exclusive_end = last + timedelta(days=1)
    holdout_start = _timestamp(_month_shift(exclusive_end, -6) if include_holdout else exclusive_end)
    holdout_times = [t for t in times if t >= holdout_start]
    development = [t for t in times if t < holdout_start]
    if len(development) <= holding_days or (include_holdout and len(holdout_times) < 100):
        result["issues"].append("insufficient_development_or_holdout_sessions")
        return result
    development = development[:-holding_days]
    result["development"] = {"start": development[0], "end": development[-1]}
    result["holdout"] = {"start": holdout_times[0], "end": holdout_times[-1]} if include_holdout else None
    validation_end_day = datetime.fromtimestamp(holdout_start, SHANGHAI).date()
    reversed_folds = []
    while True:
        validation_start_day = _month_shift(validation_end_day, -6)
        train_start_day = _month_shift(validation_start_day, -24)
        # A weekend/holiday at the requested starting boundary is normal.
        if first > train_start_day + timedelta(days=7):
            break
        validation_start, validation_end = _timestamp(validation_start_day), _timestamp(validation_end_day)
        training = [t for t in times if _timestamp(train_start_day) <= t < validation_start]
        validation = [t for t in times if validation_start <= t < validation_end]
        training = training[:-holding_days] if len(training) > holding_days else []
        validation = validation[:-holding_days] if len(validation) > holding_days else []
        if len(training) >= 400 and len(validation) >= 80:
            reversed_folds.append({"train": {"start": training[0], "end": training[-1]},
                                   "validation": {"start": validation[0], "end": validation[-1]},
                                   "train_sessions": len(training), "validation_sessions": len(validation)})
        validation_end_day = validation_start_day
    result["folds"] = [{"index": index, **fold} for index, fold in enumerate(reversed(reversed_folds))]
    if len(result["folds"]) < 3:
        result["issues"].append("at_least_three_walk_forward_windows_required")
    if any(not rows or int(rows[0]["t"]) > times[0] + 7 * 86400 for rows in bars.values()):
        result["issues"].append("universe_history_not_aligned")
    result["eligible"] = not result["issues"]
    return result


def development_bars(bars_by_symbol: dict[str, list[dict[str, Any]]], splits: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """The only dataset the proposal generator should receive."""
    bound = splits.get("development")
    if not bound:
        return {symbol: [] for symbol in bars_by_symbol}
    return {symbol: [row for row in rows if row["t"] <= bound["end"]]
            for symbol, rows in normalize_bars(bars_by_symbol).items()}


def check_prefix_invariance(spec: StrategySpecV1 | dict[str, Any], bars_by_symbol: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Recompute indicators and stateful targets using strictly truncated history."""
    value = normalize_spec(spec)
    bars = normalize_bars(bars_by_symbol)
    full = compute_indicators(value, bars)
    targets = compute_targets(value, bars)
    times = full["timestamps"]
    cutoffs = sorted(set(int(len(times) * ratio) for ratio in (0.4, 0.65, 0.85)))
    checked = 0
    for count in cutoffs:
        if count <= required_warmup(value) + 1 or count >= len(times):
            continue
        cutoff = times[count - 1]
        prefix = {symbol: [row for row in rows if row["t"] <= cutoff] for symbol, rows in bars.items()}
        calculated = compute_indicators(value, prefix)
        for key, array in calculated["values"].items():
            if not np.allclose(array, full["values"][key][:count], equal_nan=True, atol=1e-10, rtol=1e-10):
                return {"passed": False, "checked_prefixes": checked, "reason": "prefix_indicator_changed"}
        if compute_targets(value, prefix) != {t: weights for t, weights in targets.items() if t <= cutoff}:
            return {"passed": False, "checked_prefixes": checked, "reason": "prefix_targets_changed"}
        checked += 1
    return {"passed": checked >= 2, "checked_prefixes": checked,
            "reason": None if checked >= 2 else "insufficient_prefix_history"}


def check_warmup_stability(spec: StrategySpecV1 | dict[str, Any], bars_by_symbol: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    value = normalize_spec(spec)
    bars = normalize_bars(bars_by_symbol)
    full = compute_indicators(value, bars)
    times = full["timestamps"]
    warmup = required_warmup(value)
    overlap = max(40, value.holding_days * 2)
    start = len(times) - overlap
    if start <= warmup + 1:
        return {"passed": False, "reason": "insufficient_warmup_history", "required_sessions": warmup}
    # Both runs have exactly the full mathematical lookback before this overlap.
    truncation = max(0, start - warmup)
    sliced = {symbol: [row for row in rows if row["t"] >= times[truncation]] for symbol, rows in bars.items()}
    recomputed = compute_indicators(value, sliced)
    offset = start - truncation
    for key, array in recomputed["values"].items():
        if not np.allclose(array[offset:], full["values"][key][start:], equal_nan=True, atol=1e-9, rtol=1e-9):
            return {"passed": False, "reason": "warmup_indicator_changed", "required_sessions": warmup}
    full_targets = compute_targets(value, bars, start_at=times[start])
    short_targets = compute_targets(value, sliced, start_at=times[start])
    return {"passed": full_targets == short_targets, "reason": None if full_targets == short_targets else "warmup_targets_changed",
            "required_sessions": warmup, "overlap_sessions": overlap}


def doubled_cost_model(cost_model: dict[str, Any] | None) -> dict[str, Any]:
    from app.services.quant_backtest import _default_cost_model

    result = copy.deepcopy(_default_cost_model(cost_model))
    # Keep None tax rates date-aware; replacing None with zero would erase tax.
    result["fee_multiplier"] = float(result.get("fee_multiplier") or 1) * 2
    for field in ("ratio", "tick_count"):
        result["slippage"][field] = float(result["slippage"].get(field) or 0) * 2
    return result


def _metric(report: dict[str, Any], name: str, default: float = 0) -> float:
    value = (report.get("performance") or {}).get(name, default)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _quality_ok(report: dict[str, Any]) -> bool:
    return (report.get("quality") or {}).get("eligible") is True


def _statistics_complete(report: dict[str, Any]) -> bool:
    performance = report.get("performance") or {}
    required = ("total_return_pct", "annualized_return_pct", "sharpe", "max_drawdown_pct", "completed_trade_count", "turnover_pct")
    try:
        if any(name not in performance or not math.isfinite(float(performance[name])) for name in required):
            return False
        curve = report.get("equity_curve") or []
        times = [int(row["t"]) for row in curve]
        return bool(times) and times == sorted(set(times)) and all(math.isfinite(float(row["equity"])) and float(row["equity"]) > 0 for row in curve)
    except (ValueError, TypeError, KeyError):
        return False


def _bounded_bars(bars: dict[str, list[dict[str, Any]]], end: int) -> dict[str, list[dict[str, Any]]]:
    return {symbol: [row for row in rows if row["t"] <= end] for symbol, rows in bars.items()}


def _evaluate_period(spec: StrategySpecV1, bars: dict[str, list[dict[str, Any]]], period: dict[str, int], *, cost_model, initial_cash: float, include_cost_stress: bool = True) -> dict[str, Any]:
    from app.services.quant_backtest import run_target_backtest

    bounded = _bounded_bars(bars, period["end"])
    timestamps = _days(bounded)
    preceding = [t for t in timestamps if t < period["start"]]
    decision_start = preceding[-1] if preceding else period["start"]
    targets = compute_targets(spec, bounded, start_at=decision_start)
    shared = {"bars_by_symbol": bounded, "initial_cash": initial_cash,
              "evaluation_start": period["start"], "evaluation_end": period["end"]}
    candidate = run_target_backtest(**shared, targets_by_t=targets, cost_model=cost_model,
                                   max_position_pct=spec.max_position_pct, max_symbol_weight=spec.max_symbol_weight)
    stressed = run_target_backtest(**shared, targets_by_t=targets, cost_model=doubled_cost_model(cost_model),
                                  max_position_pct=spec.max_position_pct, max_symbol_weight=spec.max_symbol_weight) if include_cost_stress else None
    baseline_caps = {"max_position_pct": spec.max_position_pct, "max_symbol_weight": spec.max_symbol_weight}
    equal_targets = equal_weight_targets(bounded, start_at=decision_start)
    baselines = {"equal_weight": run_target_backtest(**shared, targets_by_t=equal_targets, cost_model=cost_model, **baseline_caps)}
    if decision_start not in equal_targets:
        baselines["equal_weight"]["quality"] = {
            **baselines["equal_weight"]["quality"], "eligible": False,
            "issues": list(baselines["equal_weight"]["quality"].get("issues") or []) + ["equal_weight_start_unavailable"],
        }
    baselines["cash"] = run_target_backtest(**shared, targets_by_t={}, cost_model=cost_model, **baseline_caps)
    for name, baseline in built_in_baselines().items():
        # Same symbol pool; baseline parameters stay fixed across every trial.
        baseline = baseline.model_copy(update={"universe": spec.universe})
        baseline_targets = compute_targets(baseline, bounded, start_at=decision_start)
        baselines[name] = run_target_backtest(**shared, targets_by_t=baseline_targets, cost_model=cost_model, **baseline_caps)
    return {"candidate": candidate, "baselines": baselines, "cost_stress": stressed}


def _period_summary(evaluations: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, bool]]:
    count = len(evaluations)
    mean = lambda values: sum(values) / len(values) if values else 0.0
    returns = [_metric(x["candidate"], "total_return_pct") for x in evaluations]
    excess = [_metric(x["candidate"], "total_return_pct") - _metric(x["baselines"]["equal_weight"], "total_return_pct") for x in evaluations]
    sharpes = [_metric(x["candidate"], "sharpe") for x in evaluations]
    turnover = mean([_metric(x["candidate"], "turnover_pct") for x in evaluations])
    max_dd = max((_metric(x["candidate"], "max_drawdown_pct") for x in evaluations), default=0)
    stress_returns = [_metric(x["cost_stress"], "total_return_pct") for x in evaluations]
    stress_return = mean(stress_returns)
    stress_aggregate = (math.prod(1 + ret / 100 for ret in stress_returns) - 1) * 100
    joint_positive = sum(ret > 0 and extra > 0 for ret, extra in zip(returns, excess))
    metrics = {
        "window_count": count, "mean_return_pct": mean(returns), "mean_excess_return_pct": mean(excess),
        "positive_excess_windows": sum(x > 0 for x in excess), "positive_return_and_excess_windows": joint_positive,
        "max_drawdown_pct": max_dd, "mean_sharpe": mean(sharpes), "median_sharpe": median(sharpes) if sharpes else 0,
        "mean_turnover_pct": turnover,
        "double_cost_mean_return_pct": stress_return,
        "completed_trade_count": sum(int(_metric(x["candidate"], "completed_trade_count")) for x in evaluations),
        "double_cost_aggregate_return_pct": stress_aggregate,
        "objective": "descending median validation Sharpe; then lower maximum drawdown; then lower mean turnover",
    }
    reports = [r for x in evaluations for r in (x["candidate"], x["cost_stress"], *x["baselines"].values())]
    gates = {
        "data_quality": bool(reports) and all(_quality_ok(r) for r in reports),
        "statistics_complete": bool(reports) and all(_statistics_complete(r) for r in reports),
        "positive_net_return": count > 0 and mean(returns) > 0,
        "excess_consistency": count > 0 and joint_positive >= math.ceil(count * 2 / 3),
        "minimum_completed_trades": metrics["completed_trade_count"] >= 30,
        "drawdown_limit": count > 0 and max_dd <= 20,
        "double_cost_positive": count > 0 and stress_aggregate > 0,
        "double_cost_drawdown": count > 0 and all(_metric(x["cost_stress"], "max_drawdown_pct") <= 20 for x in evaluations),
    }
    if not evaluations:
        # No evaluated windows means unknown returns, not a measured zero.
        metrics = {key: (None if isinstance(value, (int, float)) and key != "completed_trade_count" else value)
                   for key, value in metrics.items()}
    return metrics, gates


def evaluate_candidate(
    spec: StrategySpecV1 | dict[str, Any], bars_by_symbol: dict[str, list[dict[str, Any]]], *,
    cost_model: dict[str, Any] | None = None, initial_cash: float = 100_000,
    include_holdout: bool = True, splits: dict[str, Any] | None = None,
) -> dict[str, Any]:
    value = normalize_spec(spec)
    bars = normalize_bars(bars_by_symbol)
    if value.universe:
        if any(s not in bars for s in value.universe):
            raise ValueError("strategy universe contains missing symbols")
        bars = {s: bars[s] for s in value.universe}
    # A fixed 20-session embargo across all candidates prevents parameter-dependent splits.
    splits = copy.deepcopy(splits) if splits is not None else build_research_splits(bars, holding_days=20, include_holdout=include_holdout)
    development = development_bars(bars, splits)
    empty_check = {"passed": False, "reason": "insufficient_history"}
    checks = {"prefix": empty_check, "warmup": empty_check}
    folds = []
    if any(development.values()):
        checks = {"prefix": check_prefix_invariance(value, development), "warmup": check_warmup_stability(value, development)}
        for fold in splits["folds"]:
            evaluation = _evaluate_period(value, development, fold["validation"], cost_model=cost_model, initial_cash=initial_cash)
            folds.append({**fold, **evaluation})
    metrics, gates = _period_summary(folds)
    gates.update({"history": splits["eligible"], "prefix_invariance": checks["prefix"]["passed"], "warmup_stability": checks["warmup"]["passed"]})
    issues = list(dict.fromkeys(splits["issues"] + [key for key, passed in gates.items() if not passed]))
    return {"protocol_version": PROTOCOL_VERSION, "logic_hash": strategy_logic_hash(value),
            "eligible": all(gates.values()), "gates": gates, "issues": issues, "splits": splits,
            "folds": folds, "metrics": metrics, "checks": checks,
            "rank_key": [metrics["median_sharpe"], -metrics["max_drawdown_pct"], -metrics["mean_turnover_pct"]] if folds else []}


def evaluate_holdout(
    spec: StrategySpecV1 | dict[str, Any], bars_by_symbol: dict[str, list[dict[str, Any]]], *,
    holdout: dict[str, int], cost_model: dict[str, Any] | None = None, initial_cash: float = 100_000,
) -> dict[str, Any]:
    """Caller MUST enforce immutable freeze and exactly-once test consumption."""
    value = normalize_spec(spec)
    if holdout["start"] >= holdout["end"]:
        raise ValueError("holdout requires an increasing interval")
    bars = normalize_bars(bars_by_symbol)
    if value.universe:
        bars = {s: bars[s] for s in value.universe}
    result = _evaluate_period(value, bars, holdout, cost_model=cost_model, initial_cash=initial_cash, include_cost_stress=False)
    candidate = result["candidate"]
    net_return = _metric(candidate, "total_return_pct")
    excess = net_return - _metric(result["baselines"]["equal_weight"], "total_return_pct")
    reports = [candidate, *result["baselines"].values()]
    gates = {"data_quality": all(_quality_ok(r) for r in reports),
             "statistics_complete": all(_statistics_complete(r) for r in reports),
             "positive_net_return": net_return > 0, "positive_excess_return": excess > 0,
             "drawdown_limit": _metric(candidate, "max_drawdown_pct") <= 20}
    metrics = {"return_pct": net_return, "excess_return_pct": excess,
               "max_drawdown_pct": _metric(candidate, "max_drawdown_pct"),
               "completed_trade_count": int(_metric(candidate, "completed_trade_count"))}
    return {"protocol_version": PROTOCOL_VERSION, "logic_hash": strategy_logic_hash(value),
            "eligible": all(gates.values()), "gates": gates, "issues": [k for k, v in gates.items() if not v],
            "metrics": metrics, "period": holdout, **result}


def evaluate_forward(
    candidate_report: dict[str, Any], incumbent_report: dict[str, Any], baseline_report: dict[str, Any], *,
    min_sessions: int = 60, min_completed_trades: int = 20, max_drawdown_pct: float = 20,
) -> dict[str, Any]:
    """Paper results must be from the same calendar interval and starting capital.

    Full sell/buy round trips come from the execution ledger; sell order count
    is deliberately never treated as completed_trade_count.
    """
    reports = (candidate_report, incumbent_report, baseline_report)
    times = [tuple(int(row["t"]) for row in (r.get("equity_curve") or [])) for r in reports]
    completed = int(_metric(candidate_report, "completed_trade_count"))
    sessions = len(times[0])
    candidate_return = _metric(candidate_report, "total_return_pct")
    gates = {
        "data_quality": all(_quality_ok(r) for r in reports),
        "statistics_complete": all(_statistics_complete(r) for r in reports),
        "same_evaluation_sessions": bool(times[0]) and times[0] == times[1] == times[2],
        "minimum_forward_sessions": sessions >= max(60, min_sessions),
        "minimum_completed_trades": completed >= max(20, min_completed_trades),
        "drawdown_limit": _metric(candidate_report, "max_drawdown_pct") <= min(20, max_drawdown_pct),
        "positive_net_return": candidate_return > 0,
        "beats_incumbent_return": candidate_return > _metric(incumbent_report, "total_return_pct"),
        "incumbent_drawdown_tolerance": _metric(candidate_report, "max_drawdown_pct") <= _metric(incumbent_report, "max_drawdown_pct") + 2,
        "beats_equal_weight_return": candidate_return > _metric(baseline_report, "total_return_pct"),
    }
    return {"protocol_version": PROTOCOL_VERSION, "eligible": all(gates.values()), "gates": gates,
            "issues": [k for k, v in gates.items() if not v],
            "metrics": {"sessions": sessions, "completed_trade_count": completed,
                        "sharpe": _metric(candidate_report, "sharpe"), "return_pct": candidate_return}}
