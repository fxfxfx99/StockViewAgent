"""Bounded local evidence scanning; never fetch, call a model or infer a verdict.

Source sequence numbers, not publication timestamps, drive progress. Each batch
freezes its high watermark, and changes plus cursor commit in one transaction.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import sqlite3
import time

from app.config import settings
from app.schemas.news import analysis_content_hash, analysis_input_snapshot
from app.security.user_context import current_user_id
from app.services import news_watchlist_matcher
from app.services.market_time import SHANGHAI_TZ
from app.services.news_priority import classify_priority
from app.services.tracking_evidence import calendar_evidence, calendar_schedule_label, news_evidence
from app.storage import event_calendar_store, news_store, research_store, tracking_store

BATCH_SIZE = 500
OVERVIEW_DAYS = 7
STALE_SECONDS = 36 * 3600


def _iso(value):
    return datetime.fromtimestamp(float(value), timezone.utc).isoformat() if value is not None else None


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]


def _watchlist(user_id):
    # Reading an empty/missing list must not seed another account's defaults.
    path = settings.data_dir / "watchlists" / f"{int(user_id)}.json"
    try:
        payload = json.loads(path.read_text()) if path.exists() else {"symbols": []}
        rows = payload.get("symbols", [])
        if not isinstance(rows, list):
            raise ValueError("invalid watchlist")
        return list(dict.fromkeys(str(s).strip().upper().replace(".SH", ".SS") for s in rows if str(s).strip())), None
    except (OSError, ValueError, TypeError, AttributeError):
        return [], "自选列表暂时无法读取，已保留现有判断"


def _scope(user_id):
    theses = tracking_store.list_theses(user_id, status="active", limit=10000)
    symbols, issue = _watchlist(user_id)
    symbols = sorted(set(symbols) | {t["symbol"] for t in theses})
    return theses, symbols, issue


def _news_ref(row):
    return news_evidence(row)


def _matched_symbols(conn, row, symbols):
    if not symbols:
        return []
    snapshots = [json.loads(row["snapshot_json"])]
    previous = conn.execute("SELECT snapshot_json FROM article_content_revisions WHERE article_id=? AND id<? ORDER BY id DESC LIMIT 1",
                            (row["article_id"], row["id"])).fetchone()
    if previous:
        # A correction may remove the company name or the relationship itself.
        # The immediately preceding version still warrants notifying its reader.
        snapshots.append(json.loads(previous[0]))
    analyses = news_store._analysis_rows(conn, row["article_id"])
    matched = set()
    for snapshot in snapshots:
        matched.update(news_watchlist_matcher.article_match_evidence(snapshot, symbols))
        expected = analysis_content_hash(analysis_input_snapshot(snapshot))
        for analysis in analyses:
            symbol = analysis.get("stock_code")
            if symbol in symbols and analysis.get("input_content_hash") == expected and news_store._qualified(analysis, []):
                matched.add(symbol)
    return sorted(matched)


def _news_change(conn, row, symbol, theses, *, overview=False):
    snapshot = json.loads(row["snapshot_json"])
    previous = conn.execute("SELECT * FROM article_content_revisions WHERE article_id=? AND id<? ORDER BY id DESC LIMIT 1",
                            (row["article_id"], row["id"])).fetchone()
    before = json.loads(previous["snapshot_json"]) if previous else None
    # Added publisher attribution must not be mistaken for a body correction.
    body_fields = ("title", "summary", "content")
    supplement = before is not None and all(before.get(k) == snapshot.get(k) for k in body_fields)
    change_type = "overview" if overview else "source_supplement" if supplement else "news_correction" if before else "news_new"
    priority = 30 if overview else 10 if supplement else 80 if before else 60 + 5 * classify_priority(snapshot)["rank"]
    evidence = _news_ref(row)
    analyses = news_store._analysis_rows(conn, row["article_id"], symbol)
    analysis = analyses[0] if analyses else None
    if analysis:
        expected = analysis_content_hash(analysis_input_snapshot(snapshot))
        analysis = {**analysis, "content_changed": analysis.get("input_content_hash") != expected,
                    "usage": "既有解读，须核对对应正文版本"}
    linked = [t["id"] for t in theses if t["symbol"] == symbol]
    prefix = "近期概览 · " if overview else "来源补充 · " if supplement else "内容更正 · " if before else ""
    return {"id": f"news:{row['id']}:{symbol}", "kind": "news", "change_type": change_type,
            "overview": overview, "priority": priority, "symbol": symbol, "thesis_ids": linked,
            "title": prefix + snapshot.get("title", "新闻资料"),
            "summary": str(snapshot.get("summary") or snapshot.get("content") or "查看已归档原文")[:1200],
            "evidence_refs": [evidence], "evidence_ref": evidence,
            "previous_evidence_ref": _news_ref(previous) if previous else None,
            "published_at": evidence["published_at"], "observed_at": evidence["observed_at"],
            "analysis": analysis, "assessment": "unconfirmed", "assessment_label": "待核实", "collapsed": supplement}


def _scan_news(user_id, symbols, theses, now, limit):
    news_store.init_db()
    state = tracking_store.get_scan_state(user_id, "news")
    cursor = state["cursor"]
    known = {symbol: value for symbol, value in state.get("symbol_baselines", {}).items() if symbol in symbols}
    new_symbols = sorted(set(symbols) - set(known))
    with news_store._connect() as conn:
        conn.execute("BEGIN")
        maximum = conn.execute("SELECT COALESCE(MAX(id),0) FROM article_content_revisions").fetchone()[0]
        high = state.get("high_watermark", 0) if state.get("high_watermark", 0) > cursor else maximum
        changes = []
        overview_truncated = False
        if new_symbols:
            # A bounded recent overview establishes the baseline for added stocks.
            recent = conn.execute("""SELECT r.* FROM article_content_revisions r JOIN articles a ON a.id=r.article_id
                WHERE a.duplicate_of IS NULL AND r.id<=? AND (r.first_observed_at>=? OR r.published_ts>=?)
                AND r.id=(SELECT MAX(r2.id) FROM article_content_revisions r2 WHERE r2.article_id=r.article_id AND r2.id<=?)
                ORDER BY r.id DESC LIMIT ?""", (maximum, now - OVERVIEW_DAYS * 86400,
                now - OVERVIEW_DAYS * 86400, maximum, limit + 1)).fetchall()
            overview_truncated = len(recent) > limit
            for row in recent[:limit]:
                evidence = _matched_symbols(conn, row, new_symbols)
                changes.extend(_news_change(conn, row, symbol, theses, overview=True) for symbol in evidence)
            known.update({symbol: maximum for symbol in new_symbols})
        if not state.get("initialized"):
            rows, next_cursor = [], maximum
            high = maximum
        else:
            rows = conn.execute("""SELECT r.*,a.duplicate_of FROM article_content_revisions r
                JOIN articles a ON a.id=r.article_id WHERE r.id>? AND r.id<=? ORDER BY r.id LIMIT ?""",
                (cursor, high, limit)).fetchall()
            next_cursor = rows[-1]["id"] if rows else high
            for row in rows:
                if row["duplicate_of"]:
                    continue
                eligible = [symbol for symbol in symbols if row["id"] > known.get(symbol, 0)]
                evidence = _matched_symbols(conn, row, eligible)
                changes.extend(_news_change(conn, row, symbol, theses) for symbol in evidence)
        cutoff = conn.execute("SELECT MAX(first_observed_at) FROM article_content_revisions WHERE id<=?", (next_cursor,)).fetchone()[0]
        ingested = conn.execute("SELECT MAX(fetched_at) FROM articles").fetchone()[0]
    metadata = {"initialized": True, "symbol_baselines": known, "high_watermark": high,
                "last_scanned_at": now, "cutoff_at": _iso(cutoff), "last_ingested_at": _iso(ingested),
                "pending": next_cursor < maximum, "overview_truncated": overview_truncated,
                "last_error": None}
    return tracking_store.commit_scan(user_id, "news", cursor, next_cursor, changes, metadata,
                                      expected_version=state.get("version", 0))


def _calendar_links(theses):
    links = {}
    for thesis in theses:
        for ref in thesis.get("evidence_refs", []):
            if ref.get("kind") == "calendar" and ref.get("id"):
                links.setdefault(ref["id"], {})[thesis["id"]] = thesis
    return links


def _calendar_ref(row):
    return calendar_evidence(row)


def _calendar_change(conn, row, linked, *, overview=False):
    event = json.loads(row["payload"])
    prior = conn.execute("SELECT * FROM event_revisions WHERE event_id=? AND seq<? ORDER BY seq DESC LIMIT 1",
                         (row["event_id"], row["seq"])).fetchone()
    released = event.get("actual") is not None and event.get("status") == "released"
    description = (f"公布值：{event['actual']} {event.get('unit') or ''}" if released else
                   f"日程：{calendar_schedule_label(event)}；尚未取得公布值，不能据此判断结果")
    if event.get("status") == "cancelled":
        description = "日程已取消；不能据此判断经营或市场结果"
    evidence = _calendar_ref(row)
    return {"id": f"calendar:{row['seq']}:{_digest(sorted(linked))}", "kind": "calendar",
            "change_type": "overview" if overview else "calendar_changed" if released else "calendar_schedule",
            "overview": overview, "priority": 30 if overview else 70,
            "title": ("近期概览 · " if overview else "关联事件更新 · ") + event["title"], "summary": description,
            "symbol": next(iter(linked.values()))["symbol"] if len({t["symbol"] for t in linked.values()}) == 1 else None,
            "thesis_ids": sorted(linked), "evidence_refs": [evidence], "evidence_ref": evidence,
            "previous_evidence_ref": _calendar_ref(prior) if prior else None,
            "published_at": None, "observed_at": evidence["observed_at"],
            "assessment": "unconfirmed", "assessment_label": "待核实"}


def _scan_calendar(user_id, theses, now, limit):
    state = tracking_store.get_scan_state(user_id, "calendar")
    cursor = state["cursor"]
    links = _calendar_links(theses)
    current_links = {f"{eid}:{tid}" for eid, linked in links.items() for tid in linked}
    known = {key: value for key, value in state.get("link_baselines", {}).items() if key in current_links}
    new_links = {eid: {tid: t for tid, t in linked.items() if f"{eid}:{tid}" not in known}
                 for eid, linked in links.items()}
    with event_calendar_store.connection() as conn:
        conn.execute("BEGIN")
        maximum = conn.execute("SELECT COALESCE(MAX(seq),0) FROM event_revisions").fetchone()[0]
        high = state.get("high_watermark", 0) if state.get("high_watermark", 0) > cursor else maximum
        changes = []
        remaining_links = [(eid, linked) for eid, linked in new_links.items() if linked]
        for eid, linked in remaining_links[:limit]:
            if not linked:
                continue
            row = conn.execute("SELECT * FROM event_revisions WHERE event_id=? AND seq<=? ORDER BY seq DESC LIMIT 1", (eid, maximum)).fetchone()
            if row:
                changes.append(_calendar_change(conn, row, linked, overview=True))
            known.update({f"{eid}:{tid}": maximum for tid in linked})
        if not state.get("initialized"):
            rows, next_cursor, high = [], maximum, maximum
        else:
            rows = conn.execute("SELECT * FROM event_revisions WHERE seq>? AND seq<=? ORDER BY seq LIMIT ?", (cursor, high, limit)).fetchall()
            next_cursor = rows[-1]["seq"] if rows else high
            for row in rows:
                linked = {tid: thesis for tid, thesis in links.get(row["event_id"], {}).items()
                          if row["seq"] > known.get(f"{row['event_id']}:{tid}", 0)}
                if linked:
                    changes.append(_calendar_change(conn, row, linked))
        cutoff = conn.execute("SELECT MAX(observed_at) FROM event_revisions WHERE seq<=?", (next_cursor,)).fetchone()[0]
        ingested = conn.execute("SELECT MAX(last_seen_at) FROM event_heads").fetchone()[0]
    metadata = {"initialized": True, "link_baselines": known, "high_watermark": high,
                "last_scanned_at": now, "cutoff_at": _iso(cutoff), "last_ingested_at": _iso(ingested),
                "pending": next_cursor < maximum or len(remaining_links) > limit, "last_error": None}
    return tracking_store.commit_scan(user_id, "calendar", cursor, next_cursor, changes, metadata,
                                      expected_version=state.get("version", 0))


def _due_reviews(user_id, theses, now):
    today = datetime.fromtimestamp(now, SHANGHAI_TZ).date().isoformat()
    changes = [{"id": f"review:{t['id']}:{t['revision']}:{t['review_at']}", "kind": "review_due", "priority": 100,
                "symbol": t["symbol"], "thesis_ids": [t["id"]], "thesis_revision": t["revision"],
                "review_at": t["review_at"], "title": "关注判断到期复盘", "summary": t["reason"],
                "evidence_refs": [], "published_at": None, "observed_at": _iso(now)}
               for t in theses if t["review_at"] <= today]
    return tracking_store.put_changes(user_id, changes)


def _news_collection_health(user_id):
    db = research_store.get_store()
    with db.connection() as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runtime_events'").fetchone():
            return None
        row = conn.execute("""SELECT status,finished_at,started_at,error_code FROM runtime_events
            WHERE user_id IN (0,?) AND kind='collection' AND operation='news_archive'
            ORDER BY started_at DESC LIMIT 1""", (user_id,)).fetchone()
        return dict(row) if row else None


def status(user_id, *, now=None):
    now = time.time() if now is None else float(now)
    theses, symbols, watchlist_issue = _scope(user_id)
    config = research_store.get_config(user_id)
    heartbeat = research_store.get_state(0, "worker_heartbeat", {}) or {}
    healthy = heartbeat.get("status") in {"idle", "busy"} and now - heartbeat.get("seen_at", 0) < 150
    result = {"automatic_enabled": bool(settings.enable_research_worker and config["enabled"]),
              "paused": config["paused"], "symbols": symbols,
              "worker": {"status": heartbeat.get("status") if healthy else "offline", "healthy": healthy,
                         "seen_at": heartbeat.get("seen_at")}, "coverage": {"status": "current", "issues": []}}
    for name in ("news", "calendar"):
        saved = tracking_store.get_scan_state(user_id, name)
        public = {k: saved.get(k) for k in ("cursor", "high_watermark", "cutoff_at", "last_ingested_at", "last_scanned_at", "pending", "overview_truncated")}
        stamp = public.get("last_ingested_at")
        stale = not stamp or now - datetime.fromisoformat(stamp).timestamp() > STALE_SECONDS
        public["status"] = "failed" if saved.get("last_error") else "pending" if public["pending"] else "stale" if stale else "current"
        public["message"] = "本地扫描失败，保留上次进度" if saved.get("last_error") else "仍有已归档修订待扫描" if public["pending"] else "资料未更新，覆盖不足" if stale else "仅覆盖当前已入库资料"
        result[name] = public
    try:
        collection = _news_collection_health(user_id)
    except (OSError, sqlite3.Error, ValueError):
        collection = {"status": "failed", "error_code": "local_health_unavailable"}
    result["news"]["collection"] = collection
    if collection and collection["status"] in {"failed", "cancelled"}:
        result["news"].update(status="failed", message="新闻来源最近采集失败，保留已入库资料")
    elif not collection and result["news"]["status"] not in {"failed", "pending"}:
        result["news"].update(status="limited", message="尚无新闻来源成功采集记录，覆盖不足")
    elif collection and result["news"]["status"] not in {"failed", "pending"} and now - (collection.get("finished_at") or collection.get("started_at") or 0) > STALE_SECONDS:
        result["news"].update(status="stale", message="新闻来源采集记录已过期，覆盖不足")
    relevant_sources = {r.get("snapshot", {}).get("source_id") for t in theses for r in t.get("evidence_refs", []) if r.get("kind") == "calendar"}
    try:
        states = event_calendar_store.source_states()
    except (OSError, sqlite3.Error, ValueError):
        states = {}
        result["calendar"].update(status="failed", message="日历来源状态暂时无法读取，保留上次资料")
    result["calendar"]["sources"] = [{"id": sid, **states.get(sid, {"status": "never"})} for sid in sorted(relevant_sources - {None})]
    if any(row["status"] == "failed" for row in result["calendar"]["sources"]):
        result["calendar"].update(status="failed", message="关联日历来源最近更新失败")
    elif result["calendar"]["status"] not in {"failed", "pending"}:
        for row in result["calendar"]["sources"]:
            success = row.get("last_success_at")
            try:
                outdated = not success or now - event_calendar_store.timestamp(success) > STALE_SECONDS
            except (ValueError, TypeError):
                outdated = True
            if outdated:
                result["calendar"].update(status="stale", message="关联日历来源尚未更新，覆盖不足")
                break
        if not relevant_sources:
            result["calendar"].update(status="not_linked", message="尚未主动关联日历事件；宏观日程不会自动进入判断")
    for source in ("news", "calendar"):
        if (source == "news" and symbols or source == "calendar" and relevant_sources) and result[source]["status"] != "current":
            result["coverage"]["issues"].append({"source": source, "code": result[source]["status"], "message": result[source]["message"]})
    if watchlist_issue:
        result["coverage"]["issues"].append({"source": "watchlist", "code": "unreadable", "message": watchlist_issue})
    if any(result[s].get("overview_truncated") for s in ("news", "calendar")):
        result["coverage"]["issues"].append({"source": "news", "code": "overview_bounded", "message": "近期概览仅取最近一批归档资料"})
    if result["coverage"]["issues"]:
        result["coverage"]["status"] = "limited"
    times = [result[s]["last_scanned_at"] for s in ("news", "calendar") if result[s]["last_scanned_at"]]
    result["last_scanned_at"] = min(times) if len(times) == 2 else None
    return result


def _record_health(user_id, overview, now):
    state = tracking_store.get_scan_state(user_id, "health")
    issues = overview["coverage"]["issues"]
    fingerprint = _digest([{k: issue[k] for k in ("source", "code")} for issue in issues])
    changes = []
    if fingerprint != state.get("fingerprint") and issues:
        changes.append({"id": f"health:{state.get('version', 0) + 1}:{fingerprint}", "kind": "data_issue", "priority": 90,
                        "title": "资料覆盖需要留意", "summary": "；".join(issue["message"] for issue in issues),
                        "symbol": None, "thesis_ids": [], "evidence_refs": [], "observed_at": _iso(now)})
    return tracking_store.commit_scan(user_id, "health", state["cursor"], state["cursor"], changes,
                                      {"fingerprint": fingerprint}, expected_version=state.get("version", 0))


def refresh(user_id, *, now=None, limit=BATCH_SIZE):
    """One bounded local pass, usable even with worker or automation disabled."""
    now = time.time() if now is None else float(now)
    limit = max(1, min(int(limit), BATCH_SIZE))
    token = current_user_id.set(int(user_id))
    inserted, errors = 0, []
    try:
        theses, symbols, _ = _scope(user_id)
        for source, scan in (("news", lambda: _scan_news(user_id, symbols, theses, now, limit)),
                             ("calendar", lambda: _scan_calendar(user_id, theses, now, limit))):
            try:
                inserted += scan()["inserted"]
            except tracking_store.ScanConflict:
                errors.append(source)
            except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
                errors.append(source)
                old = tracking_store.get_scan_state(user_id, source)
                try:
                    tracking_store.commit_scan(user_id, source, old["cursor"], old["cursor"], [],
                        {**old, "last_error": "local_scan_failed"}, expected_version=old.get("version", 0))
                except tracking_store.ScanConflict:
                    pass
        due = _due_reviews(user_id, theses, now)
        inserted += due["inserted"] if isinstance(due, dict) else int(due)
        overview = status(user_id, now=now)
        try:
            inserted += _record_health(user_id, overview, now)["inserted"]
        except tracking_store.ScanConflict:
            pass
        return {**overview, "status": "partial" if errors or any(overview[s]["pending"] for s in ("news", "calendar")) else "completed",
                "inserted": inserted, "errors": errors}
    finally:
        current_user_id.reset(token)
