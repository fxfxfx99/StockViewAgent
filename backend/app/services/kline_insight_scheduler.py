"""
盘后（上海 15:00–15:15，工作日）对「曾手动解读过」且仍在自选列表中的标的自动跑 K 线解读并归档。
全局每日最多执行一次（见 kline_insight_scheduler_state.json）。
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import settings
from app.security.user_context import current_user_id
from app.services import kline_learning_service as kls
from app.storage import kline_insight_store, watchlist_store

logger = logging.getLogger(__name__)
_SH = ZoneInfo("Asia/Shanghai")
_STATE_NAME = "kline_insight_scheduler_state.json"
# 与前端一键解读默认对齐
_DEFAULT_RANGE = "1y"
_DEFAULT_INTERVAL = "1d"
_DEFAULT_CANDLE_ROWS = 32


def _state_path() -> Path:
    p = settings.data_dir / _STATE_NAME
    return p


def _load_state() -> dict:
    p = _state_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(data: dict) -> None:
    _state_path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _list_watchlist_user_ids() -> list[int]:
    d = settings.data_dir / "watchlists"
    if not d.is_dir():
        return []
    out: list[int] = []
    for p in sorted(d.glob("*.json")):
        try:
            out.append(int(p.stem))
        except ValueError:
            continue
    return out


async def run_daily_batch_if_configured() -> None:
    """执行一次批量解读（不检查时间窗口；供测试或手动触发）。"""
    today = datetime.now(_SH).strftime("%Y-%m-%d")
    uids = _list_watchlist_user_ids()
    for uid in uids:
        # 定时任务没有 HTTP 依赖，必须显式切换账户，才能使用其个人 LLM/行情凭证。
        token = current_user_id.set(uid)
        try:
            if not settings.any_llm_key_configured:
                logger.info("跳过盘后 K 线批量解读：user=%s 未配置 LLM Key", uid)
                continue
            symbols = watchlist_store.load_symbols(uid)
            for sym in symbols:
                if not kline_insight_store.is_opt_in(uid, sym):
                    continue
                try:
                    out = await kls.analyze_a_share_symbol(
                        sym,
                        range_param=_DEFAULT_RANGE,
                        interval=_DEFAULT_INTERVAL,
                        candle_rows=_DEFAULT_CANDLE_ROWS,
                    )
                    kline_insight_store.upsert_insight(
                        uid,
                        sym,
                        kline_insight_store.shanghai_report_date_str(),
                        out.get("interpretation") or "",
                        source="scheduled",
                        model=str(out.get("model") or ""),
                    )
                    logger.info("盘后解读已保存 user=%s symbol=%s date=%s", uid, sym, today)
                except Exception:
                    logger.exception("盘后解读失败 user=%s symbol=%s", uid, sym)
                await asyncio.sleep(2.0)
        finally:
            current_user_id.reset(token)


async def kline_insight_scheduler_loop() -> None:
    """后台循环：工作日 15:00–15:14 触发当日一次批量任务。"""
    while True:
        try:
            await asyncio.sleep(30)
            now = datetime.now(_SH)
            if now.weekday() >= 5:
                continue
            if not (now.hour == 15 and now.minute < 15):
                continue
            today = now.strftime("%Y-%m-%d")
            st = _load_state()
            if st.get("last_batch_date") == today:
                continue
            await run_daily_batch_if_configured()
            st["last_batch_date"] = today
            _save_state(st)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("盘后 K 线解读调度异常")
