import json

from app.storage import kline_insight_store as kis


def test_opt_in_and_history_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(kis, "_dir", lambda: tmp_path)

    uid = 42
    assert kis.is_opt_in(uid, "600519.SS") is False

    kis.set_opt_in(uid, "600519.SS", True)
    kis.upsert_insight(
        uid,
        "600519.SS",
        "2026-03-28",
        "hello",
        source="manual",
        model="moonshot-v1-32k",
    )

    h = kis.history_for_symbol(uid, "600519.SS")
    assert h["opt_in"] is True
    assert "2026-03-28" in h["dates"]
    assert h["entries"]["2026-03-28"]["interpretation"] == "hello"
    assert h["entries"]["2026-03-28"]["source"] == "manual"

    kis.upsert_insight(
        uid,
        "600519.SS",
        "2026-03-28",
        "updated",
        source="scheduled",
        model="moonshot-v1-32k",
    )
    h2 = kis.history_for_symbol(uid, "600519.SS")
    assert h2["entries"]["2026-03-28"]["interpretation"] == "updated"
    assert h2["entries"]["2026-03-28"]["source"] == "scheduled"


def test_history_hidden_without_opt_in(tmp_path, monkeypatch):
    monkeypatch.setattr(kis, "_dir", lambda: tmp_path)

    uid = 1
    p = tmp_path / "1.json"
    p.write_text(
        json.dumps(
            {
                "opt_in": {},
                "symbols": {"000001.SZ": [{"report_date": "2026-01-01", "interpretation": "x", "source": "manual", "model": "m"}]},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    h = kis.history_for_symbol(uid, "000001.SZ")
    assert h["opt_in"] is False
    assert h["dates"] == []
