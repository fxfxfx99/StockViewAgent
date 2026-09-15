"""上市公司结构化基础库：基本信息、财务指标、管理层经营讨论与分析。

数据来自本地 CSV，可重复导入；以逻辑主键 upsert，避免持续更新时重复膨胀。
"""
from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from app.config import settings
from app.storage import data_asset_manager

DB_NAME = "company_fundamentals.db"


def db_path() -> Path:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings.data_dir / DB_NAME


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path()))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS import_runs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              started_at TEXT NOT NULL,
              finished_at TEXT,
              status TEXT NOT NULL,
              source_dir TEXT NOT NULL,
              basic_rows INTEGER NOT NULL DEFAULT 0,
              financial_rows INTEGER NOT NULL DEFAULT 0,
              mda_rows INTEGER NOT NULL DEFAULT 0,
              errors_json TEXT NOT NULL DEFAULT '[]'
            );

            CREATE TABLE IF NOT EXISTS source_files (
              path TEXT PRIMARY KEY,
              dataset TEXT NOT NULL,
              sha256 TEXT NOT NULL,
              size_bytes INTEGER NOT NULL,
              mtime REAL NOT NULL,
              imported_at TEXT NOT NULL,
              row_count INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS company_basic_records (
              logical_key TEXT PRIMARY KEY,
              symbol TEXT NOT NULL,
              short_name TEXT,
              report_date TEXT NOT NULL,
              listed_co_id TEXT,
              security_id TEXT,
              full_name TEXT,
              industry_code_c TEXT,
              industry_name_c TEXT,
              province TEXT,
              city TEXT,
              listing_date TEXT,
              listing_state TEXT,
              main_business TEXT,
              business_scope TEXT,
              website TEXT,
              email TEXT,
              raw_json TEXT NOT NULL,
              row_hash TEXT NOT NULL,
              source_path TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS financial_indicator_records (
              logical_key TEXT PRIMARY KEY,
              symbol TEXT NOT NULL,
              short_name TEXT,
              report_date TEXT NOT NULL,
              report_type TEXT NOT NULL,
              source_type TEXT,
              industry_code_1 TEXT,
              industry_name_1 TEXT,
              raw_json TEXT NOT NULL,
              row_hash TEXT NOT NULL,
              source_path TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS management_discussion_records (
              logical_key TEXT PRIMARY KEY,
              symbol TEXT NOT NULL,
              short_name TEXT,
              report_date TEXT NOT NULL,
              industry_code_1 TEXT,
              industry_name_1 TEXT,
              mana_disc_anal TEXT,
              textual_similarity REAL,
              positive_vocabulary_num INTEGER,
              negative_vocabulary_num INTEGER,
              total_words_num INTEGER,
              sentences_num INTEGER,
              words_num INTEGER,
              emotion_tone1 REAL,
              emotion_tone2 REAL,
              raw_json TEXT NOT NULL,
              row_hash TEXT NOT NULL,
              source_path TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_basic_symbol_date
              ON company_basic_records(symbol, report_date DESC);
            CREATE INDEX IF NOT EXISTS idx_financial_symbol_date
              ON financial_indicator_records(symbol, report_date DESC);
            CREATE INDEX IF NOT EXISTS idx_mda_symbol_date
              ON management_discussion_records(symbol, report_date DESC);
            CREATE INDEX IF NOT EXISTS idx_basic_name
              ON company_basic_records(short_name);
            """
        )
    data_asset_manager.register_existing_asset(
        db_path(),
        kind="sqlite_db",
        key="company_fundamentals",
        source="company_fundamentals_store",
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _clean_key(key: str) -> str:
    return (key or "").strip().lstrip("\ufeff")


def _clean_row(row: dict[str, Any]) -> dict[str, str]:
    return {_clean_key(k): str(v or "").strip() for k, v in row.items() if k is not None}


def _get(row: dict[str, str], *keys: str) -> str:
    for key in keys:
        val = row.get(key)
        if val not in (None, ""):
            return str(val).strip()
    return ""


def _as_int(value: str) -> int | None:
    try:
        if value == "":
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _as_float(value: str) -> float | None:
    try:
        if value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _json_dumps(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _row_hash(row: dict[str, Any]) -> str:
    return hashlib.sha1(_json_dumps(row).encode("utf-8")).hexdigest()


def _iter_csv(path: Path) -> Iterable[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            yield _clean_row(row)


def _upsert_many(conn: sqlite3.Connection, sql: str, rows: list[tuple[Any, ...]]) -> None:
    if rows:
        conn.executemany(sql, rows)


_UPSERT_BASIC = """
INSERT INTO company_basic_records (
  logical_key, symbol, short_name, report_date, listed_co_id, security_id, full_name,
  industry_code_c, industry_name_c, province, city, listing_date, listing_state,
  main_business, business_scope, website, email, raw_json, row_hash, source_path, updated_at
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(logical_key) DO UPDATE SET
  short_name=excluded.short_name,
  listed_co_id=excluded.listed_co_id,
  security_id=excluded.security_id,
  full_name=excluded.full_name,
  industry_code_c=excluded.industry_code_c,
  industry_name_c=excluded.industry_name_c,
  province=excluded.province,
  city=excluded.city,
  listing_date=excluded.listing_date,
  listing_state=excluded.listing_state,
  main_business=excluded.main_business,
  business_scope=excluded.business_scope,
  website=excluded.website,
  email=excluded.email,
  raw_json=excluded.raw_json,
  row_hash=excluded.row_hash,
  source_path=excluded.source_path,
  updated_at=excluded.updated_at
"""

_UPSERT_FINANCIAL = """
INSERT INTO financial_indicator_records (
  logical_key, symbol, short_name, report_date, report_type, source_type,
  industry_code_1, industry_name_1, raw_json, row_hash, source_path, updated_at
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(logical_key) DO UPDATE SET
  short_name=excluded.short_name,
  source_type=excluded.source_type,
  industry_code_1=excluded.industry_code_1,
  industry_name_1=excluded.industry_name_1,
  raw_json=excluded.raw_json,
  row_hash=excluded.row_hash,
  source_path=excluded.source_path,
  updated_at=excluded.updated_at
"""

_UPSERT_MDA = """
INSERT INTO management_discussion_records (
  logical_key, symbol, short_name, report_date, industry_code_1, industry_name_1,
  mana_disc_anal, textual_similarity, positive_vocabulary_num, negative_vocabulary_num,
  total_words_num, sentences_num, words_num, emotion_tone1, emotion_tone2,
  raw_json, row_hash, source_path, updated_at
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(logical_key) DO UPDATE SET
  short_name=excluded.short_name,
  industry_code_1=excluded.industry_code_1,
  industry_name_1=excluded.industry_name_1,
  mana_disc_anal=excluded.mana_disc_anal,
  textual_similarity=excluded.textual_similarity,
  positive_vocabulary_num=excluded.positive_vocabulary_num,
  negative_vocabulary_num=excluded.negative_vocabulary_num,
  total_words_num=excluded.total_words_num,
  sentences_num=excluded.sentences_num,
  words_num=excluded.words_num,
  emotion_tone1=excluded.emotion_tone1,
  emotion_tone2=excluded.emotion_tone2,
  raw_json=excluded.raw_json,
  row_hash=excluded.row_hash,
  source_path=excluded.source_path,
  updated_at=excluded.updated_at
"""


def _source_rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(settings.data_dir.parent.resolve()))
    except ValueError:
        return str(path.resolve())


def import_basic_csv(path: Path, *, force: bool = False, batch_size: int = 2000) -> int:
    init_db()
    sha = _file_sha256(path)
    stat = path.stat()
    source = _source_rel(path)
    with connect() as conn:
        old = conn.execute("SELECT sha256, size_bytes FROM source_files WHERE path=?", (source,)).fetchone()
        if old and old["sha256"] == sha and int(old["size_bytes"]) == stat.st_size and not force:
            return 0
        rows: list[tuple[Any, ...]] = []
        count = 0
        now = _now_iso()
        for row in _iter_csv(path):
            symbol = _get(row, "Symbol")
            report_date = _get(row, "EndDate")
            if not symbol or not report_date:
                continue
            logical_key = f"{symbol}:{report_date}"
            rows.append(
                (
                    logical_key,
                    symbol,
                    _get(row, "ShortName"),
                    report_date,
                    _get(row, "ListedCoID"),
                    _get(row, "SecurityID"),
                    _get(row, "FullName"),
                    _get(row, "IndustryCodeC"),
                    _get(row, "IndustryNameC"),
                    _get(row, "PROVINCE"),
                    _get(row, "CITY"),
                    _get(row, "LISTINGDATE"),
                    _get(row, "LISTINGSTATE"),
                    _get(row, "MAINBUSSINESS"),
                    _get(row, "BusinessScope"),
                    _get(row, "Website"),
                    _get(row, "EMAIL", "SecretaryEmail"),
                    _json_dumps(row),
                    _row_hash(row),
                    source,
                    now,
                )
            )
            count += 1
            if len(rows) >= batch_size:
                _upsert_many(conn, _UPSERT_BASIC, rows)
                rows.clear()
        _upsert_many(conn, _UPSERT_BASIC, rows)
        conn.execute(
            """
            INSERT OR REPLACE INTO source_files(path,dataset,sha256,size_bytes,mtime,imported_at,row_count)
            VALUES(?,?,?,?,?,?,?)
            """,
            (source, "basic_info", sha, stat.st_size, stat.st_mtime, now, count),
        )
    data_asset_manager.register_existing_asset(db_path(), kind="sqlite_db", key="company_fundamentals", source="basic_info_import")
    return count


def import_financial_csv(path: Path, *, force: bool = False, batch_size: int = 1000) -> int:
    init_db()
    sha = _file_sha256(path)
    stat = path.stat()
    source = _source_rel(path)
    with connect() as conn:
        old = conn.execute("SELECT sha256, size_bytes FROM source_files WHERE path=?", (source,)).fetchone()
        if old and old["sha256"] == sha and int(old["size_bytes"]) == stat.st_size and not force:
            return 0
        rows: list[tuple[Any, ...]] = []
        count = 0
        now = _now_iso()
        for row in _iter_csv(path):
            symbol = _get(row, "FI_T1.Stkcd")
            report_date = _get(row, "FI_T1.Accper")
            report_type = _get(row, "FI_T1.Typrep") or "A"
            if not symbol or not report_date:
                continue
            logical_key = f"{symbol}:{report_date}:{report_type}"
            rows.append(
                (
                    logical_key,
                    symbol,
                    _get(row, "FI_T1.ShortName", "FI_T2.ShortName"),
                    report_date,
                    report_type,
                    _get(row, "FI_T1.Source"),
                    _get(row, "FI_T1.Indcd1", "FI_T2.Indcd1"),
                    _get(row, "FI_T1.Indnme1", "FI_T2.Indnme1"),
                    _json_dumps(row),
                    _row_hash(row),
                    source,
                    now,
                )
            )
            count += 1
            if len(rows) >= batch_size:
                _upsert_many(conn, _UPSERT_FINANCIAL, rows)
                rows.clear()
        _upsert_many(conn, _UPSERT_FINANCIAL, rows)
        conn.execute(
            """
            INSERT OR REPLACE INTO source_files(path,dataset,sha256,size_bytes,mtime,imported_at,row_count)
            VALUES(?,?,?,?,?,?,?)
            """,
            (source, "financial", sha, stat.st_size, stat.st_mtime, now, count),
        )
    data_asset_manager.register_existing_asset(db_path(), kind="sqlite_db", key="company_fundamentals", source="financial_import")
    return count


def import_mda_csv(path: Path, *, force: bool = False, batch_size: int = 1000) -> int:
    init_db()
    sha = _file_sha256(path)
    stat = path.stat()
    source = _source_rel(path)
    with connect() as conn:
        old = conn.execute("SELECT sha256, size_bytes FROM source_files WHERE path=?", (source,)).fetchone()
        if old and old["sha256"] == sha and int(old["size_bytes"]) == stat.st_size and not force:
            return 0
        rows: list[tuple[Any, ...]] = []
        count = 0
        now = _now_iso()
        for row in _iter_csv(path):
            symbol = _get(row, "Symbol")
            report_date = _get(row, "Enddate", "EndDate")
            if not symbol or not report_date:
                continue
            logical_key = f"{symbol}:{report_date}"
            raw = dict(row)
            raw.pop("ManaDiscAnal", None)
            rows.append(
                (
                    logical_key,
                    symbol,
                    _get(row, "ShortName"),
                    report_date,
                    _get(row, "IndustryCode1"),
                    _get(row, "IndustryName1"),
                    _get(row, "ManaDiscAnal"),
                    _as_float(_get(row, "TextualSimilarity")),
                    _as_int(_get(row, "PositiveVocabularyNum")),
                    _as_int(_get(row, "NegativeVocabularyNum")),
                    _as_int(_get(row, "TotalWordsNum")),
                    _as_int(_get(row, "SentencesNum")),
                    _as_int(_get(row, "WordsNum")),
                    _as_float(_get(row, "EmotionTone1")),
                    _as_float(_get(row, "EmotionTone2")),
                    _json_dumps(raw),
                    _row_hash(row),
                    source,
                    now,
                )
            )
            count += 1
            if len(rows) >= batch_size:
                _upsert_many(conn, _UPSERT_MDA, rows)
                rows.clear()
        _upsert_many(conn, _UPSERT_MDA, rows)
        conn.execute(
            """
            INSERT OR REPLACE INTO source_files(path,dataset,sha256,size_bytes,mtime,imported_at,row_count)
            VALUES(?,?,?,?,?,?,?)
            """,
            (source, "mda", sha, stat.st_size, stat.st_mtime, now, count),
        )
    data_asset_manager.register_existing_asset(db_path(), kind="sqlite_db", key="company_fundamentals", source="mda_import")
    return count


def import_local_2025_files(root: Path | None = None, *, force: bool = False) -> dict[str, Any]:
    base = root or settings.data_dir.parent.parent
    files = {
        "financial": base / "财务指标 2025.csv",
        "mda": base / "管理层经营讨论与分析 2025.csv",
        "basic_info": base / "上市公司基本信息 2025.csv",
    }
    init_db()
    started = _now_iso()
    errors: list[dict[str, str]] = []
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO import_runs(started_at,status,source_dir) VALUES(?,?,?)",
            (started, "running", str(base)),
        )
        run_id = int(cur.lastrowid)

    counts = {"basic_rows": 0, "financial_rows": 0, "mda_rows": 0}
    try:
        if files["basic_info"].exists():
            counts["basic_rows"] = import_basic_csv(files["basic_info"], force=force)
        else:
            errors.append({"dataset": "basic_info", "error": f"missing: {files['basic_info']}"})
        if files["financial"].exists():
            counts["financial_rows"] = import_financial_csv(files["financial"], force=force)
        else:
            errors.append({"dataset": "financial", "error": f"missing: {files['financial']}"})
        if files["mda"].exists():
            counts["mda_rows"] = import_mda_csv(files["mda"], force=force)
        else:
            errors.append({"dataset": "mda", "error": f"missing: {files['mda']}"})
        status = "ok" if not errors else "warning"
    except Exception as exc:  # noqa: BLE001
        status = "error"
        errors.append({"dataset": "import", "error": str(exc)[:1000]})
        raise
    finally:
        with connect() as conn:
            conn.execute(
                """
                UPDATE import_runs
                SET finished_at=?, status=?, basic_rows=?, financial_rows=?, mda_rows=?, errors_json=?
                WHERE id=?
                """,
                (
                    _now_iso(),
                    status if "status" in locals() else "error",
                    counts["basic_rows"],
                    counts["financial_rows"],
                    counts["mda_rows"],
                    json.dumps(errors, ensure_ascii=False),
                    run_id,
                ),
            )
    return {"run_id": run_id, "status": status, **counts, "errors": errors, "db": str(db_path())}


def status() -> dict[str, Any]:
    init_db()
    with connect() as conn:
        counts = {
            "basic_info": conn.execute("SELECT COUNT(*) FROM company_basic_records").fetchone()[0],
            "financial": conn.execute("SELECT COUNT(*) FROM financial_indicator_records").fetchone()[0],
            "mda": conn.execute("SELECT COUNT(*) FROM management_discussion_records").fetchone()[0],
        }
        files = [dict(r) for r in conn.execute("SELECT * FROM source_files ORDER BY dataset, path")]
        runs = [dict(r) for r in conn.execute("SELECT * FROM import_runs ORDER BY id DESC LIMIT 10")]
    stat = db_path().stat() if db_path().exists() else None
    return {
        "db": str(db_path()),
        "size_bytes": stat.st_size if stat else 0,
        "counts": counts,
        "source_files": files,
        "recent_runs": runs,
    }


def latest_for_symbol(symbol: str) -> dict[str, Any]:
    init_db()
    sym = symbol.strip().upper().split(".", 1)[0]
    with connect() as conn:
        basic = conn.execute(
            "SELECT * FROM company_basic_records WHERE symbol=? ORDER BY report_date DESC LIMIT 1", (sym,)
        ).fetchone()
        financial = conn.execute(
            "SELECT * FROM financial_indicator_records WHERE symbol=? ORDER BY report_date DESC, report_type LIMIT 1",
            (sym,),
        ).fetchone()
        mda = conn.execute(
            "SELECT * FROM management_discussion_records WHERE symbol=? ORDER BY report_date DESC LIMIT 1", (sym,)
        ).fetchone()
    return {
        "symbol": sym,
        "basic_info": dict(basic) if basic else None,
        "financial": dict(financial) if financial else None,
        "mda": dict(mda) if mda else None,
    }
