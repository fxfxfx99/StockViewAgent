"""Causality, accounting and execution regression tests; no network or real DB."""
from __future__ import annotations

import copy
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.routers.quant import BacktestRequest, PortfolioBacktestRequest
from app.services import quant_backtest as quant, trading_rules


def _bars(n=40, price=10.0, start="2024-01-01"):
    timestamp = int(datetime.fromisoformat(start).replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())
    return [{"t": timestamp + i * 86400, "o": price, "h": price + 0.1,
             "l": price - 0.1, "c": price, "v": 1_000_000,
             "price_basis": "raw", "volume_unit": "shares", "tradable": True,
             "no_price_limit": True, "corporate_actions_checked": True}
            for i in range(n)]


ZERO_COST = {"fee_multiplier": 0, "slippage": {"ratio": 0}, "max_volume_participation": 1}
SYM = "600000.SS"


def test_close_information_cannot_trade_at_same_open():
    bars = _bars()
    bars[25].update(c=20, h=20)
    result = quant.run_backtest(candles=bars, strategy_id="ma_cross", params={"fast": 2, "slow": 3},
                                initial_cash=100_000, cost_model=ZERO_COST)
    assert result["trades"][0]["t"] == bars[26]["t"]
    assert all(t["t"] > bars[25]["t"] for t in result["trades"])
    # Even wildly different later prices cannot rewrite an earlier order.
    changed = copy.deepcopy(bars)
    for bar in changed[30:]:
        bar.update(c=1, o=1, h=1, l=1)
    future = quant.run_backtest(candles=changed, strategy_id="ma_cross", params={"fast": 2, "slow": 3},
                                initial_cash=100_000, cost_model=ZERO_COST)
    assert [t for t in result["trades"] if t["t"] < bars[30]["t"]] == [t for t in future["trades"] if t["t"] < bars[30]["t"]]


def test_warmup_is_context_only_and_seeds_persistent_position():
    bars = _bars()
    result = quant.run_backtest(candles=bars, strategy_id="buy_and_hold", params={}, initial_cash=100_000,
                                evaluation_start=bars[20]["t"], cost_model=ZERO_COST)
    assert len(result["equity_curve"]) == 20
    assert result["equity_curve"][0]["t"] == bars[20]["t"]
    assert [t["t"] for t in result["trades"]] == [bars[20]["t"]]
    assert result["performance"]["total_return_pct"] == 0


def test_zero_max_position_means_cash_only():
    result = quant.run_backtest(candles=_bars(), strategy_id="buy_and_hold", params={},
                                initial_cash=100_000, max_position_pct=0)
    assert result["trades"] == []
    assert result["final_equity"] == 100_000
    assert result["max_position_pct"] == 0


def test_complete_fee_budget_cannot_overdraw_cash():
    bars = _bars(2, price=1)
    result = quant.run_target_backtest(bars_by_symbol={SYM: bars}, targets_by_t={bars[0]["t"]: {SYM: 1}},
                                       initial_cash=1_000_000, cost_model={"max_volume_participation": 1})
    assert result["trades"]
    assert result["final_state"]["cash"] >= 0
    fill = result["trades"][0]
    assert fill["amount"] + fill["fee"]["total"] <= 1_000_000
    assert fill["volume"] % 100 == 0


def test_no_orders_on_days_without_a_target():
    bars = _bars(5)
    bars[2].update(c=12, o=12)
    result = quant.run_target_backtest(bars_by_symbol={SYM: bars}, targets_by_t={bars[0]["t"]: {SYM: .5}},
                                       initial_cash=100_000, cost_model=ZERO_COST)
    assert len(result["trades"]) == 1
    assert result["trades"][0]["t"] == bars[1]["t"]
    assert result["final_state"]["positions"][SYM] == 5000


def test_union_calendar_preserves_missing_symbol_days():
    bars = _bars(4)
    second = "000001.SZ"
    missing = [bars[0], bars[1], bars[3]]
    result = quant.run_target_backtest(bars_by_symbol={SYM: missing, second: bars},
                                       targets_by_t={bars[0]["t"]: {SYM: .5, second: .5}, bars[1]["t"]: {}},
                                       initial_cash=100_000, cost_model=ZERO_COST)
    assert len(result["equity_curve"]) == 4
    assert any(r["reason"] == "missing_bar" and r["t"] == bars[2]["t"] for r in result["rejected_orders"])
    assert "stale_holding_valuation" in result["quality"]["issues"]
    assert result["final_state"]["positions"][SYM] == 5000
    assert result["final_state"]["positions"][second] == 0


@pytest.mark.parametrize("field,value,reason", [
    ("suspended", True, "not_tradable"),
    ("tradable", False, "not_tradable"),
    ("corporate_action", {"type": "split"}, "unmodeled_corporate_action"),
    ("price_basis", "qfq", "adjusted_price_without_raw_execution_price"),
])
def test_explicit_non_executable_data_is_rejected(field, value, reason):
    bars = _bars(2)
    bars[1][field] = value
    result = quant.run_target_backtest(bars_by_symbol={SYM: bars}, targets_by_t={bars[0]["t"]: {SYM: 1}},
                                       cost_model=ZERO_COST)
    assert result["trades"] == []
    assert any(r["reason"] == reason for r in result["rejected_orders"])


def test_limit_up_buy_and_limit_down_sell_refused():
    bars = _bars(3)
    for bar in bars:
        bar.pop("no_price_limit")
        bar.update(limit_up=11, limit_down=9)
    bars[1].update(o=11, c=11)
    blocked_buy = quant.run_target_backtest(bars_by_symbol={SYM: bars},
                                            targets_by_t={bars[0]["t"]: {SYM: 1}}, cost_model=ZERO_COST)
    assert blocked_buy["trades"] == []
    assert blocked_buy["rejected_orders"][0]["reason"] == "limit_up"
    bars[1].update(o=10, c=10)
    bars[2].update(o=9, c=9)
    blocked_sell = quant.run_target_backtest(bars_by_symbol={SYM: bars},
                                             targets_by_t={bars[0]["t"]: {SYM: 1}, bars[1]["t"]: {}}, cost_model=ZERO_COST)
    assert [t["side"] for t in blocked_sell["trades"]] == ["buy"]
    assert any(r["reason"] == "limit_down" for r in blocked_sell["rejected_orders"])


def test_capacity_uses_prior_day_volume_never_today_final_volume():
    bars = _bars(2)
    bars[0]["v"] = 2000
    bars[1]["v"] = 10_000_000
    args = dict(bars_by_symbol={SYM: bars}, targets_by_t={bars[0]["t"]: {SYM: 1}},
                cost_model={"fee_multiplier": 0, "slippage": {"ratio": 0}, "max_volume_participation": .1})
    result = quant.run_target_backtest(**args)
    assert result["trades"][0]["volume"] == 200
    bars[1]["v"] = 1
    assert quant.run_target_backtest(**args)["trades"] == result["trades"]


def test_daily_t_plus_one_survives_paper_restart():
    bars = _bars(3)
    first = quant.run_target_backtest(bars_by_symbol={SYM: bars[:2]},
                                      targets_by_t={bars[0]["t"]: {SYM: .5}}, cost_model=ZERO_COST)
    assert first["final_state"]["available"][SYM] == 0
    replay = quant.run_target_backtest(bars_by_symbol={SYM: bars[1:2]}, targets_by_t={},
                                       precommitted_targets_by_t={bars[1]["t"]: {}},
                                       initial_state=first["final_state"], cost_model=ZERO_COST)
    assert replay["trades"] == []
    next_day = quant.run_target_backtest(bars_by_symbol={SYM: bars[1:]},
                                         targets_by_t={bars[1]["t"]: {}}, initial_state=first["final_state"],
                                         cost_model=ZERO_COST)
    assert [t["side"] for t in next_day["trades"]] == ["sell"]
    assert next_day["final_state"]["positions"][SYM] == 0


def test_precommitted_target_executes_on_named_day():
    bars = _bars(1)
    bars[0]["available_volume"] = 1_000_000
    result = quant.run_target_backtest(bars_by_symbol={SYM: bars}, targets_by_t={},
                                       precommitted_targets_by_t={bars[0]["t"]: {SYM: .2}}, cost_model=ZERO_COST)
    assert result["trades"][0]["t"] == bars[0]["t"]
    assert result["trades"][0]["signal_date"] == "precommitted"


def test_duplicates_or_intraday_bars_fail_closed():
    bars = _bars(2)
    bars[1]["t"] = bars[0]["t"] + 3600
    with pytest.raises(ValueError, match="仅支持日线"):
        quant.run_target_backtest(bars_by_symbol={SYM: bars}, targets_by_t={})
    with pytest.raises(ValidationError):
        BacktestRequest(symbol=SYM, interval="5m")
    with pytest.raises(ValidationError):
        PortfolioBacktestRequest(symbols=[SYM], interval="1wk")


def test_missing_execution_metadata_prevents_promotion():
    bars = _bars(2)
    assert quant.run_target_backtest(bars_by_symbol={SYM: bars}, targets_by_t={})["quality"]["eligible"]
    for bar in bars:
        bar.pop("corporate_actions_checked")
        bar.pop("volume_unit")
    result = quant.run_target_backtest(bars_by_symbol={SYM: bars}, targets_by_t={})
    assert not result["quality"]["eligible"]
    assert "volume_unit_unverified" in result["quality"]["issues"]
    assert "corporate_action_coverage_unverified" in result["quality"]["issues"]


@pytest.mark.parametrize("symbol,requested,expected", [
    ("600000.SS", 250, 200), ("300001.SZ", 250, 200),
    ("688001.SS", 199, 0), ("688001.SS", 201, 201),
    ("830001.BJ", 101, 101),
])
def test_board_order_rules(symbol, requested, expected):
    assert trading_rules.order_quantity(symbol, requested) == expected


def test_odd_lot_can_be_liquidated():
    assert trading_rules.order_quantity("600000.SS", 50, side="sell", held=50) == 50
    assert trading_rules.order_quantity("688001.SS", 199, side="sell", held=199) == 199
    assert trading_rules.order_quantity("600000.SS", 150, side="sell", held=150) == 150


def test_fee_schedule_uses_historical_day_and_both_exchanges():
    cost = quant._default_cost_model({"commission_rate": 0, "min_commission": 0})
    before = trading_rules.fees("000001.SZ", 10, 100, "sell", cost, "2023-08-27")
    after = trading_rules.fees("000001.SZ", 10, 100, "sell", cost, "2023-08-28")
    assert before["stamp_tax"] == 1
    assert after["stamp_tax"] == .5
    assert after["transfer_fee"] == .01
    assert trading_rules.fees(SYM, 10, 100, "buy", cost, "2022-04-28")["transfer_fee"] == .02
    assert trading_rules.fees(SYM, 10, 100, "buy", cost, "2022-04-29")["transfer_fee"] == .01


def test_holdout_is_single_evaluation_and_cannot_select_parameters(monkeypatch):
    bars = _bars(180)
    for i, bar in enumerate(bars):
        price = 10 + ((i // 8) % 2) * 2 + (i % 8) * .04
        bar.update(o=price, c=price)
    calls = []
    real = quant.run_backtest
    def spy(**kwargs):
        calls.append((kwargs.get("evaluation_start"), len(kwargs["candles"])))
        return real(**kwargs)
    monkeypatch.setattr(quant, "run_backtest", spy)
    kwargs = dict(strategy_id="ma_cross", param_grid={"fast": [2, 3], "slow": [5, 8]},
                  initial_cash=100_000, cost_model=ZERO_COST)
    first = quant.optimize_backtest(candles=bars, **kwargs)
    holdout_start = first["holdout_start"]
    assert sum(start == holdout_start for start, _ in calls) == 1
    assert first["partitions"]["validation"]["end"] < holdout_start
    assert all(x["partition"] == "validation" for x in first["cost_stress"])
    changed = copy.deepcopy(bars)
    for bar in changed:
        if bar["t"] >= holdout_start:
            bar.update(o=1, c=1)
    second = quant.optimize_backtest(candles=changed, **kwargs)
    assert first["best_params"] == second["best_params"]
    assert first["candidates"] == second["candidates"]
    assert first["best"]["validation"] == second["best"]["validation"]


def test_factor_portfolio_only_trades_after_scheduled_closes():
    bars = _bars(80)
    other = _bars(80, price=20)
    result = quant.run_portfolio_factor_backtest(bars_by_symbol={SYM: bars, "000001.SZ": other},
                                                 params={"momentum_lookback": 5, "trend_ma": 5,
                                                         "volatility_lookback": 5, "rebalance_days": 10},
                                                 initial_cash=100_000, cost_model=ZERO_COST)
    expected = {row["t"] + 86400 for row in result["rebalance_log"]}
    assert result["trades"]
    assert all(trade["t"] in expected for trade in result["trades"])
    assert result["equity_curve"][0]["t"] == bars[22]["t"]


def test_all_cost_components_are_zero_in_zero_cost_stress():
    cost = quant._default_cost_model({"fee_multiplier": 0})
    assert trading_rules.fees(SYM, 10, 100, "sell", cost, "2024-01-01")["total"] == 0


def test_negative_or_nonfinite_costs_and_weights_rejected():
    bars = _bars(2)
    with pytest.raises(ValueError):
        quant.run_target_backtest(bars_by_symbol={SYM: bars}, targets_by_t={}, cost_model={"commission_rate": -1})
    with pytest.raises(ValueError):
        quant.run_target_backtest(bars_by_symbol={SYM: bars}, targets_by_t={bars[0]["t"]: {SYM: float("nan")}})


def _action(day_bar, *, cash=0.0, ratio=1.0, available_at=None):
    return {"cash_dividend": cash, "split_ratio": ratio,
            "available_at": available_at if available_at is not None else day_bar["t"] - 3600}


def test_cash_dividend_credits_old_holders_without_creating_ex_date_profit():
    bars = _bars(4)
    bars[2]["corporate_action"] = _action(bars[2], cash=.5)
    for bar in bars[2:]:
        bar.update(o=9.5, h=9.6, l=9.4, c=9.5)
    result = quant.run_target_backtest(bars_by_symbol={SYM: bars},
                                       targets_by_t={bars[0]["t"]: {SYM: .5}}, cost_model=ZERO_COST)
    assert result["final_state"]["cash"] == 52_500
    assert result["final_state"]["positions"][SYM] == 5000
    assert result["final_state"]["cost_basis"][SYM] == 50_000
    assert result["performance"]["gross_dividend_income"] == 2500
    assert result["final_equity"] == 100_000
    assert not result["quality"]["eligible"]
    assert "cash_dividend_net_payment_unverified" in result["quality"]["issues"]
    assert any("税前" in warning for warning in result["quality"]["warnings"])
    ledger = result["corporate_action_ledger"][0]
    assert ledger["old_shares"] == 5000 and ledger["gross_cash_credit"] == 2500
    assert ledger["ex_reference_price"] == 9.5


def test_ex_date_buyers_do_not_receive_previous_holders_dividend():
    bars = _bars(3)
    bars[2].update(o=9.5, h=9.6, l=9.4, c=9.5, corporate_action=_action(bars[2], cash=.5))
    result = quant.run_target_backtest(bars_by_symbol={SYM: bars},
                                       targets_by_t={bars[1]["t"]: {SYM: .5}}, cost_model=ZERO_COST)
    assert result["trades"][0]["t"] == bars[2]["t"]
    assert result["performance"]["gross_dividend_income"] == 0
    assert result["corporate_action_ledger"][0]["old_shares"] == 0
    assert result["final_equity"] == 100_000


def test_split_updates_shares_sellable_quantity_and_average_cost():
    bars = _bars(3)
    bars[2].update(o=5, h=5.1, l=4.9, c=5, corporate_action=_action(bars[2], ratio=2))
    result = quant.run_target_backtest(bars_by_symbol={SYM: bars},
                                       targets_by_t={bars[0]["t"]: {SYM: .5}}, cost_model=ZERO_COST)
    state = result["final_state"]
    assert state["positions"][SYM] == state["available"][SYM] == 10_000
    assert state["cost_basis"][SYM] == 50_000
    assert result["corporate_action_ledger"][0]["average_cost"] == 5
    assert state["last_prices"][SYM] == 5
    assert result["final_equity"] == 100_000


def test_cash_and_split_list_use_original_holdings_once():
    bars = _bars(3)
    bars[2].update(o=6, h=6.1, l=5.9, c=6,
                   corporate_action=[_action(bars[2], cash=1), _action(bars[2], ratio=1.5)])
    result = quant.run_target_backtest(bars_by_symbol={SYM: bars},
                                       targets_by_t={bars[0]["t"]: {SYM: .5}}, cost_model=ZERO_COST)
    assert result["final_state"]["cash"] == 55_000
    assert result["final_state"]["positions"][SYM] == 7500
    assert result["corporate_action_ledger"][0]["gross_cash_credit"] == 5000
    assert result["final_equity"] == 100_000


def test_split_odd_lot_can_be_fully_sold_after_action():
    bars = _bars(2)
    bars[1].update(o=20, h=20.1, l=19.9, c=20, corporate_action=_action(bars[1], ratio=.5))
    state = {"cash": 500, "positions": {SYM: 150}, "available": {SYM: 150},
             "last_prices": {SYM: 10}, "cost_basis": {SYM: 1500}, "last_trade_date": "2024-01-01"}
    result = quant.run_target_backtest(bars_by_symbol={SYM: bars}, initial_state=state,
                                       targets_by_t={bars[0]["t"]: {}}, cost_model=ZERO_COST)
    assert result["trades"][0]["volume"] == 75
    assert result["final_state"]["positions"][SYM] == 0
    assert result["final_state"]["cash"] == 2000


@pytest.mark.parametrize("action,issue", [
    ({}, "corporate_action_fields_missing"),
    ({"cash_dividend": 1, "split_ratio": 1}, "corporate_action_fields_missing"),
    ({"cash_dividend": 1, "split_ratio": 0, "available_at": 1_700_000_000}, "corporate_action_invalid_values"),
    ({"cash_dividend": 1, "split_ratio": 1, "available_at": "2024-01-03T09:30:00+08:00"}, "corporate_action_not_known_before_open"),
    ({"cash_dividend": 1, "split_ratio": 1, "available_at": "2024-01-03T12:00:00+08:00"}, "corporate_action_not_known_before_open"),
])
def test_incomplete_or_late_action_fails_closed(action, issue):
    bars = _bars(3)
    bars[2]["corporate_action"] = action
    result = quant.run_target_backtest(bars_by_symbol={SYM: bars},
                                       targets_by_t={bars[0]["t"]: {SYM: .5}, bars[1]["t"]: {}}, cost_model=ZERO_COST)
    assert issue in result["quality"]["issues"]
    assert result["performance"]["gross_dividend_income"] == 0
    assert result["final_state"]["positions"][SYM] == 5000
    assert result["final_state"]["unresolved_corporate_actions"] == [SYM]
    assert not result["quality"]["eligible"]


def test_fractional_share_settlement_is_not_silently_rounded():
    bars = _bars(2)
    bars[1]["corporate_action"] = _action(bars[1], ratio=1.001)
    state = {"cash": 500, "positions": {SYM: 150}, "last_prices": {SYM: 10},
             "cost_basis": {SYM: 1500}, "last_trade_date": "2024-01-01"}
    result = quant.run_target_backtest(bars_by_symbol={SYM: bars}, targets_by_t={}, initial_state=state)
    assert result["final_state"]["positions"][SYM] == 150
    assert "corporate_action_fractional_share_settlement_missing" in result["quality"]["issues"]


def test_corporate_action_recovery_is_idempotent_and_preserves_blockers():
    bars = _bars(4)
    bars[2].update(o=9.5, h=9.6, l=9.4, c=9.5, corporate_action=_action(bars[2], cash=.5))
    bars[3].update(o=9.5, h=9.6, l=9.4, c=9.5)
    first = quant.run_target_backtest(bars_by_symbol={SYM: bars[:3]},
                                      targets_by_t={bars[0]["t"]: {SYM: .5}}, cost_model=ZERO_COST)
    resumed = quant.run_target_backtest(bars_by_symbol={SYM: bars[1:]}, targets_by_t={},
                                        initial_state=first["final_state"], cost_model=ZERO_COST)
    assert resumed["corporate_action_ledger"] == []
    assert resumed["final_state"]["cash"] == first["final_state"]["cash"]
    assert resumed["final_state"]["cash_distributions"][SYM] == 2500
    assert resumed["final_state"]["applied_corporate_actions"] == first["final_state"]["applied_corporate_actions"]
    assert resumed["final_equity"] == 100_000
    # A failed action cannot become tradable merely because the process restarted.
    broken_state = {**first["final_state"], "unresolved_corporate_actions": [SYM]}
    blocked = quant.run_target_backtest(bars_by_symbol={SYM: bars[2:]},
                                        targets_by_t={bars[2]["t"]: {}}, initial_state=broken_state)
    assert blocked["trades"] == []
    assert not blocked["quality"]["eligible"]


def test_duplicate_action_in_same_bar_is_not_paid_twice():
    bars = _bars(3)
    event = _action(bars[2], cash=.5)
    bars[2].update(o=9.5, h=9.6, l=9.4, c=9.5, corporate_action=[event, dict(event)])
    result = quant.run_target_backtest(bars_by_symbol={SYM: bars},
                                       targets_by_t={bars[0]["t"]: {SYM: .5}}, cost_model=ZERO_COST)
    assert result["performance"]["gross_dividend_income"] == 2500
    assert len(result["final_state"]["applied_corporate_actions"]) == 1


def test_restored_gross_dividend_account_remains_exploratory():
    bars = _bars(2)
    result = quant.run_target_backtest(bars_by_symbol={SYM: bars}, targets_by_t={},
        initial_state={"cash": 100_000, "cash_distributions": {SYM: 100}})
    assert not result["quality"]["eligible"]
    assert "cash_dividend_net_payment_unverified" in result["quality"]["issues"]


def test_null_status_and_marked_synthetic_bars_are_not_eligible():
    bars = _bars(2)
    for bar in bars:
        bar.update(tradable=None, suspended=None, synthetic=True)
    result = quant.run_target_backtest(bars_by_symbol={SYM: bars}, targets_by_t={})
    assert "historical_trading_status_missing" in result["quality"]["issues"]
    assert "example_or_synthetic_data" in result["quality"]["issues"]


def test_inferred_main_board_st_limits_respect_data_schema():
    upper, lower, inferred = trading_rules.price_limits(SYM, {"st": True, "pre_close": 10}, None, "2024-01-01")
    assert (upper, lower, inferred) == (10.5, 9.5, True)


@pytest.mark.parametrize("source_offset,available_offset,expected_issue", [
    (15 * 3600, 16 * 3600, None),
    (15 * 3600, 16 * 3600 + 30 * 60, None),
    (15 * 3600, 16 * 3600 + 30 * 60 + 1, "availability_after_decision_time"),
    (14 * 3600, 16 * 3600, "source_time_before_bar_close"),
    (14 * 3600, 14 * 3600 + 1, "daily_bar_available_before_close"),
])
def test_replay_quality_validates_explicit_close_availability(source_offset, available_offset, expected_issue):
    bars = _bars(2)
    for bar in bars:
        bar.update(source_time=bar["t"] + source_offset, available_at=bar["t"] + available_offset)
    result = quant.run_target_backtest(bars_by_symbol={SYM: bars}, targets_by_t={})
    assert result["quality"]["eligible"] is (expected_issue is None)
    if expected_issue:
        assert expected_issue in result["quality"]["issues"]


def test_inferred_arrival_allows_exploration_but_is_not_eligible():
    bars = _bars(2)
    for bar in bars:
        bar.update(availability_inferred=True, available_at=bar["t"] + 86400)
    result = quant.run_target_backtest(bars_by_symbol={SYM: bars}, targets_by_t={bars[0]["t"]: {SYM: .5}})
    assert result["trades"]
    assert "historical_availability_missing" in result["quality"]["issues"]
    assert not result["quality"]["eligible"]
