"""上市公司结构化基础库：基本信息、财务指标、管理层经营讨论与分析。

数据来自本地 CSV，可重复导入；最新视图按逻辑主键 upsert，原始内容修订不可变保存。
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from app.config import settings
from app.storage import data_asset_manager

DB_NAME = "company_fundamentals.db"
_DATASET_TABLES = {
    "basic_info": "company_basic_records",
    "financial": "financial_indicator_records",
    "mda": "management_discussion_records",
}


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
            CREATE INDEX IF NOT EXISTS idx_source_files_content
              ON source_files(dataset,sha256,size_bytes);

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

            CREATE TABLE IF NOT EXISTS fundamentals_schema_meta (
              key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS company_record_revisions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              dataset TEXT NOT NULL,
              logical_key TEXT NOT NULL,
              symbol TEXT NOT NULL,
              report_date TEXT NOT NULL,
              row_hash TEXT NOT NULL,
              content_hash TEXT NOT NULL,
              raw_json TEXT NOT NULL,
              source_path TEXT NOT NULL,
              observed_at TEXT NOT NULL,
              recorded_at TEXT NOT NULL,
              published_at TEXT,
              publication_field TEXT,
              available_at TEXT,
              origin TEXT NOT NULL,
              UNIQUE(dataset,logical_key,content_hash)
            );
            CREATE INDEX IF NOT EXISTS idx_company_revision_symbol
              ON company_record_revisions(symbol,dataset,observed_at,id);
            CREATE TRIGGER IF NOT EXISTS company_revision_no_update BEFORE UPDATE ON company_record_revisions
              BEGIN SELECT RAISE(ABORT,'Company content revisions are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS company_revision_no_delete BEFORE DELETE ON company_record_revisions
              BEGIN SELECT RAISE(ABORT,'Company content revisions are immutable'); END;
            """
        )
        if not conn.execute("SELECT 1 FROM fundamentals_schema_meta WHERE key='content_revisions_v1'").fetchone():
            for dataset, table in _DATASET_TABLES.items():
                for row in conn.execute(f"SELECT * FROM {table}").fetchall():
                    _record_revision(conn, dataset, dict(row), origin="legacy_backfill")
            conn.execute("INSERT INTO fundamentals_schema_meta VALUES('content_revisions_v1','1')")
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
    if not rows:
        return
    dataset = {_UPSERT_BASIC: "basic_info", _UPSERT_FINANCIAL: "financial", _UPSERT_MDA: "mda"}[sql]
    table = _DATASET_TABLES[dataset]
    columns = [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]
    keys = list({row[0] for row in rows})
    # Retain pre-existing rows too, including databases populated before this
    # migration. Batches stay below conservative SQLite variable limits.
    for offset in range(0, len(keys), 800):
        part = keys[offset:offset + 800]
        placeholders = ",".join("?" for _ in part)
        for old in conn.execute(f"SELECT * FROM {table} WHERE logical_key IN ({placeholders})", part).fetchall():
            _record_revision(conn, dataset, dict(old), origin="before_update")
    for row in rows:
        _record_revision(conn, dataset, dict(zip(columns, row)), origin="csv_import")
    conn.executemany(sql, rows)


def _explicit_timestamp(value: Any) -> str | None:
    """Do not invent a time or timezone for a date-only announcement."""
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        if isinstance(value, (float, int)):
            dt = datetime.fromtimestamp(value, timezone.utc)
        else:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                return None
        return dt.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError, OSError, OverflowError):
        return None


def _publication_metadata(raw: dict) -> tuple[str | None, str | None, str | None]:
    # Accounting period EndDate/Accper and local import time are deliberately
    # absent. Preserve date-only source declarations without granting PIT use.
    fields = ("published_at", "PublishedAt", "PublicationDate", "AnnouncementDate", "AnnDate",
              "DeclareDate", "Annodt", "FI_T1.Annodt", "FI_T1.AnnDate", "公告时间", "公告日期")
    field = next((key for key in fields if raw.get(key) not in (None, "")), None)
    value = str(raw[field]) if field else None
    published = _explicit_timestamp(value) or value
    available = next((_explicit_timestamp(raw[key]) for key in ("available_at", "AvailableAt")
                      if raw.get(key) not in (None, "")), None)
    return published, field, available


def _record_revision(conn: sqlite3.Connection, dataset: str, record: dict, *, origin: str) -> None:
    raw = json.loads(record["raw_json"])
    if dataset == "mda":
        # The latest view stores the long text separately; the revision must be
        # self-contained or a subsequent correction would lose the earlier text.
        raw["ManaDiscAnal"] = record.get("mana_disc_anal") or ""
    encoded = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    published, field, available = _publication_metadata(raw)
    conn.execute("""INSERT OR IGNORE INTO company_record_revisions
      (dataset,logical_key,symbol,report_date,row_hash,content_hash,raw_json,source_path,
       observed_at,recorded_at,published_at,publication_field,available_at,origin)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        dataset, record["logical_key"], record["symbol"], record["report_date"], record["row_hash"],
        digest, encoded, record["source_path"], record["updated_at"], _now_iso(), published, field, available, origin))


def list_revisions(symbol: str, *, dataset: str | None = None, logical_key: str | None = None,
                   observed_before: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    """Explain revisions and local observations; not automatic PIT factor input."""
    if dataset is not None and dataset not in _DATASET_TABLES:
        raise ValueError("Unknown fundamentals dataset")
    cutoff = _explicit_timestamp(observed_before)
    if observed_before is not None and cutoff is None:
        raise ValueError("observed_before requires a timestamp with timezone")
    init_db()
    sym = symbol.strip().upper().split(".", 1)[0]
    with connect() as conn:
        rows = conn.execute("""SELECT * FROM company_record_revisions WHERE symbol=?
          AND (? IS NULL OR dataset=?) AND (? IS NULL OR logical_key=?)
          AND (? IS NULL OR observed_at<=?) ORDER BY observed_at DESC,id DESC LIMIT ?""",
          (sym, dataset, dataset, logical_key, logical_key, cutoff, cutoff, max(1, min(int(limit), 10000)))).fetchall()
    return [{**dict(row), "raw": json.loads(row["raw_json"]), "usage": "explanation_and_revision_audit",
             "historical_availability_verified": False} for row in rows]


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
        return path.resolve().relative_to(settings.data_dir.resolve()).as_posix()
    except ValueError:
        # An explicit external import remains an honest origin reference. The
        # self-contained row revisions do not depend on this file to be read.
        return str(path.resolve())


def _already_imported(conn, source, dataset, sha, size) -> bool:
    old = conn.execute("SELECT * FROM source_files WHERE path=?", (source,)).fetchone()
    if old is not None:
        return old["dataset"] == dataset and old["sha256"] == sha and int(old["size_bytes"]) == size
    # Relocation can leave a legacy absolute/parent-relative provenance key.
    # Reuse byte-identical evidence and add the new locator without rewriting
    # immutable historical source paths or replaying every CSV row.
    old = conn.execute("SELECT * FROM source_files WHERE dataset=? AND sha256=? AND size_bytes=? LIMIT 1",
                       (dataset, sha, size)).fetchone()
    if old is None:
        return False
    conn.execute("""INSERT INTO source_files(path,dataset,sha256,size_bytes,mtime,imported_at,row_count)
                    VALUES(?,?,?,?,?,?,?)""",
                 (source, dataset, sha, size, old["mtime"], old["imported_at"], old["row_count"]))
    return True


def import_basic_csv(path: Path, *, force: bool = False, batch_size: int = 2000) -> int:
    init_db()
    sha = _file_sha256(path)
    stat = path.stat()
    source = _source_rel(path)
    with connect() as conn:
        if not force and _already_imported(conn, source, "basic_info", sha, stat.st_size):
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
        if not force and _already_imported(conn, source, "financial", sha, stat.st_size):
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
        if not force and _already_imported(conn, source, "mda", sha, stat.st_size):
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
    base = root or settings.data_dir / "imports" / "company_fundamentals"
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
            (started, "running", _source_rel(base)),
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


def financial_history(symbol: str, limit: int = 12) -> dict[str, Any]:
    """Read local reported values, preserving source fields and unknown units."""
    from datetime import date
    from zoneinfo import ZoneInfo
    from app.schemas.tracking import canonical_symbol
    symbol = canonical_symbol(symbol)
    result = {"symbol": symbol, "items": [],
              "note": "仅本地导入记录；原始字段未附单位时不推断、不换算。请按同报表类型与同报告期比较，报告期不是披露时间。"}
    path = settings.data_dir / DB_NAME
    if not path.exists():
        return result
    metadata = {"stkcd", "symbol", "shortname", "accper", "enddate", "typrep", "source", "source_type",
                "indcd1", "indnme1", "report_date", "report_type", "updated_at", "published_at", "unit", "currency",
                "annodt", "availableat", "available_at", "publishedat", "created_at", "observed_at", "recorded_at"}
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='financial_indicator_records'").fetchone():
            return result
        rows = conn.execute("SELECT * FROM financial_indicator_records WHERE symbol=? AND report_date<=? ORDER BY report_date DESC,report_type LIMIT ?",
                            (symbol[:6], today.isoformat(), max(1, min(int(limit), 100)))).fetchall()
    for row in rows:
        try:
            report_date = date.fromisoformat(row["report_date"])
        except (TypeError, ValueError):
            continue
        if report_date > today:
            continue
        raw = json.loads(row["raw_json"])
        values, units = {}, {}
        for key, value in raw.items():
            if key.split(".")[-1].lower() in metadata or re.search(r"(?:date|time|_id|code|单位)$", key, re.I) or isinstance(value, bool):
                continue
            text = str(value).strip()
            percent = text.endswith("%")
            try:
                number = float(text[:-1] if percent else text)
            except (ValueError, TypeError):
                continue
            if not math.isfinite(number):
                continue
            values[key] = number
            explicit = raw.get(key + "_unit") or raw.get(key + ".unit")
            units[key] = "%" if percent else str(explicit)[:50] if explicit else None
        published, _, _ = _publication_metadata(raw)
        result["items"].append({"report_date": row["report_date"], "report_type": row["report_type"],
                                "source_type": row["source_type"], "source_name": Path(row["source_path"]).name,
                                "observed_at": row["updated_at"], "published_at": published, "row_hash": row["row_hash"],
                                "values": values, "units": units})
    return result
