"""Shutdown drains every mocked service even when another finalizer fails."""
import asyncio
import logging

from fastapi import FastAPI
import pytest

from app.config import settings


@pytest.mark.parametrize("body_error,collection_enabled", [(False, True), (True, False)])
def test_shutdown_error_does_not_skip_research_drain(monkeypatch, caplog, body_error, collection_enabled):
    # conftest redirects all import-time storage initialization to a temp directory.
    from app.main import lifespan
    from app.services import (
        company_updates, data_collection, earnings, event_calendar_service,
        investment_research, kline_insight_scheduler, monitoring, overseas_news,
        startup_data_check, xueqiu_comments_scheduler, xueqiu_comments_service,
    )
    from app.storage import data_asset_manager

    for flag in ("enable_scheduler", "enable_startup_data_check", "company_auto_refresh_enabled",
                 "xueqiu_comments_auto_refresh_enabled", "overseas_news_enabled",
                 "enable_monitoring", "enable_earnings", "enable_investment_research"):
        monkeypatch.setattr(settings, flag, True)
    monkeypatch.setattr(settings, "enable_data_collection", collection_enabled)
    monkeypatch.setattr(settings, "enable_research_worker", False)
    monkeypatch.setattr(data_asset_manager, "refresh_catalog_from_disk", lambda: None)
    caplog.set_level(logging.WARNING, logger="app.main")

    async def exercise():
        started, stopped, worker_tasks, timeouts = set(), set(), {}, {}
        draining, release = asyncio.Event(), asyncio.Event()

        def worker(name):
            async def run():
                started.add(name)
                worker_tasks[name] = asyncio.current_task()
                try:
                    await asyncio.Event().wait()
                finally:
                    if name == "research":
                        draining.set()
                        await release.wait()
                    stopped.add(name)
                    if name == "monitoring":
                        raise RuntimeError("private-provider-error-must-not-be-logged")
            return run

        services = (
            (kline_insight_scheduler, "kline_insight_scheduler_loop", "scheduler"),
            (startup_data_check, "delayed_startup_data_check", "startup"),
            (company_updates, "company_updates_loop", "company"),
            (xueqiu_comments_service, "worker_loop", "comments"),
            (xueqiu_comments_scheduler, "scheduler_loop", "comments_scheduler"),
            (event_calendar_service, "scheduler_loop", "calendar"),
            (data_collection, "worker_loop", "collection"),
            (overseas_news, "scheduler_loop", "overseas"),
            (monitoring, "worker_loop", "monitoring"),
            (earnings, "worker_loop", "earnings"),
            (investment_research, "worker_loop", "research"),
        )
        for module, function, name in services:
            monkeypatch.setattr(module, function, worker(name))

        original_wait_for = asyncio.wait_for

        async def recorded_wait_for(awaitable, timeout):
            for name, task in worker_tasks.items():
                if awaitable is task:
                    timeouts[name] = timeout
            return await original_wait_for(awaitable, timeout)

        monkeypatch.setattr(asyncio, "wait_for", recorded_wait_for)

        async def application():
            async with lifespan(FastAPI()):
                await asyncio.sleep(0)
                expected = {name for _, _, name in services}
                expected.remove("calendar" if collection_enabled else "collection")
                assert started == expected
                if body_error:
                    raise ValueError("application body failed")

        application_task = asyncio.create_task(application())
        try:
            await original_wait_for(draining.wait(), 1)
            await asyncio.sleep(0)
            # The earlier monitoring finalizer has failed, but lifespan must
            # still wait until the later research worker releases its resources.
            assert not application_task.done()
            assert "research" not in stopped
            release.set()
            if body_error:
                with pytest.raises(ValueError, match="application body failed"):
                    await application_task
            else:
                await application_task
            assert stopped == started
            assert all(task.done() for task in worker_tasks.values())
            expected_timeouts = {"startup": 3.0, "company": 3.0, "comments": 3.0,
                                 "comments_scheduler": 3.0, "overseas": 5.0}
            if not collection_enabled:
                expected_timeouts["calendar"] = 3.0
            assert timeouts == expected_timeouts
        finally:
            release.set()
            # Also avoid leaking fake workers if a regression makes assertions fail.
            for task in worker_tasks.values():
                if not task.done():
                    task.cancel()
            await asyncio.gather(application_task, *worker_tasks.values(), return_exceptions=True)

    asyncio.run(exercise())
    assert "后台任务退出失败" in caplog.text and "RuntimeError" in caplog.text
    assert "private-provider-error-must-not-be-logged" not in caplog.text
