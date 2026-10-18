from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.schemas.research import StrategySpecV1
from app.services import research_evaluation as evaluation
from app.services.research_strategy import SHANGHAI


def history(years=5):
    start = datetime(2020, 1, 1, tzinfo=SHANGHAI)
    end = start.replace(year=start.year + years)
    rows = []
    while start < end:
        if start.weekday() < 5:
            close = 10 + len(rows) * .01
            rows.append({"t": int(start.timestamp()), "o": close, "h": close + .1,
                         "l": close - .1, "c": close, "v": 1e7,
                         "price_basis": "raw", "volume_unit": "shares", "tradable": True,
                         "no_price_limit": True, "corporate_actions_checked": True})
        start += timedelta(days=1)
    return {"600000.SS": rows}


def strategy():
    return StrategySpecV1.model_validate({
        "entry": {"op": "gt", "args": [{"op": "field", "field": "c"}, {"op": "const", "value": 0}]},
        "rank": {"op": "sma", "window": 20, "args": [{"op": "field", "field": "c"}]},
    })


def report(ret=10, dd=3, completed=20, sessions=60, eligible=True):
    return {"performance": {"total_return_pct": ret, "annualized_return_pct": ret * 2,
                            "max_drawdown_pct": dd, "sharpe": ret / 10, "completed_trade_count": completed, "turnover_pct": 100},
            "quality": {"eligible": eligible, "issues": []},
            "equity_curve": [{"t": i, "equity": 100000 + i} for i in range(sessions)]}


def test_walk_forward_splits_purge_and_final_holdout_are_disjoint():
    data = history()
    splits = evaluation.build_research_splits(data)
    assert splits["eligible"]
    assert len(splits["folds"]) >= 3
    times = [row["t"] for row in data["600000.SS"]]
    for fold in splits["folds"]:
        assert fold["train"]["end"] < fold["validation"]["start"]
        assert sum(fold["train"]["end"] < t < fold["validation"]["start"] for t in times) >= 20
        assert fold["validation"]["end"] <= splits["development"]["end"] < splits["holdout"]["start"]
    assert all(row["t"] <= splits["development"]["end"] for row in evaluation.development_bars(data, splits)["600000.SS"])


def test_short_history_is_exploration_only_and_followup_has_no_new_holdout():
    assert not evaluation.build_research_splits(history(2))["eligible"]
    full = evaluation.build_research_splits(history(), include_holdout=False)
    held = evaluation.build_research_splits(history())
    assert full["holdout"] is None
    assert full["development"]["end"] > held["development"]["end"]


def test_past_only_expressions_pass_prefix_and_warmup_checks():
    data = history(1)
    assert evaluation.check_prefix_invariance(strategy(), data)["passed"]
    assert evaluation.check_warmup_stability(strategy(), data)["passed"]


def test_candidate_evaluation_never_passes_holdout_prices_to_engine(monkeypatch):
    data = history()
    split = evaluation.build_research_splits(data)
    calls = []

    def period(spec, bars, bounds, **kwargs):
        assert max(row["t"] for rows in bars.values() for row in rows) <= split["development"]["end"]
        calls.append(bounds)
        return {"candidate": report(), "cost_stress": report(ret=8),
                "baselines": {name: report(ret=2) for name in ("equal_weight", "ma_trend", "cash")}}

    monkeypatch.setattr(evaluation, "_evaluate_period", period)
    result = evaluation.evaluate_candidate(strategy(), data, splits=split)
    assert result["eligible"]
    assert len(calls) == len(split["folds"])
    assert result["rank_key"][0] == result["metrics"]["median_sharpe"]


def test_any_failed_data_quality_blocks_candidate_promotion(monkeypatch):
    def period(*args, **kwargs):
        return {"candidate": report(eligible=False), "cost_stress": report(),
                "baselines": {name: report(ret=2) for name in ("equal_weight", "ma_trend", "cash")}}
    monkeypatch.setattr(evaluation, "_evaluate_period", period)
    result = evaluation.evaluate_candidate(strategy(), history())
    assert not result["eligible"]
    assert not result["gates"]["data_quality"]


def test_forward_requires_sixty_sessions_and_twenty_closed_round_trips():
    result = evaluation.evaluate_forward(report(), report(ret=3), report(ret=4))
    assert result["eligible"]
    assert not evaluation.evaluate_forward(report(completed=19), report(ret=3), report(ret=4))["eligible"]
    assert not evaluation.evaluate_forward(report(sessions=59), report(ret=3, sessions=59), report(ret=4, sessions=59))["eligible"]
    wrong_dates = report(ret=3)
    wrong_dates["equity_curve"][0]["t"] = -1
    assert not evaluation.evaluate_forward(report(), wrong_dates, report(ret=4))["eligible"]
    assert not evaluation.evaluate_forward(report(dd=21), report(ret=3), report(ret=4))["eligible"]


def test_forward_compares_net_return_and_allows_two_point_drawdown_tolerance():
    candidate = report(dd=5)
    candidate["performance"]["sharpe"] = .1  # Sharpe is not an extra forward gate.
    assert evaluation.evaluate_forward(candidate, report(ret=3, dd=3), report(ret=4))["eligible"]
    assert not evaluation.evaluate_forward(report(dd=5.01), report(ret=3, dd=3), report(ret=4))["eligible"]
    assert not evaluation.evaluate_forward(report(ret=10), report(ret=11), report(ret=4))["eligible"]


def test_cost_stress_preserves_date_aware_taxes_and_doubles_fees_and_slippage():
    stressed = evaluation.doubled_cost_model(None)
    assert stressed["stamp_tax_rate"] is None
    assert stressed["transfer_fee_rate"] is None
    assert stressed["fee_multiplier"] == 2
    assert stressed["slippage"]["ratio"] == .002


def test_incomplete_or_nonfinite_statistics_cannot_promote():
    candidate = report()
    candidate["performance"].pop("max_drawdown_pct")
    assert not evaluation.evaluate_forward(candidate, report(ret=3), report(ret=4))["eligible"]
    candidate = report()
    candidate["performance"]["turnover_pct"] = float("nan")
    assert not evaluation.evaluate_forward(candidate, report(ret=3), report(ret=4))["eligible"]


def test_real_engine_period_has_three_baselines_and_round_trip_statistics():
    data = history(1)
    rows = data["600000.SS"]
    result = evaluation._evaluate_period(strategy(), data, {"start": rows[30]["t"], "end": rows[130]["t"]},
                                         cost_model=None, initial_cash=100000)
    assert set(result["baselines"]) == {"equal_weight", "ma_trend", "cash"}
    assert result["baselines"]["cash"]["performance"]["total_return_pct"] == 0
    assert len(result["baselines"]["equal_weight"]["trades"]) == 1
    assert result["candidate"]["performance"]["completed_trade_count"] > 0
    assert result["candidate"]["quality"]["eligible"]
    assert min(row["t"] for row in result["candidate"]["equity_curve"]) == rows[30]["t"]


def test_holdout_interval_validation():
    with pytest.raises(ValueError):
        evaluation.evaluate_holdout(strategy(), history(1), holdout={"start": 2, "end": 1})


def test_holdout_has_no_historical_trade_count_or_cost_stress_gate(monkeypatch):
    def period(*args, **kwargs):
        assert kwargs["include_cost_stress"] is False
        return {"candidate": report(completed=2), "cost_stress": None,
                "baselines": {name: report(ret=2) for name in ("equal_weight", "ma_trend", "cash")}}
    monkeypatch.setattr(evaluation, "_evaluate_period", period)
    result = evaluation.evaluate_holdout(strategy(), history(1), holdout={"start": 1, "end": 2})
    assert result["eligible"]
    assert set(result["gates"]) == {"data_quality", "statistics_complete", "positive_net_return", "positive_excess_return", "drawdown_limit"}


def test_unavailable_equal_weight_start_is_not_a_cash_benchmark():
    data = history(1)
    rows = data["600000.SS"]
    data["000001.SZ"] = [row for i, row in enumerate(rows) if i != 29]
    result = evaluation._evaluate_period(strategy(), data, {"start": rows[30]["t"], "end": rows[60]["t"]},
                                         cost_model=None, initial_cash=100000)
    assert not result["baselines"]["equal_weight"]["quality"]["eligible"]
    assert "equal_weight_start_unavailable" in result["baselines"]["equal_weight"]["quality"]["issues"]


def test_missing_history_does_not_report_measured_zero_returns():
    result = evaluation.evaluate_candidate(strategy(), {s: rows[:2] for s, rows in history(1).items()})
    assert not result['eligible']
    assert result['metrics']['mean_return_pct'] is None
    assert result['metrics']['median_sharpe'] is None
    assert result['metrics']['completed_trade_count'] == 0
    assert result['rank_key'] == []
