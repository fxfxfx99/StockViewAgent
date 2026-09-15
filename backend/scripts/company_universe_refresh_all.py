#!/usr/bin/env python3
"""
一键：先拉取 A 股列表，再按 offset/limit 分批调用管理员「全市场 F10 缓存」接口直到跑完。

用法（需管理员 JWT）：
  cd backend && source .venv/bin/activate
  export STOCKVIEW_ADMIN_TOKEN='你的JWT'
  python scripts/company_universe_refresh_all.py

可选环境变量：
  STOCKVIEW_API_BASE  默认 http://127.0.0.1:8001/api
  STOCKVIEW_CHUNK     默认 80
  STOCKVIEW_SKIP_FRESH_DAYS  默认 60（缓存仍新鲜则跳过；设为 0 可强制全量重拉）

仅刷新部分代码：
  python scripts/company_universe_refresh_all.py --symbols 600519.SS,000001.SZ
"""
from __future__ import annotations

import argparse
import os
import sys
import time


def main() -> int:
    p = argparse.ArgumentParser(description="全市场公司 F10 缓存分批刷新")
    p.add_argument(
        "--api-base",
        default=os.environ.get("STOCKVIEW_API_BASE", "http://127.0.0.1:8001/api").rstrip("/"),
        help="API 根路径（须以 /api 结尾）",
    )
    p.add_argument(
        "--token",
        default=os.environ.get("STOCKVIEW_ADMIN_TOKEN", ""),
        help="管理员 JWT；或设环境变量 STOCKVIEW_ADMIN_TOKEN",
    )
    p.add_argument("--chunk", type=int, default=int(os.environ.get("STOCKVIEW_CHUNK", "80")))
    p.add_argument(
        "--skip-fresh-days",
        type=float,
        default=float(os.environ.get("STOCKVIEW_SKIP_FRESH_DAYS", "60")),
    )
    p.add_argument(
        "--symbols",
        default="",
        help="逗号分隔，只刷这些代码时忽略 offset 分批",
    )
    p.add_argument(
        "--skip-stocks-refresh",
        action="store_true",
        help="跳过 POST /stocks/refresh（已有人工拉过列表时）",
    )
    args = p.parse_args()

    try:
        import httpx
    except ImportError:
        print("需要 httpx：pip install httpx", file=sys.stderr)
        return 1

    base = args.api_base
    token = (args.token or "").strip()
    if not token:
        print("请提供管理员 JWT：--token 或环境变量 STOCKVIEW_ADMIN_TOKEN", file=sys.stderr)
        return 1

    headers = {"Authorization": f"Bearer {token}"}

    with httpx.Client(timeout=600.0) as client:
        if not args.skip_stocks_refresh:
            print("POST /stocks/refresh …")
            r = client.post(f"{base}/stocks/refresh")
            if r.status_code >= 400:
                print(f"stocks/refresh 失败 {r.status_code}: {r.text[:500]}", file=sys.stderr)
                return 1
            data = r.json()
            n = int(data.get("count") or 0)
            print(f"  A 股列表已更新，共 {n} 条")
        else:
            r = client.get(f"{base}/stocks/meta")
            if r.status_code >= 400:
                print(f"stocks/meta 失败 {r.status_code}", file=sys.stderr)
                return 1
            n = int(r.json().get("count") or 0)
            print(f"跳过列表刷新；当前本地列表 count={n}")

        sym_list = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        if sym_list:
            print(f"POST /admin/company-universe/refresh symbols={len(sym_list)} …")
            rr = client.post(
                f"{base}/admin/company-universe/refresh",
                headers=headers,
                json={"symbols": sym_list, "skip_fresh_days": args.skip_fresh_days},
            )
            if rr.status_code >= 400:
                print(f"失败 {rr.status_code}: {rr.text[:800]}", file=sys.stderr)
                return 1
            out = rr.json()
            print(f"  updated={out.get('updated')} skipped={out.get('skipped_fresh')} errors={len(out.get('errors') or {})}")
            return 0

        if n <= 0:
            print("本地无 A 股列表，请先成功执行 stocks/refresh", file=sys.stderr)
            return 1

        offset = 0
        chunk = max(1, min(400, args.chunk))
        total_updated = 0
        total_err = 0
        while offset < n:
            print(f"POST /admin/company-universe/refresh offset={offset} limit={chunk} …")
            rr = client.post(
                f"{base}/admin/company-universe/refresh",
                headers=headers,
                json={
                    "offset": offset,
                    "limit": chunk,
                    "skip_fresh_days": args.skip_fresh_days,
                },
            )
            if rr.status_code >= 400:
                print(f"失败 {rr.status_code}: {rr.text[:800]}", file=sys.stderr)
                return 1
            out = rr.json()
            u = int(out.get("updated") or 0)
            sk = int(out.get("skipped_fresh") or 0)
            errs = out.get("errors") or {}
            total_updated += u
            total_err += len(errs)
            print(f"  batch updated={u} skipped_fresh={sk} errors={len(errs)}")
            offset += chunk
            time.sleep(0.3)

        print(f"完成：累计 updated≈{total_updated}，error 条目≈{total_err}（各批可能重叠 skip）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
