"""用户维度的 K 线解读历史：手动解读与盘后定时解读均写入 backend/data/kline_insights/{user_id}.json。"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.config import settings


def shanghai_report_date_str() -> str:
    """解读归档用的自然日（上海时区），与盘后 15:00 任务同一天标签一致。"""
    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")

_DIR_NAME = "kline_insights"


def _dir() -> Path:
    d = settings.data_dir / _DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _file(user_id: int) -> Path:
    return _dir() / f"{int(user_id)}.json"


def load_raw(user_id: int) -> dict[str, Any]:
    p = _file(user_id)
    if not p.exists():
        return {"opt_in": {}, "symbols": {}}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"opt_in": {}, "symbols": {}}
    if not isinstance(raw, dict):
        return {"opt_in": {}, "symbols": {}}
    raw.setdefault("opt_in", {})
    raw.setdefault("symbols", {})
    if not isinstance(raw["opt_in"], dict):
        raw["opt_in"] = {}
    if not isinstance(raw["symbols"], dict):
        raw["symbols"] = {}
    return raw


def _save_raw(user_id: int, data: dict[str, Any]) -> None:
    _file(user_id).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def is_opt_in(user_id: int, symbol: str) -> bool:
    sym = (symbol or "").strip().upper()
    if not sym:
        return False
    return bool(load_raw(user_id).get("opt_in", {}).get(sym))


def set_opt_in(user_id: int, symbol: str, value: bool = True) -> None:
    sym = (symbol or "").strip().upper()
    if not sym:
        return
    data = load_raw(user_id)
    data.setdefault("opt_in", {})[sym] = bool(value)
    _save_raw(user_id, data)


def upsert_insight(
    user_id: int,
    symbol: str,
    report_date: str,
    interpretation: str,
    *,
    source: str,
    model: str,
) -> None:
    """同一标的同一自然日仅保留一条（覆盖）。"""
    sym = (symbol or "").strip().upper()
    rd = (report_date or "").strip()[:10]
    if not sym or not rd:
        return
    data = load_raw(user_id)
    rows: list[dict[str, Any]] = list(data.setdefault("symbols", {}).get(sym) or [])
    row = {
        "report_date": rd,
        "interpretation": (interpretation or "").strip(),
        "source": source if source in ("manual", "scheduled") else "manual",
        "model": (model or "").strip(),
    }
    replaced = False
    for i, x in enumerate(rows):
        if str(x.get("report_date") or "")[:10] == rd:
            rows[i] = row
            replaced = True
            break
    if not replaced:
        rows.append(row)
    rows.sort(key=lambda x: str(x.get("report_date") or ""), reverse=True)
    data.setdefault("symbols", {})[sym] = rows
    _save_raw(user_id, data)


def history_for_symbol(user_id: int, symbol: str) -> dict[str, Any]:
    """供 GET：仅当用户曾对该标的点击过解读（opt_in）才返回条目。"""
    sym = (symbol or "").strip().upper()
    if not sym:
        return {"opt_in": False, "dates": [], "entries": {}}
    data = load_raw(user_id)
    if not bool(data.get("opt_in", {}).get(sym)):
        return {"opt_in": False, "dates": [], "entries": {}}
    rows: list[dict[str, Any]] = list(data.get("symbols", {}).get(sym) or [])
    dates = [str(x.get("report_date") or "")[:10] for x in rows if x.get("report_date")]
    dates = sorted(set(dates), reverse=True)
    entries = {str(x.get("report_date") or "")[:10]: x for x in rows if x.get("report_date")}
    return {"opt_in": True, "dates": dates, "entries": entries}
