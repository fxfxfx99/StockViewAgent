"""The legacy chart/backtest APIs share immutable history and holdout access."""
import asyncio

import pytest

from app.routers import market
from app.security.user_context import current_user_id
from app.services import kline_pipeline, quant_backtest as quant
from app.storage.research_store import ResearchStore
from tests.test_quant_replay import _bars, ZERO_COST, SYM


def _optimize(**overrides):
    return {"symbol": SYM, "range_param": "1y", "interval": "1d", "strategy_id": "ma_cross",
            "param_grid": {"fast": [2, 3], "slow": [5]}, "initial_cash": 100_000,
            "cost_model": ZERO_COST, **overrides}


def test_market_and_quant_share_user_history_and_offline_fallback(monkeypatch):
    bars = _bars(40)
    async def fetch(*args):
        assert current_user_id.get() == 42
        return {"candles": bars, "price_basis": "raw", "volume_unit": "shares", "kline_source": "fixture"}
    monkeypatch.setattr(kline_pipeline, "fetch_a_share_kline_with_fallbacks", fetch)
    token = current_user_id.set(42)
    try:
        chart = asyncio.run(market.get_kline(SYM, "1y", "1d"))
        assert chart["research_batch_id"]
        async def offline(*args):
            raise RuntimeError("offline fixture")
        monkeypatch.setattr(kline_pipeline, "fetch_a_share_kline_with_fallbacks", offline)
        replay = asyncio.run(quant._market_bundle(SYM, "5y", "1d"))
        assert [bar["t"] for bar in replay["candles"]] == [bar["t"] for bar in chart["candles"]]
        assert replay["served_from_cache"] and replay["cache_stale"]
        assert ResearchStore().get_state(42, f"history_head:{SYM}:1d")
        assert ResearchStore().get_state(43, f"history_head:{SYM}:1d") is None
    finally:
        current_user_id.reset(token)


def test_intraday_chart_keeps_original_pipeline_and_does_not_archive(monkeypatch):
    async def fetch(*args):
        return {"candles": _bars(2)}
    monkeypatch.setattr(kline_pipeline, "fetch_a_share_kline_with_fallbacks", fetch)
    result = asyncio.run(market.get_kline(SYM, "1d", "5m"))
    assert "research_batch_id" not in result
    assert ResearchStore().get_state(0, f"history_head:{SYM}:5m") is None


def test_recorded_optimization_consumes_holdout_once_and_reuses_saved_result(monkeypatch):
    bars = _bars(100)
    async def fetch(*args):
        return {"candles": bars}
    monkeypatch.setattr(quant, "_market_bundle", fetch)
    calls = []
    original = quant.run_backtest
    def spy(**kwargs):
        calls.append(kwargs.get("evaluation_start"))
        return original(**kwargs)
    monkeypatch.setattr(quant, "run_backtest", spy)
    token = current_user_id.set(5)
    try:
        first = asyncio.run(quant.optimize_backtest_from_market(**_optimize()))
        holdout_start = first["holdout_start"]
        assert first["holdout_status"] == "evaluated"
        assert sum(t == holdout_start for t in calls) == 1
        calls.clear()
        again = asyncio.run(quant.optimize_backtest_from_market(**_optimize()))
        assert again["reused_result"] and again["holdout_evaluation_count"] == 0
        assert again["best"] == first["best"] and not calls
        altered = asyncio.run(quant.optimize_backtest_from_market(**_optimize(param_grid={"fast": [4], "slow": [6]})))
        assert altered["holdout_status"] == "already_consumed"
        assert "holdout" not in altered["best"] and "test" not in altered["best"]
        assert holdout_start not in calls
        store = ResearchStore()
        assert store.get_state(5, "holdout_access")["request_id"] == first["request_id"]
        assert len(store.list_entities(5, "quant_optimization_request")) == 2
        assert len(store.list_entities(5, "quant_optimization_result")) == 1
        assert len(store.list_entities(5, "quant_validation_result")) == 1
        assert store.get_state(6, "holdout_access") is None
    finally:
        current_user_id.reset(token)


def test_research_holdout_gate_also_blocks_legacy_optimization(monkeypatch):
    async def fetch(*args):
        return {"candles": _bars(100)}
    monkeypatch.setattr(quant, "_market_bundle", fetch)
    ResearchStore().set_state(0, "holdout_access", {"experiment_id": "already-frozen"})
    result = asyncio.run(quant.optimize_backtest_from_market(**_optimize()))
    assert result["holdout_status"] == "already_consumed"
    assert result["holdout_evaluation_count"] == 0
    assert "test" not in result["best"]


def test_failed_holdout_dispatch_is_not_retried_after_restart(monkeypatch):
    async def fetch(*args):
        return {"candles": _bars(100)}
    monkeypatch.setattr(quant, "_market_bundle", fetch)
    original = quant.run_backtest
    calls = []
    def fail_holdout(**kwargs):
        if kwargs.get("evaluation_start") == _bars(100)[80]["t"]:
            calls.append(True)
            raise RuntimeError("fixture interrupted holdout")
        return original(**kwargs)
    monkeypatch.setattr(quant, "run_backtest", fail_holdout)
    with pytest.raises(RuntimeError, match="interrupted holdout"):
        asyncio.run(quant.optimize_backtest_from_market(**_optimize(train_ratio=.6)))
    result = asyncio.run(quant.optimize_backtest_from_market(**_optimize(train_ratio=.6)))
    assert result["holdout_status"] == "already_consumed"
    assert len(calls) == 1
    assert any(row["payload"]["status"] == "failed_consumed" for row in ResearchStore().list_entities(0, "quant_optimization_access"))


def test_concurrent_legacy_requests_dispatch_holdout_only_once(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import traceback
    bars = _bars(100)
    barrier = threading.Barrier(2)
    original = quant.optimize_backtest
    def synchronize_validation(**kwargs):
        result = original(**kwargs)
        barrier.wait(timeout=10)
        return result
    monkeypatch.setattr(quant, "optimize_backtest", synchronize_validation)
    args = _optimize()
    for key in ("symbol", "range_param", "interval"):
        args.pop(key)
    def run():
        try:
            return quant._recorded_market_optimization(0, {"candles": bars}, SYM, "1y", "1d", args)
        except Exception:
            # A pre-barrier failure must not be hidden behind the other
            # worker's timeout. Both complete tracebacks are reported below.
            barrier.abort()
            raise
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run), pool.submit(run)]
        results, errors = [], []
        for index, future in enumerate(futures):
            try:
                results.append(future.result(timeout=15))
            except Exception:
                errors.append(f"worker {index}:\n{traceback.format_exc()}")
        assert not errors, "\n".join(errors)
    assert sorted(result["holdout_evaluation_count"] for result in results) == [0, 1]
    assert len(ResearchStore().list_entities(0, "quant_optimization_result")) == 1


def test_legacy_backtest_cannot_discard_data_provenance_blockers(monkeypatch):
    async def fetch(*args):
        return {"candles": _bars(40), "research_batch_ids": ["fixture-batch"],
                "research_quality": {"eligible": False, "issues": ["historical_availability_missing"]}}
    monkeypatch.setattr(quant, "_market_bundle", fetch)
    result = asyncio.run(quant.backtest_from_market(SYM, "1y", "1d", "buy_and_hold", {}, 100_000))
    assert not result["quality"]["eligible"]
    assert "historical_availability_missing" in result["quality"]["issues"]
    assert result["research_batch_ids"] == ["fixture-batch"]
