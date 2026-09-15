import sqlite3

from app.storage import decision_signal_store


def test_signal_lifecycle_and_outcome(tmp_path, monkeypatch):
    db_path = tmp_path / "signals.db"
    def connect():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn
    monkeypatch.setattr(decision_signal_store, "_conn", connect)
    signal = decision_signal_store.create(7, {
        "symbol": "600519.SS", "action": "increase", "confidence": 0.7,
        "score": 68, "reason": "多策略共振", "risks": ["回撤"],
        "watch_conditions": ["量能"], "evidence": [{"strategy": "trend"}],
        "source_type": "transaction_agent", "source_meta": {"as_of": "2026-07-16"},
    })
    decision_signal_store.upsert_outcome(signal["id"], {
        "horizon_days": 5, "eval_status": "evaluated", "base_price": 100,
        "end_price": 105, "return_pct": 5, "direction_correct": 1,
    })
    rows = decision_signal_store.list_signals(7, symbol="600519.SS", status="active")
    assert rows[0]["risks"] == ["回撤"]
    assert rows[0]["outcomes"][0]["return_pct"] == 5
    assert decision_signal_store.update_status(7, signal["id"], "closed")
    assert decision_signal_store.list_signals(7, status="closed")[0]["id"] == signal["id"]
