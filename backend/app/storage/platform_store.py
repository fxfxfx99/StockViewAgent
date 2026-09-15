"""平台级配置（管理员）：如 K 线可选 SDK 开关。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.config import settings
from app.storage import data_asset_manager

_FILE = "platform.json"


def _path() -> Path:
    return settings.data_dir / _FILE


def _default() -> dict[str, Any]:
    return {
        "kline_sdk": {
            "baostock": True,
            "pytdx": True,
        },
    }


def load_platform() -> dict[str, Any]:
    p = _path()
    if not p.exists():
        data = _default()
        data_asset_manager.atomic_write_json(
            p,
            data,
            kind="platform_config",
            key="platform",
            source="default",
            backup=False,
        )
        return data
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return _default()
    if not isinstance(raw, dict):
        return _default()
    base = _default()
    base.update(raw)
    if isinstance(raw.get("kline_sdk"), dict):
        base["kline_sdk"] = {**_default()["kline_sdk"], **raw["kline_sdk"]}
    return base


def save_platform_merge(**kwargs: Any) -> dict[str, Any]:
    cur = load_platform()
    for k, v in kwargs.items():
        if v is None:
            continue
        if k == "kline_sdk" and isinstance(v, dict):
            cur["kline_sdk"] = {**(cur.get("kline_sdk") or {}), **v}
        else:
            cur[k] = v
    data_asset_manager.atomic_write_json(
        _path(),
        cur,
        kind="platform_config",
        key="platform",
        source="admin",
    )
    return cur


def kline_sdk_enabled(name: str) -> bool:
    p = load_platform()
    m = p.get("kline_sdk") or {}
    return bool(m.get(name, True))
