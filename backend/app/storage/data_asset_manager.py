"""统一数据资产管理：原子写入、轻量备份与目录索引。

所有本地资产跟随 STOCKVIEW_DATA_DIR；索引与写入前备份位于 _managed。
"""
from __future__ import annotations

from contextlib import contextmanager
from functools import wraps
import fcntl
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


@contextmanager
def catalog_lock():
    """Serialize catalog read/modify/write across API and worker processes."""
    with (managed_root() / "catalog.lock").open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _catalog_writer(function):
    @wraps(function)
    def locked(*args, **kwargs):
        with catalog_lock():
            return function(*args, **kwargs)
    return locked


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


@_catalog_writer
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
    if Path(rel).is_absolute():
        return  # External sources are not portable assets of this directory.
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
    if rel.startswith("research/"):
        return {"kind": "research_immutable_data", "key": rel, "source": "research_archive"}
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
        "research.sqlite3": ("sqlite_db", "research", "research_store"),
    }
    if name in mapping:
        kind, key, source = mapping[name]
        return {"kind": kind, "key": key, "source": source}
    if name.endswith(".db"):
        return {"kind": "sqlite_db", "key": stem, "source": "app"}
    if name.endswith(".json"):
        return {"kind": "json_data", "key": stem, "source": ""}
    return {"kind": "data_file", "key": stem, "source": ""}


# Categories describe ownership/retention without deciding deletion policy.
_CATEGORIES = {
    "research": ("研究与策略证据", "durable"),
    "knowledge": ("资料与知识库", "durable"),
    "news": ("新闻与公司资料", "durable"),
    "accounts": ("账户与配置", "durable"),
    "cache": ("行情与接口缓存", "rebuildable"),
    "runtime": ("运行日志与状态", "runtime"),
    "backups": ("历史备份", "backup"),
    "index": ("索引与迁移记录", "durable"),
    "other": ("其他本地资产", "durable"),
}


def _category(rel: str) -> str:
    first = rel.split("/", 1)[0]
    if rel.startswith(("_managed/backups/", "_managed/legacy/")):
        return "backups"
    if first == "_managed" or first == "INDEX.md":
        return "index"
    if rel.endswith(("-wal", "-journal")):
        return _category(rel.rsplit("-", 1)[0])  # May contain committed database changes.
    if first == "runtime" or rel.endswith((".lock", ".pid", "-shm")):
        return "runtime"
    if first in {"research", "research.sqlite3", "research_agent.db", "quant.db", "decision_signals.db", "stock_picker_agents.db"}:
        return "research"
    if first in {"issuer_uploads", "kline_learning", "imports", "strategy_knowledge.db", "kline_insights"}:
        return "knowledge"
    if first in {"news.db", "company_fundamentals.db", "company_updates", "company_basic_universe.json", "issuer_basic_info_universe.json", "xueqiu_comments.db"}:
        return "news"
    if first in {"users.db", "watchlists", "watchlist.json", "watchlist_profiles.json", "llm_runtime.json", "integrations.json", "platform.json", "cn_headline_sources_override.json"}:
        return "accounts"
    if "cache" in first or first in {"a_share_stocks.json"}:
        return "cache"
    return "other"


def _walk_files(root: Path):
    # Never traverse symlinks: a portable root must not silently index another
    # directory, nor follow a mount/symlink loop during inventory.
    for base, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not (Path(base) / d).is_symlink())
        for name in sorted(files):
            path = Path(base) / name
            if path.is_symlink() or not path.is_file():
                continue
            try:
                yield path, path.stat()
            except FileNotFoundError:
                continue  # Cache rotation may race with a read-only scan.


def scan_data_assets() -> dict[str, Any]:
    """Inventory real files, including backups; never read secret contents."""
    root = settings.data_dir.resolve()
    catalog = _load_catalog()
    catalog_assets = catalog.get("assets") if isinstance(catalog.get("assets"), dict) else {}
    assets: list[dict[str, Any]] = []
    all_items: list[dict[str, Any]] = []
    categories = {key: {"id": key, "label": value[0], "retention": value[1], "files": 0, "size_bytes": 0}
                  for key, value in _CATEGORIES.items()}
    for path, stat in _walk_files(root):
        rel = path.relative_to(root).as_posix()
        meta = dict(catalog_assets.get(rel) or {})
        inferred = _infer_asset_meta(rel, path)
        for field in ("kind", "key", "source"):
            if meta.get(field) in (None, "", "unclassified"):
                meta[field] = inferred[field]
        meta.update(path=rel, size_bytes=stat.st_size, category=_category(rel),
                    updated_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat())
        # Discard stale absolute backup locators from older catalogs.
        meta["backups"] = [b for b in (meta.get("backups") or []) if isinstance(b, str) and not Path(b).is_absolute() and ".." not in Path(b).parts]
        all_items.append(meta)
        category = categories[meta["category"]]
        category["files"] += 1
        category["size_bytes"] += stat.st_size
        if not rel.startswith(f"{_MANAGED_DIR}/"):
            assets.append(meta)
    totals = {"files": len(assets), "size_bytes": sum(a["size_bytes"] for a in assets)}
    directory_totals = {"files": len(all_items), "size_bytes": sum(a["size_bytes"] for a in all_items)}
    by_kind: dict[str, dict[str, int]] = {}
    for a in assets:
        entry = by_kind.setdefault(a["kind"], {"files": 0, "size_bytes": 0})
        entry["files"] += 1
        entry["size_bytes"] += a["size_bytes"]
    return {
        "data_root": str(root), "managed_dir": _MANAGED_DIR, "catalog": f"{_MANAGED_DIR}/{_CATALOG_NAME}",
        "totals": totals, "directory_totals": directory_totals, "by_kind": by_kind, "items": assets,
        "categories": list(categories.values()),
        "index": {"path": f"{_MANAGED_DIR}/{_CATALOG_NAME}", "guide": "INDEX.md", "updated_at": catalog.get("updated_at")},
        "migration": {
            "commands": "./backend/.venv/bin/python scripts/storage.py migrate --to '/path/to/StockViewAgent-data'",
            "notes": ["先停止 start.sh 管理的服务及 Docker；在项目根目录执行，替换为尚不存在的新目录路径。",
                      "迁移校验 SQLite 和文件哈希，成功后原目录仍保留；按命令输出设置 STOCKVIEW_DATA_DIR 后再启动。",
                      "目录包含已保存的 API 配置、账户和资料，请作为私有备份保管；外部 .env 与外部数据服务需另行配置。",
                      "刷新索引只扫描文件，不抓取行情、不调用模型，也不清理历史证据。"],
        },
    }


@_catalog_writer
def refresh_catalog_from_disk() -> dict[str, Any]:
    """Rebuild portable relative locators without discarding metadata."""
    scanned = scan_data_assets()
    catalog = _load_catalog()
    root = settings.data_dir.resolve()
    next_assets = {item["path"]: item for item in scanned["items"]}
    # Backups are part of the persistent inventory too. The catalog and its
    # lock/guide are generated artifacts; excluding them avoids self-reference.
    for path, stat in _walk_files(root / _MANAGED_DIR):
        rel = path.relative_to(root).as_posix()
        if rel in {f"{_MANAGED_DIR}/{_CATALOG_NAME}", f"{_MANAGED_DIR}/catalog.lock"}:
            continue
        next_assets[rel] = {**_infer_asset_meta(rel, path), "path": rel, "size_bytes": stat.st_size,
                            "category": _category(rel), "updated_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()}
    catalog.update(version=2, assets=next_assets)
    _write_catalog(catalog)
    return scan_data_assets()
