"""全 A 股公司基本信息缓存（东方财富 F10 公司概况），与自选股 profile 分离以加快首次展示。"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from app.config import settings

_FILE = "company_basic_universe.json"
_KEYS = ("name", "org_name", "main_business", "industry", "intro")


def _path() -> Path:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings.data_dir / _FILE


def _load_raw() -> dict[str, Any]:
    p = _path()
    if not p.exists():
        return {"meta": {"updated_at": 0, "version": 1}, "symbols": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"meta": {"updated_at": 0, "version": 1}, "symbols": {}}
        if "symbols" not in data or not isinstance(data["symbols"], dict):
            data["symbols"] = {}
        data.setdefault("meta", {})
        return data
    except (json.JSONDecodeError, OSError):
        return {"meta": {"updated_at": 0, "version": 1}, "symbols": {}}


def _atomic_write(data: dict[str, Any]) -> None:
    p = _path()
    raw = json.dumps(data, ensure_ascii=False, indent=2)
    fd, tmp = tempfile.mkstemp(
        dir=p.parent, prefix=".company_basic_", suffix=".tmp", text=True
    )
    try:
        os.write(fd, raw.encode("utf-8"))
        os.close(fd)
        os.replace(tmp, p)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def get_symbol(symbol: str) -> dict[str, Any] | None:
    sym = symbol.strip().upper()
    data = _load_raw()
    row = data["symbols"].get(sym)
    if not isinstance(row, dict):
        return None
    auto = row.get("auto")
    if not isinstance(auto, dict):
        return None
    return {
        "auto": {k: str(auto.get(k) or "").strip() for k in _KEYS},
        "em_fetched_at": int(row.get("em_fetched_at") or 0),
        "fetch_error": row.get("fetch_error"),
    }


def upsert_symbol(symbol: str, auto: dict[str, Any], fetch_error: str | None) -> None:
    sym = symbol.strip().upper()
    data = _load_raw()
    now = int(time.time())
    prev_row = data["symbols"].get(sym)
    prev_ts = int(prev_row.get("em_fetched_at") or 0) if isinstance(prev_row, dict) else 0
    data["symbols"][sym] = {
        "auto": {k: str(auto.get(k) or "").strip() for k in _KEYS},
        "em_fetched_at": now if not fetch_error else prev_ts,
        "fetch_error": fetch_error,
    }
    data["meta"]["updated_at"] = now
    data["meta"]["version"] = 1
    _atomic_write(data)


def meta() -> dict[str, Any]:
    data = _load_raw()
    m = data.get("meta") or {}
    syms = data.get("symbols") or {}
    ok = sum(
        1
        for v in syms.values()
        if isinstance(v, dict)
        and not (v.get("fetch_error"))
        and (str((v.get("auto") or {}).get("org_name") or "").strip() or str((v.get("auto") or {}).get("intro") or "").strip())
    )
    return {
        "symbol_count": len(syms),
        "with_f10_ok": ok,
        "updated_at": int(m.get("updated_at") or 0),
        "file": str(_path()),
    }


def merge_into_display(merged: dict[str, str], symbol: str) -> dict[str, str]:
    """若 merged 中基础字段为空，用全市场缓存补齐（不覆盖已有非空、不含用户 manual）。"""
    u = get_symbol(symbol)
    if not u:
        return merged
    auto = u.get("auto") or {}
    out = dict(merged)
    for k in _KEYS:
        if not (out.get(k) or "").strip() and (auto.get(k) or "").strip():
            out[k] = str(auto[k]).strip()
    return out
