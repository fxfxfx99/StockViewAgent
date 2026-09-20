"""公司资料补充的最后成功结果；每个标的一份原子写入的公共缓存。"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.config import settings
from app.storage import data_asset_manager

_SYMBOL = re.compile(r"^\d{6}\.(?:SS|SH|SZ|BJ)$")


def _path(symbol: str) -> Path:
    symbol = symbol.strip().upper()
    if not _SYMBOL.fullmatch(symbol):
        raise ValueError("仅支持 A 股 Yahoo 格式代码")
    return settings.data_dir / "company_updates" / f"{symbol}.json"


def load(symbol: str) -> dict[str, Any]:
    raw = data_asset_manager.read_json_file(_path(symbol), default={})
    if not isinstance(raw, dict) or not isinstance(raw.get("bundle"), dict):
        return {}
    return raw


def save(symbol: str, record: dict[str, Any]) -> None:
    data_asset_manager.atomic_write_json(
        _path(symbol),
        record,
        kind="company_updates",
        key=symbol,
        source="company_sources",
        backup=False,
    )
