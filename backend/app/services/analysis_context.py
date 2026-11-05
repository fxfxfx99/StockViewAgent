"""Build a bounded, dated evidence bundle from actual archived news and saved profiles."""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.schemas.news import analysis_input_snapshot, clean_text, utc_iso
from app.services import event_calendar_context, public_data_context
from app.services.news_priority import classify_priority
from app.storage import news_store


def _publication_timestamp(item: dict) -> float | None:
    try:
        value = item.get("published_ts")
        if value is None and item.get("published_at"):
            date = datetime.fromisoformat(str(item["published_at"]).replace("Z", "+00:00"))
            value = date.timestamp() if date.tzinfo is not None else None
        value = float(value) if value is not None else None
        return value if value is not None and math.isfinite(value) and 0 <= value <= time.time() + 300 else None
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _feature_snapshot(symbol: str, cutoff: float | None, *, identity_only: bool = False) -> tuple[dict | None, str]:
    """Never substitute today's profile for a missing historical version."""
    if cutoff is None:
        return None, "unknown_event_time"
    try:
        from app.services import stock_features

        getter = stock_features.get_snapshot_identity if identity_only else stock_features.get_snapshot
        snapshot = getter(symbol, as_of=utc_iso(cutoff))
        if snapshot is None:
            return None, "missing"
        if not isinstance(snapshot, dict) or snapshot.get("stock_code") != symbol or not snapshot.get("version_id"):
            return None, "invalid"
        available = event_calendar_context.as_of_cutoff(snapshot.get("as_of"))
        created = event_calendar_context.as_of_cutoff(snapshot.get("created_at"))
        checked = event_calendar_context.as_of_cutoff(snapshot.get("last_checked_at"))
        if (available is None or available > cutoff or created is not None and created > cutoff
                or checked is not None and checked > cutoff):
            return None, "future_version"
        return snapshot, "available"
    except Exception:
        return None, "unavailable"


def _feature_signature(snapshot: dict | None, status: str) -> str:
    # Include content as well as the immutable version, so damaged/migrated data
    # can never silently reuse an interpretation of different company facts.
    if snapshot and snapshot.get("content_hash"):
        # Immutable store identities let archive pages validate hundreds of
        # articles without loading/parsing each 200 KiB disclosure snapshot.
        content = {key: snapshot[key] for key in ("version_id", "content_hash")}
    else:
        content = {key: value for key, value in snapshot.items() if key != "last_checked_at"} if snapshot else None
    value = {"status": status, "snapshot": content, "retrieval_version": 2}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False, default=str).encode()).hexdigest()


def feature_context_signature(item: dict, symbols: list[str]) -> dict[str, str]:
    cutoff = _publication_timestamp(item)
    return {symbol: _feature_signature(*_feature_snapshot(symbol, cutoff)) for symbol in symbols}


def analysis_features_current(item: dict, analysis: dict) -> bool:
    """Shared with the archive's reuse gate; it only reads local saved versions."""
    symbol = str(analysis.get("stock_code") or "")
    if not symbol:
        return True
    snapshot, status = _feature_snapshot(symbol, _publication_timestamp(item), identity_only=True)
    if not analysis.get("stock_feature_signature"):
        return snapshot is None  # Legacy outputs need refreshing only once facts exist.
    return analysis["stock_feature_signature"] == _feature_signature(snapshot, status)


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    return [str(part).strip() for part in value or [] if isinstance(part, (str, int)) and str(part).strip()]


def _feature_terms(link: dict) -> list[str]:
    return list(dict.fromkeys(_strings(link.get("event_keywords") or link.get("keywords"))))[:30]


def _matches_term(text: str, term: str) -> bool:
    term = term.casefold().strip()
    if len(term) < 2:
        return False
    # Latin tickers/terms must not match the middle of another word.
    return bool(re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", text)) if term.isascii() else term in text


def _factor_match(link: dict, query: str) -> tuple[list[str], list[str]]:
    terms = [term for term in _feature_terms(link) if _matches_term(query, term)]
    scopes = _strings(link.get("scope_terms"))
    matched_scopes = [term for term in scopes if (_matches_term(query, term) if term.isascii() else term.casefold() in query)]
    # A copper-cost disclosure must not become an oil-price exposure merely
    # because both stories contain "price increase". Unknown scopes stay marked.
    return (terms, matched_scopes) if not scopes or matched_scopes else ([], [])


def _feature_source_excerpt(source: dict, features: list[dict], preferred_ids: set[str]) -> str:
    """Keep actual source passages, prioritizing event-related disclosure quotes."""
    text = clean_text(source.get("text") or source.get("quote"), 100000)
    if len(text) <= 6000:
        return text
    snippets = []
    ordered = sorted(features, key=lambda feature: str(feature.get("feature_id") or feature.get("id") or feature.get("key")) not in preferred_ids)
    for feature in ordered:
        for ref in feature.get("evidence") or []:
            if not isinstance(ref, dict) or ref.get("source_id") != source.get("source_id"):
                continue
            quote = clean_text(ref.get("quote"), 1600)
            if quote and quote in text and quote not in snippets and sum(map(len, snippets)) + len(quote) < 5200:
                snippets.append(quote)
    return "\n[…]\n".join([*snippets, text[:700]])[:6000]


def _stock_feature_context(item: dict, stocks: list[dict], cutoff: float | None) -> tuple[dict, list[dict]]:
    snapshot_input = analysis_input_snapshot(item)
    query = f"{snapshot_input['title']} {snapshot_input['content_excerpt']}".casefold()
    result, evidence = {}, []
    for index, stock in enumerate(stocks, 1):
        symbol = stock["stock_code"]
        snapshot, status = _feature_snapshot(symbol, cutoff)
        entry = {"stock_code": symbol, "status": status, "version_id": None,
                 "as_of": None, "signature": _feature_signature(snapshot, status),
                 "features": [], "matched_factors": [], "coverage": {}, "limitations": []}
        result[symbol] = entry
        if snapshot is None:
            entry["limitations"] = ["事件时点没有可用的该股特征库版本；不得用当前资料倒填历史暴露。" if cutoff is not None
                                    else "事件时间未知，无法选取当时可见的个股特征库版本。"]
            continue
        entry.update(version_id=snapshot["version_id"], as_of=snapshot["as_of"], last_checked_at=snapshot.get("last_checked_at"),
                     coverage=snapshot.get("coverage") or {})
        all_links = [row for row in (snapshot.get("factor_links") or [])[:80] if isinstance(row, dict)]
        preferred_ids = {identifier for link in all_links if _factor_match(link, query)[0]
                         for identifier in _strings(link.get("feature_ids"))}
        matched_scopes = {scope for link in all_links for scope in _factor_match(link, query)[1]}
        all_features = [row for row in snapshot.get("features") or [] if isinstance(row, dict)]
        all_features.sort(key=lambda feature: (
            str(feature.get("feature_id") or feature.get("id") or feature.get("key")) not in preferred_ids,
            bool(matched_scopes) and not any(scope in str(feature.get("value") or "") for scope in matched_scopes),
        ))
        all_features = all_features[:100]
        source_ids = {}
        stock_evidence = []
        for source in (snapshot.get("evidence_sources") or [])[:16]:
            if not isinstance(source, dict) or source.get("stock_code", symbol) != symbol:
                continue
            original_id = str(source.get("source_id") or "")
            if not original_id or original_id in source_ids:
                continue
            source_times = [source.get(key) for key in ("observed_at", "available_at") if source.get(key)]
            try:
                published_at = source.get("published_at")
                # A disclosed calendar date has no intraday precision. It must
                # not override a known earlier local observation on that day.
                if isinstance(published_at, str) and len(published_at) == 10:
                    event_calendar_context.as_of_cutoff(published_at)  # Validate the date.
                    if published_at > datetime.fromtimestamp(cutoff, ZoneInfo("Asia/Shanghai")).date().isoformat():
                        continue
                elif published_at is not None:
                    source_times.append(published_at)
                if any(event_calendar_context.as_of_cutoff(stamp) > cutoff for stamp in source_times):
                    continue
            except (ValueError, TypeError, OverflowError):
                continue
            title, text = clean_text(source.get("title"), 240), _feature_source_excerpt(source, all_features, preferred_ids)
            if not title and not text:
                continue
            identifier = f"F{index}_{len(stock_evidence) + 1}"
            source_ids[original_id] = identifier
            stock_evidence.append({"source_id": identifier, "kind": "stock_feature_evidence", "stock_code": symbol,
                                   "feature_version": snapshot["version_id"], "original_source_id": original_id,
                                   "title": title, "text": text, "source": clean_text(source.get("source"), 120),
                                   "url": source.get("url") or "", "published_at": source.get("published_at"),
                                   "observed_at": source.get("observed_at"), "available_at": source.get("available_at")})
        features = {}
        for feature in all_features:
            if not isinstance(feature, dict):
                continue
            identifier = str(feature.get("feature_id") or feature.get("id") or feature.get("key") or "")
            references = [source_ids[ref] for ref in _strings(feature.get("evidence_source_ids") or feature.get("source_ids")) if ref in source_ids]
            if not identifier or not references or feature.get("stock_code", symbol) != symbol:
                continue
            # If extraction supplied an exact disclosure quote, the bounded
            # source actually sent to the model must still contain that quote.
            quotes = [ref for ref in feature.get("evidence") or [] if isinstance(ref, dict) and ref.get("quote")]
            if quotes:
                supported = {source_ids.get(ref.get("source_id")) for ref in quotes
                             if any(source["original_source_id"] == ref.get("source_id")
                                    and clean_text(ref["quote"]) in source["text"] for source in stock_evidence)}
                references = [reference for reference in references if reference in supported]
                if not references:
                    continue
            features[identifier] = {**feature, "feature_id": identifier, "source_ids": references,
                                    "evidence_source_ids": references,
                                    "evidence": [{**ref, "original_source_id": ref.get("source_id"),
                                                  "source_id": source_ids[ref["source_id"]]}
                                                 for ref in feature.get("evidence") or [] if isinstance(ref, dict)
                                                 and source_ids.get(ref.get("source_id")) in references]}
        matches = []
        for link in all_links:
            if not isinstance(link, dict):
                continue
            factor_id = str(link.get("factor_id") or link.get("id") or "")
            matched_terms, matched_scopes = _factor_match(link, query)
            linked_ids = [key for key in _strings(link.get("feature_ids")) if key in features
                          and (not matched_scopes or any(scope in str(features[key].get("value") or "") for scope in matched_scopes))][:4]
            if not factor_id or not matched_terms or not linked_ids:
                continue
            references = list(dict.fromkeys(ref for key in linked_ids for ref in features[key]["evidence_source_ids"]))
            matches.append({**link, "factor_id": factor_id, "feature_ids": linked_ids,
                            "matched_terms": matched_terms, "matched_scope_terms": matched_scopes,
                            "scope_match": "quoted_scope" if matched_scopes else "unidentified", "source_ids": references, "evidence_source_ids": references,
                            "evidence": [ref for key in linked_ids for ref in features[key]["evidence"]],
                            "match_method": "saved_factor_keyword+evidenced_company_exposure", "is_inference": True})
        matches.sort(key=lambda match: (-len(match["matched_terms"]), match["factor_id"]))
        entry["matched_factors"] = matches[:6]
        selected_features = list(dict.fromkeys(key for match in matches[:6] for key in match["feature_ids"]))
        # Unmatched facts are still useful context, but cannot be presented as a
        # detected event channel. Keep this bundle bounded for large watchlists.
        selected_features += [key for key in features if key not in selected_features]
        entry["features"] = [features[key] for key in selected_features[:24]]
        if any(match["scope_match"] == "unidentified" for match in matches[:6]):
            entry["limitations"].append("部分候选因素尚未识别具体产品、地区或币种对象，须核对事件是否作用于本公司暴露。")
        if any(feature.get("status") == "candidate" for feature in entry["features"]):
            entry["limitations"].append("库中规则提取的候选片段仍需核实主体、金额与净暴露，不能当作已验证的股价敏感度。")
        used_sources = {ref for feature in entry["features"] for ref in feature["evidence_source_ids"]}
        evidence.extend(source for source in stock_evidence if source["source_id"] in used_sources)
        if not features:
            entry["status"] = "no_evidence"
            entry["limitations"].append("该股库中尚无可追溯至当时来源的特征，不能把通用行业假设当作公司暴露。")
        elif not matches:
            entry["limitations"].append("本条事件未命中该股已存且有证据的因素；公司特定传导尚未建立，不能据此声称没有影响。")
        available = event_calendar_context.as_of_cutoff(snapshot.get("last_checked_at") or snapshot["as_of"])
        entry["age_days"] = round(max(0, cutoff - available) / 86400, 1)
        if entry["age_days"] > 8:
            entry["limitations"].append("该股可见特征版本距事件超过一周，相关暴露可能已经变化。")
    return result, evidence


def build_analysis_context(item: dict[str, Any], stocks: list[dict]) -> dict:
    """History is context, not proof of this event; never add future-dated articles."""
    now = time.time()
    published = _publication_timestamp(item)
    snapshot = analysis_input_snapshot(item)
    body = clean_text(item.get("content"))
    summary = clean_text(item.get("summary"))
    title = clean_text(item.get("title"))
    # Search RSS frequently repeats the headline as its entire description.
    # That is still title-only evidence, not additional reporting detail.
    summary_words = re.sub(r"[\W_]+", "", summary).casefold()
    title_words = re.sub(r"[\W_]+", "", title).casefold()
    if summary_words and summary_words in title_words:
        summary = ""
    kind = "full_text" if body else "summary" if summary else "title_only"
    limitations = []
    cap = 100
    if kind == "title_only":
        cap = 40
        limitations.append("仅有新闻标题，缺少正文与摘要，不能据此确认金额、业绩或事件细节。")
    elif kind == "summary":
        cap = 60 if len(summary) < 120 else 80
        limitations.append("本条仅提供摘要，尚未取得来源正文，判断需结合后续披露验证。")
    elif len(body) < 120:
        cap = 60
        limitations.append("来源提供的正文较短，可能缺少事件背景和完整条件。")
    if published is None:
        cap = min(cap, 55)
        limitations.append("发布时间无法确认，不得把该事件当作最新催化。")
    for stock in stocks:
        if not stock.get("industry") and not stock.get("main_business"):
            limitations.append(f"{stock['stock_code']} 缺少已保存的行业与主营资料，间接关联依据有限。")
    evidence = [{
        "source_id": "N0", "kind": "current_news", "article_id": item.get("id"),
        "title": snapshot["title"], "text": snapshot["content_excerpt"],
        "source": snapshot["source"], "published_at": utc_iso(published),
        "url": item.get("url") or item.get("link") or "",
    }]
    for index, stock in enumerate(stocks[:8], 1):
        # These are saved inputs, not independently verified company facts.
        profile = "\n".join(f"{key}: {stock[key]}" for key in
                            ("stock_code", "stock_name", "org_name", "industry", "main_business") if stock.get(key))
        evidence.append({
            "source_id": f"P{index}", "kind": "company_profile", "stock_code": stock["stock_code"],
            "title": f"{stock.get('stock_name') or stock['stock_code']} · 已保存公司资料",
            "text": profile[:1800], "source": "本地公司资料", "published_at": None, "url": "",
        })
    background = []
    # The cutoff is the event's publication time, so later news is never read backwards.
    cutoff = min(published, now) if published is not None else now
    try:
        history = news_store.analysis_background([s["stock_code"] for s in stocks],
                                                 exclude_id=str(item.get("id") or ""), before_ts=cutoff, limit=4)
    except Exception:
        history = []
        limitations.append("历史背景暂不可读取，本轮仅依据当前新闻和公司资料。")
    seen_urls = {item.get("url") or item.get("link") or ""}
    seen_titles = {clean_text(item.get("title")).casefold()}
    for row in history:
        url = row.get("url") or row.get("link") or ""
        title = clean_text(row.get("title"))
        if (url and url in seen_urls) or title.casefold() in seen_titles:
            continue
        seen_urls.add(url)
        seen_titles.add(title.casefold())
        source = {
            "source_id": f"H{len(background) + 1}", "kind": "background_news", "article_id": row["id"],
            "title": title, "text": clean_text(row.get("content") or row.get("summary"), 1800),
            "source": row.get("source"), "published_at": utc_iso(row.get("published_ts")), "url": url,
            "observed_at": utc_iso(row.get("content_observed_at")),
        }
        background.append(source)
        evidence.append(source)
    calendar = event_calendar_context.retrieve(
        "news", " ".join([snapshot["title"], snapshot["content_excerpt"][:1200]]),
        as_of=cutoff, symbols=[s["stock_code"] for s in stocks],
        industry=" ".join(str(s.get("industry") or "") for s in stocks),
    )
    evidence.extend(calendar.get("evidence_sources") or [])
    limitations.extend(calendar.get("limitations") or [])
    public_data = public_data_context.retrieve(snapshot["title"], as_of=cutoff, symbols=[s["stock_code"] for s in stocks])
    feature_context, feature_evidence = _stock_feature_context(item, stocks, published)
    evidence.extend(feature_evidence)
    evidence.extend(public_data["evidence_sources"])
    limitations.extend(public_data["limitations"])
    return {"version": 2, "analysis_asof": utc_iso(now),
            "attention": classify_priority(item),
            "quality": {"text_kind": kind, "publication_date_known": published is not None,
                        "limitations": limitations, "confidence_cap": cap},
            "news_background": background, "economic_calendar": calendar, "public_data": public_data,
            "stock_features": feature_context, "evidence_sources": evidence}
