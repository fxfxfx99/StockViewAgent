"""Bounded local adapters for explicit monitoring channels; never fetch or call AI."""
from __future__ import annotations

import json

from app.config import settings
from app.security.user_context import current_user_id


def _forum(plan, now):
    from app.services import monitoring_engine as engine
    from app.storage import xueqiu_comments_store
    uid = current_user_id.get()
    if not uid or not (settings.data_dir / "xueqiu_comments.db").exists():
        return []
    state, _ = xueqiu_comments_store.read(uid, plan["target_code"])
    rows = (state.get("result") or {}).get("items") or []
    result = []
    for row in rows[:8]:
        if not isinstance(row, dict) or not row.get("text"):
            continue
        original = {key: row.get(key) for key in ("id", "title", "text", "url", "created_at", "author")}
        result.append({**engine._fact("forum:" + engine._hash(original)[:24], row.get("title") or "雪球讨论观点",
                        str(row["text"]), url=row.get("url"), source="雪球讨论 · 未核实观点",
                        published=row.get("created_at"), observed=state.get("updated_at"), now=now),
                       "evidence_role": "opinion", "factual_support": False})
    return result


def _strategy(plan, now):
    from app.services import monitoring_engine as engine
    from app.storage import decision_signal_store
    uid = current_user_id.get()
    if not uid or not (settings.data_dir / "decision_signals.db").exists():
        return []
    result = []
    for row in decision_signal_store.list_signals(uid, symbol=plan["target_code"], limit=5):
        original = {key: row.get(key) for key in ("id", "action", "reason", "watch_conditions", "risks", "status", "created_at", "updated_at")}
        result.append({**engine._fact("strategy:" + row["id"] + ":" + engine._hash(original)[:16],
                        "已保存策略观点 · " + str(row.get("status") or ""), engine._json(original),
                        source="当前账户策略记录 · 观点", observed=row.get("updated_at"), published=row.get("created_at"), now=now),
                       "evidence_role": "opinion", "factual_support": False})
    return result


def _announcements(plan, now):
    from app.services import monitoring_engine as engine
    from app.storage import company_updates_store, earnings_store
    symbol = plan["target_code"]
    result = []
    if (settings.data_dir / "earnings.sqlite3").exists():
        for row in earnings_store.list_reports(symbol)[:8]:
            # The catalog proves a disclosure exists; it is not the PDF content.
            original = {key: row.get(key) for key in ("title", "report_period", "report_type", "document_kind", "announcement_date")}
            result.append({**engine._fact("announcement:" + row["id"], row["title"],
                            "公告目录记录，尚未把财报正文作为本条证据：" + engine._json(original),
                            source=row.get("source") or "财报公告", url=row.get("announcement_url"),
                            observed=row.get("first_seen_at"), published=row.get("published_at"), now=now),
                           "evidence_role": "announcement_metadata", "factual_support": True})
    record = company_updates_store.load(symbol)
    bundle = record.get("bundle") or {}
    observed = (bundle.get("section_fetched_at") or {}).get("major_events")
    for row in (bundle.get("major_events") or [])[:8]:
        if not isinstance(row, dict) or not row.get("title"):
            continue
        published = row.get("created_at")
        if isinstance(published, (float, int)) and published > 1e11:
            published /= 1000
        original = {key: row.get(key) for key in ("title", "message", "text", "text_excerpt", "url", "created_at")}
        result.append({**engine._fact("announcement:cached:" + engine._hash(original)[:24], row["title"],
                        str(row.get("message") or row.get("text") or row.get("text_excerpt") or row["title"]),
                        source=(bundle.get("sources") or {}).get("major_events") or "本地公司公告缓存", url=row.get("url"),
                        observed=observed, published=published, now=now),
                       "evidence_role": "announcement_excerpt", "factual_support": True})
    return result[:12]


def _indicators(plan, now):
    from app.services import monitoring_engine as engine
    from app.services.kline_pipeline import _validate_index_bundle
    from app.services.mytt_compute import compute_series
    from app.storage import kline_bundle_cache
    symbol = "INDEX_" + plan["target_code"] if plan["target_type"] == "index" else plan["target_code"]
    entries = []
    for window in ("1y", "6mo", "3mo", "2y", "5y"):
        entry = kline_bundle_cache.load_bundle(symbol, window, "1d")
        try:
            if entry:
                _validate_index_bundle(entry["payload"])
                entries.append(entry)
        except (ValueError, TypeError, KeyError):
            continue
    if not entries:
        return []
    entry = max(entries, key=lambda item: item["payload"]["candles"][-1]["t"])
    bars = entry["payload"]["candles"][-250:]
    if len(bars) < 20:
        return []
    series, _ = compute_series(bars, ["ma5", "ma20", "macd", "rsi"])
    values = {name: {curve: points[-1] for curve, points in curves.items() if points and points[-1] is not None}
              for name, curves in series.items()}
    values = {name: value for name, value in values.items() if value}
    if not values:
        return []
    original = {"as_of": bars[-1]["t"], "close": bars[-1]["c"], "indicators": values, "bars_used": len(bars),
                "method": "MyTT MA5/MA20, MACD(12,26,9), RSI24; local-indicators-v1",
                "input_hash": engine._hash(bars)}
    fact = engine._fact("indicator:" + symbol + ":" + engine._hash(original)[:24], symbol + "本地趋势指标",
                        engine._json(original), source="本地日K计算 · MyTT", observed=entry.get("saved_at"),
                        published=bars[-1]["t"], max_age=14 * 86400, now=now)
    # Use the same cache snapshot as the calculation. A later refresh must not
    # make older indicators appear current.
    stamp = float(bars[-1]["t"])
    observed = engine._timestamp(entry.get("saved_at"))
    if stamp > now and engine.market_date(stamp) == engine.market_date(now) and observed and observed <= now:
        fact["stale"] = False
    return [{**fact, "evidence_role": "derived_indicator", "factual_support": True}]


def collect_channel(channel, plan, now):
    adapters = {"forum": _forum, "strategy": _strategy, "announcements": _announcements, "indicators": _indicators}
    supported = plan.get("target_type") == "stock" or channel == "indicators" and plan.get("target_type") == "index"
    if not supported:
        return []
    return adapters[channel](plan, now)
