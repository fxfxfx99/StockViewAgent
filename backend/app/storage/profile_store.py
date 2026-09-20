"""股票列表标的：公司信息缓存（东方财富 F10）+ 用户手动补充字段。"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from app.config import settings

PROFILES_FILE = "watchlist_profiles.json"
_MERGE_KEYS = ("name", "org_name", "main_business", "industry", "intro")


def _path() -> Path:
    return settings.data_dir / PROFILES_FILE


def _load_all_raw() -> dict[str, Any]:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    p = _path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_all_raw(data: dict[str, Any]) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    _path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get_entry(symbol: str) -> dict[str, Any]:
    sym = symbol.strip().upper()
    allp = _load_all_raw()
    raw = allp.get(sym)
    if not isinstance(raw, dict):
        return {"auto": {}, "manual": {}, "fetch_error": None, "em_fetched_at": 0, "tushare": {}}
    tu = raw.get("tushare")
    return {
        "auto": dict(raw.get("auto") or {}),
        "manual": dict(raw.get("manual") or {}),
        "fetch_error": raw.get("fetch_error"),
        "em_fetched_at": int(raw.get("em_fetched_at") or 0),
        "tushare": dict(tu) if isinstance(tu, dict) else {},
    }


def upsert_auto(symbol: str, auto: dict[str, Any], fetch_error: str | None) -> None:
    sym = symbol.strip().upper()
    allp = _load_all_raw()
    prev = allp.get(sym)
    if not isinstance(prev, dict):
        prev = {}
    manual = dict(prev.get("manual") or {}) if isinstance(prev.get("manual"), dict) else {}
    tu = prev.get("tushare")
    tushare = dict(tu) if isinstance(tu, dict) else {}
    previous_auto = prev.get("auto") if isinstance(prev.get("auto"), dict) else {}
    now = int(time.time())
    em_ts = now if not fetch_error else int(prev.get("em_fetched_at") or 0)
    row = {
        "auto": {
            # 外部源失败时保留最后成功的数据，同时记录本次错误供界面展示。
            key: str(auto.get(key) or (previous_auto.get(key) if fetch_error else "") or "")
            for key in _MERGE_KEYS
        },
        "manual": manual,
        "fetch_error": fetch_error,
        "em_fetched_at": em_ts,
    }
    if tushare:
        row["tushare"] = tushare
    allp[sym] = row
    _save_all_raw(allp)


def upsert_tushare(symbol: str, payload: dict[str, Any], fetch_error: str | None) -> None:
    """合并 Tushare 缓存（basic + daily），不覆盖东方财富 F10 的 auto。"""
    sym = symbol.strip().upper()
    allp = _load_all_raw()
    prev = allp.get(sym)
    if not isinstance(prev, dict):
        prev = {"auto": {}, "manual": {}, "fetch_error": None, "em_fetched_at": 0}
    previous_tushare = prev.get("tushare")
    block = dict(previous_tushare) if fetch_error and isinstance(previous_tushare, dict) else {}
    block.update(payload)
    block["fetch_error"] = fetch_error
    if not fetch_error:
        block["fetched_at"] = int(time.time())
    prev["tushare"] = block
    allp[sym] = prev
    _save_all_raw(allp)


def patch_manual(symbol: str, patch: dict[str, Any]) -> dict[str, Any]:
    sym = symbol.strip().upper()
    allp = _load_all_raw()
    prev = allp.get(sym)
    if not isinstance(prev, dict):
        prev = {"auto": {}, "manual": {}, "fetch_error": None, "em_fetched_at": 0}
    manual = dict(prev.get("manual") or {})
    for k in _MERGE_KEYS:
        if k not in patch:
            continue
        v = patch[k]
        if v is None or (isinstance(v, str) and not v.strip()):
            manual.pop(k, None)
        elif isinstance(v, str):
            manual[k] = v.strip()
    prev["manual"] = manual
    allp[sym] = prev
    _save_all_raw(allp)
    return manual


def remove_symbol(symbol: str) -> None:
    sym = symbol.strip().upper()
    allp = _load_all_raw()
    if sym in allp:
        del allp[sym]
        _save_all_raw(allp)


def merge_display(entry: dict[str, Any]) -> dict[str, str]:
    auto = entry.get("auto") or {}
    manual = entry.get("manual") or {}
    out: dict[str, str] = {}
    for k in _MERGE_KEYS:
        mv = manual.get(k)
        if isinstance(mv, str) and mv.strip():
            out[k] = mv.strip()
        else:
            out[k] = str(auto.get(k) or "").strip()
    return out


def prune_orphans(valid_symbols: set[str]) -> None:
    allp = _load_all_raw()
    for k in list(allp.keys()):
        if k not in valid_symbols:
            del allp[k]
    _save_all_raw(allp)
