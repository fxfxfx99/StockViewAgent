import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.services import decision_signal_service as service, transaction_agent_service
from app.storage import decision_signal_store as store


def test_knowledge_coverage_does_not_raise_confidence(monkeypatch):
    monkeypatch.setattr(transaction_agent_service.strategy_knowledge_service, "query_strategy_knowledge", lambda *a, **k: [{"title": "Evidence", "key_points": [], "risks": []}] * 6)
    monkeypatch.setattr(transaction_agent_service, "_build_detailed_analysis", lambda *args: {})
    result = transaction_agent_service._apply_knowledge_context({"strategy": "trend", "confidence": 0.55})
    assert result["confidence"] == 0.55
    assert result["metadata"]["knowledge_coverage"] == {"source_count": 6, "performance_validated": False}


def test_evaluation_paginates_old_signals_and_uses_next_open(monkeypatch):
    date = datetime(2024, 1, 2, tzinfo=ZoneInfo("Asia/Shanghai"))
    bars = [{"t": (date + timedelta(days=i)).timestamp(), "o": 100 + i, "c": 104 + i} for i in range(25)]
    for i in range(205):
        signal = store.create(7, {"symbol": "600519.SS", "action": "increase", "source_meta": {"holding_days": 5}})
        with store._conn() as conn:
            conn.execute("UPDATE decision_signal SET created_at=? WHERE id=?", ((date + timedelta(hours=16)).timestamp(), signal["id"]))
    store.create(8, {"symbol": "600519.SS"})

    async def fetch(*args):
        return {"candles": bars}

    monkeypatch.setattr(service.research_data, "fetch_bundle", fetch)
    result = asyncio.run(service.evaluate(7))
    assert result["signal_count"] == result["evaluated"] == 205
    outcomes = store.list_signals(7)[0]["outcomes"]
    five_day = next(o for o in outcomes if o["horizon_days"] == 5)
    assert five_day["base_price"] == 101
    assert five_day["end_price"] == 109
    assert asyncio.run(service.evaluate(7))["signal_count"] == 0
    assert len(store.pending_signals(8, engine_version=service.ENGINE_VERSION)) == 1


def test_incomplete_history_cannot_reanchor_old_prediction(monkeypatch):
    signal = store.create(7, {"symbol": "600519.SS", "source_meta": {"holding_days": 5}})
    with store._conn() as conn:
        conn.execute("UPDATE decision_signal SET created_at=1 WHERE id=?", (signal["id"],))

    async def fetch(*args):
        return {"candles": [{"t": 1700000000 + i * 86400, "o": 100, "c": 101} for i in range(25)]}

    monkeypatch.setattr(service.research_data, "fetch_bundle", fetch)
    result = asyncio.run(service.evaluate(7))
    assert result["evaluated"] == 0
    assert result["unable"] == 1
