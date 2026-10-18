"""Paper orders must be committed before execution, never reconstructed later."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.schemas.research import StrategySpecV1
from app.services import research_paper as paper
from app.services.research_strategy import SHANGHAI, strategy_logic_hash
from app.storage import research_store as store

USER = 71
SYMBOL = "600000.SS"


def trading_days(count=90):
    day = datetime(2026, 1, 5, tzinfo=SHANGHAI)
    result = []
    while len(result) < count:
        if day.weekday() < 5:
            result.append(day)
        day += timedelta(days=1)
    return result


def bar(day, price=10.0, symbol=SYMBOL):
    return {"symbol": symbol, "t": int(day.timestamp()), "date": day.date().isoformat(),
            "o": price, "h": price + .1, "l": price - .1, "c": price, "v": 10_000_000,
            "price_basis": "raw", "volume_unit": "shares", "tradable": True,
            "no_price_limit": True, "corporate_actions_checked": True,
            "available_at": (day + timedelta(hours=15, minutes=1)).timestamp()}


@pytest.fixture
def world(monkeypatch):
    days = trading_days()
    clock = SimpleNamespace(now=(days[0] + timedelta(hours=16)).timestamp(), days=days, snapshots={})
    monkeypatch.setattr(paper, "time", SimpleNamespace(time=lambda: clock.now))

    def snapshot(name, rows, *, strict=True):
        record = {"id": name, "payload": {"strict": strict,
                  "quality": {"eligible": strict, "issues": [] if strict else ["incomplete"]},
                  "symbols": sorted({row["symbol"] for row in rows})}}
        clock.snapshots[name] = (record, copy.deepcopy(rows))
        store.set_state(USER, "latest_snapshot", name)
        return record

    def read(user_id, snapshot_id, *, as_of=None):
        assert user_id == USER
        rows = clock.snapshots[snapshot_id][1]
        return [copy.deepcopy(row) for row in rows if as_of is None or row["available_at"] <= as_of]

    monkeypatch.setattr(paper.research_data, "read_snapshot", read)
    monkeypatch.setattr(paper.research_data, "get_snapshot", lambda user_id, snapshot_id: clock.snapshots[snapshot_id][0])
    clock.add = snapshot
    clock.at_close = lambda index: setattr(clock, "now", (days[index] + timedelta(hours=16)).timestamp())
    clock.add("seed", [bar(days[0])])
    return clock


def new_track(world, track_id="test-track", *, kind="equal_weight", strategy_id=None):
    return paper._new_track(USER, track_id, name="test", symbols=[SYMBOL], snapshot_id="seed",
                            kind=kind, strategy_id=strategy_id)


def strategy_record(symbols=(SYMBOL,)):
    spec = StrategySpecV1.model_validate({"name": "test strategy", "universe": symbols,
         "entry": {"op": "gt", "args": [{"op": "field", "field": "c"}, {"op": "const", "value": 0}]},
         "holding_days": 5, "rebalance_days": 1})
    return store.put_entity(USER, "strategy", {"spec": spec.model_dump(mode="json")}, strategy_logic_hash(spec))


def test_offline_gap_does_not_reconstruct_historical_orders(world):
    track = new_track(world)
    world.at_close(10)
    gap = world.add("gap", [bar(day) for day in world.days[:11]])
    state = paper._advance(USER, track, gap, world.now)
    assert state["trades"] == []
    assert len(state["equity_curve"]) == 10
    assert state["pending"]["committed_at"] == world.now
    world.at_close(11)
    next_day = world.add("next", [bar(day) for day in world.days[:12]])
    state = paper._advance(USER, track, next_day, world.now)
    assert len(state["trades"]) == 1
    assert state["trades"][0]["t"] == int(world.days[11].timestamp())
    assert state["engine"]["cash"] >= 0
    latest = state["equity_curve"][-1]
    assert latest["equity"] == pytest.approx(state["engine"]["cash"] + state["engine"]["positions"][SYMBOL] * 10, abs=1e-5)


def test_same_day_retry_and_event_pointer_crash_do_not_trade_twice(world):
    track = new_track(world)
    seed = world.snapshots["seed"][0]
    before = paper._advance(USER, track, seed, world.now)
    assert before["pending"]
    world.at_close(1)
    snapshot = world.add("next", [bar(day) for day in world.days[:2]])
    after = paper._advance(USER, track, snapshot, world.now)
    assert len(after["trades"]) == 1
    assert paper._advance(USER, track, snapshot, world.now) == after
    events = store.list_entities(USER, "paper_event")
    # Simulate process loss after the durable event but before its state pointer.
    store.set_state(USER, f"paper:{track['id']}", before)
    world.now += 10
    recovered = paper._advance(USER, track, snapshot, world.now)
    assert recovered == after
    assert len(store.list_entities(USER, "paper_event")) == len(events)


def test_instruction_committed_after_open_cannot_fill_that_open(world):
    track = new_track(world)
    world.now = (world.days[1] + timedelta(hours=10)).timestamp()
    state = paper._advance(USER, track, world.snapshots["seed"][0], world.now)
    assert state["pending"]["committed_at"] == world.now
    world.at_close(1)
    one = world.add("one", [bar(day) for day in world.days[:2]])
    state = paper._advance(USER, track, one, world.now)
    assert state["trades"] == []
    world.at_close(2)
    two = world.add("two", [bar(day) for day in world.days[:3]])
    state = paper._advance(USER, track, two, world.now)
    assert state["trades"][0]["t"] == int(world.days[2].timestamp())


def test_old_seed_snapshot_does_not_add_prelaunch_forward_sessions(world):
    world.at_close(35)
    track = new_track(world)
    current = world.add("current", [bar(day) for day in world.days[:36]])
    state = paper._advance(USER, track, current, world.now)
    assert state["equity_curve"] == []
    assert state["trades"] == []
    world.at_close(36)
    next_day = world.add("next", [bar(day) for day in world.days[:37]])
    state = paper._advance(USER, track, next_day, world.now)
    assert len(state["equity_curve"]) == 1
    assert state["equity_curve"][0]["t"] == int(world.days[36].timestamp())


def test_non_strict_snapshot_keeps_forward_results_ineligible(world):
    track = new_track(world)
    paper._advance(USER, track, world.snapshots["seed"][0], world.now)
    world.at_close(1)
    weak = world.add("weak", [bar(day) for day in world.days[:2]], strict=False)
    state = paper._advance(USER, track, weak, world.now)
    assert "snapshot_not_strict" in state["quality_issues"]
    assert paper._report(track, state)["quality"]["eligible"] is False


def test_non_strict_decision_remains_ineligible_when_execution_snapshot_is_strict(world):
    weak = world.add("weak-seed", [bar(world.days[0])], strict=False)
    track = paper._new_track(USER, "weak-decision", name="weak", symbols=[SYMBOL], snapshot_id=weak["id"], kind="equal_weight")
    committed = paper._advance(USER, track, weak, world.now)
    assert "snapshot_not_strict" in committed["quality_issues"]
    world.at_close(1)
    strict = world.add("strong-next", [bar(day) for day in world.days[:2]])
    state = paper._advance(USER, track, strict, world.now)
    assert not paper._report(track, state)["quality"]["eligible"]


def test_risk_stop_commits_liquidation_for_next_open_and_preserves_cash(world):
    store.update_config(USER, {"max_symbol_weight": 1.0})
    track = new_track(world)
    paper._advance(USER, track, world.snapshots["seed"][0], world.now)
    world.at_close(1)
    entry = world.add("entry", [bar(day) for day in world.days[:2]])
    state = paper._advance(USER, track, entry, world.now)
    assert state["engine"]["positions"][SYMBOL] > 0
    world.at_close(2)
    drop_rows = [bar(world.days[0]), bar(world.days[1]), bar(world.days[2], 5)]
    drop = world.add("drop", drop_rows)
    stopped = paper._advance(USER, track, drop, world.now)
    assert stopped["risk_stopped"]
    assert stopped["status"] == "liquidating"
    assert stopped["pending"]["targets"] == {}
    assert stopped["engine"]["positions"][SYMBOL] > 0  # no fictional same-close sale
    world.at_close(3)
    exit_day = world.add("exit", drop_rows + [bar(world.days[3], 5)])
    liquidated = paper._advance(USER, track, exit_day, world.now)
    assert not any(liquidated["engine"]["positions"].values())
    assert liquidated["status"] == "cash_observation"
    assert liquidated["engine"]["cash"] > 0
    assert liquidated["equity_curve"][-1]["equity"] == pytest.approx(liquidated["engine"]["cash"], abs=1e-5)
    paper._advance(USER, track, exit_day, world.now)
    audit = [e for e in store.list_entities(USER, "promotion") if e["payload"].get("status") == "risk_stopped"]
    assert len(audit) == 1
    assert audit[0]["payload"]["track_id"] == track["id"]
    assert audit[0]["payload"]["max_drawdown_pct"] > audit[0]["payload"]["threshold_pct"] == 20


@pytest.mark.parametrize("include_stale_history", [True, False])
def test_risk_stop_can_commit_exit_when_unheld_pool_symbol_is_stale(world, include_stale_history):
    stale_symbol = "600001.SS"
    world.add("seed", [bar(world.days[0]), bar(world.days[0], symbol=stale_symbol)])
    track = paper._new_track(USER, "stale-pool-stop", name="stop", symbols=[SYMBOL, stale_symbol],
                            snapshot_id="seed", kind="equal_weight")
    state = paper._load_state(USER, track)
    state["engine"].update(cash=40000, positions={SYMBOL: 5000, stale_symbol: 0},
                          available={SYMBOL: 5000, stale_symbol: 0},
                          cost_basis={SYMBOL: 50000, stale_symbol: 0},
                          last_prices={SYMBOL: 10, stale_symbol: 10})
    state.update(risk_stopped=True, status="liquidating", last_signal_t=int(world.days[0].timestamp()))
    store.set_state(USER, f"paper:{track['id']}", state)
    world.at_close(1)
    rows = [bar(world.days[0]), bar(world.days[1], 5)]
    if include_stale_history:
        rows.append(bar(world.days[0], symbol=stale_symbol))
    snapshot = world.add("stale-current", rows)
    state = paper._advance(USER, track, snapshot, world.now)
    assert state["pending"] is not None
    assert state["pending"]["targets"] == {}
    assert state["pending"]["committed_at"] == world.now


def test_sixty_days_alone_never_promote_without_completed_trades(world):
    record = strategy_record()
    group = paper.start_shadow(USER, record, "seed", experiment_id="experiment-one")
    active = store.get_state(USER, "paper_active")
    world.at_close(70)
    world.add("later", [bar(day) for day in world.days[:71]])
    paper.update_paper(USER, "later")
    status = store.get_state(USER, f"shadow:{group['id']}")
    assert status["status"] == "observing"
    assert status["evaluation"]["metrics"]["sessions"] >= 60
    assert not status["evaluation"]["gates"]["minimum_completed_trades"]
    assert store.get_state(USER, "paper_active") == active


def test_rollback_preserves_current_assets_instead_of_old_track_wealth(world):
    previous = new_track(world, "previous")
    active = new_track(world, "active")
    old_state = paper._load_state(USER, previous)
    old_state["engine"]["cash"] = 900000
    store.set_state(USER, "paper:previous", old_state)
    active_state = paper._load_state(USER, active)
    active_state["engine"].update(cash=80000, positions={SYMBOL: 2000}, available={SYMBOL: 2000},
                                  cost_basis={SYMBOL: 20000}, last_prices={SYMBOL: 10})
    active_state["pending"] = {"targets": {SYMBOL: .2}, "committed_at": world.now}
    store.set_state(USER, "paper:active", active_state)
    store.set_state(USER, "paper_active", {"track_id": "active", "strategy_id": None, "since": world.now})
    store.set_state(USER, "paper_previous", {"track_id": "previous", "strategy_id": None, "since": world.now - 1})
    decision = paper.rollback(USER)
    rolled = store.get_state(USER, f"paper:{decision['new']['track_id']}")
    assert rolled["engine"] == active_state["engine"]
    assert rolled["equity_curve"] == active_state["equity_curve"]
    assert rolled["pending"] is None
    assert rolled["engine"]["cash"] != old_state["engine"]["cash"]


def test_shadow_must_outperform_both_incumbent_and_baseline(world, monkeypatch):
    record = strategy_record()
    group = paper.start_shadow(USER, record, "seed", experiment_id="comparison")
    original = store.get_state(USER, "paper_active")
    def fake_report(track, state):
        role = track["id"].rsplit(":", 1)[-1]
        ret = {"candidate": 5, "incumbent": 8, "baseline": 3}[role]
        return {"performance": {"total_return_pct": ret, "annualized_return_pct": ret * 3,
                "max_drawdown_pct": 2, "sharpe": 1, "completed_trade_count": 20, "turnover_pct": 100},
                "quality": {"eligible": True}, "equity_curve": [{"t": i, "equity": 100000 + i} for i in range(60)]}
    monkeypatch.setattr(paper, "_report", fake_report)
    paper._maybe_promote(USER, group)
    decision = store.get_state(USER, f"shadow:{group['id']}")
    assert not decision["evaluation"]["gates"]["beats_incumbent_return"]
    assert decision["status"] == "observing"
    assert store.get_state(USER, "paper_active") == original


def test_changed_pool_rebinds_incumbent_rules_to_comparison_pool(world):
    old_record = strategy_record()
    old_track = new_track(world, "old-incumbent", kind="strategy", strategy_id=old_record["id"])
    store.set_state(USER, "paper_active", {"track_id": old_track["id"], "strategy_id": old_record["id"], "since": world.now})
    new_symbol = "000001.SZ"
    new_record = strategy_record((new_symbol,))
    snapshot = world.add("new-pool", [bar(world.days[0]), bar(world.days[0], symbol=new_symbol)])
    group = paper.start_shadow(USER, new_record, snapshot["id"], experiment_id="pool-change")
    incumbent = paper._track(USER, group["payload"]["tracks"]["incumbent"])
    state = paper._advance(USER, incumbent, snapshot, world.now)
    assert state["pending"] is not None
    assert set(state["pending"]["targets"]) <= set(incumbent["payload"]["symbols"])


def test_promotion_recovers_crash_after_active_pointer_commit(world, monkeypatch):
    record = strategy_record()
    group = paper.start_shadow(USER, record, "seed", experiment_id="promotion-crash")
    previous = store.get_state(USER, "paper_active")
    def winning_report(track, state):
        role = track["id"].rsplit(":", 1)[-1]
        ret = {"candidate": 10, "incumbent": 3, "baseline": 4}[role]
        return {"performance": {"total_return_pct": ret, "annualized_return_pct": ret * 3,
                "max_drawdown_pct": 2, "sharpe": 1, "completed_trade_count": 20, "turnover_pct": 100},
                "quality": {"eligible": True}, "equity_curve": [{"t": i, "equity": 100000 + i} for i in range(60)]}
    monkeypatch.setattr(paper, "_report", winning_report)
    real_set = store.set_state
    def interrupted_set(user_id, key, value):
        if key == "paper_previous":
            raise RuntimeError("simulated process interruption")
        return real_set(user_id, key, value)
    monkeypatch.setattr(store, "set_state", interrupted_set)
    with pytest.raises(RuntimeError, match="simulated"):
        paper._maybe_promote(USER, group)
    committed = store.get_state(USER, "paper_active")
    assert committed != previous
    monkeypatch.setattr(store, "set_state", real_set)
    paper._maybe_promote(USER, group)
    assert store.get_state(USER, "paper_active") == committed
    assert store.get_state(USER, "paper_previous") == previous
    assert store.get_state(USER, f"shadow:{group['id']}")["status"] == "promoted"
    events = [e for e in store.list_entities(USER, "promotion") if e["id"] == f"forward:{group['id']}"]
    assert len(events) == 1


def winning_group(world, monkeypatch, *, experiment_id="asset-transfer", symbols=(SYMBOL,)):
    record = strategy_record(symbols)
    group = paper.start_shadow(USER, record, store.get_state(USER, "latest_snapshot"), experiment_id=experiment_id)
    monkeypatch.setattr(paper.research_evaluation, "evaluate_forward", lambda *args: {"eligible": True, "gates": {}})
    return group


def test_promotion_inherits_live_assets_and_only_buys_from_candidate_pool(world, monkeypatch):
    incumbent = paper.ensure_baseline(USER, "seed")
    previous = store.get_state(USER, "paper_active")
    live = paper._load_state(USER, incumbent)
    live["engine"].update(cash=80000, positions={SYMBOL: 2000}, available={SYMBOL: 2000},
                          cost_basis={SYMBOL: 20000}, last_prices={SYMBOL: 10},
                          applied_corporate_actions=["previously-applied-action"], cash_distributions={SYMBOL: 123})
    live.update(equity_curve=[{"t": int(world.days[0].timestamp()), "equity": 100000}],
                rejected_orders=[{"reason": "old-rejection"}], quality_issues=["old-quality-issue"],
                pending={"targets": {SYMBOL: .2}, "committed_at": world.now}, last_signal_t=int(world.days[0].timestamp()))
    store.set_state(USER, f"paper:{incumbent['id']}", live)
    new_symbol = "000001.SZ"
    snapshot = world.add("both", [bar(world.days[0]), bar(world.days[0], symbol=new_symbol)])
    group = winning_group(world, monkeypatch, symbols=(new_symbol,))
    candidate_id = group["payload"]["tracks"]["candidate"]
    candidate = paper._track(USER, candidate_id)
    shadow = paper._load_state(USER, candidate)
    shadow["engine"]["cash"] = 900000
    store.set_state(USER, f"paper:{candidate_id}", shadow)

    paper._maybe_promote(USER, group)
    pointer = store.get_state(USER, "paper_active")
    assert pointer["track_id"] == f"active:forward:{group['id']}"
    assert pointer["track_id"] != candidate_id
    promoted = paper._track(USER, pointer["track_id"])
    inherited = paper._load_state(USER, promoted)
    assert inherited["engine"] == live["engine"]
    assert inherited["equity_curve"] == live["equity_curve"]
    assert inherited["trades"] == live["trades"]
    assert inherited["rejected_orders"] == live["rejected_orders"]
    assert inherited["quality_issues"] == live["quality_issues"]
    assert inherited["pending"] is None
    assert inherited["last_signal_t"] is None
    assert promoted["payload"]["initial_cash"] == incumbent["payload"]["initial_cash"]
    assert set(promoted["payload"]["symbols"]) == {SYMBOL, new_symbol}
    assert promoted["payload"]["execution_spec"]["universe"] == [new_symbol]
    assert set(paper.required_symbols(USER)) == {SYMBOL, new_symbol}
    assert store.get_state(USER, "paper_previous") == previous
    assert store.get_state(USER, f"paper:{candidate_id}") == shadow

    committed = paper._advance(USER, promoted, snapshot, world.now)
    assert set(committed["pending"]["targets"]) == {new_symbol}
    world.at_close(1)
    next_day = world.add("both-next", [bar(day, symbol=symbol) for day in world.days[:2] for symbol in (SYMBOL, new_symbol)])
    executed = paper._advance(USER, promoted, next_day, world.now)
    assert executed["engine"]["positions"][SYMBOL] == 0
    assert executed["engine"]["positions"][new_symbol] > 0
    assert not any(t["side"] == "buy" and t["symbol"] == SYMBOL for t in executed["trades"])
    assert executed["engine"]["applied_corporate_actions"] == ["previously-applied-action"]
    assert executed["equity_curve"][-1]["equity"] == pytest.approx(
        executed["engine"]["cash"] + sum(executed["engine"]["positions"].values()) * 10, abs=1e-5)
    assert executed["equity_curve"][-1]["equity"] < 100000  # only actual execution costs change wealth


def test_promotion_preserves_existing_risk_stop(world, monkeypatch):
    group = winning_group(world, monkeypatch, experiment_id="stopped-account")
    active = store.get_state(USER, "paper_active")
    original = paper._load_state(USER, paper._track(USER, active["track_id"]))
    original.update(risk_stopped=True, status="cash_observation", risk_drawdown_pct=25)
    store.set_state(USER, f"paper:{active['track_id']}", original)
    paper._maybe_promote(USER, group)
    promoted = paper._track(USER, store.get_state(USER, "paper_active")["track_id"])
    state = paper._load_state(USER, promoted)
    assert state["risk_stopped"] and state["status"] == "cash_observation"
    state = paper._advance(USER, promoted, world.snapshots["seed"][0], world.now)
    assert state["pending"]["targets"] == {}


def test_promotion_recovers_crash_before_intent_without_duplicating_transfer(world, monkeypatch):
    group = winning_group(world, monkeypatch, experiment_id="intent-crash")
    previous = store.get_state(USER, "paper_active")
    real_put = store.put_entity
    def interrupted_put(user_id, kind, payload, entity_id=None):
        if kind == "promotion":
            raise RuntimeError("intent interrupted")
        return real_put(user_id, kind, payload, entity_id)
    monkeypatch.setattr(store, "put_entity", interrupted_put)
    with pytest.raises(RuntimeError, match="intent interrupted"):
        paper._maybe_promote(USER, group)
    assert store.get_state(USER, "paper_active") == previous
    transfers = store.list_entities(USER, "paper_transfer")
    assert len(transfers) == 1
    frozen_state = store.get_state(USER, f"paper:{transfers[0]['payload']['track_id']}")
    monkeypatch.setattr(store, "put_entity", real_put)
    world.now += 1
    paper._maybe_promote(USER, group)
    assert len(store.list_entities(USER, "paper_transfer")) == 1
    assert store.get_state(USER, f"paper:{transfers[0]['payload']['track_id']}") == frozen_state
    assert store.get_state(USER, "paper_previous") == previous
    assert store.get_state(USER, f"shadow:{group['id']}")["status"] == "promoted"


def test_promotion_recovery_never_discards_intervening_live_settlement(world, monkeypatch):
    group = winning_group(world, monkeypatch, experiment_id="advanced-ledger")
    previous = store.get_state(USER, "paper_active")
    real_cas = store.compare_and_set_state
    def interrupted_cas(user_id, key, before, after):
        if key == "paper_active":
            raise RuntimeError("pointer interrupted")
        return real_cas(user_id, key, before, after)
    monkeypatch.setattr(store, "compare_and_set_state", interrupted_cas)
    with pytest.raises(RuntimeError, match="pointer interrupted"):
        paper._maybe_promote(USER, group)
    live = store.get_state(USER, f"paper:{previous['track_id']}")
    live["engine"]["cash"] += 10
    store.set_state(USER, f"paper:{previous['track_id']}", live)
    monkeypatch.setattr(store, "compare_and_set_state", real_cas)
    paper._maybe_promote(USER, group)
    assert store.get_state(USER, "paper_active") == previous
    assert store.get_state(USER, f"paper:{previous['track_id']}") == live
    assert store.get_state(USER, f"shadow:{group['id']}")["status"] == "superseded"
    displayed_event = next(e for e in paper.paper_summary(USER)["events"] if e["id"] == f"forward:{group['id']}")
    assert displayed_event["payload"]["status"] == "superseded"
    assert displayed_event["payload"]["reason"] == "source_ledger_changed"


@pytest.mark.parametrize("crash_after_active", [False, True])
def test_same_rollback_job_never_switches_twice(world, monkeypatch, crash_after_active):
    previous = new_track(world, "previous-rules")
    active = new_track(world, "current-account")
    paper._load_state(USER, previous)
    live = paper._load_state(USER, active)
    live["engine"]["cash"] = 123456
    store.set_state(USER, f"paper:{active['id']}", live)
    original = {"track_id": active["id"], "strategy_id": None, "since": world.now}
    store.set_state(USER, "paper_active", original)
    store.set_state(USER, "paper_previous", {"track_id": previous["id"], "strategy_id": None, "since": world.now - 1})
    payload = {"job_id": "durable-rollback-job"}
    real_set = store.set_state
    if crash_after_active:
        def interrupted_set(user_id, key, value):
            if key == "paper_previous":
                raise RuntimeError("rollback interrupted")
            return real_set(user_id, key, value)
        monkeypatch.setattr(store, "set_state", interrupted_set)
        with pytest.raises(RuntimeError, match="rollback interrupted"):
            paper.rollback(USER, payload)
        monkeypatch.setattr(store, "set_state", real_set)
    decision = paper.rollback(USER, payload)
    committed = store.get_state(USER, "paper_active")
    assert committed == decision["new"]
    assert store.get_state(USER, "paper_previous") == original
    assert paper.rollback(USER, payload) == decision
    assert store.get_state(USER, "paper_active") == committed
    assert store.get_state(USER, "paper_previous") == original
    assert len(store.list_entities(USER, "paper_transfer")) == 1
    assert len(store.list_entities(USER, "promotion")) == 1
    # A delayed retry after another operation must not restore old pointers.
    later = {"track_id": "later-account", "strategy_id": "later", "since": world.now + 1}
    store.set_state(USER, "paper_active", later)
    store.set_state(USER, "paper_previous", committed)
    assert paper.rollback(USER, payload) == decision
    assert store.get_state(USER, "paper_active") == later
    assert store.get_state(USER, "paper_previous") == committed
