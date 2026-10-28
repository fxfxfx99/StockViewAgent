"""Resolve source identities locally; caller-provided facts cannot replace archives."""
from __future__ import annotations

import copy
from datetime import datetime
import json
import time

from app.schemas.tracking import EvidenceRef, SHANGHAI


def news_evidence(row) -> dict:
    from app.schemas.news import utc_iso

    original = json.loads(row["snapshot_json"])
    snapshot = {key: str(original.get(key) or "")[:limit] for key, limit in
                (("title", 500), ("url", 2000), ("source", 120), ("summary", 1000), ("content", 4000))}
    snapshot.update(published_ts=original.get("published_ts"), content_hash=row["content_hash"],
                    snapshot_scope="archived_content_excerpt")
    return {"kind": "news", "id": row["article_id"], "revision_id": str(row["id"]),
            "title": snapshot["title"], "url": snapshot["url"],
            "published_at": utc_iso(row["published_ts"]), "observed_at": utc_iso(row["first_observed_at"]),
            "excerpt": (snapshot["content"] or snapshot["summary"])[:4000], "snapshot": snapshot}


def calendar_schedule_label(event) -> str:
    """Render precise times in Shanghai; date-only sources retain their date."""
    if event.get("all_day"):
        return (f"{event['scheduled_date']}（来源日期，全天／准确时间未定；"
                f"{event.get('timezone') or '来源时区未提供'}）")
    scheduled = datetime.fromisoformat(event["scheduled_at"].replace("Z", "+00:00"))
    return scheduled.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M") + "（北京时间）"


def calendar_evidence(row) -> dict:
    from app.storage import event_calendar_store

    snapshot = json.loads(row["payload"])
    excerpt = event_calendar_store.document_text(snapshot).replace(
        f"日程 {snapshot['scheduled_at']}", f"日程 {calendar_schedule_label(snapshot)}", 1)
    return {"kind": "calendar", "id": row["event_id"], "revision_id": row["revision_id"],
            "title": snapshot["title"], "url": snapshot["source_url"],
            "published_at": None, "observed_at": event_calendar_store.iso(row["observed_at"]),
            "excerpt": excerpt[:4000], "snapshot": snapshot}


def freeze_evidence_refs(user_id: int, refs: list[dict], *, existing_refs=()) -> list[dict]:
    """Public source revisions are shared; personal strategy snapshots are labelled."""
    result, seen = [], set()
    existing = {(ref["kind"], ref["id"], ref.get("revision_id")): ref for ref in existing_refs}
    for raw in refs:
        ref = EvidenceRef.model_validate(raw).model_dump(exclude_none=True)
        kind, identity = ref["kind"], ref["id"]
        if kind == "news":
            from app.storage import news_store

            news_store.init_db()
            with news_store._connect() as conn:
                revision = ref.get("revision_id")
                clause = " AND CAST(id AS TEXT)=?" if revision else ""
                args = [identity, revision] if revision else [identity]
                row = conn.execute("SELECT * FROM article_content_revisions WHERE article_id=?" + clause +
                                   " ORDER BY id DESC LIMIT 1", args).fetchone()
            if row is None:
                raise ValueError("引用的新闻版本不存在于本地归档")
            frozen = news_evidence(row)
        elif kind == "calendar":
            from app.storage import event_calendar_store

            with event_calendar_store.connection() as conn:
                revision = ref.get("revision_id")
                clause = " AND revision_id=?" if revision else ""
                args = [identity, revision] if revision else [identity]
                row = conn.execute("SELECT * FROM event_revisions WHERE event_id=?" + clause +
                                   " ORDER BY seq DESC LIMIT 1", args).fetchone()
            if row is None:
                raise ValueError("引用的日历版本不存在于本地归档")
            frozen = calendar_evidence(row)
        else:
            snapshot = copy.deepcopy(ref.get("snapshot") or {})
            snapshot["evidence_role"] = "user_saved_opinion_not_independent_source"
            snapshot["independent_source"] = False
            old = existing.get((kind, identity, ref.get("revision_id")))
            unchanged = old is not None and ("snapshot" not in ref or snapshot == old.get("snapshot"))
            unchanged = unchanged and all(ref[key] == old.get(key) for key in
                                          ("title", "url", "published_at", "excerpt") if key in ref)
            # Reattaching an unchanged saved opinion cannot make it look new.
            frozen = copy.deepcopy(old) if unchanged else {**ref, "snapshot": snapshot, "observed_at": time.time()}
        key = (frozen["kind"], frozen["id"], frozen.get("revision_id"))
        if key not in seen:
            result.append(frozen)
            seen.add(key)
    return result


def validate_signal(user_id: int, symbol: str, signal_id: str | None) -> None:
    if signal_id is None:
        return
    from app.storage import decision_signal_store

    decision_signal_store.init_db()
    conn = decision_signal_store._conn()
    try:
        row = conn.execute("SELECT symbol FROM decision_signal WHERE id=? AND user_id=?",
                           (signal_id, int(user_id))).fetchone()
    finally:
        conn.close()
    if row is None or row["symbol"] != symbol:
        raise ValueError("关联信号须属于当前账户及同一标的")
