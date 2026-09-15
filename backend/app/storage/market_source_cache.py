"""行情数据源响应缓存：保存最近成功的快照、板块等小型结构化结果。"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from app.config import settings
from app.storage import data_asset_manager

_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def _path(kind: str, key: str):
    directory = settings.data_dir / "market_source_cache" / _SAFE.sub("_", kind)
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{_SAFE.sub('_', key)}.json"


def load_payload(kind: str, key: str) -> dict[str, Any] | None:
    path = _path(kind, key)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def save_payload(kind: str, key: str, payload: Any, *, source: str) -> dict[str, Any]:
    entry = {
        "payload": payload,
        "source": source,
        "saved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    item_count = len(payload) if isinstance(payload, list) else None
    data_asset_manager.atomic_write_json(
        _path(kind, key),
        entry,
        kind="market_source_cache",
        key=f"{kind}:{key}",
        source=source,
        item_count=item_count,
    )
    return entry


def load_health() -> dict[str, Any]:
    path = _path("_health", "market_sources")
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def record_health(source_id: str, *, ok: bool, detail: str = "") -> dict[str, Any]:
    health = load_health()
    prev = health.get(source_id) if isinstance(health.get(source_id), dict) else {}
    fail_count = 0 if ok else min(int(prev.get("fail_count") or 0) + 1, 999)
    health[source_id] = {
        "ok": ok,
        "fail_count": fail_count,
        "detail": detail[:500],
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    data_asset_manager.atomic_write_json(
        _path("_health", "market_sources"),
        health,
        kind="market_source_health",
        key="market_sources",
        source="runtime",
        item_count=len(health),
    )
    return health[source_id]
