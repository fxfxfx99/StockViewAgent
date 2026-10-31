import json
import re
import shutil
from pathlib import Path

from app.config import settings
from app.storage import data_asset_manager
from app.storage import profile_store

_SYMBOL_RE = re.compile(r"^[A-Z0-9.^]{1,16}$")

_LEGACY_FILE = "watchlist.json"
DEFAULT_SYMBOLS = ("600519.SS", "000001.SZ", "600036.SS", "000333.SZ", "601318.SS")


def _watchlists_dir() -> Path:
    d = settings.data_dir / "watchlists"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _user_file(user_id: int) -> Path:
    return _watchlists_dir() / f"{int(user_id)}.json"


def migrate_legacy_watchlist_for_admin(admin_user_id: int = 1) -> None:
    """将旧版 watchlist.json 复制为管理员自选股文件（仅首次）。"""
    legacy = settings.data_dir / _LEGACY_FILE
    target = _user_file(admin_user_id)
    if legacy.exists() and not target.exists():
        try:
            shutil.copyfile(legacy, target)
        except OSError:
            pass


def load_symbols(user_id: int) -> list[str]:
    migrate_legacy_watchlist_for_admin(1)
    p = _user_file(user_id)
    if not p.exists():
        data_asset_manager.atomic_write_json(
            p,
            {"symbols": list(DEFAULT_SYMBOLS)},
            kind="watchlist",
            key=str(user_id),
            source="default",
            item_count=len(DEFAULT_SYMBOLS),
            backup=False,
        )
    raw = json.loads(p.read_text(encoding="utf-8"))
    symbols = raw.get("symbols") or []
    out: list[str] = []
    seen: set[str] = set()
    for s in symbols:
        t = str(s).strip().upper()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def load_all_symbols_union() -> list[str]:
    """Read existing watchlists for shared jobs without creating default holdings.

    Explicit user-facing load_symbols keeps its first-use defaults and legacy
    migration. Background collectors can read the legacy admin list directly.
    """
    directory = settings.data_dir / "watchlists"
    paths = sorted(directory.glob("*.json")) if directory.exists() and not directory.is_symlink() else []
    legacy = settings.data_dir / _LEGACY_FILE
    if not (directory / "1.json").exists() and legacy.exists():
        paths.append(legacy)
    seen: set[str] = set()
    out: list[str] = []
    for path in paths:
        if path.is_symlink() or not path.is_file():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            for symbol in raw.get("symbols") or []:
                value = str(symbol).strip().upper()
                if value and _SYMBOL_RE.fullmatch(value) and value not in seen:
                    seen.add(value)
                    out.append(value)
        except (OSError, json.JSONDecodeError, AttributeError):
            continue
    return out


def save_symbols(user_id: int, symbols: list[str]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for s in symbols:
        t = str(s).strip().upper()
        if not _SYMBOL_RE.match(t):
            continue
        if t not in seen:
            seen.add(t)
            cleaned.append(t)
    data_asset_manager.atomic_write_json(
        _user_file(user_id),
        {"symbols": cleaned},
        kind="watchlist",
        key=str(user_id),
        source="user",
        item_count=len(cleaned),
    )
    # 公司资料缓存由所有账户共享，不能因一个账户删除标的而清除其他账户的数据。
    profile_store.prune_orphans(set(load_all_symbols_union()))
    return cleaned
