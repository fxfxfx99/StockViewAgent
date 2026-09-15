"""Deduplicated news events and independently gated per-stock interpretations."""
from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from app.config import settings
from app.schemas.news import analysis_content_hash, analysis_input_snapshot, normalize_news, utc_iso
from app.services import news_dedup, news_watchlist_matcher
from app.services.news_priority import classify_priority
from app.storage import watchlist_store


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    path = getattr(settings, "database_path", settings.data_dir / "news.db")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _json(value: Any, fallback: Any) -> Any:
    if isinstance(value, type(fallback)):
        return value
    try:
        result = json.loads(value or "null")
        return result if isinstance(result, type(fallback)) else fallback
    except (ValueError, TypeError):
        return fallback


def _json_list(value: Any) -> list:
    return _json(value, [])


def _row_data(row: Any) -> dict:
    item = dict(row)
    for field in ("matched_symbols", "related_codes", "source_articles"):
        item[field] = _json_list(item.get(field))
    item["match_evidence"] = _json(item.get("match_evidence"), {})
    item["url"] = item.get("url") or item.get("link") or ""
    item["published_at"] = utc_iso(item.get("published_ts"))
    return item


def _threshold() -> int:
    return max(0, min(100, int(getattr(settings, "effective_news_relevance_threshold",
                                      getattr(settings, "news_relevance_threshold", 60)))))


def _score(analysis: dict) -> float:
    try:
        return float(analysis.get("relevance_score") or 0)
    except (ValueError, TypeError):
        return 0


def _qualified(analysis: dict, evidence: list) -> bool:
    """A completed interpretation must have a real relationship and information value.

    Explicit company/product keywords or publisher stock links are admission paths,
    but cannot override a model verdict of weak/unrelated or no information value.
    """
    return (analysis.get("status") == "analyzed" and analysis.get("information_value") is not False
            and analysis.get("relevance_type") != "weak" and _score(analysis) > 0
            and (_score(analysis) >= _threshold() or bool(evidence)))


def _pending_eligible(analysis: dict, evidence: list) -> bool:
    """Reconsider an old threshold rejection when current admission rules allow it.

    A weak relationship or a lack of information value remains a rejection even
    after adding keywords. Completed impacts stay stable and are not repeated.
    """
    if analysis.get("status") == "analyzed":
        return False
    supported = (analysis.get("information_value") is True and analysis.get("relevance_type") != "weak"
                 and _score(analysis) > 0 and (_score(analysis) >= _threshold() or bool(evidence)))
    if analysis.get("status") == "irrelevant":
        return supported
    return bool(evidence) or supported


def _known_symbols(conn: sqlite3.Connection, extra: list[str] | None = None) -> list[str]:
    symbols = set(watchlist_store.load_all_symbols_union()) | set(extra or [])
    symbols.update(row[0] for row in conn.execute("SELECT DISTINCT stock_code FROM article_stock_analysis"))
    # Old links identify which saved profiles to consult, never prove a match themselves.
    for row in conn.execute("SELECT matched_symbols FROM articles"):
        symbols.update(str(s) for s in _json_list(row[0]) if isinstance(s, str))
    return sorted(s.strip().upper() for s in symbols if s and s.strip())


def _aliases(item: dict) -> list[str]:
    aliases = ["id:" + str(item["id"])]
    for source in [item, *item.get("source_articles", [])]:
        url = news_dedup.canonical_url(source.get("url") or source.get("link") or "")
        if url:
            aliases.append("url:" + url)
    return list(dict.fromkeys(aliases))


def _save_aliases(conn: sqlite3.Connection, item: dict, aid: str) -> None:
    conn.executemany("INSERT INTO article_aliases(alias,article_id) VALUES(?,?) ON CONFLICT(alias) DO UPDATE SET article_id=excluded.article_id",
                     [(alias, aid) for alias in _aliases(item)])


def _resolve_id(conn: sqlite3.Connection, aid: str) -> str:
    seen = set()
    while aid not in seen:
        seen.add(aid)
        row = conn.execute("SELECT duplicate_of FROM articles WHERE id=?", [aid]).fetchone()
        if not row or not row[0]:
            break
        aid = row[0]
    return aid


def _write_article(conn: sqlite3.Connection, item: dict, aid: str, symbols: list[str], existing: dict | None = None) -> dict:
    evidence = news_watchlist_matcher.article_match_evidence(item, symbols)
    fetched = item.get("fetched_at") or time.time()
    observed = (existing or {}).get("content_observed_at") or fetched
    if existing and analysis_content_hash(analysis_input_snapshot(existing)) != analysis_content_hash(analysis_input_snapshot(item)):
        observed = max(observed, fetched, item.get("content_observed_at") or 0)
    conn.execute("""
      INSERT INTO articles(id,title,link,summary,published,published_ts,source,symbol_hint,saved_at,
        matched_symbols,content,identity_key,fetched_at,content_observed_at,related_codes,source_articles,match_evidence,duplicate_of)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)
      ON CONFLICT(id) DO UPDATE SET title=excluded.title,link=excluded.link,summary=excluded.summary,
        published=excluded.published,published_ts=excluded.published_ts,source=excluded.source,symbol_hint=excluded.symbol_hint,
        matched_symbols=excluded.matched_symbols,content=excluded.content,fetched_at=excluded.fetched_at,
        content_observed_at=excluded.content_observed_at,related_codes=excluded.related_codes,
        source_articles=excluded.source_articles,match_evidence=excluded.match_evidence,duplicate_of=NULL
    """, (aid, item["title"], item.get("url") or item.get("link") or "", item.get("summary") or "",
          item.get("published") or "", item.get("published_ts"), item.get("source") or "", item.get("symbol_hint"),
          (existing or {}).get("saved_at") or time.time(), json.dumps(sorted(evidence)), item.get("content") or "",
          item["id"], fetched, observed, json.dumps(item.get("related_codes") or []),
          json.dumps(item.get("source_articles") or [], ensure_ascii=False), json.dumps(evidence, ensure_ascii=False)))
    _save_aliases(conn, item, aid)
    return _row_data(conn.execute("SELECT * FROM articles WHERE id=?", [aid]).fetchone())


def _copy_analysis(conn: sqlite3.Connection, old_id: str, aid: str) -> None:
    ranks = {"analyzed": 3, "irrelevant": 2, "error": 1, "unavailable": 0, "stale": 0}
    for row in conn.execute("SELECT * FROM article_stock_analysis WHERE article_id=?", [old_id]).fetchall():
        existing = conn.execute("SELECT * FROM article_stock_analysis WHERE article_id=? AND stock_code=?",
                                [aid, row["stock_code"]]).fetchone()
        if existing and (ranks.get(existing["status"], 0), existing["analyzed_at"]) >= (ranks.get(row["status"], 0), row["analyzed_at"]):
            continue
        if existing:
            conn.execute("""INSERT INTO article_stock_analysis_history
              (article_id,stock_code,status,relevance_score,impact_strength,sentiment,analysis_json,analyzed_at,reason)
              VALUES(?,?,?,?,?,?,?,?,?)""", [*[existing[k] for k in ("article_id", "stock_code", "status", "relevance_score",
                  "impact_strength", "sentiment", "analysis_json", "analyzed_at")], "event_merge"])
        conn.execute("""INSERT OR REPLACE INTO article_stock_analysis
          (article_id,stock_code,status,relevance_score,impact_strength,sentiment,analysis_json,analyzed_at)
          VALUES(?,?,?,?,?,?,?,?)""", [aid, *[row[k] for k in ("stock_code", "status", "relevance_score", "impact_strength", "sentiment", "analysis_json", "analyzed_at")]])
    # Duplicate source rows and their original analyses remain stored for review.


def _reconcile(conn: sqlite3.Connection) -> dict:
    symbols = _known_symbols(conn)
    originals = conn.execute("SELECT * FROM articles WHERE duplicate_of IS NULL ORDER BY saved_at,id").fetchall()
    canonical: dict[str, dict] = {}
    aliases: dict[str, str] = {}
    duplicates = 0
    for raw in originals:
        item = normalize_news(_row_data(raw))
        if not item:
            continue
        item["content_observed_at"] = raw["content_observed_at"]
        aid = next((aliases[a] for a in _aliases(item) if a in aliases and
                    (a.startswith("id:") or news_dedup.same_event(canonical[aliases[a]], item))), None)
        if aid is None:
            aid = next((key for key, previous in canonical.items() if news_dedup.same_event(previous, item)), None)
        if aid is not None:
            previous = canonical[aid]
            merged = news_dedup.merge_news(previous, item)
            merged["fetched_at"] = max(previous.get("fetched_at") or 0, item.get("fetched_at") or 0)
            merged["content_observed_at"] = max(previous.get("content_observed_at") or 0, item.get("content_observed_at") or 0)
            canonical[aid] = _write_article(conn, merged, aid, symbols, previous)
            conn.execute("UPDATE articles SET duplicate_of=? WHERE id=?", [aid, raw["id"]])
            _copy_analysis(conn, raw["id"], aid)
            _save_aliases(conn, item, aid)
            duplicates += 1
        else:
            aid = raw["id"]
            canonical[aid] = _write_article(conn, item, aid, symbols, _row_data(raw))
        for alias in _aliases(item):
            aliases[alias] = aid
    return {"events": len(canonical), "duplicates": duplicates}


def _wrap_legacy_analysis(data: dict[str, Any], fallback_symbol: str) -> list[dict[str, Any]]:
    """Convert legacy article-level analysis_json into per-stock rows."""
    related = data.get("related_stocks")
    if isinstance(related, list) and related:
        return [row for row in related if isinstance(row, dict) and row.get("stock_code")]
    direction_map = {"偏多": "bullish", "偏空": "bearish", "中性": "neutral", "不确定": "neutral"}
    sentiment_map = {"bullish": "positive", "bearish": "negative", "neutral": "neutral"}
    direction = str(data.get("impact_direction") or "")
    mapped = direction_map.get(direction, direction if direction in sentiment_map else "")
    symbols = []
    raw_symbols = data.get("related_symbols")
    if isinstance(raw_symbols, list):
        symbols.extend(str(s).strip().upper() for s in raw_symbols if str(s).strip())
    hint = fallback_symbol.strip().upper()
    if hint and hint not in symbols:
        symbols.append(hint)
    if not symbols:
        return []
    rows = []
    for symbol in dict.fromkeys(symbols):
        row = dict(data)
        row["stock_code"] = symbol
        row["status"] = data.get("status") or "analyzed"
        if mapped:
            row["impact_direction"] = mapped
            row.setdefault("sentiment", sentiment_map.get(mapped, "neutral"))
        rows.append(row)
    return rows


def _migrate_legacy_article_analysis(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT 1 FROM news_schema_meta WHERE key='legacy_analysis_json_v1'").fetchone():
        return
    for row in conn.execute(
        "SELECT id, analysis_json, symbol_hint FROM articles WHERE analysis_json IS NOT NULL AND TRIM(analysis_json) != ''"
    ):
        if conn.execute(
            "SELECT 1 FROM article_stock_analysis WHERE article_id=?", [row["id"]]
        ).fetchone():
            continue
        try:
            data = json.loads(row["analysis_json"])
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        for entry in _wrap_legacy_analysis(data, str(row["symbol_hint"] or "")):
            status = entry.get("status", "analyzed")
            if status not in {"analyzed", "irrelevant", "unavailable", "error", "stale"}:
                status = "analyzed"
            conn.execute(
                """INSERT OR IGNORE INTO article_stock_analysis(article_id,stock_code,status,relevance_score,
                  impact_strength,sentiment,analysis_json,analyzed_at) VALUES(?,?,?,?,?,?,?,?)""",
                [
                    row["id"],
                    str(entry["stock_code"]).upper(),
                    status,
                    entry.get("relevance_score"),
                    entry.get("impact_strength"),
                    entry.get("sentiment"),
                    json.dumps(entry, ensure_ascii=False),
                    entry.get("analyzed_at") or time.time(),
                ],
            )
    conn.execute("INSERT INTO news_schema_meta VALUES('legacy_analysis_json_v1','1')")


def init_db() -> None:
    with _connect() as conn:
        conn.executescript("""
          CREATE TABLE IF NOT EXISTS articles (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, link TEXT NOT NULL, summary TEXT,
            published TEXT, published_ts INTEGER, source TEXT, symbol_hint TEXT,
            saved_at REAL NOT NULL, analysis_json TEXT);
          CREATE INDEX IF NOT EXISTS idx_articles_pub ON articles(published_ts DESC);
          CREATE TABLE IF NOT EXISTS article_stock_analysis (
            article_id TEXT NOT NULL, stock_code TEXT NOT NULL, status TEXT NOT NULL,
            relevance_score INTEGER, impact_strength INTEGER, sentiment TEXT,
            analysis_json TEXT NOT NULL, analyzed_at REAL NOT NULL,
            PRIMARY KEY (article_id, stock_code), FOREIGN KEY (article_id) REFERENCES articles(id));
          CREATE INDEX IF NOT EXISTS idx_analysis_stock ON article_stock_analysis(stock_code,status);
          CREATE TABLE IF NOT EXISTS article_aliases(alias TEXT PRIMARY KEY,article_id TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS news_schema_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS article_stock_analysis_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, article_id TEXT NOT NULL, stock_code TEXT NOT NULL,
            status TEXT NOT NULL, relevance_score INTEGER, impact_strength INTEGER, sentiment TEXT,
            analysis_json TEXT NOT NULL, analyzed_at REAL NOT NULL, reason TEXT NOT NULL);
        """)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(articles)")}
        for name, kind in (("matched_symbols", "TEXT"), ("content", "TEXT"), ("identity_key", "TEXT"),
                           ("fetched_at", "REAL"), ("content_observed_at", "REAL"), ("related_codes", "TEXT"),
                           ("source_articles", "TEXT"), ("match_evidence", "TEXT"), ("duplicate_of", "TEXT")):
            if name not in columns:
                conn.execute(f"ALTER TABLE articles ADD COLUMN {name} {kind}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_identity ON articles(identity_key)")
        conn.execute("UPDATE articles SET fetched_at=saved_at WHERE fetched_at IS NULL")
        conn.execute("UPDATE articles SET content_observed_at=fetched_at WHERE content_observed_at IS NULL")
        if not conn.execute("SELECT 1 FROM news_schema_meta WHERE key='matching_events_v3'").fetchone():
            _reconcile(conn)
            conn.execute("INSERT INTO news_schema_meta VALUES('matching_events_v3','1')")
        _migrate_legacy_article_analysis(conn)
        if not conn.execute("SELECT 1 FROM news_schema_meta WHERE key='match_evidence_backfill_v1'").fetchone():
            symbols = _known_symbols(conn)
            rows = conn.execute("SELECT * FROM articles WHERE duplicate_of IS NULL").fetchall()
            for row in rows:
                evidence = news_watchlist_matcher.article_match_evidence(_row_data(row), symbols)
                conn.execute(
                    "UPDATE articles SET matched_symbols=?,match_evidence=? WHERE id=?",
                    [json.dumps(sorted(evidence)), json.dumps(evidence, ensure_ascii=False), row["id"]],
                )
            conn.execute("INSERT INTO news_schema_meta VALUES('match_evidence_backfill_v1','1')")


def _find_existing(conn: sqlite3.Connection, item: dict) -> dict | None:
    aliases = _aliases(item)
    for alias in aliases:
        known = conn.execute("SELECT article_id FROM article_aliases WHERE alias=?", [alias]).fetchone()
        if known:
            aid = _resolve_id(conn, known[0])
            row = conn.execute("SELECT * FROM articles WHERE id=?", [aid]).fetchone()
            if row and (alias.startswith("id:") or news_dedup.same_event(_row_data(row), item)):
                return _row_data(row)
    direct = conn.execute("SELECT * FROM articles WHERE id=? OR identity_key=? LIMIT 1", [item["id"], item["id"]]).fetchone()
    if direct:
        return _row_data(conn.execute("SELECT * FROM articles WHERE id=?", [_resolve_id(conn, direct["id"])]).fetchone())
    stamp = item.get("published_ts")
    if stamp is None:
        return None
    rows = conn.execute("SELECT * FROM articles WHERE duplicate_of IS NULL AND published_ts BETWEEN ? AND ?",
                        [stamp - 48 * 3600, stamp + 48 * 3600])
    for row in rows:
        previous = _row_data(row)
        if news_dedup.same_event(previous, item):
            return previous
    return None


def upsert_many(items: list[dict[str, Any]]) -> int:
    if not items:
        return 0
    init_db()
    with _connect() as conn:
        extra = [str(s) for item in items for s in item.get("matched_symbols", []) if isinstance(s, str)]
        symbols = _known_symbols(conn, extra)
        count = 0
        for item in items:
            incoming = normalize_news(item)
            if not incoming:
                continue
            existing = _find_existing(conn, incoming)
            aid = existing["id"] if existing else incoming["id"]
            merged = news_dedup.merge_news(existing, incoming) if existing else incoming
            merged["fetched_at"] = max((existing or {}).get("fetched_at") or 0, incoming["fetched_at"])
            _write_article(conn, merged, aid, symbols, existing)
            _save_aliases(conn, incoming, aid)
            item["id"] = aid
            count += 1
        return count


def save_analysis(article_id: str, analysis: dict[str, Any]) -> None:
    init_db()
    rows = analysis.get("related_stocks")
    if not isinstance(rows, list):
        rows = _wrap_legacy_analysis(analysis, str(analysis.get("stock_code") or analysis.get("symbol_hint") or ""))
        if not rows and analysis.get("stock_code"):
            rows = [analysis]
    with _connect() as conn:
        aid = _resolve_id(conn, article_id)
        if not conn.execute("SELECT 1 FROM articles WHERE id=?", [aid]).fetchone():
            return
        for row in rows:
            if not isinstance(row, dict) or not row.get("stock_code"):
                continue
            status = row.get("status", "error")
            if status not in {"analyzed", "irrelevant", "unavailable", "error", "stale"}:
                status = "error"
            symbol = str(row["stock_code"]).upper()
            if aid != article_id and conn.execute("SELECT 1 FROM article_stock_analysis WHERE article_id=? AND stock_code=? AND status='analyzed'", [aid, symbol]).fetchone():
                continue
            conn.execute("""INSERT INTO article_stock_analysis(article_id,stock_code,status,relevance_score,
              impact_strength,sentiment,analysis_json,analyzed_at) VALUES(?,?,?,?,?,?,?,?)
              ON CONFLICT(article_id,stock_code) DO UPDATE SET status=excluded.status,relevance_score=excluded.relevance_score,
              impact_strength=excluded.impact_strength,sentiment=excluded.sentiment,analysis_json=excluded.analysis_json,analyzed_at=excluded.analyzed_at""",
              [aid, symbol, status, row.get("relevance_score"), row.get("impact_strength"), row.get("sentiment"), json.dumps(row, ensure_ascii=False), time.time()])


def _analysis_rows(conn: sqlite3.Connection, article_id: str, symbol: str | None = None) -> list[dict]:
    sql = "SELECT analysis_json,analyzed_at FROM article_stock_analysis WHERE article_id=?"
    args = [article_id]
    if symbol:
        sql += " AND stock_code=?"
        args.append(symbol)
    results = []
    for row in conn.execute(sql, args):
        value = _json(row[0], {})
        if value:
            results.append({**value, "analyzed_at": row[1]})
    return results


def _as_news(row: Any, analyses: list[dict] | None = None, symbol: str | None = None) -> dict:
    item = _row_data(row)
    item.pop("analysis_json", None)
    item.pop("identity_key", None)
    evidence = item["match_evidence"]
    current_hash = analysis_content_hash(analysis_input_snapshot(item))
    for entry in analyses or []:
        previous_hash = entry.get("input_content_hash")
        entry["content_changed"] = previous_hash != current_hash if previous_hash else None
    item["related_stocks"] = analyses or []
    qualified = [a for a in analyses or [] if _qualified(a, evidence.get(a.get("stock_code"), []))]
    preferred = sorted(qualified, key=lambda a: _score(a) * (a.get("impact_strength") or 0), reverse=True)
    item["analysis"] = (preferred or analyses or [None])[0]
    item["analysis_status"] = item["analysis"].get("status", "pending") if item["analysis"] else "pending"
    item["content_changed"] = item["analysis"].get("content_changed") if item["analysis"] else None
    # Attention is a review-order hint, independent of the model's actual impact
    # verdict. Compute it on read so already archived announcements benefit too.
    item["attention"] = classify_priority(item)
    if symbol:
        item["match_evidence"] = list(evidence.get(symbol, []))
        if qualified and _score(qualified[0]) >= _threshold():
            item["match_evidence"].append({"kind": "relevance", "value": f"{int(_score(qualified[0]))} 分（阈值 {_threshold()}）"})
    return item


def _stock_items(conn: sqlite3.Connection, symbol: str, *, since_ts: float | None = None) -> list[dict]:
    rows = conn.execute("""SELECT a.* FROM articles a WHERE a.duplicate_of IS NULL AND
      (EXISTS(SELECT 1 FROM json_each(CASE WHEN json_valid(a.match_evidence) THEN a.match_evidence ELSE '{}' END) e WHERE e.key=?)
       OR EXISTS(SELECT 1 FROM article_stock_analysis s WHERE s.article_id=a.id AND s.stock_code=? AND s.relevance_score>=?))""",
      [symbol, symbol, _threshold()]).fetchall()
    output = []
    for row in rows:
        if since_ts is not None and (row["published_ts"] is None or row["published_ts"] < since_ts):
            continue
        analyses = _analysis_rows(conn, row["id"], symbol)
        evidence = _json(row["match_evidence"], {}).get(symbol, [])
        result = analyses[0] if analyses else {}
        qualified = _qualified(result, evidence)
        candidate = _pending_eligible(result, evidence)
        if qualified or candidate:
            item = _as_news(row, analyses, symbol)
            item["interpretation_qualified"] = qualified
            if candidate and result.get("status") == "irrelevant":
                # Preserve the previous verdict for review without labelling it
                # a finished interpretation under the newly applicable rules.
                item["analysis_status"] = "pending"
                item["relevance_recheck_required"] = True
            output.append(item)
    return output


def list_archive(limit: int = 40, offset: int = 0, symbol: str | None = None,
                 sort: str = "latest", since_ts: float | None = None, scope: str = "related",
                 lookback_days: int | None = None) -> tuple[list[dict], int]:
    """List archive pages; an explicit history window also admits older candidates.

    Display-only stale rechecks keep their original analysis intact. Without an
    explicit window, the existing daily pending/archive behavior stays unchanged.
    """
    if lookback_days is not None and not 1 <= lookback_days <= 30:
        raise ValueError("lookback_days must be between 1 and 30")
    init_db()
    symbol = symbol.strip().upper() if symbol else None
    now = time.time()
    if lookback_days is not None:
        since_ts = max(since_ts or 0, now - lookback_days * 86400)
    with _connect() as conn:
        if symbol:
            items = _stock_items(conn, symbol, since_ts=since_ts)
        else:
            rows = conn.execute("SELECT * FROM articles WHERE duplicate_of IS NULL").fetchall()
            items = [_as_news(row, _analysis_rows(conn, row["id"])) for row in rows
                     if since_ts is None or (row["published_ts"] or 0) >= since_ts]
        if lookback_days is not None:
            # Future/undated items cannot substantiate coverage of a dated window.
            items = [item for item in items if item.get("published_ts") is not None
                     and since_ts <= item["published_ts"] <= now]
        if scope == "analysis":
            items = [item for item in items if item.get("interpretation_qualified", item["analysis_status"] == "analyzed")]
        elif scope == "pending" and lookback_days is not None:
            selected = [symbol] if symbol else watchlist_store.load_all_symbols_union()
            candidates = []
            for item in items:
                previous = {row["stock_code"]: row for row in item.get("related_stocks", [])}
                evidence = item.get("match_evidence") or ([] if symbol else {})
                eligible = [code for code in selected if _pending_eligible(
                    previous.get(code, {}), evidence if symbol else evidence.get(code, []))]
                if not eligible:
                    continue
                if any(previous.get(code, {}).get("status") == "stale" for code in eligible):
                    item["analysis_status"] = "pending"
                    item["age_recheck_required"] = True
                candidates.append(item)
            items = candidates
        elif scope == "pending":
            max_days = int(getattr(settings, "effective_news_max_age_days", getattr(settings, "news_max_age_days", 7)))
            cutoff = time.time() - max_days * 86400 if max_days > 0 else 0
            items = [item for item in items if not item.get("interpretation_qualified")
                     and item["analysis_status"] not in {"analyzed", "irrelevant", "stale"}
                     and (item.get("published_ts") is None or item["published_ts"] >= cutoff)]
        def order(item):
            chronology = (item.get("published_ts") or 0, item.get("saved_at") or 0, item["id"])
            if sort != "importance":
                return chronology
            attention = item["attention"]["rank"]
            if scope == "pending":
                return (attention, *chronology)
            analysis = item.get("analysis") or {}
            importance = _score(analysis) * (analysis.get("impact_strength") or 0) if item.get("interpretation_qualified") else 0
            return (importance, attention, *chronology)
        items.sort(key=order, reverse=True)
        start = max(0, offset)
        return items[start:start + max(1, min(limit, 200))], len(items)


def list_recent_for_symbol(symbol: str, limit: int = 20) -> list[dict]:
    return list_archive(limit=limit, symbol=symbol)[0]


def analysis_background(symbols: list[str], *, exclude_id: str, before_ts: float, limit: int = 4) -> list[dict]:
    if not symbols:
        return []
    init_db()
    with _connect() as conn:
        items = {item["id"]: item for symbol in symbols for item in _stock_items(conn, symbol, since_ts=before_ts - 7 * 86400)}
    eligible = [item for item in items.values() if item["id"] != exclude_id
                and (item.get("published_ts") or float("inf")) <= before_ts
                and (item.get("content_observed_at") or float("inf")) <= before_ts]
    return sorted(eligible, key=lambda item: item["published_ts"], reverse=True)[:max(1, min(limit, 4))]


def list_pending(symbols: list[str], limit: int = 10, *, max_age_days: int | None = None,
                 exclude_ids: set[str] | None = None) -> list[dict]:
    """Filter per-stock work, then prioritize major events and earnings by date.

    Priority cannot add candidates or bypass date, relevance, completion, error
    cooldown or deduplication gates. Limit only after ordering all eligible work.
    """
    if not symbols:
        return []
    init_db()
    max_days = (int(max_age_days) if max_age_days is not None else
                int(getattr(settings, "effective_news_max_age_days", getattr(settings, "news_max_age_days", 7))))
    if max_age_days is not None and not 1 <= max_days <= 30:
        raise ValueError("max_age_days must be between 1 and 30")
    now = time.time()
    cutoff = now - max_days * 86400 if max_days > 0 else 0
    with _connect() as conn:
        if max_age_days is not None:
            rows = conn.execute("SELECT * FROM articles WHERE duplicate_of IS NULL AND published_ts BETWEEN ? AND ? ORDER BY published_ts DESC,saved_at DESC", [cutoff, now]).fetchall()
        else:
            rows = conn.execute("SELECT * FROM articles WHERE duplicate_of IS NULL AND (published_ts IS NULL OR published_ts>=?) ORDER BY published_ts DESC,saved_at DESC", [cutoff]).fetchall()
        selected = []
        for row in rows:
            if row["id"] in (exclude_ids or set()):
                continue
            analyses = _analysis_rows(conn, row["id"])
            previous = {entry["stock_code"]: entry for entry in analyses}
            evidence = _json(row["match_evidence"], {})
            eligible = []
            for symbol in symbols:
                result = previous.get(symbol, {})
                if result.get("status") == "error" and now - result.get("analyzed_at", 0) < 1800:
                    continue
                if _pending_eligible(result, evidence.get(symbol, [])):
                    eligible.append(symbol)
            if eligible:
                item = _as_news(row, analyses)
                item["analysis_symbols"] = eligible[:8]
                selected.append(item)
        selected.sort(key=lambda item: (item["attention"]["rank"], item.get("published_ts") or 0,
                                         item.get("saved_at") or 0, item["id"]), reverse=True)
        return selected[:max(1, min(limit, 40))]


def summary_for_symbols(symbols: list[str]) -> list[dict]:
    init_db()
    cutoff = time.time() - 7 * 86400
    output = []
    with _connect() as conn:
        for symbol in symbols:
            items = _stock_items(conn, symbol)
            recent = [item for item in items if (item.get("published_ts") or 0) >= cutoff]
            interpreted = [item for item in recent if item["interpretation_qualified"]]
            counts = {mood: sum(item["analysis"].get("sentiment") == mood for item in interpreted) for mood in ("positive", "neutral", "negative")}
            analyzed = sum(counts.values())
            sentiment = "positive" if counts["positive"] > counts["negative"] else "negative" if counts["negative"] > counts["positive"] else "neutral"
            aggregate = {"status": "analyzed", "sentiment": sentiment, "news_count": analyzed, "sample_count": analyzed,
                         "period_days": 7, "method": "按近7天通过关联筛选的新闻解读方向计数汇总，不是独立模型综合分析。",
                         "reasoning": f"近7天已分析{analyzed}条：正面{counts['positive']}条、中性{counts['neutral']}条、负面{counts['negative']}条。"} if analyzed else None
            important = max(interpreted, key=lambda item: _score(item["analysis"]) * (item["analysis"].get("impact_strength") or 0), default=None)
            latest = max((item for item in items if item["interpretation_qualified"]), key=lambda item: item.get("published_ts") or 0, default=None)
            output.append({"symbol": symbol, "total": len(items), "recent_count": len(recent), "period_days": 7,
                           **counts, "analyzed": analyzed, "aggregate_analysis": aggregate,
                           "important_news": important, "latest_analysis": latest["analysis"] if latest else None})
    return output


def stats() -> dict:
    init_db()
    with _connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM articles WHERE duplicate_of IS NULL").fetchone()[0]
        pairs = conn.execute("SELECT COUNT(*) FROM article_stock_analysis s JOIN articles a ON a.id=s.article_id WHERE a.duplicate_of IS NULL AND s.status='analyzed'").fetchone()[0]
    return {"stored_articles": count, "analyzed_pairs": pairs}


def rematch_all_matched_symbols() -> int:
    """Recompute active and historical company links; preserve raw news and analyses."""
    init_db()
    news_watchlist_matcher.invalidate_match_hints_cache()
    with _connect() as conn:
        symbols = _known_symbols(conn)
        rows = conn.execute("SELECT * FROM articles WHERE duplicate_of IS NULL").fetchall()
        for row in rows:
            evidence = news_watchlist_matcher.article_match_evidence(_row_data(row), symbols)
            conn.execute("UPDATE articles SET matched_symbols=?,match_evidence=? WHERE id=?",
                         [json.dumps(sorted(evidence)), json.dumps(evidence, ensure_ascii=False), row["id"]])
    return len(rows)
