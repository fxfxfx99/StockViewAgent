"""K 线 bundle 本地 JSON 缓存：在线拉取失败时回退，保证前端仍可出图与导出。"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import settings
from app.services.market_time import market_date
from app.storage import data_asset_manager

_DIR_NAME = "kline_bundle_cache"


def _dir() -> Path:
    p = settings.data_dir / _DIR_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def _safe_key(sym: str, range_param: str, interval: str) -> str:
    raw = f"{sym.strip().upper()}_{range_param}_{interval}"
    return re.sub(r"[^a-zA-Z0-9._-]+", "_", raw)[:200]


def _path(sym: str, range_param: str, interval: str) -> Path:
    return _dir() / f"{_safe_key(sym, range_param, interval)}.json"


def last_bar_date_str(candles: list[dict[str, Any]]) -> str | None:
    if not candles:
        return None
    try:
        t = float(candles[-1].get("t") or 0)
        return market_date(t).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def load_bundle(sym: str, range_param: str, interval: str) -> dict[str, Any] | None:
    p = _path(sym, range_param, interval)
    if not p.is_file():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or "payload" not in raw:
            return None
        return raw
    except (json.JSONDecodeError, OSError):
        return None


def save_bundle(sym: str, range_param: str, interval: str, payload: dict[str, Any]) -> None:
    """成功拉取后写入；payload 为完整 kline bundle。"""
    entry = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "payload": _strip_ephemeral(dict(payload)),
    }
    p = _path(sym, range_param, interval)
    data_asset_manager.atomic_write_json(
        p,
        entry,
        kind="kline_bundle_cache",
        key=_safe_key(sym, range_param, interval),
        source=str(payload.get("kline_source") or "unknown"),
        item_count=len(payload.get("candles") or []),
    )


def _strip_ephemeral(d: dict[str, Any]) -> dict[str, Any]:
    drop = {
        "served_from_cache",
        "cache_stale",
        "cache_warning",
        "fetch_error_detail",
        "data_as_of",
    }
    return {k: v for k, v in d.items() if k not in drop}


def merge_stale_response(
    entry: dict[str, Any],
    *,
    fetch_error: str,
) -> dict[str, Any]:
    payload = dict(entry.get("payload") or {})
    candles = payload.get("candles") or []
    last_d = last_bar_date_str(candles)
    payload["served_from_cache"] = True
    payload["cache_stale"] = True
    payload["data_as_of"] = last_d
    payload["fetch_error_detail"] = fetch_error[:2000]
    if last_d:
        payload["cache_warning"] = (
            f"在线行情源暂不可用，已使用本地缓存。数据已更新至 {last_d}。原因：{fetch_error[:500]}"
        )
    else:
        payload["cache_warning"] = f"在线行情源暂不可用，已使用本地缓存（无有效日期）。原因：{fetch_error[:500]}"
    payload["cache_saved_at"] = entry.get("saved_at")
    return payload
