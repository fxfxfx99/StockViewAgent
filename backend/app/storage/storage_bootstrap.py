"""Consolidate legacy local assets once without moving or deleting originals.

Only the default repository data directory imports repository-era locations.
An explicitly selected new data directory remains an isolated instance.
"""
from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import uuid

from app.storage.data_portability import DataPortabilityError, _directory_fd, _publish_no_replace

_PROJECT = Path(__file__).resolve().parents[3]
_LAYOUT_VERSION = 1
_CSV_NAMES = ("财务指标 2025.csv", "管理层经营讨论与分析 2025.csv", "上市公司基本信息 2025.csv")
_GUIDE = """# StockViewAgent 本地数据目录

这个文件夹保存本机持续积累的资料、研究证据、账户配置和运行记录。整体搬迁即可延续现有数据；应用代码和 Python/Node 依赖仍由项目提供。

| 位置 | 内容 | 保留原则 |
| --- | --- | --- |
| `research.sqlite3`、`research/` | 任务、策略版本、实验、数据清单、不可变行情批次、前向模拟、运行用量 | 长期保留，勿单独删改 |
| `strategy_knowledge.db`、`decision_signals.db` | 正反研究证据和信号评价 | 长期保留 |
| `tracking.sqlite3` | 个人关注判断、不可覆盖的修改与复盘历史、证据快照、变化和阅读状态 | 长期保留，随目录备份与搬迁 |
| `monitoring.sqlite3` | 用户观察计划、模型验证状态、调用预算、任务、证据报告、站内提醒和人工复盘 | 长期保留，随目录备份与搬迁 |
| `earnings.sqlite3`、`earnings/` | 正式财报版本、原始PDF、逐页文本、个人解读、任务和调用记录 | 长期保留，随目录备份与搬迁 |
| `investment_research.sqlite3`、`investment_research/` | 个人研究档案、问题、材料、历史报告、明确采用记录和模型任务 | 长期保留，随目录备份与搬迁 |
| `economic_events.sqlite3` | 官方经济日历、不可变事件修订、全文检索索引与各账户 Agent 检索配置 | 长期保留 |
| `data_center.sqlite3`、`datasets/` | 公共数据采集配置、队列、预算、来源状态、不可变内容批次与检索修订 | 长期保留，不作为普通缓存清理 |
| `merrill.sqlite3` | 美林时钟跨资产周度历史、宏观月数据、采集覆盖、每周状态和个人模型解读 | 长期保留，随数据目录备份与搬迁 |
| `issuer_uploads/`、`kline_learning/` | 原始上传文件、提示词、资料清单、RAG 索引 | 保留原文件；向量索引可重建 |
| `imports/company_fundamentals/` | 财务、管理层讨论和公司信息 CSV | 导入原件，长期保留 |
| `news.db`、`company_fundamentals.db`、`company_updates/` | 新闻、财务修订和公司资料 | 长期保留 |
| `overseas_news.db` | 海外快讯、来源健康、抓取游标和采集租约 | 长期保留；来源异常不清空新闻 |
| `users.db`、`watchlists/`、配置 JSON | 账户、API 配置、自选股、接口优先级 | 私有文件，搬迁会一并携带凭据 |
| `market_source_cache/`、`kline_bundle_cache/`、`tushare_daily_cache/` | 已知格式的行情与接口缓存 | 到期并通过预览校验后可回收，不等于研究快照 |
| `market_history_cache/` | 累计市场历史序列 | 可能无法完整重建，长期保留 |
| `runtime/logs/`、`runtime/pids/` | 进程日志与运行状态 | PID/锁不搬迁，日志可保留 |
| `_managed/catalog.json` | 相对路径、分类、大小和更新时间的完整索引 | 可刷新重建，不含文件正文或 API 密钥 |
| `_managed/backups/`、`_managed/legacy/` | JSON 写入前版本、旧目录冲突归档 | 勿把它们当临时缓存清理 |
| `_managed/cache/` | 缓存策略、预览计划与管理锁 | 自动回收默认关闭；容量预算不是硬盘配额 |
| `_managed/quarantine/` | 可恢复缓存与批次操作记录 | 仍占磁盘；仅管理员手动确认才永久删除，恢复不覆盖新文件 |
| `_managed/layout.json`、`_managed/migration-manifest.json` | 目录身份、旧数据归拢报告、迁移校验清单 | 随数据保留 |

## 查看和搬迁

管理员可在 `/data` 查看分类文件、自动采集任务和缓存回收区。数据库及 WAL、研究证据、公开资料批次、上传原件、账户与凭据配置不进入普通缓存清理。

在项目根目录执行：

```sh
./backend/.venv/bin/python scripts/storage.py status
./backend/.venv/bin/python scripts/storage.py index
# 停止本地启动服务及 Docker 后，复制到一个尚不存在的新目录：
./backend/.venv/bin/python scripts/storage.py migrate --to '/path/to/StockViewAgent-data'
```

成功后按输出设置 `STOCKVIEW_DATA_DIR`，再启动项目。`STOCKVIEW_DATA_DIR` 的相对路径以 `backend/` 为基准；`--to` 的相对路径以执行命令时的目录为基准。迁移保持原目录不变，校验 SQLite 和文件哈希；不要在服务写入时手工复制 SQLite 主文件而遗漏 WAL。

`verify` 检查迁移完成时的文件清单；新目录恢复运行后文件会继续变化，旧清单不代表实时校验结果。外部 `.env`、JWT 密钥、外部行情服务和未导入的外部文件不在本目录，需在新机器单独配置。修改密钥或向量模型后按需重建索引。

旧版默认位置首次升级时只做无覆盖复制，原目录保留。自定义空目录不会自动导入另一实例的资料或凭据。冲突或未能复制的内容见 `_managed/layout.json`，不会静默覆盖已有成果。
"""


def _check_components(base: Path, path: Path):
    """Legacy locations must stay inside their declared directory tree."""
    try:
        parts = path.relative_to(base).parts
    except ValueError:
        raise ValueError("storage_path_outside_root") from None
    current = base
    if current.is_symlink():
        raise ValueError("storage_symlink")
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("storage_symlink")


def _atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _snapshot(source):
    files = [source] if source.is_file() else sorted(source.rglob("*"))
    result = {}
    for path in files:
        if path.is_symlink():
            raise ValueError("legacy_symlink")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError("legacy_special_file")
        stat = path.stat()
        result[path] = (stat.st_size, stat.st_mtime_ns)
    return result


def _copy_file(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Merge a SQLite WAL into the copy; original database remains untouched.
    with source.open("rb") as handle:
        database = handle.read(16) == b"SQLite format 3\x00"
    if database:
        with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as incoming, sqlite3.connect(destination) as outgoing:
            incoming.backup(outgoing)
            if outgoing.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("legacy_database_invalid")
    else:
        shutil.copy2(source, destination)
        if _sha(source) != _sha(destination):
            raise ValueError("legacy_copy_mismatch")
    destination.chmod(0o600)


def _copy_asset(source, destination):
    if source.is_symlink():
        raise ValueError("legacy_symlink")
    before = _snapshot(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".legacy-stage-", dir=destination.parent))
    staged = stage / "payload"
    try:
        if source.is_dir():
            staged.mkdir()
            for path in before:
                if path.name.endswith(("-wal", "-shm", "-journal", ".lock", ".pid")):
                    continue
                _copy_file(path, staged / path.relative_to(source))
        else:
            _copy_file(source, staged)
        if _snapshot(source) != before:
            raise ValueError("legacy_source_changed")
        # Remove only empty placeholder directories. rmdir fails if a writer
        # creates a file concurrently; never recursively delete the destination.
        if destination.is_symlink():
            raise ValueError("legacy_destination_symlink")
        if destination.is_dir():
            for child in sorted(destination.rglob("*"), key=lambda p: len(p.parts), reverse=True):
                if child.is_symlink() or not child.is_dir():
                    raise FileExistsError("legacy_destination_exists")
                child.rmdir()
            destination.rmdir()
        with _directory_fd(destination.parent) as parent_fd:
            _publish_no_replace(parent_fd, f"{stage.name}/payload", destination.name)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def initialize_local_storage(data_root: Path | None = None, *, project_root: Path | None = None):
    if data_root is None:
        from app.config import settings
        data_root = settings.data_dir
    root = Path(data_root).resolve()
    project = Path(project_root).resolve() if project_root else _PROJECT
    root.mkdir(parents=True, exist_ok=True)
    managed = root / "_managed"
    _check_components(root, managed)
    managed.mkdir(exist_ok=True)
    _check_components(root, managed / "bootstrap.lock")
    _check_components(root, managed / "layout.json")
    with (managed / "bootstrap.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        marker = managed / "layout.json"
        if marker.exists():
            state = json.loads(marker.read_text(encoding="utf-8"))
            if not isinstance(state, dict) or state.get("layout_version") != _LAYOUT_VERSION:
                raise ValueError("数据目录版本无法识别，请保留目录并检查升级版本")
        else:
            state = {"layout_version": _LAYOUT_VERSION, "directory_id": str(uuid.uuid4()),
                     "created_at": datetime.now(timezone.utc).isoformat(), "legacy_import": {}}
        default = root == (project / "backend" / "data").resolve()
        if default:
            sources = {"issuer_uploads": project / "issuer_uploads", "kline_learning": project / "kline_learning",
                       "llm_runtime.json": project / "backend" / "app" / "data" / "llm_runtime.json",
                       "runtime/logs/legacy": project / "logs"}
            sources.update({f"imports/company_fundamentals/{name}": project / name for name in _CSV_NAMES})
            reports = state.setdefault("legacy_import", {})
            for relative, source in sources.items():
                previous = reports.get(relative, {})
                if previous.get("status") in {"copied", "archived_conflict", "not_present"}:
                    continue
                target = root / relative
                try:
                    _check_components(project, source)
                    _check_components(root, target)
                    if not source.exists():
                        reports[relative] = {"status": "not_present"}
                        continue
                    if relative == "llm_runtime.json":
                        value = json.loads(source.read_text(encoding="utf-8"))
                        if not isinstance(value, dict):
                            raise ValueError("legacy_invalid_json")
                    if target.is_symlink():
                        raise ValueError("legacy_destination_symlink")
                    occupied = target.is_file() or (target.is_dir() and any(p.is_file() or p.is_symlink() for p in target.rglob("*")))
                    if occupied:
                        target = managed / "legacy" / relative
                        _check_components(root, target)
                        status = "archived_conflict"
                        if not target.exists():
                            _copy_asset(source, target)
                    else:
                        _copy_asset(source, target)
                        status = "copied"
                    reports[relative] = {"status": status, "destination": target.relative_to(root).as_posix()}
                except (OSError, ValueError, sqlite3.Error, DataPortabilityError) as exc:
                    # No original files are removed; retry transient failures on
                    # the next launch. Never include file contents or secrets.
                    reports[relative] = {"status": "needs_attention", "reason": type(exc).__name__}
                _atomic_json(marker, state)
        for name in ("issuer_uploads/files", "kline_learning/files", "imports/company_fundamentals", "runtime/logs", "runtime/pids"):
            _check_components(root, root / name)
            (root / name).mkdir(parents=True, exist_ok=True)
        _atomic_json(marker, state)
        guide = root / "INDEX.md"
        _check_components(root, guide)
        if not guide.exists():
            guide.write_text(_GUIDE, encoding="utf-8")
        return state
