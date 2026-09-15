"""统一数据资产管理：原子写入、轻量备份与目录索引。

现有业务文件仍保留在 backend/data 原位置；本模块只新增 backend/data/_managed
作为索引与备份目录，避免一次性迁移造成路径兼容风险。
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import settings

_MANAGED_DIR = "_managed"
_CATALOG_NAME = "catalog.json"
_BACKUP_DIR = "backups"
_TIME_FMT = "%Y%m%dT%H%M%S%fZ"


def managed_root() -> Path:
    root = settings.data_dir / _MANAGED_DIR
    (root / _BACKUP_DIR).mkdir(parents=True, exist_ok=True)
    return root


def catalog_path() -> Path:
    return managed_root() / _CATALOG_NAME


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(settings.data_dir.resolve()))
    except ValueError:
        return str(path.resolve())


def _asset_id(path: Path) -> str:
    return hashlib.sha1(_relative(path).encode("utf-8")).hexdigest()[:16]


def _asset_backup_dir(path: Path) -> Path:
    d = managed_root() / _BACKUP_DIR / _asset_id(path)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_catalog() -> dict[str, Any]:
    path = catalog_path()
    if not path.exists():
        return {"version": 1, "updated_at": _now_iso(), "assets": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "updated_at": _now_iso(), "assets": {}}
    if not isinstance(raw, dict):
        return {"version": 1, "updated_at": _now_iso(), "assets": {}}
    raw.setdefault("version", 1)
    raw.setdefault("assets", {})
    return raw


def _write_catalog(catalog: dict[str, Any]) -> None:
    catalog["updated_at"] = _now_iso()
    path = catalog_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(catalog, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp_name, path)
    finally:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except OSError:
            pass


def _json_size_hint(data: Any) -> int:
    try:
        return len(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError):
        return 0


def _record_asset(
    path: Path,
    *,
    kind: str,
    key: str,
    source: str = "",
    item_count: int | None = None,
    backup_path: Path | None = None,
    size_bytes: int | None = None,
) -> None:
    rel = _relative(path)
    catalog = _load_catalog()
    assets = catalog.setdefault("assets", {})
    prev = assets.get(rel) if isinstance(assets.get(rel), dict) else {}
    backups = list(prev.get("backups") or [])
    if backup_path is not None:
        backups.append(_relative(backup_path))
    max_backups = max(0, int(settings.data_asset_max_backups))
    if max_backups:
        backups = backups[-max_backups:]
    else:
        backups = []
    assets[rel] = {
        "kind": kind,
        "key": key,
        "source": source,
        "path": rel,
        "size_bytes": size_bytes if size_bytes is not None else (path.stat().st_size if path.exists() else 0),
        "item_count": item_count,
        "updated_at": _now_iso(),
        "backups": backups,
    }
    _write_catalog(catalog)


def _prune_backups(path: Path) -> None:
    max_backups = max(0, int(settings.data_asset_max_backups))
    if max_backups <= 0:
        return
    d = _asset_backup_dir(path)
    files = sorted([p for p in d.glob("*.json") if p.is_file()], key=lambda p: p.stat().st_mtime)
    for old in files[:-max_backups]:
        try:
            old.unlink()
        except OSError:
            pass


def _backup_existing(path: Path) -> Path | None:
    if not path.exists() or not settings.enable_data_asset_backups:
        return None
    try:
        suffix = path.suffix or ".json"
        name = f"{_now().strftime(_TIME_FMT)}{suffix}"
        backup = _asset_backup_dir(path) / name
        backup.write_bytes(path.read_bytes())
        _prune_backups(path)
        return backup
    except OSError:
        return None


def atomic_write_json(
    path: Path,
    data: Any,
    *,
    kind: str,
    key: str,
    source: str = "",
    item_count: int | None = None,
    backup: bool = True,
) -> None:
    """JSON 原子写入；覆盖前按配置保留备份，并刷新 catalog。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_path = _backup_existing(path) if backup else None
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp_name, path)
    finally:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except OSError:
            pass
    _record_asset(
        path,
        kind=kind,
        key=key,
        source=source,
        item_count=item_count,
        backup_path=backup_path,
        size_bytes=path.stat().st_size if path.exists() else _json_size_hint(data),
    )


def read_json_file(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def register_existing_asset(
    path: Path,
    *,
    kind: str,
    key: str,
    source: str = "",
    item_count: int | None = None,
) -> None:
    if path.exists():
        _record_asset(path, kind=kind, key=key, source=source, item_count=item_count)


def _infer_asset_meta(rel: str, path: Path) -> dict[str, Any]:
    name = path.name
    stem = path.stem
    if rel.startswith("kline_bundle_cache/"):
        return {"kind": "kline_bundle_cache", "key": stem, "source": "local_cache"}
    if rel.startswith("market_history_cache/"):
        parts = rel.split("/")
        kind = parts[1] if len(parts) > 2 else "series"
        return {"kind": "market_history_cache", "key": f"{kind}:{stem}", "source": kind}
    if rel.startswith("market_source_cache/_health/"):
        return {"kind": "market_source_health", "key": stem, "source": "runtime"}
    if rel.startswith("market_source_cache/"):
        parts = rel.split("/")
        source_kind = parts[1] if len(parts) > 2 else "payload"
        return {"kind": "market_source_cache", "key": f"{source_kind}:{stem}", "source": source_kind}
    if rel.startswith("tushare_daily_cache/"):
        parts = rel.split("/")
        source_kind = parts[1] if len(parts) > 2 else "payload"
        return {"kind": "tushare_daily_cache", "key": f"{source_kind}:{stem}", "source": "tushare"}
    if rel.startswith("watchlists/"):
        return {"kind": "watchlist", "key": stem, "source": "user"}
    if rel.startswith("kline_insights/"):
        return {"kind": "kline_insights", "key": stem, "source": "scheduler_or_user"}
    mapping = {
        "a_share_stocks.json": ("stock_universe", "a_share_stocks", "eastmoney_clist"),
        "issuer_basic_info_universe.json": ("stock_universe", "issuer_basic_info", "issuer_uploads"),
        "company_basic_universe.json": ("company_profile_cache", "company_basic_universe", "eastmoney_f10"),
        "watchlist.json": ("watchlist_legacy", "legacy", "legacy"),
        "watchlist_profiles.json": ("watchlist_profiles", "profiles", "user"),
        "platform.json": ("platform_config", "platform", "admin"),
        "integrations.json": ("integrations_config", "integrations", "admin"),
        "startup_data_check_state.json": ("startup_data_check", "latest", "startup"),
        "cn_headline_sources_state.json": ("news_source_state", "cn_headline_sources", "maintenance"),
        "cn_headline_sources_override.json": ("news_source_config", "cn_headline_sources_override", "admin"),
        "news.db": ("sqlite_db", "news", "app"),
        "users.db": ("sqlite_db", "users", "app"),
        "quant.db": ("sqlite_db", "quant", "app"),
        "research_agent.db": ("sqlite_db", "research_agent", "app"),
        "company_fundamentals.db": ("sqlite_db", "company_fundamentals", "company_fundamentals_store"),
        "strategy_knowledge.db": ("sqlite_db", "strategy_knowledge", "strategy_knowledge_store"),
        "decision_signals.db": ("sqlite_db", "decision_signals", "decision_signal_store"),
    }
    if name in mapping:
        kind, key, source = mapping[name]
        return {"kind": kind, "key": key, "source": source}
    if name.endswith(".db"):
        return {"kind": "sqlite_db", "key": stem, "source": "app"}
    if name.endswith(".json"):
        return {"kind": "json_data", "key": stem, "source": ""}
    return {"kind": "data_file", "key": stem, "source": ""}


def scan_data_assets() -> dict[str, Any]:
    """扫描 backend/data 并合并 catalog，用于管理页/API 展示。"""
    catalog = _load_catalog()
    catalog_assets = catalog.get("assets") if isinstance(catalog.get("assets"), dict) else {}
    assets: list[dict[str, Any]] = []
    for p in sorted(settings.data_dir.rglob("*")):
        if not p.is_file():
            continue
        rel = _relative(p)
        if rel.startswith(f"{_MANAGED_DIR}/"):
            continue
        meta = dict(catalog_assets.get(rel) or {})
        inferred = _infer_asset_meta(rel, p)
        stat = p.stat()
        if meta.get("kind") in (None, "", "unclassified"):
            meta["kind"] = inferred["kind"]
        if meta.get("key") in (None, ""):
            meta["key"] = inferred["key"]
        if meta.get("source") in (None, ""):
            meta["source"] = inferred["source"]
        meta["path"] = rel
        meta["size_bytes"] = stat.st_size
        meta.setdefault("updated_at", datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat())
        assets.append(meta)

    totals: dict[str, Any] = {"files": len(assets), "size_bytes": sum(int(a.get("size_bytes") or 0) for a in assets)}
    by_kind: dict[str, dict[str, int]] = {}
    for a in assets:
        kind = str(a.get("kind") or "unclassified")
        ent = by_kind.setdefault(kind, {"files": 0, "size_bytes": 0})
        ent["files"] += 1
        ent["size_bytes"] += int(a.get("size_bytes") or 0)

    return {
        "managed_dir": _relative(managed_root()),
        "catalog": _relative(catalog_path()),
        "totals": totals,
        "by_kind": by_kind,
        "items": assets,
    }


def refresh_catalog_from_disk() -> dict[str, Any]:
    """把当前磁盘文件写入 catalog；已有业务写入记录的 kind/source 会被保留。"""
    scanned = scan_data_assets()
    catalog = _load_catalog()
    existing = catalog.get("assets") if isinstance(catalog.get("assets"), dict) else {}
    next_assets: dict[str, Any] = {}
    for item in scanned["items"]:
        rel = str(item.get("path") or "")
        if not rel:
            continue
        prev = existing.get(rel) if isinstance(existing.get(rel), dict) else {}
        merged = {**item}
        for field in ("kind", "key", "source", "item_count", "backups"):
            if field in prev and (field not in merged or merged.get(field) in (None, "", "unclassified")):
                merged[field] = prev[field]
        next_assets[rel] = merged
    catalog["assets"] = next_assets
    _write_catalog(catalog)
    return scan_data_assets()
