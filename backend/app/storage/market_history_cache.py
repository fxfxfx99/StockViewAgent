"""公开市场时间序列缓存：按日期增量合并，供图表、下载与上游故障降级。"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from app.config import settings
from app.storage import data_asset_manager

_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")
_MAX_ROWS = 5000


def _path(kind: str, key: str):
    directory = settings.data_dir / "market_history_cache" / _SAFE.sub("_", kind)
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{_SAFE.sub('_', key)}.json"


def load_series(kind: str, key: str) -> dict[str, Any] | None:
    path = _path(kind, key)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def merge_save_series(kind: str, key: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    previous = load_series(kind, key) or {}
    merged: dict[str, dict[str, Any]] = {}
    for index, row in enumerate((previous.get("items") or []) + list(rows or [])):
        if not isinstance(row, dict):
            continue
        base_identity = str(row.get("trade_date") or row.get("date") or row.get("time") or index)
        if row.get("sector_code"):
            identity = f"{base_identity}:{row.get('sector_code')}"
        elif row.get("stock_code") and kind not in {"stock_capital_flow"}:
            identity = f"{base_identity}:{row.get('stock_code')}"
        else:
            identity = base_identity
        merged[identity] = row
    items = sorted(merged.values(), key=lambda row: str(row.get("trade_date") or row.get("date") or ""), reverse=True)
    items = items[:_MAX_ROWS]
    payload = {
        "items": items,
        "saved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "retention_rows": _MAX_ROWS,
    }
    data_asset_manager.atomic_write_json(
        _path(kind, key),
        payload,
        kind="market_history_cache",
        key=f"{kind}:{key}",
        source=kind,
        item_count=len(items),
    )
    return payload
