"""全市场公司 F10 批量同步：写入 universe_company_store。"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from app.services import a_share_stocks, company_profile_em
from app.storage import universe_company_store


async def refresh_symbols(
    symbols: list[str],
    *,
    sleep_sec: float = 0.12,
    skip_fresh_days: float = 0.0,
) -> dict[str, Any]:
    """
    按列表拉取东方财富 F10 并写入全市场缓存。
    skip_fresh_days>0 时：若该标的已有成功缓存且未超过 N 天则跳过（减轻东财压力）。
    """
    errors: dict[str, str] = {}
    skipped = 0
    updated = 0
    now = time.time()

    for sym in symbols:
        u = sym.strip().upper()
        if not u:
            continue
        if skip_fresh_days > 0:
            prev = universe_company_store.get_symbol(u)
            if prev and not prev.get("fetch_error"):
                ts = int(prev.get("em_fetched_at") or 0)
                if ts and (now - ts) / 86400.0 < skip_fresh_days:
                    skipped += 1
                    await asyncio.sleep(sleep_sec)
                    continue
        try:
            auto, err = await asyncio.to_thread(company_profile_em.fetch_company_survey, u)
            lk = a_share_stocks.lookup_by_yahoo_symbol(u)
            if lk and not (auto.get("name") or "").strip():
                auto["name"] = (lk.get("name") or "").strip()
            universe_company_store.upsert_symbol(u, auto, err)
            if err:
                errors[u] = err
            else:
                updated += 1
        except Exception as e:  # noqa: BLE001
            errors[u] = f"异常：{e!s}"
            universe_company_store.upsert_symbol(
                u,
                {"name": "", "org_name": "", "main_business": "", "industry": "", "intro": ""},
                errors[u],
            )
        await asyncio.sleep(sleep_sec)

    meta = universe_company_store.meta()
    return {
        "updated": updated,
        "skipped_fresh": skipped,
        "errors": errors,
        "meta": meta,
    }


def all_listed_symbols() -> list[str]:
    rows = a_share_stocks.load_stocks()
    out: list[str] = []
    for r in rows:
        s = (r.get("symbol") or "").strip().upper()
        if s:
            out.append(s)
    return out
