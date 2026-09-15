"""
A 股全市场代码 + 简称列表：从东方财富 clist 接口拉取，存 backend/data/a_share_stocks.json。
搜索支持中文简称、代码片段（用于前端自动补全）。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import httpx

from app.config import settings
from app.services import market_source_policy
from app.storage import data_asset_manager

FILENAME = "a_share_stocks.json"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
# 沪深京 A 股（不含已退市；以东方财富分区为准）
_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81"
_CLIST = "https://push2.eastmoney.com/api/qt/clist/get"
_SUGGEST = "https://searchapi.eastmoney.com/api/suggest/get"
_BAIDU_RANK = "https://finance.pae.baidu.com/selfselect/getmarketrank"
_SINA_RANK = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
_MIN_EXPECTED_A_SHARE_COUNT = 1000

_cache_rows: list[dict[str, Any]] | None = None
_cache_mtime: float | None = None


def _path() -> Path:
    return settings.data_dir / FILENAME


def _suffix_from_em(code: str, f13: Any) -> str:
    c = (code or "").strip().zfill(6)
    if not c.isdigit():
        return ".SZ"
    try:
        mi = int(f13) if f13 is not None and str(f13).strip() != "" else None
    except (TypeError, ValueError):
        mi = None
    if mi == 1:
        return ".SS"
    if mi == 0:
        return ".SZ"
    if c.startswith("6") or c.startswith("9"):
        return ".SS"
    if c.startswith(("0", "3")):
        return ".SZ"
    if c.startswith(("4", "8")):
        return ".BJ"
    return ".SZ"


def _suffix_from_code(code: str) -> str:
    c = (code or "").strip().zfill(6)
    if c.startswith(("6", "9")):
        return ".SS"
    if c.startswith(("4", "8", "92")):
        return ".BJ"
    return ".SZ"


def _normalize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        code = str(row.get("code") or row.get("stock_code") or "").strip().zfill(6)
        name = str(row.get("name") or row.get("short_name") or "").strip()
        if not code.isdigit() or len(code) != 6 or not name or code in seen:
            continue
        seen.add(code)
        suf = str(row.get("suffix") or "").strip().upper() or _suffix_from_code(code)
        if suf == ".SH":
            suf = ".SS"
        if suf not in (".SS", ".SZ", ".BJ"):
            suf = _suffix_from_code(code)
        out.append({"code": code, "name": name, "symbol": f"{code}{suf}"})
    out.sort(key=lambda x: x["code"])
    return out


def _load_file_raw() -> dict[str, Any]:
    p = _path()
    if not p.exists():
        return {"updated_at": 0, "stocks": []}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"updated_at": 0, "stocks": []}


def load_stocks() -> list[dict[str, Any]]:
    global _cache_rows, _cache_mtime
    p = _path()
    if not p.exists():
        return []
    try:
        m = p.stat().st_mtime
    except OSError:
        return []
    if _cache_rows is None or m != _cache_mtime:
        data = _load_file_raw()
        _cache_rows = list(data.get("stocks") or [])
        _cache_mtime = m
    return _cache_rows


def symbol_from_code6(code: str) -> str | None:
    """6 位数字 -> Yahoo 格式代码（如 600519.SS）；无匹配返回 None。"""
    c = (code or "").strip().zfill(6)
    if not c.isdigit() or len(c) != 6:
        return None
    for s in load_stocks():
        if (str(s.get("code") or "").strip().zfill(6)) == c:
            sym = (s.get("symbol") or "").strip().upper()
            return sym or None
    from app.services import issuer_basic_info_universe as ibu

    rows = ibu.get_stocks_for_search_if_available()
    if rows:
        for s in rows:
            if (str(s.get("code") or "").strip().zfill(6)) == c:
                sym = (s.get("symbol") or "").strip().upper()
                return sym or None
    return None


def lookup_by_yahoo_symbol(symbol: str) -> dict[str, Any] | None:
    sym = (symbol or "").strip().upper()
    for s in load_stocks():
        if (s.get("symbol") or "").upper() == sym:
            return s
    from app.services import issuer_basic_info_universe as ibu

    rows = ibu.get_stocks_for_search_if_available()
    if rows:
        for s in rows:
            if (s.get("symbol") or "").upper() == sym:
                return s
    return None


_NAME_SUFFIX_RE = re.compile(r"[-－](UW|WD|W|U|C|D)$", re.IGNORECASE)


def _name_search_keys(name: str) -> list[str]:
    """简称检索键：完整名 + 去后缀（如 大普微-UW → 大普微）+ 去 *ST。"""
    n = (name or "").strip()
    if not n:
        return []
    keys: list[str] = [n]
    base = _NAME_SUFFIX_RE.sub("", n).strip()
    if base and base not in keys:
        keys.append(base)
    if n.startswith("*ST") and len(n) > 3:
        keys.append(n[3:].strip())
    elif n.upper().startswith("ST") and len(n) > 2 and n[2] not in (" ", "＊"):
        keys.append(n[2:].strip())
    return keys


def _merged_search_rows() -> list[dict[str, Any]]:
    """
    东财全表 + 本地上传基本信息表合并（按代码去重）。
    同代码优先东财简称，便于覆盖 CSV 未收录的次新股；仅在上传表中的代码仍保留。
    """
    by_code: dict[str, dict[str, Any]] = {}
    for s in load_stocks():
        code = str(s.get("code") or "").strip().zfill(6)
        if code.isdigit() and len(code) == 6:
            by_code[code] = dict(s)
    from app.services import issuer_basic_info_universe as ibu

    extra = ibu.get_stocks_for_search_if_available()
    if extra:
        for s in extra:
            code = str(s.get("code") or "").strip().zfill(6)
            if not code.isdigit() or len(code) != 6:
                continue
            if code not in by_code:
                by_code[code] = dict(s)
    return list(by_code.values())


def _score_stock_match(q: str, name: str, code: str, sym: str) -> int:
    ql = q.lower()
    score = 0
    for key in _name_search_keys(name):
        if key.startswith(q):
            score = max(score, 100)
        elif q in key:
            score = max(score, 80)
    if code.startswith(q):
        score = max(score, 70)
    elif q in code:
        score = max(score, 60)
    elif ql and (ql in code.lower() or ql in sym.lower()):
        score = max(score, 50)
    return score


def _fetch_suggest_rows(query: str, limit: int) -> list[dict[str, Any]]:
    """东财即时联想（次新股/非常规简称兜底）。"""
    q = (query or "").strip()
    if len(q) < 2:
        return []
    try:
        with httpx.Client(timeout=8.0, headers={"User-Agent": UA}) as client:
            r = client.get(
                _SUGGEST,
                params={"input": q, "type": "14", "count": max(1, min(limit, 20))},
            )
            r.raise_for_status()
            data = r.json()
            table = ((data.get("QuotationCodeTable") or {}).get("Data") or [])
            raw_rows: list[dict[str, Any]] = []
            for item in table:
                if not isinstance(item, dict):
                    continue
                code = str(item.get("Code") or item.get("UnifiedCode") or "").strip().zfill(6)
                name = str(item.get("Name") or "").strip()
                mkt = str(item.get("MarketType") or item.get("MktNum") or "").strip()
                if mkt == "1":
                    suf = ".SS"
                elif mkt == "2":
                    suf = ".SZ"
                else:
                    suf = _suffix_from_code(code)
                raw_rows.append({"code": code, "name": name, "suffix": suf})
            return _normalize_rows(raw_rows)
    except Exception:  # noqa: BLE001
        return []


def search_stocks(query: str, limit: int = 15, live_fallback: bool = True) -> list[dict[str, Any]]:
    q = (query or "").strip()
    if not q:
        return []
    rows = _merged_search_rows()
    if not rows:
        rows = load_stocks()
    if not rows:
        if live_fallback:
            return _fetch_suggest_rows(q, limit)[: max(1, min(limit, 50))]
        return []
    limit = max(1, min(limit, 50))
    scored: list[tuple[int, dict[str, Any]]] = []
    for s in rows:
        name = s.get("name") or ""
        code = s.get("code") or ""
        sym = s.get("symbol") or ""
        if not code:
            continue
        score = _score_stock_match(q, name, code, sym)
        if score <= 0:
            continue
        scored.append((score, s))
    scored.sort(key=lambda x: (-x[0], x[1].get("code", "")))
    out = [x[1] for x in scored[:limit]]
    if not out and live_fallback:
        live = _fetch_suggest_rows(q, limit)
        if live:
            return live[:limit]
    return out


def _fetch_eastmoney_rows() -> list[dict[str, Any]]:
    all_rows: list[dict[str, Any]] = []
    pn = 1
    pz = 500
    with httpx.Client(timeout=45.0, headers={"User-Agent": UA}) as client:
        while True:
            params = {
                "pn": pn,
                "pz": pz,
                "po": 0,
                "np": 1,
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                "fltt": 2,
                "invt": 2,
                "fid": "f12",
                "fs": _FS,
                "fields": "f12,f14,f13",
            }
            r = client.get(_CLIST, params=params)
            r.raise_for_status()
            data = r.json()
            diff = (((data.get("data") or {}).get("diff")) or [])
            if not diff:
                break
            for row in diff:
                code = str(row.get("f12") or "").strip().zfill(6)
                name = (row.get("f14") or "").strip()
                suf = _suffix_from_em(code, row.get("f13"))
                all_rows.append({"code": code, "name": name, "suffix": suf})
            if len(diff) < pz:
                break
            pn += 1
            if pn > 40:
                break
    return _normalize_rows(all_rows)


def _fetch_baidu_rows() -> list[dict[str, Any]]:
    headers = {
        "User-Agent": UA,
        "Accept": "application/vnd.finance-web.v1+json",
        "Referer": "https://gushitong.baidu.com/",
        "Origin": "https://gushitong.baidu.com",
    }
    out: list[dict[str, Any]] = []
    with httpx.Client(timeout=45.0, headers=headers, follow_redirects=True) as client:
        for page_no in range(0, 50):
            params = {
                "sort_type": "1",
                "sort_key": "14",
                "from_mid": "1",
                "group": "pclist",
                "type": "ab",
                "finClientType": "pc",
                "pn": page_no * 200,
                "rn": 200,
            }
            r = client.get(_BAIDU_RANK, params=params)
            r.raise_for_status()
            data = r.json()
            if str(data.get("ResultCode")) != "0":
                continue
            result = (((data.get("Result") or {}).get("Result") or []))
            if not result:
                break
            rank = (
                (((result[0] or {}).get("DisplayData") or {}).get("resultData") or {})
                .get("tplData", {})
                .get("result", {})
                .get("rank")
                or []
            )
            if not rank:
                break
            for item in rank:
                exchange = str(item.get("exchange") or "").upper()
                suffix = f".{exchange}" if exchange in ("SZ", "BJ") else ".SS" if exchange in ("SH", "SS") else ""
                out.append({"code": item.get("code"), "name": item.get("name"), "suffix": suffix})
            if len(rank) < 200:
                break
    return _normalize_rows(out)


def _fetch_sina_rows() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with httpx.Client(timeout=45.0, headers={"User-Agent": UA, "Referer": "https://finance.sina.com.cn/"}) as client:
        for page in range(1, 200):
            params = {
                "page": page,
                "num": 80,
                "sort": "changepercent",
                "asc": 0,
                "node": "hs_a",
                "symbol": "",
                "_s_r_a": "page",
            }
            r = client.get(_SINA_RANK, params=params)
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, list) or not data:
                break
            for item in data:
                if isinstance(item, dict):
                    out.append({"code": item.get("code"), "name": item.get("name")})
            if len(data) < 80:
                break
    return _normalize_rows(out)


def fetch_and_save() -> dict[str, Any]:
    """拉取全部列表并写入本地；按策略链自动降级。"""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    fetchers = {
        "eastmoney": _fetch_eastmoney_rows,
        "baidu": _fetch_baidu_rows,
        "sina": _fetch_sina_rows,
    }
    errors: list[str] = []
    all_rows: list[dict[str, Any]] = []
    used_source = ""
    for source in market_source_policy.stock_universe_chain():
        fn = fetchers.get(source)
        if fn is None:
            continue
        try:
            rows = fn()
            if len(rows) >= _MIN_EXPECTED_A_SHARE_COUNT:
                all_rows = rows
                used_source = source
                break
            errors.append(f"{source} 返回数量异常：{len(rows)} 条")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{source}: {exc!s}"[:500])
    if len(all_rows) < _MIN_EXPECTED_A_SHARE_COUNT:
        previous_count = len((_load_file_raw().get("stocks") or []))
        raise RuntimeError(
            f"股票列表多源刷新失败，已拒绝覆盖本地文件；最后数量 {len(all_rows)} 条"
            f"（当前本地 {previous_count} 条）"
            f"；错误：{'; '.join(errors)[:1200]}"
        )
    payload = {"updated_at": int(time.time()), "count": len(all_rows), "stocks": all_rows}
    data_asset_manager.atomic_write_json(
        _path(),
        payload,
        kind="stock_universe",
        key="a_share_stocks",
        source=f"{used_source}_stock_universe",
        item_count=len(all_rows),
    )
    global _cache_rows, _cache_mtime
    _cache_rows = all_rows
    _cache_mtime = _path().stat().st_mtime
    return {"count": len(all_rows), "updated_at": payload["updated_at"], "source": used_source}


def meta() -> dict[str, Any]:
    raw = _load_file_raw()
    return {
        "count": len(raw.get("stocks") or []),
        "updated_at": raw.get("updated_at") or 0,
        "path": str(_path()),
    }
