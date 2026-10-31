#!/usr/bin/env python3
"""Fetch the requested fifty-year window into STOCKVIEW_DATA_DIR/merrill.sqlite3."""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import settings
from app.services.merrill_sources import sync_all
from app.storage.data_portability import data_directory_lock
from app.storage.merrill_store import catalog


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--incremental", action="store_true", help="Only refetch recent daily history; revised ETF/macro history is still refreshed")
    parser.add_argument("--assets", help="Comma-separated asset IDs; empty string skips all assets")
    parser.add_argument("--macros", help="Comma-separated macro IDs; empty string skips all macro series")
    args = parser.parse_args()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    # Shared with the background worker: never perform two bulk downloads at once.
    with data_directory_lock(settings.data_dir), (settings.data_dir / "merrill_worker.lock").open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("美林时钟后台正在同步，请完成后重试") from None
        result = asyncio.run(sync_all(full=not args.incremental,
                                     asset_ids=args.assets.split(",") if args.assets else [] if args.assets == "" else None,
                                     macro_ids=args.macros.split(",") if args.macros else [] if args.macros == "" else None))
    print(json.dumps({"database": str(settings.data_dir / "merrill.sqlite3"), "sync": result,
                      "coverage": [{key: row[key] for key in ("id", "available_from", "available_to", "row_count", "coverage_ratio", "last_error")}
                                   for row in catalog()]}, ensure_ascii=False, indent=2))
    return 1 if result["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
