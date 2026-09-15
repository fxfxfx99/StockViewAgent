"""Attribute news candidates to each stock using concrete text or provider evidence.

Search request hints and prior matches are retrieval metadata, never evidence by
themselves. The relevance/impact pipeline still assesses the admitted candidates.
"""
from __future__ import annotations

import re
import time
import unicodedata
from typing import Any

from app.services import a_share_stocks
from app.services.stock_code_utils import normalize_symbol
from app.storage import integrations_store, profile_store, watchlist_store

_hints_cache: tuple[float, tuple[list[str], dict[str, list[str]]]] | None = None
_HINTS_TTL_SEC = 45.0
_GENERIC_NAMES = {"公司", "集团", "股份", "有限公司", "股份有限公司", "有限责任公司"}


def invalidate_match_hints_cache() -> None:
    global _hints_cache
    _hints_cache = None


def _text(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or ""))


def normalize_match_keywords(value: Any) -> dict[str, list[str]]:
    """Validate the editable, per-stock keyword map; terms are literal text."""
    if not isinstance(value, dict) or len(value) > 100:
        raise ValueError("新闻关键词须按股票代码配置，最多 100 只股票。")
    output: dict[str, list[str]] = {}
    for raw_symbol, keywords in value.items():
        symbol = normalize_symbol(raw_symbol) if isinstance(raw_symbol, str) else None
        if not symbol:
            raise ValueError("新闻关键词包含无效的股票代码。")
        if not isinstance(keywords, list) or len(keywords) > 20:
            raise ValueError("每只股票最多配置 20 个关键词，须使用列表。")
        values = output.setdefault(symbol, [])
        seen = {term.casefold() for term in values}
        for raw_keyword in keywords:
            if not isinstance(raw_keyword, str):
                raise ValueError("新闻关键词必须是文本。")
            term = re.sub(r"\s+", " ", _text(raw_keyword)).strip()
            if not 2 <= len(term) <= 40 or not any(char.isalnum() for char in term):
                raise ValueError("每个新闻关键词须为 2–40 个字符，并包含文字或数字。")
            if term.casefold() not in seen:
                values.append(term)
                seen.add(term.casefold())
        if len(values) > 20:
            raise ValueError("每只股票最多配置 20 个关键词。")
    return output


def load_match_keywords() -> dict[str, list[str]]:
    """Expose only the public news keyword field from private integrations."""
    news = integrations_store.load_integrations().get("news")
    try:
        return normalize_match_keywords(news.get("match_keywords", {})) if isinstance(news, dict) else {}
    except ValueError:
        return {}


def _company_hints(symbol: str) -> list[str]:
    merged = profile_store.merge_display(profile_store.get_entry(symbol))
    names = [merged.get("name"), merged.get("org_name")]
    if not merged.get("name"):
        names.append((a_share_stocks.lookup_by_yahoo_symbol(symbol) or {}).get("name"))
    return list(dict.fromkeys(name for value in names
                if len(name := _text(value).strip()) >= 2 and name not in _GENERIC_NAMES))


def load_watchlist_match_hints() -> tuple[list[str], dict[str, list[str]]]:
    """Return watchlist symbols and complete company aliases; never truncate names."""
    global _hints_cache
    now = time.time()
    if _hints_cache and now - _hints_cache[0] < _HINTS_TTL_SEC:
        return _hints_cache[1]
    wl = list(dict.fromkeys(symbol for raw in watchlist_store.load_all_symbols_union()
                            if (symbol := normalize_symbol(raw))))
    payload = wl, {symbol: _company_hints(symbol) for symbol in wl}
    _hints_cache = now, payload
    return payload


def _provider_symbol(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    value = _text(raw).strip().upper()
    secid = re.fullmatch(r"([012])\.(\d{6})", value)
    if secid:
        market, code = secid.groups()
        # Eastmoney uses 0 for Shenzhen and Beijing. Never discard another
        # explicit market tag and then match just its six-digit suffix.
        suffix = "SS" if market == "1" else "BJ" if market == "2" or code.startswith(("4", "8", "92")) else "SZ"
        return f"{code}.{suffix}"
    hk = re.fullmatch(r"(?:116\.|HK\.?)(\d{1,5})", value)
    if hk:
        return normalize_symbol(f"{hk.group(1)}.HK")
    us = re.fullmatch(r"(?:105|106|107)\.([A-Z]{1,6}(?:[.-][A-Z])?)", value)
    return normalize_symbol(us.group(1) if us else value)


_NUMERIC_CODE = re.compile(
    r"(?<![A-Z0-9.])(?:[012]\.\d{6}|(?:SH|SS|SZ|BJ)\.?\d{6}|"
    r"\d{6}\.(?:SS|SH|SZ|BJ)|(?:116\.|HK\.?)\d{1,5}|\d{1,5}\.HK|\d{6})(?![A-Z0-9.])",
    re.I,
)


def _contains_term(blob: str, term: str) -> bool:
    # Chinese words can adjoin Chinese text; Latin/numeric terms must not
    # match inside longer words, tickers, or amounts.
    before = r"(?<![A-Z0-9])" if term[0].isascii() and term[0].isalnum() else ""
    after = r"(?![A-Z0-9])" if term[-1].isascii() and term[-1].isalnum() else ""
    return bool(re.search(before + re.escape(term) + after, blob, re.I))


def article_match_evidence(item: dict[str, Any], wl: list[str],
                           hints: dict[str, list[str]] | None = None) -> dict[str, list[dict[str, str]]]:
    """Return only supported stock associations and their inspectable evidence."""
    symbols = list(dict.fromkeys(symbol for raw in wl if (symbol := normalize_symbol(raw))))
    if hints is None:
        _, current_hints = load_watchlist_match_hints()
        # Historical callers may request removed stocks explicitly.
        hints = {symbol: current_hints[symbol] if symbol in current_hints else _company_hints(symbol)
                 for symbol in symbols}
    allowed = set(symbols)
    keywords = load_match_keywords()
    output: dict[str, list[dict[str, str]]] = {}

    def record(symbol: str, kind: str, value: str, source: Any = None) -> None:
        evidence = {"kind": kind, "value": value}
        if source:
            evidence["source"] = str(source)[:500]
        rows = output.setdefault(symbol, [])
        if evidence not in rows:
            rows.append(evidence)

    sources = [item]
    if isinstance(item.get("source_articles"), list):
        sources.extend(row for row in item["source_articles"] if isinstance(row, dict))
    source_codes = {target for article in sources[1:]
                    for code in (article.get("related_codes") if isinstance(article.get("related_codes"), list) else [])
                    if (target := _provider_symbol(code))}
    for article in sources:
        source = article.get("source")
        codes = article.get("related_codes")
        for code in codes if isinstance(codes, list) else []:
            symbol = _provider_symbol(code)
            # A merged row may contain the union of all providers' codes. Prefer
            # the original publisher attribution when that provenance is present.
            if symbol in allowed and not (article is item and symbol in source_codes):
                record(symbol, "provider", str(code).strip(), source)
        blob = _text("\n".join(str(article.get(key) or "") for key in ("title", "summary", "content")))
        # Extract whole numeric tokens once so an explicit wrong exchange cannot
        # fall back to matching the six-digit part of the same token.
        numeric = [(token.group(), _provider_symbol(token.group())) for token in _NUMERIC_CODE.finditer(blob)]
        for symbol in symbols:
            for token, target in numeric:
                if target == symbol:
                    record(symbol, "company_keyword", token, source)
            if re.fullmatch(r"[A-Z]{1,6}(?:-[A-Z])?", symbol):
                ticker = re.escape(symbol).replace(r"\-", "[.-]")
                if len(symbol) == 1:
                    # Single-letter US tickers otherwise match ordinary prose.
                    pattern = rf"(?:\$|(?:NASDAQ|NYSE|AMEX)\s*:\s*){ticker}(?![A-Z0-9.-])"
                else:
                    pattern = rf"(?<![A-Z0-9.-]){ticker}(?![A-Z0-9.-])"
                if re.search(pattern, blob, re.I):
                    record(symbol, "company_keyword", symbol, source)
            for raw_alias in hints.get(symbol) or []:
                alias = _text(raw_alias).strip()
                # Numeric aliases would bypass exchange-aware token matching.
                if len(alias) >= 2 and not alias.isdigit() and alias not in _GENERIC_NAMES and _contains_term(blob, alias):
                    record(symbol, "company_keyword", alias, source)
            for term in keywords.get(symbol, []):
                if _contains_term(blob, term):
                    record(symbol, "configured_keyword", term, source)
    return {symbol: output[symbol] for symbol in sorted(output)}


def match_article_to_watchlist(item: dict[str, Any], wl: list[str], hints: dict[str, list[str]]) -> list[str]:
    return sorted(article_match_evidence(item, wl, hints))


def enrich_items_with_matches(items: list[dict[str, Any]], wl: list[str] | None = None,
                              hints: dict[str, list[str]] | None = None) -> list[dict[str, Any]]:
    if wl is None or hints is None:
        wl, hints = load_watchlist_match_hints()
    out = []
    for item in items:
        evidence = article_match_evidence(item, wl, hints)
        out.append({**item, "matched_symbols": sorted(evidence), "match_evidence": evidence})
    return out
