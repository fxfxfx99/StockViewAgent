"""Auditable orchestration: frozen datasets, candidates, experiments and gates."""
from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime
import hashlib
from pathlib import Path
import re
import time
import uuid

from app.schemas.research import StrategySpecV1
from app.services import research_data, research_evaluation as evaluation, research_generation as generation
from app.services import research_strategy as strategy
from app.services.market_time import SHANGHAI_TZ
from app.storage import research_store as store, watchlist_store

PROTOCOL_VERSION = "research-lifecycle-v1"
_SYMBOL = re.compile(r"^\d{6}\.(SS|SZ|BJ)$")


def canonical_symbols(values):
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        raise ValueError("研究股票池须为股票代码列表")
    symbols = sorted(set(str(s).strip().upper().replace(".SH", ".SS") for s in values))
    if not symbols or len(symbols) > 500 or any(not _SYMBOL.fullmatch(s) for s in symbols):
        raise ValueError("研究股票池须为 1–500 个 A 股代码")
    return symbols


def snapshot_bars(user_id, snapshot_id, symbols=None):
    grouped = defaultdict(list)
    wanted = set(symbols) if symbols else None
    for row in research_data.read_snapshot(user_id, snapshot_id):
        if wanted is None or row["symbol"] in wanted:
            grouped[row["symbol"]].append(row)
    return dict(grouped)


def _code_digest():
    root = Path(__file__).parent
    names = ("quant_backtest.py", "trading_rules.py", "research_strategy.py", "research_evaluation.py")
    digest = hashlib.sha256()
    for name in names:
        digest.update(name.encode())
        digest.update((root / name).read_bytes())
    digest.update((root.parent / "schemas" / "research.py").read_bytes())
    return digest.hexdigest()


def register_strategy(user_id, spec, *, origin="manual"):
    value = StrategySpecV1.model_validate(spec)
    if not value.hypothesis.strip() or not value.applicability.strip() or not any(s.strip() for s in value.failure_conditions):
        raise ValueError("策略必须包含研究假设、适用情形和失效条件")
    if not value.sources:
        value = value.model_copy(update={"sources": (f"local:{origin}",)})
    config = store.get_config(user_id)
    if value.max_symbol_weight > config["max_symbol_weight"] or value.max_position_pct > config["max_position_pct"]:
        raise ValueError("策略仓位超过研究配置上限")
    if value.parent_version_id and not store.get_entity(user_id, "strategy", value.parent_version_id):
        raise ValueError("父策略版本不属于当前账户")
    digest = strategy.strategy_logic_hash(value)
    existing = store.get_entity(user_id, "strategy", digest)
    if existing:
        return existing
    return store.put_entity(user_id, "strategy", {
        "spec": value.model_dump(mode="json"), "logic_hash": digest,
        "explanation": strategy.explain_strategy(value), "origin": origin,
    }, digest)


def enqueue(user_id, kind, payload=None, idempotency_key=None):
    if kind not in {"collect", "research", "evaluate", "github", "paper", "rollback"}:
        raise ValueError("未知研究任务")
    return store.queue_job(user_id, kind, payload or {}, idempotency_key or f"manual:{uuid.uuid4()}", max_attempts=3)


async def collect_data(user_id, payload=None):
    payload = payload or {}
    symbols = canonical_symbols(payload.get("symbols") or watchlist_store.load_symbols(user_id))
    pool = store.put_entity(user_id, "pool_version", {"symbols": symbols, "selection": "current_watchlist", "historical_selection_bias": True}, store.content_hash(symbols))
    # Continue observing existing holdings after a stock is removed from watchlist.
    from app.services import research_paper
    needed = sorted(set(symbols) | set(research_paper.required_symbols(user_id)))
    batches, errors, offline = [], {}, False
    for symbol in needed:
        try:
            batch = await research_data.fetch_history(user_id, symbol, range_param="5y", offline=bool(payload.get("offline")))
            batches.append(batch["id"])
            offline = offline or bool(batch.get("offline_fallback"))
        except (ValueError, RuntimeError, OSError, httpx_error_types()) as exc:
            errors[symbol] = f"采集失败（{type(exc).__name__}），保留历史数据"
    if not batches:
        result = {"status": "data_unavailable", "errors": errors, "pool_version_id": pool["id"]}
        store.put_entity(user_id, "collection", result)
        return result
    snapshot = research_data.create_snapshot(user_id, batches, pool_version_id=pool["id"], metadata={
        "selection_bias": "current_watchlist", "missing_symbols": sorted(errors), "offline_fallback": offline,
    })
    store.set_state(user_id, "latest_snapshot", snapshot["id"])
    store.set_state(user_id, "latest_pool", pool["id"])
    result = {"status": "partial" if errors else "collected", "snapshot_id": snapshot["id"], "pool_version_id": pool["id"], "errors": errors, "quality": snapshot["payload"]["quality"]}
    store.put_entity(user_id, "collection", result)
    await asyncio.to_thread(research_paper.update_paper, user_id, snapshot["id"])
    return result


def httpx_error_types():
    import httpx
    return httpx.HTTPError


def import_data(user_id, bundles, metadata):
    symbols = canonical_symbols(bundles)
    pool = store.put_entity(user_id, "pool_version", {"symbols": symbols, "selection": "imported", "historical_selection_bias": True})
    snapshot = research_data.import_snapshot(user_id, bundles, metadata=metadata, pool_version_id=pool["id"])
    store.set_state(user_id, "latest_snapshot", snapshot["id"])
    store.set_state(user_id, "latest_pool", pool["id"])
    return snapshot


def _experiment(user_id, strategy_entity, snapshot, *, include_holdout=True, splits=None, campaign_id=None, initial_cash=None):
    from app.services.quant_backtest import ENGINE_VERSION
    config = store.get_config(user_id)
    initial_cash = config["initial_cash"] if initial_cash is None else initial_cash
    inputs = {"strategy_id": strategy_entity["id"], "snapshot_id": snapshot["id"], "engine_version": ENGINE_VERSION,
              "code_hash": _code_digest(), "protocol_version": PROTOCOL_VERSION,
              "initial_cash": initial_cash, "include_holdout": include_holdout, "splits": splits,
              "campaign_id": campaign_id}
    experiment_id = store.content_hash(inputs)
    previous = store.get_entity(user_id, "experiment", experiment_id)
    if previous:
        return previous
    store.put_entity(user_id, "experiment_request", inputs, experiment_id)
    try:
        bars = snapshot_bars(user_id, snapshot["id"], strategy_entity["payload"]["spec"].get("universe"))
        report = evaluation.evaluate_candidate(strategy_entity["payload"]["spec"], bars,
                    initial_cash=initial_cash, include_holdout=include_holdout, splits=splits)
        report["snapshot_quality"] = snapshot["payload"]["quality"]
        if not snapshot["payload"].get("strict"):
            report["eligible"] = False
            report.setdefault("gates", {})["strict_snapshot"] = False
            report.setdefault("issues", []).append("数据尚未具备完整的历史时点与交易状态证据")
        result = {**inputs, "status": "evaluated", "report": report}
    except Exception as exc:
        # A failing candidate is evidence too. No provider URL/credentials in errors.
        result = {**inputs, "status": "failed", "error": f"评估失败（{type(exc).__name__}）"}
    return store.put_entity(user_id, "experiment", result, experiment_id)


async def evaluate_strategy(user_id, payload):
    record = store.get_entity(user_id, "strategy", payload["strategy_id"])
    snapshot_id = payload.get("snapshot_id") or store.get_state(user_id, "latest_snapshot")
    if not record or not snapshot_id:
        raise ValueError("需要当前账户的策略版本与数据快照")
    snapshot = research_data.get_snapshot(user_id, snapshot_id)
    reservation_key = f"manual-evaluate:{record['id']}:{snapshot_id}:{_code_digest()}"
    store.reserve_budget(user_id, reservation_key, candidates=1)
    return await asyncio.to_thread(_experiment, user_id, record, snapshot, include_holdout=not bool(store.get_state(user_id, "holdout_access")))


def _campaign_progress(user_id, campaign_id):
    key = f"campaign_progress:{campaign_id}"
    store.compare_and_set_state(user_id, key, None, {"attempt": 0, "experiment_ids": [], "status": "generating"})
    return store.get_state(user_id, key)


def _campaign(user_id, snapshot, goal):
    week = datetime.now(SHANGHAI_TZ).strftime("%G-W%V")
    pool_id = snapshot["payload"].get("pool_version_id")
    config = store.get_config(user_id)
    frozen_config = {key: config[key] for key in ("initial_cash", "max_symbol_weight", "max_position_pct")}
    code_hash = _code_digest()
    campaign_key = f"campaign:{week}:{pool_id}:{code_hash}:{store.content_hash(frozen_config)}"
    existing = store.get_state(user_id, campaign_key)
    if existing:
        _campaign_progress(user_id, existing)
        return store.get_entity(user_id, "campaign", existing)
    needs_holdout = not bool(store.get_state(user_id, "holdout_access"))
    pool = store.get_entity(user_id, "pool_version", pool_id) if pool_id else None
    symbols = (pool or {}).get("payload", {}).get("symbols") or snapshot["payload"]["symbols"]
    bars = snapshot_bars(user_id, snapshot["id"], symbols)
    splits = evaluation.build_research_splits(bars, holding_days=20, include_holdout=needs_holdout)
    entity = store.put_entity(user_id, "campaign", {"week": week, "snapshot_id": snapshot["id"],
        "pool_version_id": pool_id, "symbols": symbols, "splits": splits, "needs_holdout": needs_holdout,
        "goal": goal[:2000], "protocol_version": PROTOCOL_VERSION,
        "code_hash": code_hash, "financial_config": frozen_config})
    # Publish the campaign pointer only after its resumable progress exists.
    _campaign_progress(user_id, entity["id"])
    if store.compare_and_set_state(user_id, campaign_key, None, entity["id"]):
        return entity
    existing = store.get_state(user_id, campaign_key)
    _campaign_progress(user_id, existing)
    return store.get_entity(user_id, "campaign", existing)


def _feedback(user_id, experiment_ids, symbols, *, development=None):
    feedback = []
    recent = [store.get_entity(user_id, "experiment", eid) for eid in experiment_ids[-4:]]
    recent += store.list_entities(user_id, "experiment", limit=100)
    seen = set()
    for entity in recent:
        if not entity or entity["id"] in seen:
            continue
        seen.add(entity["id"])
        item = entity["payload"]
        record = store.get_entity(user_id, "strategy", item["strategy_id"])
        spec = record["payload"]["spec"] if record else (item.get("proposal") or {}).get("spec", {})
        source_campaign = store.get_entity(user_id, "campaign", item["campaign_id"]) if item.get("campaign_id") else None
        evidence_pool = source_campaign["payload"]["symbols"] if source_campaign else spec.get("universe")
        # A rejected outside-pool proposal belongs to its originating campaign;
        # its invalid proposed universe must not hide the reason for rejection.
        if evidence_pool and set(evidence_pool) != set(symbols):
            continue
        report = item.get("report") or {}
        if report and development:
            measured = (report.get("splits") or item.get("splits") or {}).get("development")
            # Feedback from a newer snapshot may contain this campaign's hidden
            # interval. Unknown or out-of-bound measurements cannot enter prompts.
            if not measured or measured["start"] < development["start"] or measured["end"] > development["end"]:
                continue
        feedback.append({"strategy_id": item["strategy_id"], "status": item["status"],
                         "hypothesis": spec.get("hypothesis", "")[:500],
                         "rules": {k: spec.get(k) for k in ("entry", "exit", "rank", "holding_days", "rebalance_days")},
                         "error": item.get("error"),
                         "validation_metrics": report.get("metrics"), "issues": report.get("issues", [])[:6]})
        if len(feedback) >= 4:
            break
    return feedback


async def _close_campaign(user_id, campaign, progress):
    if campaign["payload"].get("code_hash") != _code_digest():
        decision = {"status": "engine_changed", "reason": "研究代码已变化，旧批次关闭并在新引擎重新验证"}
    else:
        decision = await asyncio.to_thread(freeze_best_candidate, user_id, campaign, progress["experiment_ids"])
    progress = {**progress, "status": "closed", "decision": decision}
    store.set_state(user_id, f"campaign_progress:{campaign['id']}", progress)
    return {"campaign_id": campaign["id"], **progress}


async def _close_previous_campaigns(user_id, snapshot):
    """A weekly maximum is a ceiling, not a quota required to use evidence."""
    week = datetime.now(SHANGHAI_TZ).strftime("%G-W%V")
    pool_id = snapshot["payload"].get("pool_version_id")
    pool = store.get_entity(user_id, "pool_version", pool_id) if pool_id else None
    symbols = set((pool or {}).get("payload", {}).get("symbols") or snapshot["payload"]["symbols"])
    campaigns = store.list_entities(user_id, "campaign", limit=1000)
    for campaign in sorted(campaigns, key=lambda c: c["created_at"]):
        cp = campaign["payload"]
        if cp["week"] >= week or set(cp["symbols"]) != symbols:
            continue
        progress = _campaign_progress(user_id, campaign["id"])
        if progress["status"] != "closed":
            await _close_campaign(user_id, campaign, progress)


def _failed_attempt(user_id, campaign, attempt_key, proposal, reason=None):
    """Invalid and interrupted proposals remain visible alongside backtests."""
    return store.put_entity(user_id, "experiment", {
        "status": "failed", "strategy_id": None, "snapshot_id": campaign["payload"]["snapshot_id"],
        "campaign_id": campaign["id"], "attempt_id": attempt_key,
        "error": reason or proposal.get("error") or "候选未产生可执行规则",
        "proposal_status": proposal.get("status"), "proposal": proposal,
        "code_hash": campaign["payload"].get("code_hash"), "protocol_version": PROTOCOL_VERSION,
        "initial_cash": campaign["payload"]["financial_config"]["initial_cash"],
    }, f"proposal:{attempt_key}")


async def run_research(user_id, payload=None):
    payload = payload or {}
    snapshot_id = payload.get("snapshot_id") or store.get_state(user_id, "latest_snapshot")
    if not snapshot_id:
        collected = await collect_data(user_id)
        snapshot_id = collected.get("snapshot_id")
    if not snapshot_id:
        return {"status": "data_unavailable"}
    snapshot = research_data.get_snapshot(user_id, snapshot_id)
    # Finish last week's bounded search before choosing this week's holdout
    # policy: closing an older winner may consume the account's one holdout.
    await _close_previous_campaigns(user_id, snapshot)
    campaign = _campaign(user_id, snapshot, payload.get("goal", "研究自选池5–20交易日日线波段；提高扣费后收益并约束回撤"))
    cp = campaign["payload"]
    # A campaign always refers to its original immutable snapshot, even after new
    # daily data arrives. Otherwise validation/holdout boundaries drift silently.
    snapshot = research_data.get_snapshot(user_id, cp["snapshot_id"])
    key = f"campaign_progress:{campaign['id']}"
    progress = _campaign_progress(user_id, campaign["id"])
    if progress["status"] == "closed":
        return {"campaign_id": campaign["id"], **progress}
    if not cp["splits"]["eligible"]:
        return {"status": "insufficient_history", "campaign_id": campaign["id"], "issues": cp["splits"]["issues"]}
    limit = min(int(payload.get("candidate_count", 60)), store.get_config(user_id)["weekly_candidates"])
    while progress["attempt"] < limit:
        if store.get_config(user_id)["paused"]:
            return {"status": "paused", "campaign_id": campaign["id"], **progress}
        attempt_key = f"{campaign['id']}:{progress['attempt']}"
        context = {"goal": cp["goal"], "symbols": cp["symbols"], "development_period": cp["splits"].get("development"),
                   "validation_feedback": _feedback(user_id, progress["experiment_ids"], cp["symbols"], development=cp["splits"].get("development")),
                   "excluded_logic_hashes": [e["id"] for e in store.list_entities(user_id, "strategy", limit=100)]}
        try:
            proposal = await generation.generate_candidate(user_id, context, attempt_key=attempt_key)
        except store.BudgetExceeded:
            if store.budget_status(user_id)["remaining"]["candidates"] == 0:
                return await _close_campaign(user_id, campaign, progress)
            return {"campaign_id": campaign["id"], **progress, "status": "budget_exhausted"}
        if proposal["status"] in {"configuration_required", "paused", "in_progress"}:
            return {"campaign_id": campaign["id"], **proposal}
        if proposal["status"] == "generated":
            try:
                spec = StrategySpecV1.model_validate(proposal["spec"])
                if spec.universe and not set(spec.universe) <= set(cp["symbols"]):
                    raise ValueError("候选包含池外标的")
                # Bind the executable rules to the frozen pool, not a later watchlist.
                spec = spec.model_copy(update={"universe": tuple(cp["symbols"])}) if not spec.universe else spec
                record = register_strategy(user_id, spec.model_dump(mode="json"), origin=attempt_key)
                experiment = await asyncio.to_thread(_experiment, user_id, record, snapshot,
                    include_holdout=cp["needs_holdout"], splits=cp["splits"], campaign_id=campaign["id"],
                    initial_cash=cp["financial_config"]["initial_cash"])
                if experiment["id"] not in progress["experiment_ids"]:
                    progress["experiment_ids"].append(experiment["id"])
            except ValueError as exc:
                store.put_entity(user_id, "proposal_rejection", {"attempt_id": attempt_key, "reason": str(exc)[:500]}, attempt_key)
                failed = _failed_attempt(user_id, campaign, attempt_key, proposal, str(exc)[:500])
                if failed["id"] not in progress["experiment_ids"]:
                    progress["experiment_ids"].append(failed["id"])
        else:
            failed = _failed_attempt(user_id, campaign, attempt_key, proposal)
            if failed["id"] not in progress["experiment_ids"]:
                progress["experiment_ids"].append(failed["id"])
        progress["attempt"] += 1
        store.set_state(user_id, key, progress)
    return await _close_campaign(user_id, campaign, progress)


def freeze_best_candidate(user_id, campaign, experiment_ids):
    cp = campaign["payload"]
    candidates = [store.get_entity(user_id, "experiment", eid) for eid in experiment_ids]
    candidates = [e for e in candidates if e and e["payload"].get("report", {}).get("eligible")]
    if not candidates:
        return {"status": "no_qualified_candidate", "reason": "所有候选均未通过固定门槛，保留现行策略"}
    candidates.sort(key=lambda e: e["payload"]["report"].get("rank_key", []), reverse=True)
    best = candidates[0]
    if best["payload"].get("code_hash") != _code_digest():
        return {"status": "engine_changed", "reason": "验证后评估代码已变化，须在新引擎重新验证"}
    month = datetime.now(SHANGHAI_TZ).strftime("%Y-%m")
    freeze = {"campaign_id": campaign["id"], "experiment_id": best["id"], "strategy_id": best["payload"]["strategy_id"],
              "snapshot_id": cp["snapshot_id"], "code_hash": best["payload"]["code_hash"], "initial_cash": best["payload"]["initial_cash"]}
    old = store.get_state(user_id, f"freeze:{month}")
    if old is not None and old != freeze:
        return {"status": "monthly_limit", "reason": "本月已有冻结候选"}
    if old is None and not store.compare_and_set_state(user_id, f"freeze:{month}", None, freeze):
        return {"status": "monthly_limit"}
    record = store.get_entity(user_id, "strategy", freeze["strategy_id"])
    holdout = None
    if cp["needs_holdout"]:
        access = store.get_state(user_id, "holdout_access")
        if access is not None and access != freeze:
            return {"status": "holdout_already_consumed", "reason": "保留段已经被其他冻结候选使用"}
        if access is None and not store.compare_and_set_state(user_id, "holdout_access", None, freeze):
            return {"status": "holdout_already_consumed"}
        existing = store.get_entity(user_id, "holdout", best["id"])
        if existing is None:
            if not store.compare_and_set_state(user_id, f"holdout_dispatch:{best['id']}", None, freeze):
                return {"status": "holdout_interrupted", "reason": "保留段已认领但未完整落盘，本次候选不晋升"}
            report = evaluation.evaluate_holdout(record["payload"]["spec"], snapshot_bars(user_id, cp["snapshot_id"], cp["symbols"]),
                       holdout=cp["splits"]["holdout"], initial_cash=freeze["initial_cash"])
            existing = store.put_entity(user_id, "holdout", {**freeze, "report": report}, best["id"])
        holdout = existing["payload"]["report"]
        if not holdout["eligible"]:
            decision = {**freeze, "status": "holdout_rejected", "issues": holdout["issues"], "protocol_version": PROTOCOL_VERSION}
            return store.put_entity(user_id, "promotion", decision, f"holdout:{best['id']}")["payload"]
    from app.services import research_paper
    group = research_paper.start_shadow(user_id, record, cp["snapshot_id"], experiment_id=best["id"])
    decision = {**freeze, "status": "shadow_started", "shadow_id": group["id"], "protocol_version": PROTOCOL_VERSION,
                "reason": "通过历史验证，开始独立前向观察；尚未晋升", "holdout_passed": holdout is not None}
    return store.put_entity(user_id, "promotion", decision, f"shadow:{best['id']}")["payload"]


def overview(user_id):
    from app.services import research_paper
    latest = store.get_state(user_id, "latest_snapshot")
    snapshot = store.get_entity(user_id, "snapshot", latest) if latest else None
    paper = research_paper.paper_summary(user_id)
    return {"config": store.get_config(user_id), "budget": store.budget_status(user_id),
            "worker": store.get_state(0, "worker_heartbeat"), "data": snapshot,
            "jobs": store.list_jobs(user_id, limit=30), "strategies": store.list_entities(user_id, "strategy", limit=100),
            "experiments": [{**e, "payload": {k: v for k, v in e["payload"].items() if k not in {"report", "proposal"}},
                             "summary": {k: e["payload"].get("report", {}).get(k) for k in ("eligible", "metrics", "issues", "gates")}}
                            for e in store.list_entities(user_id, "experiment", limit=50)],
            "promotions": paper["events"],
            "knowledge": store.list_entities(user_id, "knowledge", limit=12),
            "paper": paper, "import_requirements": research_data.IMPORT_REQUIREMENTS,
            "protocol_version": PROTOCOL_VERSION}
