"""Tushare 日线类接口的本地 JSON 缓存（backend/data/tushare_daily_cache/）。"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from app.config import settings

_KIND_DIR: dict[str, Path] = {}


def _ensure_dir(kind: str) -> Path:
    if kind not in _KIND_DIR:
        p = settings.data_dir / "tushare_daily_cache" / kind
        p.mkdir(parents=True, exist_ok=True)
        _KIND_DIR[kind] = p
    return _KIND_DIR[kind]


def token_fingerprint(token: str) -> str:
    t = (token or "").strip()
    if not t:
        return ""
    return hashlib.sha256(t.encode("utf-8")).hexdigest()[:16]


def _safe_slug(s: str) -> str:
    x = re.sub(r"[^a-zA-Z0-9._-]+", "_", s.strip())[:160]
    return x or "x"


def cache_file(kind: str, key: str) -> Path:
    return _ensure_dir(kind) / f"{_safe_slug(key)}.json"


def load_entry(kind: str, key: str) -> dict[str, Any] | None:
    path = cache_file(kind, key)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or "payload" not in raw:
            return None
        return raw
    except Exception:
        return None


def save_payload(kind: str, key: str, payload: dict[str, Any], token: str) -> None:
    path = cache_file(kind, key)
    entry = {
        "token_fp": token_fingerprint(token),
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "payload": _strip_volatile(payload),
    }
    path.write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")


def _strip_volatile(payload: dict[str, Any]) -> dict[str, Any]:
    """写入磁盘前去掉运行时字段。"""
    drop = {"cache_stale", "cache_warning", "fetch_error", "cache_saved_at", "cache_token_changed"}
    return {k: v for k, v in payload.items() if k not in drop}


def last_bar_date_from_candles(candles: list[dict[str, Any]]) -> date | None:
    if not candles:
        return None
    try:
        t = float(candles[-1].get("t"))
        return datetime.fromtimestamp(t).date()
    except (TypeError, ValueError, OSError):
        return None


def build_cache_warning(
    *,
    last_bar: date | None,
    fetch_error: str | None,
    token_changed: bool,
    no_token: bool,
) -> str:
    parts: list[str] = []
    if no_token:
        parts.append("当前未配置 Tushare token，以下为历史本地缓存。")
    else:
        parts.append("本次在线拉取失败，已回退为本地缓存。")
    if last_bar:
        days = (date.today() - last_bar).days
        if days < 0:
            days = 0
        parts.append(f"缓存中最后一根日线：{last_bar.isoformat()}；之后约 {days} 个自然日未能更新。")
    else:
        parts.append("缓存中暂无有效 K 线日期信息。")
    if token_changed and not no_token:
        parts.append("当前 token 与写入该缓存时不一致；若刚更换 token，请确认保存后重试拉取。")
    if fetch_error:
        parts.append(f"拉取失败原因：{fetch_error}")
    parts.append("若为 token 失效或权限不足，请更新 token 或检查积分与接口权限。")
    return "".join(parts)


def merge_stale_kline(
    entry: dict[str, Any],
    *,
    current_token: str,
    fetch_error: str | None,
    no_token: bool,
) -> dict[str, Any]:
    payload = dict(entry.get("payload") or {})
    candles = payload.get("candles") or []
    last_bar = last_bar_date_from_candles(candles)
    prev_fp = (entry.get("token_fp") or "").strip()
    cur_fp = token_fingerprint(current_token)
    token_changed = bool(cur_fp and prev_fp and cur_fp != prev_fp)
    warn = build_cache_warning(
        last_bar=last_bar,
        fetch_error=fetch_error,
        token_changed=token_changed,
        no_token=no_token,
    )
    payload["cache_stale"] = True
    payload["cache_warning"] = warn
    payload["cache_saved_at"] = entry.get("saved_at")
    payload["cache_token_changed"] = token_changed
    if fetch_error:
        payload["fetch_error"] = fetch_error
    return payload


def merge_stale_indices(
    entry: dict[str, Any],
    *,
    current_token: str,
    fetch_error: str | None,
    no_token: bool,
) -> dict[str, Any]:
    payload = dict(entry.get("payload") or {})
    td = (payload.get("trade_date") or "").strip()
    parts: list[str] = []
    if no_token:
        parts.append("当前未配置 Tushare token，以下为历史本地缓存的行业列表。")
    else:
        parts.append("本次在线拉取失败，已回退为本地缓存的行业列表。")
    if td:
        try:
            y, m, d = int(td[:4]), int(td[4:6]), int(td[6:8])
            list_day = date(y, m, d)
            days = (date.today() - list_day).days
            if days < 0:
                days = 0
            parts.append(f"列表基准日：{list_day.isoformat()}；距今约 {days} 个自然日未能更新。")
        except (ValueError, TypeError):
            parts.append(f"列表基准日：{td}。")
    prev_fp = (entry.get("token_fp") or "").strip()
    cur_fp = token_fingerprint(current_token)
    token_changed = bool(cur_fp and prev_fp and cur_fp != prev_fp)
    if token_changed and not no_token:
        parts.append("当前 token 与写入该缓存时不一致。")
    if fetch_error:
        parts.append(f"拉取失败原因：{fetch_error}")
    parts.append("若为 token 失效或权限不足，请更新 token 后重试。")
    payload["cache_stale"] = True
    payload["cache_warning"] = "".join(parts)
    payload["cache_saved_at"] = entry.get("saved_at")
    payload["cache_token_changed"] = token_changed
    if fetch_error:
        payload["fetch_error"] = fetch_error
    return payload
