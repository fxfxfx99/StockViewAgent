"""
从本地上传中的「上市公司基本信息」CSV 构建股票代码列表，供搜索与添加标的使用。
CSV 为面板数据（同一 Symbol 多行、不同 EndDate），按 Symbol 去重并保留 EndDate 最新的一行 ShortName。
"""
from __future__ import annotations

import csv
import json
import re
import threading
import time
from pathlib import Path
from typing import Any

from app.config import settings
from app.services import issuer_uploads_service as ius

CACHE_NAME = "issuer_basic_info_universe.json"
_rebuild_lock = threading.Lock()


def _cache_path() -> Path:
    return settings.data_dir / CACHE_NAME


def _yahoo_suffix_for_cn_code(code: str) -> str:
    c = (code or "").strip().zfill(6)
    if not c.isdigit() or len(c) != 6:
        return ".SZ"
    if c.startswith(("6", "9")):
        return ".SS"
    if c.startswith(("0", "3")):
        return ".SZ"
    if c.startswith(("4", "8")):
        return ".BJ"
    return ".SZ"


def _norm_header_cell(h: str) -> str:
    return re.sub(r"\s+", "", (h or "").strip().strip('"').lower())


def _find_basic_info_csv_item() -> dict[str, Any] | None:
    """选取「上市公司基本信息」*.csv；若存在「上市公司基本信息 2025.csv」则优先该文件。"""
    items = list(ius._ensure_index().get("items") or [])
    candidates: list[dict[str, Any]] = []
    for x in items:
        if str(x.get("category") or "") != "basic_info":
            continue
        on = str(x.get("original_name") or "")
        if not on.lower().endswith(".csv"):
            continue
        if "上市公司基本信息" not in on:
            continue
        candidates.append(x)
    if not candidates:
        return None
    preferred = [x for x in candidates if str(x.get("original_name") or "") == "上市公司基本信息 2025.csv"]
    if preferred:
        return max(preferred, key=lambda it: str(it.get("created_at") or ""))
    return max(candidates, key=lambda it: str(it.get("created_at") or ""))


def _source_file_path(hit: dict[str, Any]) -> Path | None:
    sn = str(hit.get("stored_name") or "").strip()
    if not sn:
        return None
    p = ius._root() / ius.FILES_SUB / sn
    return p if p.is_file() else None


def _load_cache_raw() -> dict[str, Any]:
    p = _cache_path()
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _write_empty_cache() -> None:
    payload = {
        "updated_at": int(time.time()),
        "source_doc_id": "",
        "source_name": "",
        "source_mtime": 0,
        "count": 0,
        "stocks": [],
    }
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    _cache_path().write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def cache_meta() -> dict[str, Any]:
    raw = _load_cache_raw()
    stocks = raw.get("stocks") or []
    hit = _find_basic_info_csv_item()
    path = _source_file_path(hit) if hit else None
    has_source = bool(path and path.is_file())
    return {
        "universe_count": len(stocks) if isinstance(stocks, list) else 0,
        "universe_updated_at": int(raw.get("updated_at") or 0),
        "universe_source_name": raw.get("source_name") or "",
        "universe_source_doc_id": raw.get("source_doc_id") or "",
        "universe_has_csv_source": has_source,
    }


def _needs_rebuild(path: Path, raw: dict[str, Any]) -> bool:
    try:
        m = int(path.stat().st_mtime)
    except OSError:
        return False
    if int(raw.get("source_mtime") or 0) != m:
        return True
    if not (raw.get("stocks") or []):
        return True
    return False


def rebuild_universe_from_upload_blocking() -> dict[str, Any]:
    """全量解析 CSV 并写缓存；可能耗时较长（大文件）。"""
    with _rebuild_lock:
        hit = _find_basic_info_csv_item()
        if not hit:
            payload = {
                "updated_at": int(time.time()),
                "source_doc_id": "",
                "source_name": "",
                "source_mtime": 0,
                "count": 0,
                "stocks": [],
            }
            settings.data_dir.mkdir(parents=True, exist_ok=True)
            _cache_path().write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            return {"ok": True, "count": 0, "detail": "未找到本地上传的「上市公司基本信息」CSV"}

        path = _source_file_path(hit)
        if not path:
            return {"ok": False, "count": 0, "detail": "文件不存在"}

        by_sym: dict[str, tuple[str, str]] = {}
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            try:
                header = next(reader)
            except StopIteration:
                return {"ok": False, "count": 0, "detail": "CSV 为空"}
            col_index: dict[str, int] = {}
            for i, h in enumerate(header):
                col_index[_norm_header_cell(h)] = i

            def col(name: str) -> int | None:
                return col_index.get(name)

            si = col("symbol")
            ni = col("shortname")
            di = col("enddate")
            if si is None or ni is None or di is None:
                return {"ok": False, "count": 0, "detail": "表头缺少 Symbol / ShortName / EndDate 列"}

            def cell(row: list[str], idx: int | None) -> str:
                if idx is None or idx >= len(row):
                    return ""
                return row[idx].strip().strip('"')

            for row in reader:
                code = cell(row, si)
                if not re.fullmatch(r"\d{6}", code):
                    continue
                name = cell(row, ni)
                ed = cell(row, di)
                prev = by_sym.get(code)
                if prev is None or ed > prev[1]:
                    by_sym[code] = (name, ed)

        stocks: list[dict[str, str]] = []
        for code in sorted(by_sym.keys()):
            name, _ = by_sym[code]
            suf = _yahoo_suffix_for_cn_code(code)
            stocks.append({"code": code, "name": name, "symbol": f"{code}{suf}"})

        try:
            src_mtime = int(path.stat().st_mtime)
        except OSError:
            src_mtime = 0

        payload = {
            "updated_at": int(time.time()),
            "source_doc_id": str(hit.get("id") or ""),
            "source_name": str(hit.get("original_name") or ""),
            "source_mtime": src_mtime,
            "count": len(stocks),
            "stocks": stocks,
        }
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        # 紧凑 JSON，加快大列表写入
        _cache_path().write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        return {"ok": True, "count": len(stocks), "source_name": payload["source_name"]}


def ensure_cached_blocking() -> None:
    """若存在对应 CSV 且缓存落后于文件，则重建；若无此类上传则清空缓存以回退东财全表。"""
    hit = _find_basic_info_csv_item()
    if not hit:
        raw = _load_cache_raw()
        if raw.get("stocks"):
            _write_empty_cache()
        return
    path = _source_file_path(hit)
    if not path:
        return
    raw = _load_cache_raw()
    if not _needs_rebuild(path, raw):
        return
    rebuild_universe_from_upload_blocking()


def get_stocks_for_search_if_available() -> list[dict[str, Any]] | None:
    """
    若缓存中有股票，返回该列表（供与东财全表合并，不再单独独占搜索）。
    否则返回 None。
    """
    raw = _load_cache_raw()
    stocks = raw.get("stocks")
    if isinstance(stocks, list) and len(stocks) > 0:
        return stocks
    return None
