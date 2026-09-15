from __future__ import annotations

from typing import Any

from app.services import kline_pipeline, transaction_agent_service
from app.storage import decision_signal_store


async def generate_from_transaction_agent(user_id: int, symbol: str) -> dict[str, Any]:
    bundle = await transaction_agent_service.generate_strategy_views(symbol)
    summary = bundle.get("summary") or {}
    direction = summary.get("dominant_direction") or "neutral"
    action = "increase" if direction == "bullish" else "reduce" if direction == "bearish" else "watch"
    strategies = bundle.get("strategies") or []
    ranked = sorted(strategies, key=lambda x: float(x.get("confidence") or 0) * abs(float(x.get("score") or 50) - 50), reverse=True)
    evidence = [{"strategy": x.get("strategy_name"), "direction": x.get("direction"), "score": x.get("score")} for x in ranked[:6]]
    risks = list(dict.fromkeys(r for x in ranked[:5] for r in (x.get("risks") or [])))[:8]
    watch = list(dict.fromkeys(r for x in ranked[:4] for r in (x.get("reasons") or [])))[:8]
    signal = decision_signal_store.create(user_id, {
        "symbol": bundle["symbol"], "action": action, "horizon": "swing",
        "confidence": sum(float(x.get("confidence") or 0) for x in strategies) / len(strategies) if strategies else 0,
        "score": summary.get("average_score") or 50,
        "reason": f"{len(strategies)} 个策略综合方向为 {direction}；该信号用于跟踪和后验复盘。",
        "risks": risks, "watch_conditions": watch, "evidence": evidence,
        "source_type": "transaction_agent", "source_meta": bundle.get("source") or {},
    })
    return signal


async def evaluate(user_id: int, symbol: str | None = None) -> dict[str, Any]:
    signals = decision_signal_store.list_signals(user_id, symbol=symbol, limit=200)
    evaluated = unable = 0
    bundles: dict[str, list[dict[str, Any]]] = {}
    for signal in signals:
        sym = signal["symbol"]
        if sym not in bundles:
            data = await kline_pipeline.fetch_a_share_kline_with_fallbacks(sym, "1y", "1d")
            bundles[sym] = sorted(data.get("candles") or [], key=lambda x: float(x.get("t") or 0))
        forward = [b for b in bundles[sym] if float(b.get("t") or 0) >= float(signal["created_at"])]
        for days in (1, 3, 5, 10):
            if len(forward) <= days:
                decision_signal_store.upsert_outcome(signal["id"], {"horizon_days": days, "eval_status": "unable", "unable_reason": "信号后交易日不足"}); unable += 1; continue
            base, end = float(forward[0]["c"]), float(forward[days]["c"])
            ret = (end / base - 1) * 100 if base else 0
            expected = 1 if signal["action"] == "increase" else -1 if signal["action"] == "reduce" else 0
            correct = None if expected == 0 else int((ret > 0 and expected > 0) or (ret < 0 and expected < 0))
            decision_signal_store.upsert_outcome(signal["id"], {"horizon_days": days, "eval_status": "evaluated", "base_price": base, "end_price": end, "return_pct": round(ret, 4), "direction_correct": correct}); evaluated += 1
    return {"signal_count": len(signals), "evaluated": evaluated, "unable": unable, "engine_version": "decision-signal-v1"}
