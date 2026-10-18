from __future__ import annotations

from datetime import datetime, time as daytime
from typing import Any

from app.services import research_data, transaction_agent_service
from app.services.market_time import SHANGHAI_TZ, market_date
from app.storage import decision_signal_store

ENGINE_VERSION = "decision-signal-v2-next-open"


async def generate_from_transaction_agent(user_id: int, symbol: str) -> dict[str, Any]:
    bundle = await transaction_agent_service.generate_strategy_views(symbol)
    summary = bundle.get("summary") or {}
    direction = summary.get("dominant_direction") or "neutral"
    action = "increase" if direction == "bullish" else "reduce" if direction == "bearish" else "watch"
    strategies = bundle.get("strategies") or []
    ranked = sorted(strategies, key=lambda x: float(x.get("confidence") or 0) * abs(float(x.get("score") or 50) - 50), reverse=True)
    evidence = [{"strategy": x.get("strategy"), "direction": x.get("direction"), "score": x.get("score")} for x in ranked[:6]]
    risks = list(dict.fromkeys(r for x in ranked[:5] for r in (x.get("risks") or [])))[:8]
    watch = list(dict.fromkeys(r for x in ranked[:4] for r in (x.get("reasons") or [])))[:8]
    return decision_signal_store.create(user_id, {
        "symbol": bundle["symbol"], "action": action, "horizon": "swing",
        "confidence": sum(float(x.get("confidence") or 0) for x in strategies) / len(strategies) if strategies else 0,
        "score": summary.get("average_score") or 50,
        "reason": f"{len(strategies)} 个策略综合方向为 {direction}；此处仅评价方向预测，不作为可成交策略收益。",
        "risks": risks, "watch_conditions": watch, "evidence": evidence,
        "source_type": "transaction_agent", "source_meta": bundle.get("source") or {},
    })


def _horizons(signal: dict[str, Any]) -> tuple[int, ...]:
    days = (signal.get("source_meta") or {}).get("holding_days")
    if isinstance(days, int) and not isinstance(days, bool) and 1 <= days <= 252:
        return (days,)
    return {"swing": (5, 10, 20), "long": (20, 60, 120), "short": (1, 3, 5)}.get(signal.get("horizon"), (5, 10, 20))


def _open_at(bar: dict[str, Any]) -> float:
    return datetime.combine(market_date(float(bar["t"])), daytime(9, 30), tzinfo=SHANGHAI_TZ).timestamp()


async def evaluate(user_id: int, symbol: str | None = None) -> dict[str, Any]:
    evaluated = unable = total = 0
    bundles: dict[str, list[dict[str, Any]]] = {}
    after_id = ""
    while True:
        signals = decision_signal_store.pending_signals(user_id, engine_version=ENGINE_VERSION, symbol=symbol, after_id=after_id)
        if not signals:
            break
        for signal in signals:
            total += 1
            sym = signal["symbol"]
            if sym not in bundles:
                try:
                    data = await research_data.fetch_bundle(user_id, sym, "5y", "1d")
                    bundles[sym] = sorted(data.get("candles") or [], key=lambda x: float(x.get("t") or 0))
                except (RuntimeError, ValueError, OSError):
                    bundles[sym] = []
            rows = bundles[sym]
            # A truncated history must not move a multi-year-old prediction's entry
            # to today's first available bar and falsely evaluate a different trade.
            coverage_ok = bool(rows) and market_date(float(rows[0]["t"])) <= market_date(float(signal["created_at"]))
            forward = [b for b in rows if _open_at(b) > float(signal["created_at"])] if coverage_ok else []
            start = next((i for i, b in enumerate(forward) if float(b.get("o") or 0) > 0 and not b.get("suspended") and b.get("tradable", True)), None)
            if start is not None:
                forward = forward[start:]
            complete = True
            for days in _horizons(signal):
                outcome = {"engine_version": ENGINE_VERSION, "horizon_days": days}
                if start is None or len(forward) < days or not forward[days - 1].get("c"):
                    outcome.update(eval_status="pending", unable_reason="行情覆盖不足或评价周期尚未到期")
                    unable += 1
                    complete = False
                else:
                    base, end = float(forward[0]["o"]), float(forward[days - 1]["c"])
                    ret = (end / base - 1) * 100
                    expected = {"increase": 1, "reduce": -1}.get(signal["action"], 0)
                    correct = None if expected == 0 else int(ret * expected > 0)
                    outcome.update(eval_status="evaluated", base_price=base, end_price=end,
                                   return_pct=round(ret, 4), direction_correct=correct)
                    evaluated += 1
                decision_signal_store.upsert_outcome(signal["id"], outcome)
            if complete:
                decision_signal_store.upsert_outcome(signal["id"], {"engine_version": ENGINE_VERSION, "horizon_days": 0, "eval_status": "complete"})
        after_id = signals[-1]["id"]
    return {"signal_count": total, "evaluated": evaluated, "unable": unable, "engine_version": ENGINE_VERSION,
            "measurement": "directional_next_open_to_horizon_close_before_costs"}
