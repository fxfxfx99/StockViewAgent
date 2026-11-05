#!/usr/bin/env python3
"""Inspect or rebuild durable local stock knowledge, without provider requests."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import settings
from app.services import knowledge_cache_manager as manager
from app.storage import knowledge_cache_jobs as jobs
from app.storage.data_portability import data_directory_lock


def emit(value, *, stream=sys.stdout):
    print(json.dumps(value, ensure_ascii=False, indent=2), file=stream, flush=True)


async def rebuild(args):
    submitted = await asyncio.to_thread(manager.enqueue, args.scope, args.symbol, reason="本地 CLI 重建")
    identifier = submitted["job"]["id"]
    emit({"submitted": submitted}, stream=sys.stderr)
    last_report = 0.0
    while True:
        state = await asyncio.to_thread(jobs.get_job, identifier)
        if state is None:
            raise RuntimeError("未找到已提交的知识同步任务")
        if state["status"] not in {"queued", "running"}:
            emit({"job": state, "root_path": str(settings.data_dir / "knowledge_cache")})
            return 0 if state["status"] == "completed" else 1
        result = await manager.run_batch(limit=25)
        if time.monotonic() - last_report >= 5:
            emit({"progress": await asyncio.to_thread(jobs.get_job, identifier), "worker": result}, stream=sys.stderr)
            last_report = time.monotonic()
        if result["processed"] == 0:
            await asyncio.sleep(2)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="查看容量、文档数和同步队列")
    build = commands.add_parser("rebuild", help="提交本地重建任务并前台执行至完成；中断后可恢复")
    build.add_argument("--scope", choices=manager.SCOPES, default="all")
    build.add_argument("--symbol", help="可选单股代码；提示词为全局配置")
    inspect = commands.add_parser("inspect", help="检查当前或历史知识正文与差异")
    inspect.add_argument("kind", choices=("stock_features", "news_relevance", "prompts"))
    inspect.add_argument("key")
    inspect.add_argument("--revision-id")
    args = parser.parse_args(argv)
    try:
        with data_directory_lock(settings.data_dir):
            if args.command == "status":
                emit(manager.overview())
            elif args.command == "inspect":
                emit(manager.document(args.kind, args.key, args.revision_id))
            else:
                return asyncio.run(rebuild(args))
    except KeyboardInterrupt:
        print("已停止前台同步，已提交的任务进度保留，可再次运行 rebuild 继续。", file=sys.stderr)
        return 130
    except (ValueError, FileNotFoundError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
