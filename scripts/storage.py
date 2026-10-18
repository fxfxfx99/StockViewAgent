#!/usr/bin/env python3
"""Inspect, index, verify, or safely copy the configured local data directory."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import sqlite3
import sys

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.runtime_paths import resolve_data_dir
from app.storage.data_portability import (
    DataPortabilityError, data_directory_lock, migrate_data_directory, verify_data_directory,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description="管理可搬迁的本地数据目录；迁移和校验前请停止服务。")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("status", help="查看当前目录和资产分类")
    subparsers.add_parser("index", help="刷新相对路径索引，不采集新数据")
    subparsers.add_parser("verify", help="离线检查 SQLite、研究证据和迁移时的 SHA256 清单")
    migration = subparsers.add_parser("migrate", help="复制到尚不存在的新目录，保留原目录")
    migration.add_argument("--to", type=Path, required=True, help="新的目标目录，相对路径以当前目录为准")
    args = parser.parse_args(argv)
    try:
        root = resolve_data_dir(BACKEND)
        # Resolve once and give application settings exactly the same root.
        os.environ["STOCKVIEW_DATA_DIR"] = str(root)
        if args.command == "migrate":
            result = migrate_data_directory(root, args.to)
            result["next_environment"] = {"STOCKVIEW_DATA_DIR": result["destination"],
                "shell_command": "export STOCKVIEW_DATA_DIR=" + shlex.quote(result["destination"])}
            result["next_step"] = "按 shell_command 设置当前终端环境后启动项目；持久配置可将同一路径写入 backend/.env。"
        elif args.command == "verify":
            result = verify_data_directory(root)
        else:
            # Shared lock permits inspection beside the app but prevents a
            # concurrent offline migration. Catalog updates have their own lock.
            with data_directory_lock(root):
                from app.storage.data_asset_manager import refresh_catalog_from_disk, scan_data_assets
                result = refresh_catalog_from_disk() if args.command == "index" else scan_data_assets()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except DataPortabilityError as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
    except (OSError, ValueError, sqlite3.Error) as exc:
        # Do not include raw exception contents: a malformed file can contain
        # credentials, and arbitrary SQLite messages can include SQL/data.
        print(json.dumps({"ok": False, "error": type(exc).__name__,
                          "message": "数据目录操作失败；请检查路径、文件格式、磁盘空间和权限，原目录仍保留。"}, ensure_ascii=False), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
