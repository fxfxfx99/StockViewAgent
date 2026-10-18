from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pytest
from pydantic import ValidationError

from app.schemas.research import ResearchExpr, StrategySpecV1
from app.services.research_strategy import (
    SHANGHAI, compute_indicators, compute_targets, equal_weight_targets,
    explain_strategy, required_warmup, strategy_logic_hash,
)


def field(name="c"):
    return {"op": "field", "field": name}


def constant(value):
    return {"op": "const", "value": value}


def spec(**updates):
    return StrategySpecV1.model_validate({"entry": {"op": "gt", "args": [field(), constant(0)]},
                                         "holding_days": 5, "rebalance_days": 1, **updates})


def bars(count=160):
    day = datetime(2024, 1, 1, tzinfo=SHANGHAI)
    rows = []
    while len(rows) < count:
        if day.weekday() < 5:
            i = len(rows)
            close = 30 + i * 0.07 + np.sin(i / 4)
            rows.append({"t": int(day.timestamp()), "o": close - .03, "h": close + .2,
                         "l": close - .2, "c": close, "v": 1_000_000 + i * 100})
        day += timedelta(days=1)
    return {"600000.SS": rows}


@pytest.mark.parametrize("expression", [
    {"op": "eval", "value": "__import__('os')"},
    {"op": "lag", "window": -1, "args": [field()]},
    {"op": "sma", "window": 253, "args": [field()]},
    {"op": "field", "field": "tomorrow_close"},
    {"op": "sma", "window": 2.5, "args": [field()]},
    {"op": "const", "value": float("nan")},
])
def test_rejects_unbounded_or_future_operators(expression):
    with pytest.raises(ValidationError):
        ResearchExpr.model_validate(expression)


def test_rejects_type_errors_and_excessive_depth():
    with pytest.raises(ValidationError):
        spec(entry=field())
    tree = field()
    for _ in range(8):
        tree = {"op": "neg", "args": [tree]}
    with pytest.raises(ValidationError, match="depth"):
        spec(entry={"op": "gt", "args": [tree, constant(0)]})


def test_logic_dedup_ignores_prose_and_commutative_order():
    left = {"op": "gt", "args": [field(), constant(10)]}
    right = {"op": "gt", "args": [field("v"), constant(100)]}
    one = spec(entry={"op": "and", "args": [left, right]}, hypothesis="first", name="A")
    two = spec(entry={"op": "and", "args": [right, left]}, hypothesis="reworded", name="B",
               parent_version_id="new", sources=["https://example.test"], applicability="different prose")
    assert strategy_logic_hash(one) == strategy_logic_hash(two)
    assert strategy_logic_hash(one) != strategy_logic_hash(spec(holding_days=6))


def test_targets_use_close_information_and_fixed_holding_expiry():
    data = bars(20)
    strategy = spec()
    targets = compute_targets(strategy, data)
    times = [row["t"] for row in data["600000.SS"]]
    assert targets[times[0]] == {"600000.SS": .2}
    assert targets[times[4]] == {"600000.SS": .2}
    assert targets[times[5]] == {}  # expiry cannot silently reenter same close
    assert targets[times[6]] == {"600000.SS": .2}
    changed = {symbol: [dict(row) for row in rows] for symbol, rows in data.items()}
    for row in changed["600000.SS"][10:]:
        row["c"] = 1_000_000
    assert {t: w for t, w in compute_targets(strategy, changed).items() if t <= times[9]} == {
        t: w for t, w in targets.items() if t <= times[9]}


def test_nested_warmup_and_missing_values_do_not_create_entries():
    indicator = {"op": "sma", "window": 5, "args": [{"op": "return", "window": 3, "args": [field()]}]}
    strategy = spec(entry={"op": "gt", "args": [indicator, constant(-100)]})
    assert required_warmup(strategy) == 7
    targets = compute_targets(strategy, bars(20))
    assert all(not weights for weights in list(targets.values())[:7])
    assert list(targets.values())[7]
    assert explain_strategy(strategy)["warmup_sessions"] == 7


def test_cross_sectional_rank_has_no_future_normalization_and_caps():
    data = bars(10)
    data["000001.SZ"] = [{**row, "c": row["c"] * 2} for row in data["600000.SS"]]
    strategy = spec(rank={"op": "rank", "args": [field()]}, max_positions=1)
    first = next(iter(compute_targets(strategy, data).values()))
    assert first == {"000001.SZ": .2}


def test_rsi_flat_series_is_neutral_and_atr_uses_previous_close():
    data = bars(30)
    for row in data["600000.SS"]:
        row.update(o=10, h=11, l=9, c=10)
    strategy = spec(rank={"op": "rsi", "window": 14, "args": [field()]})
    values = compute_indicators(strategy, data)["values"]["rank"]
    assert np.isnan(values[13, 0])
    assert values[14, 0] == 50
    atr = compute_indicators(spec(rank={"op": "atr", "window": 14}), data)["values"]["rank"]
    assert atr[14, 0] == 2


def test_equal_weight_benchmark_is_single_initial_allocation():
    data = bars(20)
    data["000001.SZ"] = data["600000.SS"]
    targets = equal_weight_targets(data)
    assert len(targets) == 1
    assert next(iter(targets.values())) == {"000001.SZ": .5, "600000.SS": .5}


def test_duplicate_session_rejected():
    data = bars(5)
    data["600000.SS"].append(data["600000.SS"][0])
    with pytest.raises(ValueError, match="duplicate"):
        compute_targets(spec(), data)


@pytest.mark.parametrize("field_name", ["o", "h", "l", "c", "v", "amount"])
def test_explicit_late_revision_masks_every_input_field(field_name):
    data = bars(8)
    row = data["600000.SS"][2]
    row["amount"] = 10000000
    row["available_at"] = datetime(2026, 1, 1, 16, tzinfo=SHANGHAI).timestamp()
    indicator = compute_indicators(spec(rank=field(field_name)), data)
    assert np.isnan(indicator["close"][2, 0])
    assert np.isnan(indicator["values"]["entry"][2, 0])
    assert np.isnan(indicator["values"]["rank"][2, 0])


@pytest.mark.parametrize("offset,available", [(15 * 3600, True), (16 * 3600 + 30 * 60, True),
                                             (16 * 3600 + 30 * 60 + 1, False), (15 * 3600 - 1, False)])
@pytest.mark.parametrize("encoding", ["unix", "iso"])
def test_declared_availability_obeys_shanghai_decision_window(offset, available, encoding):
    data = bars(1)
    row = data["600000.SS"][0]
    declared = row["t"] + offset
    row["available_at"] = declared if encoding == "unix" else datetime.fromtimestamp(declared, SHANGHAI).isoformat()
    row["source_time"] = row["t"] + 15 * 3600
    indicators = compute_indicators(spec(), data)
    assert bool(np.isfinite(indicators["close"][0, 0])) == available
    assert bool(compute_targets(spec(), data)[row["t"]]) == available


def test_late_publication_never_backfills_lag_or_rolling_history():
    data = bars(10)
    rows = data["600000.SS"]
    # The revision becomes available four sessions later. It must not enter its
    # original slot, even when the evaluator is handed the complete later data.
    rows[2]["available_at"] = rows[6]["t"] + 16 * 3600
    rolling = spec(rank={"op": "sma", "window": 3, "args": [field()]})
    values = compute_indicators(rolling, data)["values"]["rank"][:, 0]
    assert np.isnan(values[2:5]).all()
    assert np.isfinite(values[5:]).all()
    lag = compute_indicators(spec(rank={"op": "lag", "window": 1, "args": [field()]}), data)["values"]["rank"]
    assert np.isnan(lag[3, 0])
    original = compute_targets(rolling, data)
    rows[2].update(o=1, h=1e9, l=1, c=1e9, v=1e12)
    assert compute_targets(rolling, data) == original
    prefix = compute_indicators(rolling, {"600000.SS": rows[:5]})["values"]["rank"][:, 0]
    np.testing.assert_allclose(prefix, values[:5], equal_nan=True)


def test_equal_weight_skips_unavailable_initial_bar_for_every_symbol():
    data = bars(4)
    other = "000001.SZ"
    data[other] = [dict(row) for row in data["600000.SS"]]
    data["600000.SS"][0]["available_at"] = data["600000.SS"][0]["t"] + 17 * 3600
    data[other][1]["available_at"] = data[other][1]["t"] + 17 * 3600
    targets = equal_weight_targets(data)
    assert list(targets) == [data[other][2]["t"]]
    assert next(iter(targets.values())) == {other: .5, "600000.SS": .5}
    for row in data[other]:
        row["available_at"] = datetime(2026, 1, 1, 16, tzinfo=SHANGHAI).timestamp()
    assert equal_weight_targets(data) == {}


def test_missing_availability_remains_exploratory_without_exception():
    data = bars(3)
    rows = data["600000.SS"]
    rows[0]["available_at"] = None
    assert np.isfinite(compute_indicators(spec(), data)["close"]).all()
    assert compute_targets(spec(), data)[rows[0]["t"]] == {"600000.SS": .2}
    assert list(equal_weight_targets(data)) == [rows[0]["t"]]


def test_inferred_collection_time_is_exploratory_not_an_explicit_revision():
    data = bars(2)
    rows = data["600000.SS"]
    for row in rows:
        row["available_at"] = datetime(2026, 1, 1, 16, tzinfo=SHANGHAI).timestamp()
        row["availability_inferred"] = True
    assert np.isfinite(compute_indicators(spec(), data)["close"]).all()
    assert list(equal_weight_targets(data)) == [rows[0]["t"]]
    rows[0]["availability_inferred"] = False
    assert np.isnan(compute_indicators(spec(), data)["close"][0, 0])
    assert compute_targets(spec(), data)[rows[0]["t"]] == {}
    assert list(equal_weight_targets(data)) == [rows[1]["t"]]
