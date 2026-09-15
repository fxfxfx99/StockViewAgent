import json
import re
import shutil
from pathlib import Path

from app.config import settings
from app.storage import data_asset_manager
from app.storage import profile_store

_SYMBOL_RE = re.compile(r"^[A-Z0-9.^]{1,16}$")

_LEGACY_FILE = "watchlist.json"


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
            {"symbols": ["600519.SS", "000001.SZ"]},
            kind="watchlist",
            key=str(user_id),
            source="default",
            item_count=2,
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
    """所有登录用户自选股的并集（用于新闻命中、后台同步等）。"""
    migrate_legacy_watchlist_for_admin(1)
    d = _watchlists_dir()
    seen: set[str] = set()
    out: list[str] = []
    for p in sorted(d.glob("*.json")):
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            for s in raw.get("symbols") or []:
                t = str(s).strip().upper()
                if t and _SYMBOL_RE.match(t) and t not in seen:
                    seen.add(t)
                    out.append(t)
        except OSError:
            continue
    return out if out else load_symbols(1)


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
    profile_store.prune_orphans(set(cleaned))
    return cleaned
