"""Every Shanghai calendar day at 09:00, including weekends; catch up today only."""
from __future__ import annotations

import asyncio
from datetime import datetime, time as clock_time, timedelta
import json
from zoneinfo import ZoneInfo

from app.config import settings
from app.storage import users_store, xueqiu_comments_store as store

SHANGHAI = ZoneInfo("Asia/Shanghai")


def enabled() -> bool:
    return bool(settings.enable_scheduler and getattr(settings, "xueqiu_comments_auto_refresh_enabled", True))


def daily_window(now: datetime | None = None) -> tuple[datetime, str]:
    now = now.astimezone(SHANGHAI) if now else datetime.now(SHANGHAI)
    due = datetime.combine(now.date(), clock_time(9), tzinfo=SHANGHAI)
    return due, (now.date() - timedelta(days=1)).isoformat()


def _read_watchlist(uid: int) -> list[str]:
    from app.services.xueqiu_comments_service import normalize_symbol

    path = settings.data_dir / "watchlists" / f"{uid}.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        symbols = data.get("symbols") if isinstance(data, dict) else None
        if not isinstance(symbols, list):
            return []
        cleaned = []
        for symbol in symbols[:store.MAX_USER_SYMBOLS]:
            try:
                normalized = normalize_symbol(symbol)
            except ValueError:
                continue
            if normalized not in cleaned:
                cleaned.append(normalized)
        return cleaned
    except (OSError, ValueError):
        return []


def existing_watchlists() -> dict[int, list[str]]:
    """Read existing files only: never synthesize watchlists for new/deleted users."""
    return {user.id: symbols for user in users_store.list_users() if (symbols := _read_watchlist(user.id))}


def schedule_info(uid: int, symbol: str, *, configured: dict, now: datetime | None = None) -> dict:
    from app.services.xueqiu_comments_service import can_refresh

    now = now.astimezone(SHANGHAI) if now else datetime.now(SHANGHAI)
    is_enabled = enabled()
    next_run = None
    if is_enabled and can_refresh(configured) and symbol in _read_watchlist(uid):
        due, target = daily_window(now)
        if now < due:
            next_run = int(due.timestamp())
        elif store.daily_exists(uid, symbol, target):
            next_run = int((due + timedelta(days=1)).timestamp())
        else:
            # Startup after 09:00 schedules yesterday at the next polling tick.
            next_run = int(now.timestamp())
    return {"enabled": is_enabled, "time": "09:00", "timezone": "Asia/Shanghai", "next_run_at": next_run}


def run_daily_batch(now: datetime | None = None) -> int:
    from app.services.xueqiu_comments_service import can_refresh, prerequisites

    if not enabled():
        return 0
    now = now.astimezone(SHANGHAI) if now else datetime.now(SHANGHAI)
    due, target = daily_window(now)
    if now < due:
        return 0
    queued = 0
    for uid, symbols in existing_watchlists().items():
        if not can_refresh(prerequisites(uid)):
            continue  # No daily key: configuring credentials later can still run today.
        for symbol in symbols:
            outcome = store.enqueue(uid, symbol, mode="previous_day", target_date=target, source="scheduled", now=int(now.timestamp()))
            queued += outcome == "queued"
    return queued


async def scheduler_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(run_daily_batch)
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(60)
