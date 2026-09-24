"""App startup owns the manual worker and respects the daily scheduler switch."""
import asyncio

from fastapi import FastAPI
import pytest

from app.config import settings


@pytest.mark.parametrize("scheduler_enabled,comments_enabled", [(False, True), (True, False), (True, True)])
def test_app_starts_and_stops_comment_workers(monkeypatch, scheduler_enabled, comments_enabled):
    from app.main import lifespan
    from app.services import kline_insight_scheduler, xueqiu_comments_scheduler, xueqiu_comments_service
    from app.storage import data_asset_manager

    monkeypatch.setattr(settings, "enable_scheduler", scheduler_enabled)
    monkeypatch.setattr(settings, "xueqiu_comments_auto_refresh_enabled", comments_enabled)
    monkeypatch.setattr(settings, "enable_startup_data_check", False)
    monkeypatch.setattr(settings, "company_auto_refresh_enabled", False)
    monkeypatch.setattr(data_asset_manager, "refresh_catalog_from_disk", lambda: None)
    started, stopped = set(), set()

    def loop(name):
        async def run():
            started.add(name)
            try:
                await asyncio.Event().wait()
            finally:
                stopped.add(name)
        return run

    monkeypatch.setattr(xueqiu_comments_service, "worker_loop", loop("worker"))
    monkeypatch.setattr(xueqiu_comments_scheduler, "scheduler_loop", loop("comments_daily"))
    monkeypatch.setattr(kline_insight_scheduler, "kline_insight_scheduler_loop", loop("kline_daily"))

    async def run_lifecycle():
        async with lifespan(FastAPI()):
            await asyncio.sleep(0)
            assert "worker" in started
            assert ("comments_daily" in started) == (scheduler_enabled and comments_enabled)
            assert ("kline_daily" in started) == scheduler_enabled
        assert stopped == started

    asyncio.run(run_lifecycle())
