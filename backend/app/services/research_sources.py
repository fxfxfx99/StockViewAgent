"""结构化「行研/数据入口」配置（来自用户整理的 PDF + Excel）。"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_DATA = Path(__file__).resolve().parent.parent / "data" / "research_sources.json"


@lru_cache(maxsize=1)
def load_catalog() -> dict:
    raw = _DATA.read_text(encoding="utf-8")
    return json.loads(raw)


def reload_catalog() -> dict:
    load_catalog.cache_clear()
    return load_catalog()
