import asyncio

import pytest

from app.config import settings
from app.security.user_context import current_user_id
from app.services import kline_insight_scheduler as scheduler
from app.storage import kline_insight_store, llm_runtime, user_credentials_store, watchlist_store


def test_scheduled_analysis_uses_each_accounts_credentials(monkeypatch, tmp_path):
    monkeypatch.setattr(llm_runtime, "_FILE", tmp_path / "llm_runtime.json")
    for uid in (1, 2, 3):
        watchlist_store.save_symbols(uid, ["600519.SS"])
        kline_insight_store.set_opt_in(uid, "600519.SS")
    for uid in (1, 2):
        user_credentials_store.merge_user_credentials(uid, {"llm": {"api_key": f"test-key-{uid}"}})

    observed = []

    async def analyze(symbol, **kwargs):
        uid = current_user_id.get()
        observed.append((uid, settings.kline_llm_key))
        return {"interpretation": f"analysis-{uid}", "model": "test-model"}

    async def skip_delay(_):
        pass

    monkeypatch.setattr(scheduler.kls, "analyze_a_share_symbol", analyze)
    monkeypatch.setattr(scheduler.asyncio, "sleep", skip_delay)
    asyncio.run(scheduler.run_daily_batch_if_configured())

    assert observed == [(1, "test-key-1"), (2, "test-key-2")]
    for uid in (1, 2):
        entries = kline_insight_store.history_for_symbol(uid, "600519.SS")["entries"]
        assert next(iter(entries.values()))["interpretation"] == f"analysis-{uid}"
    assert kline_insight_store.history_for_symbol(3, "600519.SS")["dates"] == []
    assert current_user_id.get() is None


def test_scheduler_restores_account_context_after_cancellation(monkeypatch):
    watchlist_store.save_symbols(1, ["600519.SS"])
    kline_insight_store.set_opt_in(1, "600519.SS")
    user_credentials_store.merge_user_credentials(1, {"llm": {"api_key": "test-key"}})

    async def cancel(symbol, **kwargs):
        raise asyncio.CancelledError

    async def run():
        with pytest.raises(asyncio.CancelledError):
            await scheduler.run_daily_batch_if_configured()
        assert current_user_id.get() is None

    monkeypatch.setattr(scheduler.kls, "analyze_a_share_symbol", cancel)
    asyncio.run(run())
