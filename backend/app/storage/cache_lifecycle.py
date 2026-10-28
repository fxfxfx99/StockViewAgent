"""Conservative cache retention with reviewable plans and reversible quarantine.

Only known response-cache formats may move. SQLite, collected history, uploaded
material and evidence are never candidates. Quarantine does not free disk space
and has no automatic permanent-deletion path.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import time
import uuid

from app.storage import data_asset_manager as assets

_DAY = 86400
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
_BASE = "_managed/cache"
_POLICY = f"{_BASE}/policy.json"
_MAX_CACHE_READ = 32 * 1024 ** 2
_RULES = {
    "market_source_cache": {"days": 7, "label": "行情接口快照", "domain": "market"},
    "kline_bundle_cache": {"days": 90, "label": "K 线展示缓存", "domain": "market"},
    "tushare_daily_cache": {"days": 180, "label": "Tushare 接口缓存", "domain": "market"},
}
_DEFAULT_POLICY = {"enabled": False, "budget_bytes": 2 * 1024 ** 3,
                   "max_batch_files": 500, "max_batch_bytes": 256 * 1024 ** 2,
                   "ttl_days": {key: value["days"] for key, value in _RULES.items()}}


class CacheLifecycleError(ValueError):
    pass


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _clock(now=None):
    value = time.time() if now is None else now
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise CacheLifecycleError("时间必须包含时区")
        value = value.timestamp()
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise CacheLifecycleError("时间无效")
    return float(value)


def _relative(value):
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise CacheLifecycleError("文件路径必须是数据目录内的相对路径")
    path = PurePosixPath(value)
    if path.is_absolute() or value != path.as_posix() or ".." in path.parts or value == "." or re.match(r"^[A-Za-z]:", value):
        raise CacheLifecycleError("文件路径必须是数据目录内的相对路径")
    return value


@contextmanager
def _root_fd(*, create=False):
    root = Path(assets.settings.data_dir).absolute()
    if root.is_symlink():
        raise CacheLifecycleError("数据根目录不能是符号链接")
    if create:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(root, _DIR_FLAGS)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _parent(root, relative, *, create=False):
    parts = PurePosixPath(_relative(relative)).parts
    directory = os.dup(root)
    try:
        for part in parts[:-1]:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=directory)
                except FileExistsError:
                    pass
            child = os.open(part, _DIR_FLAGS, dir_fd=directory)
            os.close(directory)
            directory = child
        yield directory, parts[-1]
    finally:
        os.close(directory)


@contextmanager
def _open(root, relative):
    with _parent(root, relative) as (directory, name):
        descriptor = os.open(name, _FILE_FLAGS, dir_fd=directory)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise CacheLifecycleError("只允许无硬链接的普通缓存文件")
            yield descriptor, info
        finally:
            os.close(descriptor)


def _read_json(root, relative, *, maximum=4 * 1024 ** 2):
    with _open(root, relative) as (descriptor, info):
        if info.st_size > maximum:
            raise CacheLifecycleError("缓存管理记录过大")
        with os.fdopen(os.dup(descriptor), "rb") as handle:
            return json.loads(handle.read(maximum + 1))


def _write_json(root, relative, value):
    with _parent(root, relative, create=True) as (directory, name):
        temporary = f".{name}.{uuid.uuid4().hex}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=directory)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass


@contextmanager
def _locked():
    with _root_fd(create=True) as root, _parent(root, f"{_BASE}/lifecycle.lock", create=True) as (directory, name):
        descriptor = os.open(name, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=directory)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode) or os.fstat(descriptor).st_nlink != 1:
                raise CacheLifecycleError("缓存管理锁文件无效")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield root
        finally:
            os.close(descriptor)


def _validate_policy(value):
    if not isinstance(value, dict) or set(value) - set(_DEFAULT_POLICY):
        raise CacheLifecycleError("未知缓存策略字段")
    if "ttl_days" in value and not isinstance(value["ttl_days"], dict):
        raise CacheLifecycleError("缓存保留时间必须为对象")
    result = {**_DEFAULT_POLICY, **value, "ttl_days": {**_DEFAULT_POLICY["ttl_days"], **value.get("ttl_days", {})}}
    if type(result["enabled"]) is not bool:
        raise CacheLifecycleError("enabled 必须是布尔值")
    for key, lower, upper in (("budget_bytes", 64 * 1024 ** 2, 1024 ** 4),
                              ("max_batch_files", 1, 1000), ("max_batch_bytes", 1024 ** 2, 2 * 1024 ** 3)):
        if type(result[key]) is not int or not lower <= result[key] <= upper:
            raise CacheLifecycleError(f"{key} 超出允许范围")
    if set(result["ttl_days"]) != set(_RULES) or any(type(v) is not int or not 1 <= v <= 3650 for v in result["ttl_days"].values()):
        raise CacheLifecycleError("缓存保留时间必须为 1 到 3650 天，且只能设置已知缓存目录")
    return result


def get_policy():
    try:
        with _root_fd() as root:
            return _validate_policy(_read_json(root, _POLICY))
    except (FileNotFoundError, json.JSONDecodeError):
        return _validate_policy({})


def update_policy(patch):
    if not isinstance(patch, dict) or ("ttl_days" in patch and not isinstance(patch["ttl_days"], dict)):
        raise CacheLifecycleError("缓存策略必须为对象")
    with _locked() as root:
        current = get_policy()
        value = _validate_policy({**current, **patch, "ttl_days": {**current["ttl_days"], **patch.get("ttl_days", {})}})
        _write_json(root, _POLICY, value)
        return value


def describe_asset(relative, info, *, policy=None, now=None):
    """Path-based retention classification; eligibility is rechecked against content at cleanup."""
    relative = _relative(relative)
    parts = PurePosixPath(relative).parts
    first = parts[0]
    name = parts[-1].lower()
    domain = ({"economic_events.sqlite3": "economic_events", "news.db": "news", "company_updates": "company",
               "company_fundamentals.db": "company", "research": "research", "research.sqlite3": "research", "tracking.sqlite3": "research",
               "issuer_uploads": "uploads", "kline_learning": "knowledge", "imports": "uploads",
               "users.db": "accounts", "watchlists": "accounts", "market_history_cache": "market_history"}).get(first, "other")
    if first == "datasets":
        domain = parts[1] if len(parts) > 2 else "public_data"
    if first in {"watchlist.json", "watchlist_profiles.json", "llm_runtime.json", "integrations.json", "platform.json", "cn_headline_sources_override.json"}:
        domain = "accounts"
    if first in {"strategy_knowledge.db", "kline_insights"}:
        domain = "knowledge"
    rule = _RULES.get(first)
    eligible = (rule is not None and len(parts) >= 2 and name.endswith(".json")
                and "_health" not in parts and not any(part.startswith(".") for part in parts)
                and not any(word in name for word in ("credential", "secret", "config", "token")))
    if name.endswith((".db", ".sqlite", ".sqlite3", "-wal", "-shm", "-journal", ".lock", ".pid")):
        eligible = False
    if getattr(info, "st_nlink", 1) != 1:
        eligible = False
    policy = policy or get_policy()
    expires = info.st_mtime + policy["ttl_days"][first] * _DAY if eligible else None
    expired = bool(expires is not None and expires <= _clock(now))
    role = "rebuildable_cache" if eligible else "protected_data"
    reason = "仅已知接口缓存可隔离；数据库、历史序列、资料、账户与研究证据长期保留"
    if first == "market_history_cache":
        reason = "累计历史序列可能无法由上游完整重建，自动维护长期保留"
    if relative.startswith("_managed/quarantine/"):
        role, reason = "quarantine", "可恢复回收区，不自动永久删除；仍占用磁盘空间"
    elif first == "_managed":
        role, reason = "management", "管理索引、备份和操作记录受保护"
    if eligible:
        domain, reason = rule["domain"], f"{rule['label']}：最后修改后保留 {policy['ttl_days'][first]} 天"
    return {"data_domain": domain, "storage_role": role, "protected": not eligible,
            "rebuildable": eligible, "expired": expired, "expires_at": _iso(expires) if expires else None,
            "expiry_reason": reason}


def _fingerprint(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _inspect(root, path, policy, now):
    with _open(root, path) as (descriptor, info):
        meta = describe_asset(path, info, policy=policy, now=now)
        if not meta["rebuildable"]:
            raise CacheLifecycleError("文件受保护，不属于可重建缓存")
        if not meta["expired"]:
            raise CacheLifecycleError("缓存尚未达到保留期限")
        if info.st_size > min(policy["max_batch_bytes"], _MAX_CACHE_READ):
            raise CacheLifecycleError("单文件超过本批容量或 32 MiB 在线核验限制，已保留")
        with os.fdopen(os.dup(descriptor), "rb") as handle:
            raw = handle.read(min(policy["max_batch_bytes"], _MAX_CACHE_READ) + 1)
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise CacheLifecycleError("内容不符合缓存格式，已保护") from None
        if not isinstance(value, dict) or "payload" not in value or not value.get("saved_at"):
            raise CacheLifecycleError("内容不符合已知缓存格式，已保护")
        try:
            saved = datetime.fromisoformat(value["saved_at"]).timestamp()
            if not math.isfinite(saved) or saved <= 0:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            raise CacheLifecycleError("缓存保存时间无法核验，已保护") from None
        expiration = max(saved, info.st_mtime) + policy["ttl_days"][PurePosixPath(path).parts[0]] * _DAY
        if expiration > now:
            raise CacheLifecycleError("缓存保存时间尚未达到保留期限")
        if _fingerprint(os.fstat(descriptor)) != _fingerprint(info):
            raise CacheLifecycleError("缓存正在变化，请稍后重新预览")
        return {"path": path, "size_bytes": info.st_size, "updated_at": _iso(info.st_mtime),
                "expires_at": _iso(expiration), "reason": meta["expiry_reason"],
                "fingerprint": _fingerprint(info), "sha256": hashlib.sha256(raw).hexdigest()}


def _selected_paths(paths):
    if paths is None:
        return None
    if not isinstance(paths, list) or not paths or len(paths) > 1000:
        raise CacheLifecycleError("请选择 1 到 1000 个缓存文件")
    return list(dict.fromkeys(_relative(path) for path in paths))


def _public_plan(plan):
    return {key: value for key, value in plan.items() if key != "items"} | {
        "items": [{key: value for key, value in item.items() if key not in {"fingerprint", "sha256"}} for item in plan["items"]]}


def preview_cleanup(paths=None, *, now=None):
    now = _clock(now)
    selected = _selected_paths(paths)
    with _locked() as root:
        policy = get_policy()
        inventory = assets.scan_data_assets()
        candidates = [item for item in inventory["items"] if item["rebuildable"]]
        active_bytes = sum(item["size_bytes"] for item in candidates)
        if selected is None:
            selected = [item["path"] for item in sorted(candidates, key=lambda item: item["updated_at"])
                        if datetime.fromisoformat(item["expires_at"]).timestamp() <= now]
        items, skipped, total = [], [], 0
        for path in selected:
            try:
                if len(items) >= policy["max_batch_files"]:
                    raise CacheLifecycleError("已达到本批处理数量限制")
                item = _inspect(root, path, policy, now)
                if len(items) >= policy["max_batch_files"] or total + item["size_bytes"] > policy["max_batch_bytes"]:
                    raise CacheLifecycleError("已达到本批处理数量或容量限制")
                items.append(item)
                total += item["size_bytes"]
            except (OSError, CacheLifecycleError) as exc:
                skipped.append({"path": path, "reason": str(exc) if isinstance(exc, CacheLifecycleError) else "文件不存在、已变化或路径不安全"})
        plan = {"plan_id": uuid.uuid4().hex, "created_at": _iso(now), "expires_at": _iso(now + 1800),
                "items": items, "total_files": len(items), "total_bytes": total, "skipped": skipped,
                "cache_bytes": active_bytes, "budget_bytes": policy["budget_bytes"],
                "over_budget_bytes": max(0, active_bytes - policy["budget_bytes"]),
                "disk_space_freed_bytes": 0, "note": "执行后移入可恢复回收区；不永久删除，不释放磁盘空间。容量预算不会淘汰未过期缓存。"}
        _write_json(root, f"{_BASE}/plans/{plan['plan_id']}.json", plan)
        return _public_plan(plan)


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise CacheLifecycleError("操作标识无效")
    return value


def _batch_path(batch):
    return f"_managed/quarantine/{_identifier(batch)}/manifest.json"


def _transfer(root, source, target, *, fingerprint=None, digest=None):
    """Publish without replacing; pin parents to descriptors and retain a recoverable copy on races."""
    with _open(root, source) as (descriptor, info):
        if fingerprint and _fingerprint(info) != fingerprint:
            raise CacheLifecycleError("缓存已更新，需重新预览")
        if digest:
            value = hashlib.sha256()
            while block := os.read(descriptor, 1024 * 1024):
                value.update(block)
            if value.hexdigest() != digest:
                raise CacheLifecycleError("文件校验失败，已保留原文件")
        with _parent(root, source) as (origin, source_name), _parent(root, target, create=True) as (destination, target_name):
            if _fingerprint(os.stat(source_name, dir_fd=origin, follow_symlinks=False)) != _fingerprint(info):
                raise CacheLifecycleError("缓存已更新，需重新预览")
            # link(..., O_EXCL semantics) refuses an existing destination. All
            # paths stay inside one data root, so no cross-filesystem copy occurs.
            os.link(source_name, target_name, src_dir_fd=origin, dst_dir_fd=destination, follow_symlinks=False)
            linked = os.stat(target_name, dir_fd=destination, follow_symlinks=False)
            current = os.stat(source_name, dir_fd=origin, follow_symlinks=False)
            if (linked.st_dev, linked.st_ino) != (info.st_dev, info.st_ino) or not stat.S_ISREG(linked.st_mode):
                os.unlink(target_name, dir_fd=destination)
                raise CacheLifecycleError("文件在移动时变化，已停止操作")
            if (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino):
                os.unlink(source_name, dir_fd=origin)


def execute_cleanup(plan_id, *, now=None):
    now = _clock(now)
    plan_id = _identifier(plan_id)
    with _locked() as root:
        plan_path = f"{_BASE}/plans/{plan_id}.json"
        plan = _read_json(root, plan_path)
        if plan.get("batch_id"):
            return _read_json(root, _batch_path(plan["batch_id"]))
        if datetime.fromisoformat(plan["expires_at"]).timestamp() < now:
            raise CacheLifecycleError("清理预览已过期，请重新预览")
        policy = get_policy()
        batch = {"batch_id": uuid.uuid4().hex, "plan_id": plan_id, "created_at": _iso(now),
                 "items": [], "moved_files": 0, "moved_bytes": 0, "disk_space_freed_bytes": 0}
        _write_json(root, _batch_path(batch["batch_id"]), batch)
        plan["batch_id"] = batch["batch_id"]
        _write_json(root, plan_path, plan)
        for original in plan["items"]:
            item = {**original, "status": "pending", "quarantine_path": f"_managed/quarantine/{batch['batch_id']}/files/{original['path']}"}
            batch["items"].append(item)
            _write_json(root, _batch_path(batch["batch_id"]), batch)
            try:
                current = _inspect(root, item["path"], policy, now)
                if current["fingerprint"] != item["fingerprint"] or current["sha256"] != item["sha256"]:
                    raise CacheLifecycleError("缓存已更新，需重新预览")
                _transfer(root, item["path"], item["quarantine_path"], fingerprint=item["fingerprint"], digest=item["sha256"])
                with _open(root, item["quarantine_path"]) as (_, info):
                    item["quarantine_fingerprint"] = _fingerprint(info)
                item["status"] = "quarantined"
                batch["moved_files"] += 1
                batch["moved_bytes"] += item["size_bytes"]
            except (OSError, CacheLifecycleError) as exc:
                item.update(status="skipped", error=str(exc) if isinstance(exc, CacheLifecycleError) else "文件不存在、已变化或路径不安全")
            _write_json(root, _batch_path(batch["batch_id"]), batch)
        assets.refresh_catalog_from_disk()
        return batch


def list_quarantine(*, offset=0, limit=50):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200:
        raise CacheLifecycleError("回收区分页参数无效")
    batches = []
    try:
        with _root_fd() as root, _parent(root, "_managed/quarantine/placeholder") as (directory, _):
            for name in os.listdir(directory):
                if re.fullmatch(r"[0-9a-f]{32}", name):
                    try:
                        batches.append(_read_json(root, _batch_path(name)))
                    except (OSError, ValueError):
                        continue
    except FileNotFoundError:
        pass
    batches.sort(key=lambda item: item["created_at"], reverse=True)
    return {"items": batches[offset:offset + limit], "total": len(batches), "offset": offset, "limit": limit,
            "note": "回收区可恢复，不会自动永久删除，仍占用磁盘空间。"}


def restore_quarantine(batch_id, paths=None):
    selected = _selected_paths(paths)
    with _locked() as root:
        batch = _read_json(root, _batch_path(batch_id))
        available = {item["path"] for item in batch["items"]}
        if selected is not None and set(selected) - available:
            raise CacheLifecycleError("所选文件不属于这个回收批次")
        restored = 0
        for item in batch["items"]:
            if selected is not None and item["path"] not in selected:
                continue
            if item["status"] in {"restored", "purged"}:
                continue
            # Validate stored locators too; a manifest is data, never authority.
            expected = f"_managed/quarantine/{batch_id}/files/{_relative(item['path'])}"
            if item.get("quarantine_path") != expected or not describe_asset(item["path"], type("Info", (), {"st_mtime": time.time()})())["rebuildable"]:
                raise CacheLifecycleError("回收记录路径无效")
            try:
                _transfer(root, expected, item["path"], digest=item["sha256"])
                item.update(status="restored", restored_at=_iso(time.time()))
                item.pop("restore_error", None)
                restored += 1
            except (OSError, CacheLifecycleError) as exc:
                item["restore_error"] = "原路径已有文件，保留当前文件与回收副本" if isinstance(exc, FileExistsError) else (str(exc) if isinstance(exc, CacheLifecycleError) else "回收文件不可用或路径不安全")
            _write_json(root, _batch_path(batch_id), batch)
        assets.refresh_catalog_from_disk()
        return {**batch, "restored_files": restored}


def purge_quarantine(batch_id):
    """Explicit manual removal of one recorded batch; never called by maintenance.

    The authenticated router must require a fresh confirm=true request. Files
    changed since quarantine, pending operations, and restored files are retained.
    freed_bytes counts successfully removed file bytes, not a statvfs estimate.
    """
    batch_id = _identifier(batch_id)
    with _locked() as root:
        batch = _read_json(root, _batch_path(batch_id))
        freed, purged, skipped = 0, 0, []
        for item in batch["items"]:
            if item["status"] == "purged":
                continue
            if item["status"] != "quarantined":
                skipped.append({"path": item["path"], "reason": "仅可永久删除已隔离且未恢复的缓存"})
                continue
            expected = f"_managed/quarantine/{batch_id}/files/{_relative(item['path'])}"
            if item.get("quarantine_path") != expected or not item.get("quarantine_fingerprint"):
                skipped.append({"path": item["path"], "reason": "回收记录缺少可信文件指纹，已保留"})
                continue
            try:
                with _open(root, expected) as (descriptor, info):
                    if not describe_asset(item["path"], info)["rebuildable"] or _fingerprint(info) != item["quarantine_fingerprint"]:
                        raise CacheLifecycleError("回收文件已变化，已保留")
                    digest = hashlib.sha256()
                    while block := os.read(descriptor, 1024 * 1024):
                        digest.update(block)
                    if digest.hexdigest() != item["sha256"]:
                        raise CacheLifecycleError("回收文件校验失败，已保留")
                    with _parent(root, expected) as (directory, name):
                        if _fingerprint(os.stat(name, dir_fd=directory, follow_symlinks=False)) != item["quarantine_fingerprint"]:
                            raise CacheLifecycleError("回收文件在操作期间变化，已保留")
                        os.unlink(name, dir_fd=directory)
                    freed += info.st_size
                    purged += 1
                    item.update(status="purged", purged_at=_iso(time.time()))
                _write_json(root, _batch_path(batch_id), batch)
            except (OSError, CacheLifecycleError) as exc:
                skipped.append({"path": item["path"], "reason": str(exc) if isinstance(exc, CacheLifecycleError) else "回收文件不可用或路径不安全"})
        assets.refresh_catalog_from_disk()
        return {"batch_id": batch_id, "purged_files": purged, "freed_bytes": freed,
                "deleted_files": purged, "deleted_bytes": freed, "skipped": skipped,
                "note": "freed_bytes 为成功永久删除的文件大小；实际可用磁盘空间由文件系统决定。"}


def run_maintenance():
    policy = get_policy()
    if not policy["enabled"]:
        return {"status": "disabled", "moved_files": 0, "moved_bytes": 0}
    plan = preview_cleanup()
    if not plan["total_files"]:
        return {"status": "no_expired_cache", "moved_files": 0, "moved_bytes": 0,
                "over_budget_bytes": plan["over_budget_bytes"]}
    return {"status": "completed", **execute_cleanup(plan["plan_id"])}
