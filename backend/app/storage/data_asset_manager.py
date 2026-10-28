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
import stat
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
    if Path(settings.data_dir).is_symlink() or root.is_symlink() or (root / _BACKUP_DIR).is_symlink():
        raise ValueError("数据管理目录不能是符号链接")
    (root / _BACKUP_DIR).mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


def catalog_path() -> Path:
    return managed_root() / _CATALOG_NAME


@contextmanager
def catalog_lock():
    """Serialize catalog read/modify/write across API and worker processes."""
    descriptor = os.open(managed_root() / "catalog.lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "a+") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode) or os.fstat(handle.fileno()).st_nlink != 1:
            raise ValueError("数据索引锁文件无效")
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
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 64 * 1024 ** 2:
                raise ValueError("数据索引文件无效")
            raw = json.load(handle)
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
    if rel.startswith("datasets/"):
        return {"kind": "collected_immutable_data", "key": rel, "source": "data_center"}
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
        "tracking.sqlite3": ("sqlite_db", "tracking", "tracking_store"),
        "economic_events.sqlite3": ("sqlite_db", "economic_events", "event_calendar_store"),
        "data_center.sqlite3": ("sqlite_db", "data_center", "data_collection_store"),
    }
    if name in mapping:
        kind, key, source = mapping[name]
        return {"kind": kind, "key": key, "source": source}
    if name.endswith((".db", ".sqlite", ".sqlite3")):
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
    "quarantine": ("缓存回收区", "recoverable"),
    "index": ("索引与迁移记录", "durable"),
    "other": ("其他本地资产", "durable"),
}


def _category(rel: str) -> str:
    first = rel.split("/", 1)[0]
    if rel.startswith("_managed/quarantine/"):
        return "quarantine"
    if rel.startswith(("_managed/backups/", "_managed/legacy/")):
        return "backups"
    if first == "_managed" or first == "INDEX.md":
        return "index"
    if rel.endswith(("-wal", "-journal")):
        return _category(rel.rsplit("-", 1)[0])  # May contain committed database changes.
    if first == "runtime" or rel.endswith((".lock", ".pid", "-shm")):
        return "runtime"
    if first in {"research", "research.sqlite3", "tracking.sqlite3", "research_agent.db", "quant.db", "decision_signals.db", "stock_picker_agents.db"}:
        return "research"
    if first in {"issuer_uploads", "kline_learning", "imports", "strategy_knowledge.db", "kline_insights", "economic_events.sqlite3", "data_center.sqlite3", "datasets"}:
        return "knowledge"
    if first in {"news.db", "company_fundamentals.db", "company_updates", "company_basic_universe.json", "issuer_basic_info_universe.json", "xueqiu_comments.db"}:
        return "news"
    if first in {"users.db", "watchlists", "watchlist.json", "watchlist_profiles.json", "llm_runtime.json", "integrations.json", "platform.json", "cn_headline_sources_override.json"}:
        return "accounts"
    if "cache" in first or first in {"a_share_stocks.json"}:
        return "cache"
    return "other"


def _walk_files(root: Path):
    # Descriptor traversal also refuses an ancestor swapped to a symlink during
    # the scan. Inventory reads metadata only, including for credential files.
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    def walk(directory, base):
        for name in sorted(os.listdir(directory)):
            try:
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(name, flags, dir_fd=directory)
                    try:
                        yield from walk(child, base / name)
                    finally:
                        os.close(child)
                elif stat.S_ISREG(info.st_mode):
                    yield base / name, info
            except (FileNotFoundError, NotADirectoryError, PermissionError):
                continue  # Rotation and inaccessible files do not abort inventory.
    try:
        directory = os.open(root, flags)
    except (FileNotFoundError, NotADirectoryError):
        return
    try:
        yield from walk(directory, root)
    finally:
        os.close(directory)


def scan_data_assets() -> dict[str, Any]:
    """Inventory real files, including backups; never read secret contents."""
    root = settings.data_dir.resolve()
    catalog = _load_catalog()
    catalog_assets = catalog.get("assets") if isinstance(catalog.get("assets"), dict) else {}
    assets: list[dict[str, Any]] = []
    all_items: list[dict[str, Any]] = []
    categories = {key: {"id": key, "label": value[0], "retention": value[1], "files": 0, "size_bytes": 0}
                  for key, value in _CATEGORIES.items()}
    from app.storage import cache_lifecycle
    policy = cache_lifecycle.get_policy()
    for path, stat in _walk_files(root):
        rel = path.relative_to(root).as_posix()
        meta = dict(catalog_assets.get(rel) or {})
        inferred = _infer_asset_meta(rel, path)
        for field in ("kind", "key", "source"):
            if meta.get(field) in (None, "", "unclassified"):
                meta[field] = inferred[field]
        meta.update(path=rel, size_bytes=stat.st_size, category=_category(rel),
                    updated_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat())
        meta.update(cache_lifecycle.describe_asset(rel, stat, policy=policy))
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


def query_data_assets(*, category: str = "", q: str = "", source: str = "", lifecycle: str = "",
                      offset: int = 0, limit: int = 100, sort: str = "updated_at",
                      descending: bool = True) -> dict[str, Any]:
    """Metadata-only, bounded file inventory; never expose file contents or accept a path to read."""
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("offset 必须非负，limit 必须在 1 到 500 之间")
    if sort not in {"path", "updated_at", "size_bytes", "category", "source"}:
        raise ValueError("不支持的文件排序字段")
    if category and category not in _CATEGORIES:
        raise ValueError("未知数据分类")
    if lifecycle not in {"", "protected", "rebuildable", "expired"}:
        raise ValueError("未知缓存生命周期筛选")
    if not all(isinstance(v, str) and len(v) <= 200 for v in (q, source)):
        raise ValueError("筛选文本过长")
    inventory = scan_data_assets()
    # Include the recovery area and metadata/backups in file management, while
    # keeping scan_data_assets.items backwards compatible for existing clients.
    root = settings.data_dir.resolve()
    from app.storage import cache_lifecycle
    policy = cache_lifecycle.get_policy()
    items = list(inventory["items"])
    for path, info in _walk_files(root / _MANAGED_DIR):
        rel = path.relative_to(root).as_posix()
        items.append({**_infer_asset_meta(rel, path), "path": rel, "size_bytes": info.st_size,
                      "category": _category(rel), "updated_at": datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(),
                      **cache_lifecycle.describe_asset(rel, info, policy=policy)})
    sources = sorted({str(item["source"]) for item in items if item.get("source")})
    summary = {"cache_bytes": sum(item["size_bytes"] for item in items if item["rebuildable"]),
               "expired_bytes": sum(item["size_bytes"] for item in items if item["expired"]),
               "protected_bytes": sum(item["size_bytes"] for item in items if item["protected"]),
               "quarantine_bytes": sum(item["size_bytes"] for item in items if item["storage_role"] == "quarantine"),
               "budget_bytes": policy["budget_bytes"]}
    needle = q.casefold().strip()
    items = [item for item in items if (not category or item["category"] == category)
             and (not source or item.get("source") == source)
             and (not needle or needle in " ".join(str(item.get(key) or "") for key in ("path", "source", "key", "data_domain")).casefold())
             and (not lifecycle or bool(item.get(lifecycle)))]
    items.sort(key=lambda item: (item.get(sort) or (0 if sort == "size_bytes" else ""), item["path"]), reverse=descending)
    return {"items": items[offset:offset + limit], "total": len(items), "offset": offset, "limit": limit,
            "size_bytes": sum(item["size_bytes"] for item in items), "categories": inventory["categories"],
            "directory_totals": inventory["directory_totals"], "index": inventory["index"],
            "sources": sources, "summary": summary}


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
