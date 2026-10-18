"""Forward paper ledger: only previously recorded instructions can be filled.

Historical data may be backfilled, but a missing historical decision is never
recreated as a forward order. Each day is an immutable event plus a recoverable
state pointer; retries apply the same event rather than trading twice.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, time as daytime
import time

from app.services import research_data, research_evaluation, research_strategy
from app.services.market_time import SHANGHAI_TZ, market_date
from app.storage import research_store as store


def _bars(user_id, snapshot_id, symbols):
    grouped = {s: [] for s in symbols}
    for row in research_data.read_snapshot(user_id, snapshot_id, as_of=time.time()):
        if row["symbol"] in grouped:
            grouped[row["symbol"]].append(row)
    return grouped


def _track(user_id, track_id):
    return store.get_entity(user_id, "paper_track", track_id)


def _new_track(user_id, track_id, *, name, symbols, snapshot_id, strategy_id=None, kind="strategy", started_at=None, initial_cash=None,
               max_position_pct=None, max_symbol_weight=None, rule_symbols=None, execution_spec=None):
    previous = _track(user_id, track_id)
    if previous:
        return previous
    config = store.get_config(user_id)
    bars = _bars(user_id, snapshot_id, symbols)
    timestamps = sorted({int(b["t"]) for rows in bars.values() for b in rows})
    if not timestamps:
        raise ValueError("模拟账户需要已有历史行情")
    started_at = time.time() if started_at is None else started_at
    caps = {"max_position_pct": config["max_position_pct"] if max_position_pct is None else max_position_pct,
            "max_symbol_weight": config["max_symbol_weight"] if max_symbol_weight is None else max_symbol_weight}
    rule_symbols = sorted(symbols if rule_symbols is None else rule_symbols)
    if not rule_symbols or not set(rule_symbols) <= set(symbols):
        raise ValueError("策略选股池必须包含于执行行情池")
    frozen_spec = None
    if strategy_id:
        original = store.get_entity(user_id, "strategy", strategy_id)
        if not original:
            raise ValueError("模拟策略版本不存在")
        frozen_spec = {**(execution_spec or original["payload"]["spec"]), "universe": rule_symbols, **caps}
    payload = {"name": name, "symbols": sorted(symbols), "strategy_id": strategy_id, "kind": kind,
               "rule_symbols": rule_symbols,
               "seed_snapshot_id": snapshot_id, "started_at": started_at, "decision_start": timestamps[-1],
               "initial_cash": config["initial_cash"] if initial_cash is None else initial_cash,
               "execution_spec": frozen_spec, **caps}
    record = store.put_entity(user_id, "paper_track", payload, track_id)
    return record


def _initial_state(track):
    p = track["payload"]
    return {"engine": {"cash": p["initial_cash"], "positions": {}, "available": {}, "cost_basis": {},
                       "last_prices": {}, "last_trade_date": market_date(p["started_at"]).isoformat()},
            "equity_curve": [], "trades": [], "rejected_orders": [], "quality_issues": [],
            "pending": None, "last_signal_t": None, "risk_stopped": False, "status": "observing", "updated_at": p["started_at"]}


def _load_state(user_id, track):
    key = f"paper:{track['id']}"
    state = store.get_state(user_id, key)
    if state is None:
        state = _initial_state(track)
        store.compare_and_set_state(user_id, key, None, state)
        state = store.get_state(user_id, key)
    return state


def _report(track, state):
    from app.services.quant_backtest import _performance
    curve, trades = state["equity_curve"], state["trades"]
    return {"initial_cash": track["payload"]["initial_cash"], "equity_curve": curve, "trades": trades,
            "performance": _performance(curve, trades, track["payload"]["initial_cash"]),
            "quality": {"eligible": bool(curve) and not state["quality_issues"], "issues": state["quality_issues"]}}


def _record_state_event(user_id, track, event_id, before, after, extra):
    key = f"paper:{track['id']}"
    old_event = store.get_entity(user_id, "paper_event", event_id)
    if old_event:
        event = old_event["payload"]
        if store.content_hash(before) == event["before_hash"]:
            store.compare_and_set_state(user_id, key, before, event["after"])
        return store.get_state(user_id, key)
    store.put_entity(user_id, "paper_event", {"track_id": track["id"], "before_hash": store.content_hash(before), "after": after, **extra}, event_id)
    if not store.compare_and_set_state(user_id, key, before, after):
        raise RuntimeError("Paper state changed while committing its immutable event")
    return after


def _targets_at_close(user_id, track, bars, last_t, state):
    p = track["payload"]
    if state["risk_stopped"] or p["kind"] == "cash":
        return {}
    if p["kind"] == "equal_weight":
        if state["last_signal_t"] is not None:
            return None
        symbols = p.get("rule_symbols", p["symbols"])
        weight = min(p["max_symbol_weight"], p["max_position_pct"] / len(symbols))
        return {s: weight for s in symbols}
    record = store.get_entity(user_id, "strategy", p["strategy_id"])
    if not record:
        raise ValueError("Missing frozen paper strategy")
    targets = research_strategy.compute_targets(p.get("execution_spec") or record["payload"]["spec"], bars, start_at=p["decision_start"])
    return targets.get(last_t)


def _advance(user_id, track, snapshot, now):
    from app.services.quant_backtest import run_target_backtest
    p = track["payload"]
    bars = _bars(user_id, snapshot["id"], p["symbols"])
    state = _load_state(user_id, track)
    if not any(bars.values()):
        return state
    timestamps = sorted({int(b["t"]) for rows in bars.values() for b in rows})
    today = datetime.fromtimestamp(now, SHANGHAI_TZ)
    for timestamp in timestamps:
        day = market_date(timestamp)
        close_at = datetime.combine(day, daytime(15), tzinfo=SHANGHAI_TZ).timestamp()
        if close_at > now or close_at <= p["started_at"] or day.isoformat() <= (state["engine"].get("last_trade_date") or ""):
            continue
        event_id = f"day:{track['id']}:{day.isoformat()}"
        existing = store.get_entity(user_id, "paper_event", event_id)
        if existing:
            state = _record_state_event(user_id, track, event_id, state, existing["payload"]["after"], {})
            continue
        pending = state["pending"]
        open_at = datetime.combine(day, daytime(9, 30), tzinfo=SHANGHAI_TZ).timestamp()
        execution = {}
        if pending and pending["committed_at"] < open_at:
            execution[timestamp] = pending["targets"]
        # Include preceding rows solely for capacity/context. Unknown data is never
        # turned into a fictional transaction, and missing decisions remain cash.
        bounded = {s: [b for b in rows if int(b["t"]) <= timestamp][-2:] for s, rows in bars.items()}
        result = run_target_backtest(bars_by_symbol=bounded, targets_by_t={},
            precommitted_targets_by_t=execution, initial_state=state["engine"],
            initial_cash=p["initial_cash"], evaluation_start=timestamp, evaluation_end=timestamp,
            max_position_pct=p["max_position_pct"], max_symbol_weight=p["max_symbol_weight"])
        issues = set(state["quality_issues"]) | set(result["quality"]["issues"])
        if not snapshot["payload"].get("strict"):
            issues.add("snapshot_not_strict")
        after = {**state, "engine": result["final_state"],
                 "equity_curve": state["equity_curve"] + result["equity_curve"],
                 "trades": state["trades"] + result["trades"],
                 "rejected_orders": state["rejected_orders"] + result["rejected_orders"],
                 "quality_issues": sorted(issues), "pending": None if execution else pending,
                 "updated_at": now}
        perf = _report(track, after)["performance"]
        if perf.get("max_drawdown_pct", 0) > 20:
            after["risk_stopped"] = True
            after["status"] = "liquidating"
            after.setdefault("risk_started_day", day.isoformat())
            after.setdefault("risk_drawdown_pct", perf["max_drawdown_pct"])
        if after["risk_stopped"] and not any(after["engine"]["positions"].values()):
            after["status"] = "cash_observation"
        state = _record_state_event(user_id, track, event_id, state, after,
            {"kind": "settlement", "snapshot_id": snapshot["id"], "trade_date": day.isoformat(),
             "execution": execution, "decision_recorded_at": pending["committed_at"] if execution else None,
             "replayed_missing_decision": False})
    last_t = timestamps[-1]
    last_day = market_date(last_t)
    closed = datetime.combine(last_day, daytime(15), tzinfo=SHANGHAI_TZ).timestamp() <= now
    # No intraday, ancient/stale, partially observed or already committed signal.
    complete_close = all(any(int(b["t"]) == last_t and float(b.get("available_at") or now) <= now for b in bars[s])
                         for s in p.get("rule_symbols", p["symbols"]))
    if state["risk_stopped"] and not store.get_entity(user_id, "promotion", f"risk:{track['id']}"):
        store.put_entity(user_id, "promotion", {"status": "risk_stopped", "track_id": track["id"],
            "strategy_id": p["strategy_id"], "trade_date": state.get("risk_started_day"),
            "max_drawdown_pct": state.get("risk_drawdown_pct"), "threshold_pct": 20,
            "reason": "回撤超过20%，停止新开仓，按后续可成交条件退出并进入现金观察"}, f"risk:{track['id']}")
    if closed and (complete_close or state["risk_stopped"]) and 0 <= (today.date() - last_day).days <= 4 and state["last_signal_t"] != last_t:
        targets = _targets_at_close(user_id, track, bars, last_t, state)
        after = {**state, "last_signal_t": last_t, "updated_at": now}
        if not snapshot["payload"].get("strict"):
            after["quality_issues"] = sorted(set(state["quality_issues"]) | {"snapshot_not_strict"})
        if targets is not None:
            after["pending"] = {"targets": targets, "signal_t": last_t, "committed_at": now, "snapshot_id": snapshot["id"]}
        state = _record_state_event(user_id, track, f"signal:{track['id']}:{last_t}", state, after,
            {"kind": "forward_signal", "snapshot_id": snapshot["id"], "committed_at": now, "targets": targets})
    return state


def ensure_baseline(user_id, snapshot_id):
    pointer = store.get_state(user_id, "paper_active")
    if pointer:
        return _track(user_id, pointer["track_id"])
    snapshot = research_data.get_snapshot(user_id, snapshot_id)
    pool_id = snapshot["payload"].get("pool_version_id")
    pool = store.get_entity(user_id, "pool_version", pool_id) if pool_id else None
    symbols = (pool or {}).get("payload", {}).get("symbols") or snapshot["payload"]["symbols"]
    track_id = f"baseline:{pool_id or snapshot_id}"
    track = _new_track(user_id, track_id, name="同池等权基线", symbols=symbols, snapshot_id=snapshot_id, kind="equal_weight")
    _load_state(user_id, track)
    store.compare_and_set_state(user_id, "paper_active", None, {"track_id": track_id, "strategy_id": None, "since": track["created_at"]})
    return track


def start_shadow(user_id, record, snapshot_id, *, experiment_id):
    group_id = f"shadow:{experiment_id}"
    existing = store.get_entity(user_id, "paper_group", group_id)
    if existing:
        return existing
    # The campaign's seed fixes validation, not the start of forward observation.
    # Use the latest available snapshot for current decisions and indicators.
    latest = store.get_state(user_id, "latest_snapshot")
    if latest:
        latest_record = research_data.get_snapshot(user_id, latest)
        required = set(record["payload"]["spec"].get("universe") or [])
        if required <= set(latest_record["payload"]["symbols"]):
            snapshot_id = latest
    incumbent = ensure_baseline(user_id, snapshot_id)
    snapshot = research_data.get_snapshot(user_id, snapshot_id)
    symbols = record["payload"]["spec"].get("universe") or snapshot["payload"]["symbols"]
    now = time.time()
    initial_cash = store.get_config(user_id)["initial_cash"]
    tracks = {}
    for role, kind, sid, name in (
        ("candidate", "strategy", record["id"], record["payload"]["spec"]["name"]),
        ("incumbent", incumbent["payload"]["kind"], incumbent["payload"]["strategy_id"], "同期现行策略"),
        ("baseline", "equal_weight", None, "同期等权基线"),
    ):
        track_id = f"{group_id}:{role}"
        _new_track(user_id, track_id, name=name, symbols=symbols, snapshot_id=snapshot_id,
                   strategy_id=sid, kind=kind, started_at=now, initial_cash=initial_cash,
                   max_position_pct=record["payload"]["spec"]["max_position_pct"],
                   max_symbol_weight=record["payload"]["spec"]["max_symbol_weight"])
        tracks[role] = track_id
    group = store.put_entity(user_id, "paper_group", {"strategy_id": record["id"], "name": record["payload"]["spec"]["name"],
        "experiment_id": experiment_id, "tracks": tracks, "started_at": now, "incumbent_id": incumbent["id"]}, group_id)
    store.set_state(user_id, f"shadow:{group_id}", {"status": "observing", "evaluation": None})
    return group


def _prepare_transfer(user_id, transition_id, active, rule_track, *, name):
    """Freeze the real account once; a shadow supplies rules, never assets."""
    saved = store.get_entity(user_id, "paper_transfer", transition_id)
    if saved is None:
        source_track = _track(user_id, active["track_id"])
        source_state = _load_state(user_id, source_track)
        rules = rule_track["payload"]
        rule_symbols = rules.get("rule_symbols", rules["symbols"])
        # Keep even zero positions: engine state keys must remain representable.
        symbols = sorted(set(rule_symbols) | set(source_state["engine"].get("positions", {})))
        saved = store.put_entity(user_id, "paper_transfer", {
            "previous": active, "source_state_hash": store.content_hash(source_state),
            "source_state": source_state, "rule_track_id": rule_track["id"],
            "track_id": f"active:{transition_id}", "name": name, "symbols": symbols,
            "rule_symbols": rule_symbols, "strategy_id": rules["strategy_id"], "kind": rules["kind"],
            "execution_spec": rules.get("execution_spec"), "max_position_pct": rules["max_position_pct"],
            "max_symbol_weight": rules["max_symbol_weight"],
            "initial_cash": source_track["payload"]["initial_cash"],
            "snapshot_id": store.get_state(user_id, "latest_snapshot") or rules["seed_snapshot_id"],
            "started_at": time.time(),
        }, transition_id)
    transfer = saved["payload"]
    track = _new_track(user_id, transfer["track_id"], name=transfer["name"], symbols=transfer["symbols"],
        snapshot_id=transfer["snapshot_id"], strategy_id=transfer["strategy_id"], kind=transfer["kind"],
        started_at=transfer["started_at"], initial_cash=transfer["initial_cash"],
        max_position_pct=transfer["max_position_pct"], max_symbol_weight=transfer["max_symbol_weight"],
        rule_symbols=transfer["rule_symbols"], execution_spec=transfer["execution_spec"])
    after = deepcopy(transfer["source_state"])
    after.update(pending=None, last_signal_t=None, updated_at=transfer["started_at"])
    # Preserve the complete engine state, including action fingerprints and cash
    # distributions, cumulative trades, quality flags and any existing risk stop.
    store.compare_and_set_state(user_id, f"paper:{track['id']}", None, after)
    return transfer, {"track_id": track["id"], "strategy_id": transfer["strategy_id"], "since": transfer["started_at"]}


def _activate_transfer(user_id, transition_id, evidence):
    """Recover pointer updates without resetting a newer account or its ledger."""
    key = f"paper_transfer:{transition_id}"
    if store.get_state(user_id, key, {}).get("status") == "committed":
        return True
    current = store.get_state(user_id, "paper_active")
    if current == evidence["previous"]:
        transfer = store.get_entity(user_id, "paper_transfer", transition_id)
        if transfer:
            source = store.get_state(user_id, f"paper:{current['track_id']}")
            if store.content_hash(source) != transfer["payload"]["source_state_hash"]:
                store.set_state(user_id, key, {"status": "superseded", "reason": "source_ledger_changed"})
                return False  # an intervening settlement must never be discarded
        store.compare_and_set_state(user_id, "paper_active", current, evidence["new"])
        current = store.get_state(user_id, "paper_active")
    if current != evidence["new"]:
        store.set_state(user_id, key, {"status": "superseded", "reason": "active_pointer_changed"})
        return False
    store.set_state(user_id, "paper_previous", evidence["previous"])
    store.set_state(user_id, key, {"status": "committed"})
    return True


def _maybe_promote(user_id, group):
    status_key = f"shadow:{group['id']}"
    status = store.get_state(user_id, status_key, {"status": "observing"})
    if status["status"] != "observing":
        return
    prior_event = store.get_entity(user_id, "promotion", f"forward:{group['id']}")
    if prior_event:
        evidence = prior_event["payload"]
        if _activate_transfer(user_id, f"forward:{group['id']}", evidence):
            store.set_state(user_id, status_key, {"status": "promoted", "evaluation": evidence["evaluation"]})
        else:
            store.set_state(user_id, status_key, {"status": "superseded", "evaluation": evidence["evaluation"]})
        return
    tracks = {role: _track(user_id, tid) for role, tid in group["payload"]["tracks"].items()}
    reports = {role: _report(track, _load_state(user_id, track)) for role, track in tracks.items()}
    decision = research_evaluation.evaluate_forward(reports["candidate"], reports["incumbent"], reports["baseline"])
    next_status = {"status": "observing", "evaluation": decision}
    if decision["eligible"]:
        active = store.get_state(user_id, "paper_active")
        # An older shadow cannot silently replace a newer incumbent it never beat.
        if active["track_id"] != group["payload"]["incumbent_id"]:
            next_status["status"] = "superseded"
        else:
            transition_id = f"forward:{group['id']}"
            transfer, new = _prepare_transfer(user_id, transition_id, active, tracks["candidate"],
                                               name=tracks["candidate"]["payload"]["name"])
            evidence = {"status": "promoted", "shadow_id": group["id"], "strategy_id": new["strategy_id"],
                        "previous": transfer["previous"], "new": new, "evaluation": decision,
                        "source_state_hash": transfer["source_state_hash"], "rule_track_id": transfer["rule_track_id"],
                        "reason": "冻结策略通过独立前向门槛；保留现行账户资产和累计账本，仅切换后续规则"}
            store.put_entity(user_id, "promotion", evidence, transition_id)
            next_status["status"] = "promoted" if _activate_transfer(user_id, transition_id, evidence) else "superseded"
    store.set_state(user_id, status_key, next_status)


def update_paper(user_id, snapshot_id):
    snapshot = research_data.get_snapshot(user_id, snapshot_id)
    active = ensure_baseline(user_id, snapshot_id)
    needed = {active["id"]}
    groups = store.list_entities(user_id, "paper_group", limit=1000)
    for group in groups:
        status = store.get_state(user_id, f"shadow:{group['id']}", {"status": "observing"})
        if status["status"] == "observing":
            needed.update(group["payload"]["tracks"].values())
    now = time.time()
    for track_id in sorted(needed):
        track = _track(user_id, track_id)
        _advance(user_id, track, snapshot, now)
    for group in groups:
        _maybe_promote(user_id, group)
    return paper_summary(user_id)


def required_symbols(user_id):
    symbols = set()
    active = store.get_state(user_id, "paper_active")
    if active and (track := _track(user_id, active["track_id"])):
        symbols.update(track["payload"]["symbols"])
    for group in store.list_entities(user_id, "paper_group", limit=1000):
        state = store.get_state(user_id, f"shadow:{group['id']}", {"status": "observing"})
        if state["status"] == "observing":
            for tid in group["payload"]["tracks"].values():
                symbols.update(_track(user_id, tid)["payload"]["symbols"])
    return sorted(symbols)


def _summary(user_id, track_id):
    track = _track(user_id, track_id)
    if not track:
        return None
    state = _load_state(user_id, track)
    report = _report(track, state)
    perf = report["performance"]
    return {"track_id": track_id, "strategy_id": track["payload"]["strategy_id"], "name": track["payload"]["name"],
            "status": state["status"], "cash": state["engine"]["cash"], "positions": state["engine"]["positions"],
            "equity": perf.get("final_equity", track["payload"]["initial_cash"]), "performance": perf,
            "session_count": len(state["equity_curve"]), "completed_trades": perf.get("completed_trade_count", 0),
            "quality": report["quality"], "updated_at": state["updated_at"], "pending": state["pending"]}


def paper_summary(user_id):
    active = store.get_state(user_id, "paper_active")
    previous = store.get_state(user_id, "paper_previous")
    shadows = []
    for group in store.list_entities(user_id, "paper_group", limit=50):
        state = store.get_state(user_id, f"shadow:{group['id']}", {"status": "observing", "evaluation": None})
        shadows.append({"id": group["id"], "name": group["payload"]["name"], "strategy_id": group["payload"]["strategy_id"],
                        **state, **{role: _summary(user_id, tid) for role, tid in group["payload"]["tracks"].items()}})
    events = store.list_entities(user_id, "promotion", limit=20)
    for event in events:
        # The immutable decision is written before its pointers. Surface the
        # recovery status so an uncommitted intent is never shown as a success.
        if event["payload"].get("source_state_hash"):
            state = store.get_state(user_id, f"paper_transfer:{event['id']}", {"status": "pending"})
            if state["status"] != "committed":
                event["payload"] = {**event["payload"], "intended_status": event["payload"]["status"], **state}
    return {"active": _summary(user_id, active["track_id"]) if active else None,
            "previous": _summary(user_id, previous["track_id"]) if previous else None,
            "shadows": shadows, "events": events}


def rollback(user_id, payload=None):
    payload = payload or {}
    request_id = payload.get("job_id") or payload.get("idempotency_key") or payload.get("_job_id")
    rid = f"rollback:request:{store.content_hash(str(request_id))}" if request_id else None
    # The same worker job may retry after either pointer was committed. Resolve
    # its original event before consulting the now-changed previous pointer.
    prior_event = store.get_entity(user_id, "promotion", rid) if rid else None
    if prior_event:
        decision = prior_event["payload"]
        if not _activate_transfer(user_id, rid, decision):
            raise ValueError("回滚期间账户已变化，不能覆盖新的账户状态")
        return decision
    active = store.get_state(user_id, "paper_active")
    previous = store.get_state(user_id, "paper_previous")
    if not active or not previous:
        raise ValueError("没有可回滚的前一现行版本")
    requested = payload.get("strategy_id")
    if requested is not None and requested != previous["strategy_id"]:
        raise ValueError("只能回滚到已经验证的前一版本")
    # Preserve the actual account, cash and holdings; switch rules for future
    # signals rather than jumping to a stale account's fabricated wealth.
    old_track = _track(user_id, previous["track_id"])
    rid = rid or f"rollback:{store.content_hash({'active': active, 'previous': previous})}"
    transfer, new = _prepare_transfer(user_id, rid, active, old_track, name=f"回滚：{old_track['payload']['name']}")
    decision = {"status": "rolled_back", "previous": transfer["previous"], "new": new,
                "source_state_hash": transfer["source_state_hash"], "rule_track_id": transfer["rule_track_id"],
                "reason": "用户回滚到前一版本；保留账户资产，下一次收盘起应用规则"}
    store.put_entity(user_id, "promotion", decision, rid)
    if not _activate_transfer(user_id, rid, decision):
        raise ValueError("回滚期间账户已变化，不能覆盖新的账户状态")
    return decision
