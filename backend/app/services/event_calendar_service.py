"""Official calendar monitoring and dated retrieval for actual agent evidence."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
import fcntl
import logging
import re
import time

from app.config import settings
from app.security.user_context import current_user_id
from app.services import event_calendar_sources as sources
from app.storage import event_calendar_store as store

logger = logging.getLogger(__name__)
REFRESH_SECONDS = 3600


async def _durable_call(function, *args, **kwargs):
    # Cancellation cannot kill a SQLite writer thread. Join it before releasing
    # the refresh lease so another process cannot overtake its publication.
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


def source_status():
    states = store.source_states()
    now = time.time()
    result = []
    for source in sources.SOURCE_CATALOG:
        state = states.get(source["id"], {})
        success = state.get("last_success_at")
        result.append({**source, "status": "never", "event_count": 0, "error": None,
                       **state, "stale": success is None or now - store.timestamp(success) > REFRESH_SECONDS * 2})
    return result


def _range(start=None, end=None):
    today = datetime.now(store.SHANGHAI).date()
    first = date.fromisoformat(start) if start else today - timedelta(days=14)
    last = date.fromisoformat(end) if end else today + timedelta(days=30)
    if first > last or (last - first).days > 365:
        raise ValueError("日历起止日期须有序且不超过366天")
    return first.isoformat(), last.isoformat()


def calendar(*, start=None, end=None, region=None, min_importance=2, category=None, q=""):
    first, last = _range(start, end)
    rows = store.visible_events()
    query = q.strip().casefold()
    matches = store.matching_chunks(query) if query else {}
    rows = [row for row in rows if first <= row["calendar_date"] <= last
            and (not region or row["region"] == region) and row["importance"] >= min_importance
            and (not category or row["category"] == category)
            and (not query or query in (row["title"] + row["description"]).casefold() or row["revision_id"] in matches)]
    rows.sort(key=lambda row: (row["calendar_date"], row["scheduled_at"], -row["importance"], row["id"]))
    status = source_status()
    return {"items": rows, "total": len(rows), "sources": status, "timezone": "Asia/Shanghai",
            "start": first, "end": last, "updated_at": max((s.get("last_success_at") or "" for s in status), default="") or None}


async def refresh(*, scheduled=False):
    """Cross-process lease plus persisted cooldown; failure never deletes evidence."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with (settings.data_dir / "economic_events.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "in_progress", "results": source_status(), "updated_at": None}
        try:
            states = store.source_states()
            now = time.time()

            async def collect(source):
                sid = source["id"]
                previous = states.get(sid, {})
                attempt = previous.get("last_attempt_at")
                wait = REFRESH_SECONDS if scheduled else 60
                if attempt and now - store.timestamp(attempt) < wait:
                    return {"id": sid, **previous, "skipped": True}
                try:
                    rows = await sources.fetch_source(sid, now=datetime.fromtimestamp(now, timezone.utc))
                    if not rows:
                        raise ValueError("source_empty")
                    if any(row.get("source_id") != sid for row in rows):
                        raise ValueError("source_identity_mismatch")
                    counts = await _durable_call(store.ingest_events, rows, observed_at=time.time())
                    state = {"status": "success", "last_attempt_at": store.iso(now),
                             "last_success_at": store.iso(time.time()), "event_count": counts["received"],
                             "revisions_added": counts["added"], "error": None, "error_code": None}
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # Only a bounded diagnostic class crosses the UI boundary.
                    logger.warning("事件日历来源 %s 更新失败: %s", sid, type(exc).__name__)
                    code = getattr(exc, "error_code", "source_unavailable")
                    if not isinstance(code, str) or not (re.fullmatch(r"http_[45]\d{2}", code) or code in {"http_error", "timeout", "network_error", "response_too_large", "invalid_calendar", "unknown_source"}):
                        code = "source_unavailable"
                    state = {**previous, "status": "failed", "last_attempt_at": store.iso(now), "error_code": code,
                             "error": f"来源暂不可用或格式不符（{code}），保留上次记录"}
                    counts = {"received": 0, "added": 0}
                await _durable_call(store.set_source_state, sid, state)
                from app.services.runtime_telemetry import record_collection
                record_collection("economic_calendar", sid, records_received=counts["received"],
                                  records_added=counts["added"], status="success" if state["status"] == "success" else "failed",
                                  error_code=state["error_code"], user_id=0)
                return {"id": sid, **state}

            tasks = [asyncio.create_task(collect(source)) for source in sources.SOURCE_CATALOG]
            try:
                results = await asyncio.gather(*tasks)
            except BaseException:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
            return {"status": "completed", "results": results, "updated_at": store.iso(time.time())}
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


async def scheduler_loop():
    await asyncio.sleep(10)
    while True:
        try:
            await refresh(scheduled=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("事件日历监控本轮失败，稍后重试")
        await asyncio.sleep(60)


def build_agent_context(agent, query="", *, as_of=None, user_id=None, symbols=None, industry=None):
    """Retrieve persisted chunks after PIT filtering, never fetch during inference.

    Structured date/region/importance filters and Chinese/English FTS5/BM25
    ranking are used together. Major events are retained as calendar context
    even when a company name alone does not match a macro release's title.
    """
    if agent not in store.DEFAULT_CONFIG["agents"]:
        raise ValueError("未知事件解读 Agent")
    uid = current_user_id.get() if user_id is None else user_id
    config = store.get_config(uid)
    cutoff = time.time() if as_of is None else min(store.timestamp(as_of), time.time())
    enabled = config["enabled"] and config["agents"][agent]
    text_query = " ".join(str(value) for value in [query, industry or "", " ".join(symbols or [])] if value).strip()[:2000]
    result = {"enabled": enabled, "items": [], "evidence_sources": [], "limitations": [],
              "retrieval": {"method": "sqlite_fts5_bm25+structured_event_filters", "agent": agent,
                            "query": text_query, "as_of": store.iso(cutoff), "top_k": config["top_k"],
                            "filters": {k: config[k] for k in ("regions", "categories", "min_importance", "lookback_days", "lookahead_days")},
                            "historical_policy": "locally_observed_revision_at_or_before_cutoff"}}
    if not enabled:
        return result
    day = datetime.fromtimestamp(cutoff, store.SHANGHAI).date()
    start, end = day - timedelta(days=config["lookback_days"]), day + timedelta(days=config["lookahead_days"])
    rows = [row for row in store.visible_events(as_of=cutoff)
            if start.isoformat() <= row["calendar_date"] <= end.isoformat()
            and row["importance"] >= config["min_importance"] and row["status"] != "cancelled"
            and (not config["regions"] or row["region"] in config["regions"])
            and (not config["categories"] or row["category"] in config["categories"])]
    hits = store.matching_chunks(text_query, as_of=cutoff)
    # Category terminology is indexed with each chunk, without adding invented
    # event facts. Relevance is retrieval rank, never a bullish/bearish signal.
    rows.sort(key=lambda row: (0 if row["revision_id"] in hits else 1,
                              hits.get(row["revision_id"], {}).get("rank", 0), -row["importance"],
                              abs(store.timestamp(row["scheduled_at"]) - cutoff), row["id"]))
    for row in rows[:config["top_k"]]:
        hit = hits.get(row["revision_id"])
        source_id = f"E{len(result['items']) + 1}"
        snippet = store.document_text({**row, "description": (hit["snippet"] if hit else row["description"])[:900]})
        item = {**row, "source_id": source_id, "provider_id": row["source_id"], "snippet": snippet,
                "match_reason": "全文检索命中" if hit else "监控窗口内的重要宏观日程",
                "citation": f"event:{row['id']}:{row['revision_id']}"}
        result["items"].append(item)
        result["evidence_sources"].append({"source_id": source_id, "kind": "economic_event", "title": row["title"],
            "text": snippet, "source": row["source"], "url": row["source_url"], "published_at": None,
            "observed_at": row["observed_at"], "event_id": row["id"], "revision_id": row["revision_id"],
            "scheduled_at": row["scheduled_at"], "citation": item["citation"]})
    if not rows:
        result["limitations"].append("当前时点与筛选条件下没有已入库事件证据；不代表没有重大事件。")
    else:
        result["limitations"].append("日程表示已知安排，不等于已公布结果；未提供的实际值、预期与前值不可推测。事件相关性不代表个股影响方向。")
    if as_of is not None:
        result["limitations"].append("历史检索仅反映本地当时已观察的证据，不重建当时的来源健康状态。")
    else:
        stale = [s["name"] for s in source_status() if s["stale"] or s["status"] == "failed"]
        if stale:
            result["limitations"].append("部分来源尚未同步或需要更新：" + "、".join(stale))
    return result
