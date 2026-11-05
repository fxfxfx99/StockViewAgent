"""Stock-scoped retrieval knowledge, independent of prompts and economic facts.

Only company aliases and explicitly configured per-stock terms admit candidates.
Industry labels never fan a story out to every company in an industry. Business
scopes remain conditional context tied to immutable, dated feature evidence.
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections import OrderedDict
from pathlib import Path
from threading import RLock

from app.config import settings
from app.services import a_share_stocks
from app.services.stock_code_utils import normalize_symbol
from app.storage import knowledge_cache_store as store, profile_store

SCHEMA_VERSION = 1
LOGIC_VERSION = "stock-news-relevance-v3"
_GENERIC = {"公司", "集团", "股份", "有限公司", "股份有限公司", "有限责任公司"}
_lock = RLock()
_dependencies: dict[tuple[str, str], tuple] = {}
_documents: OrderedDict[tuple[str, str], tuple[str, dict]] = OrderedDict()


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False, default=str).encode()).hexdigest()


def _symbol(value: str) -> str:
    symbol = normalize_symbol(value)
    if not symbol:
        raise ValueError("无效股票代码")
    return symbol


def _stamp(path: Path) -> tuple:
    try:
        stat = path.stat()
        return stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size
    except FileNotFoundError:
        return ()


def _validate_source_json(path: Path) -> dict:
    """Do not turn a damaged optional source into deletion of known knowledge."""
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"本地知识来源 {path.name} 无法读取；保留已有知识版本。") from exc
    if not isinstance(value, dict):
        raise ValueError(f"本地知识来源 {path.name} 格式无效；保留已有知识版本。")
    return value


def _values(value) -> list[str]:
    return [str(item) for item in value or [] if isinstance(item, (str, int))] if isinstance(value, list) else []


def _remember(key: tuple[str, str], revision_id: str, content: dict) -> None:
    _documents[key] = revision_id, content
    _documents.move_to_end(key)
    while len(_documents) > 256:
        _documents.popitem(last=False)


def _cached_document(symbol: str, head: dict) -> dict:
    key = str(settings.data_dir.resolve()), symbol
    cached = _documents.get(key)
    if cached and cached[0] == head["revision_id"]:
        _documents.move_to_end(key)
        return {"metadata": head, "content": copy.deepcopy(cached[1])}
    document = store.get_document("news_relevance", symbol)
    if document is None or not isinstance(document.get("content"), dict):
        raise ValueError("新闻相关性知识库缺失或损坏")
    if document["content"].get("stock_code") != symbol or document["content"].get("schema_version") != SCHEMA_VERSION:
        raise ValueError("新闻相关性知识库标的或结构不匹配")
    _remember(key, document["metadata"]["revision_id"], document["content"])
    return {"metadata": document["metadata"], "content": copy.deepcopy(document["content"])}


def ensure_stock_knowledge(symbol: str, *, force=False, actor_id=None, identity_only=False) -> dict:
    from app.services import news_watchlist_matcher, stock_features

    symbol = _symbol(symbol)
    key = str(settings.data_dir.resolve()), symbol
    with _lock:
        identity = stock_features.get_snapshot_identity(symbol)
        dependencies = (_stamp(settings.data_dir / "watchlist_profiles.json"),
                        _stamp(settings.data_dir / "integrations.json"),
                        _stamp(settings.data_dir / "a_share_stocks.json"), _hash(identity))
        head = store.get_head("news_relevance", symbol)
        if not force and head and _dependencies.get(key) == dependencies:
            return {"metadata": head} if identity_only else _cached_document(symbol, head)
        _validate_source_json(settings.data_dir / "watchlist_profiles.json")
        integrations = _validate_source_json(settings.data_dir / "integrations.json")
        news = integrations.get("news")
        if news is not None and not isinstance(news, dict):
            raise ValueError("新闻关键词来源格式无效；保留已有知识版本。")
        news_watchlist_matcher.normalize_match_keywords((news or {}).get("match_keywords", {}))
        raw = profile_store.get_entry(symbol)
        profile = profile_store.merge_display(raw)
        aliases = []
        for field in ("name", "org_name"):
            term = str(profile.get(field) or "").strip()
            if len(term) >= 2 and term not in _GENERIC and not term.isdigit():
                aliases.append({"term": term, "status": "identity", "source": "manual_profile" if
                                (raw.get("manual") or {}).get(field) else "cached_company_profile", "field": field})
        if not profile.get("name"):
            fallback = str((a_share_stocks.lookup_by_yahoo_symbol(symbol) or {}).get("name") or "").strip()
            if len(fallback) >= 2 and fallback not in _GENERIC:
                aliases.append({"term": fallback, "status": "identity", "source": "a_share_stock_catalog", "field": "name"})
        configured = [{"term": term, "status": "candidate", "source": "user_keyword_configuration",
                       "note": "用户检索关键词，尚非公司暴露或新闻相关性的证明"}
                      for term in news_watchlist_matcher.load_match_keywords().get(symbol, [])]
        snapshot = stock_features.get_snapshot(symbol) if identity else None
        if snapshot is not None:
            # The source can advance between its lightweight identity and body reads.
            identity = snapshot
        sources = {str(row.get("source_id")): row for row in (snapshot or {}).get("evidence_sources", [])
                   if isinstance(row, dict) and row.get("source_id") and row.get("stock_code", symbol) == symbol}
        features = {str(row.get("feature_id") or row.get("id") or ""): row for row in (snapshot or {}).get("features", [])
                    if isinstance(row, dict) and row.get("stock_code", symbol) == symbol}
        scopes = []
        for link in (snapshot or {}).get("factor_links", [])[:80]:
            if not isinstance(link, dict):
                continue
            feature_ids = [fid for fid in _values(link.get("feature_ids")) if fid in features]
            for term in _values(link.get("scope_terms"))[:30]:
                if not 1 <= len(term) <= 100:
                    continue
                supporting = [fid for fid in feature_ids if term in str(features[fid].get("value") or "")]
                refs = list(dict.fromkeys(ref for fid in supporting for ref in
                            _values(features[fid].get("evidence_source_ids") or features[fid].get("source_ids")) if ref in sources))
                if not supporting or not refs:
                    continue
                scopes.append({"term": term, "status": "candidate", "relation_status": "conditional",
                               "factor_id": str(link.get("factor_id") or link.get("id") or ""),
                               "event_keywords": _values(link.get("event_keywords"))[:30],
                               "feature_ids": supporting[:8], "feature_version": snapshot["version_id"],
                               "feature_statuses": {fid: features[fid].get("status", "candidate") for fid in supporting[:8]},
                               "source_ids": refs[:8], "available_at": snapshot.get("created_at") or snapshot.get("as_of"),
                               "source": "stock_feature_evidence",
                               "quotes": [str(features[fid].get("value") or "")[:1200] for fid in supporting[:3]]})
        content = {"schema_version": SCHEMA_VERSION, "stock_code": symbol,
                   "aliases": aliases, "configured_keywords": configured, "business_scopes": scopes,
                   "profile_context": {field: {"value": str(profile.get(field) or "")[:1600], "status": "candidate",
                                              "source": "saved_company_profile"} for field in ("industry", "main_business")},
                   "feature_identity": {k: identity.get(k) for k in ("version_id", "content_hash")} if identity else None,
                   "sources": [{"source_id": ref, "source": str(sources[ref].get("source") or "")[:200],
                                "title": str(sources[ref].get("title") or "")[:300], "url": sources[ref].get("url") or "",
                                "published_at": sources[ref].get("published_at"), "observed_at": sources[ref].get("observed_at")}
                               for ref in dict.fromkeys(ref for scope in scopes for ref in scope["source_ids"])],
                   "rules": {"industry_alone_admits_candidate": False, "business_scope_admits_candidate": False,
                             "configured_keywords_are_facts": False, "historical_features_require_asof_version": True}}
        metadata = store.put_document("news_relevance", symbol, content, reason="sources_synchronized",
                                      actor_id=actor_id, provenance={"builder_version": SCHEMA_VERSION,
                                      "source": "local_profiles+explicit_keywords+stock_feature_evidence"})
        _dependencies[key] = dependencies
        _remember(key, metadata["revision_id"], content)
        return {"metadata": metadata, "content": copy.deepcopy(content)}


def get_stock_knowledge(symbol: str, *, revision_id=None) -> dict | None:
    return store.get_document("news_relevance", _symbol(symbol), revision_id=revision_id) if revision_id else ensure_stock_knowledge(symbol)


def snapshot_knowledge(symbols: list[str]) -> dict[str, dict]:
    return {symbol: ensure_stock_knowledge(symbol) for symbol in symbols}


def context_from_snapshot(documents: dict, feature_context: dict) -> dict:
    """Current aliases are retrieval metadata; economics must match event-time facts."""
    result = {}
    for symbol, document in documents.items():
        content, metadata = document["content"], document["metadata"]
        available = feature_context.get(symbol, {})
        version = available.get("version_id")
        feature_ids = {row.get("feature_id") or row.get("id") for row in available.get("features", [])}
        scopes = [scope for scope in content["business_scopes"] if version and scope["feature_version"] == version
                  and set(scope["feature_ids"]).intersection(feature_ids)]
        result[symbol] = {"revision_id": metadata["revision_id"], "content_hash": metadata["content_hash"],
                          "aliases": content["aliases"], "configured_keywords": content["configured_keywords"],
                          "business_scopes": scopes, "rules": content["rules"],
                          "note": "别名和用户关键词仅供主体检索；业务scope为候选推断。历史事件仅可使用stock_features中当时可见的证据。"}
    return result


def analysis_signature(prompt_versions: dict, knowledge: dict, *, threshold: int | None = None) -> str:
    if threshold is None:
        threshold = int(getattr(settings, "effective_news_relevance_threshold", getattr(settings, "news_relevance_threshold", 60)))
    threshold = max(0, min(threshold, 100))
    metadata = knowledge.get("metadata", knowledge)
    return _hash({"logic_version": LOGIC_VERSION, "prompt_versions": prompt_versions,
                  "knowledge": {"revision_id": metadata.get("revision_id"), "content_hash": metadata.get("content_hash")},
                  "relevance_threshold": threshold})


def current_analysis_signature(symbol: str) -> str:
    from app.services import prompt_registry
    return analysis_signature(prompt_registry.current_versions(), ensure_stock_knowledge(symbol, identity_only=True))


def analysis_logic_current(item: dict, analysis: dict) -> bool:
    symbol = str(analysis.get("stock_code") or "")
    if not symbol:
        return True
    try:
        return bool(analysis.get("analysis_logic_signature")) and analysis["analysis_logic_signature"] == current_analysis_signature(symbol)
    except (OSError, ValueError):
        return False
