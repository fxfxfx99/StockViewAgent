"""Small, credential-free datasets from their original public publishers.

Adapters only fetch and normalise; storage, revisions, retries and retention are
owned by the data ingestion service. A statistical period is never substituted
for publication time. Current downloaded historical values are current vintages,
not evidence that those values were known in that historical period.
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import json
import math
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx


SOURCE_CATALOG = (
    {"id": "ecb_fx", "name": "欧洲央行参考汇率", "category": "fx", "url": "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml", "official": True, "update_interval_seconds": 21600, "retention_days": 730, "description": "最近约 90 天的官方欧元参考汇率；每欧元兑换外币数量，不是实时可成交报价。"},
    {"id": "wb_gdp_growth", "name": "世界银行实际 GDP 增长率", "category": "macro", "url": "https://api.worldbank.org/v2/country/CN;US;JP;DE;GB;IN/indicator/NY.GDP.MKTP.KD.ZG", "official": True, "update_interval_seconds": 86400, "retention_days": 1825, "description": "六个经济体近十年年度实际 GDP 增长率；当前数据版本，历史值可能修订。"},
    {"id": "wb_inflation", "name": "世界银行消费价格通胀", "category": "macro", "url": "https://api.worldbank.org/v2/country/CN;US;JP;DE;GB;IN/indicator/FP.CPI.TOTL.ZG", "official": True, "update_interval_seconds": 86400, "retention_days": 1825, "description": "六个经济体近十年年度消费价格通胀率；不是月度 CPI 或市场预期。"},
    {"id": "wb_unemployment", "name": "世界银行失业率（ILO 模型估计）", "category": "macro", "url": "https://api.worldbank.org/v2/country/CN;US;JP;DE;GB;IN/indicator/SL.UEM.TOTL.ZS", "official": True, "update_interval_seconds": 86400, "retention_days": 1825, "description": "六个经济体近十年年度失业率，ILO 模型估计占劳动力比例；不等同各国月度调查口径。"},
    {"id": "fed_policy", "name": "美联储货币政策公告", "category": "policy", "url": "https://www.federalreserve.gov/feeds/press_monetary.xml", "official": True, "update_interval_seconds": 3600, "retention_days": 730, "description": "官方 RSS 中的政策声明、会议纪要与贴现率公告标题、摘要、发布时间和原文链接；不自动抓取原文全文。"},
    {"id": "ecb_publications", "name": "欧洲央行公告与讲话", "category": "policy", "url": "https://www.ecb.europa.eu/rss/press.html", "official": True, "update_interval_seconds": 3600, "retention_days": 730, "description": "官方 RSS 中的新闻稿、讲话和访谈；保存所提供的标题、摘要、发布时间和原文链接。"},
)
_SOURCES = {source["id"]: source for source in SOURCE_CATALOG}
_WB_INDICATORS = {"wb_gdp_growth": "NY.GDP.MKTP.KD.ZG", "wb_inflation": "FP.CPI.TOTL.ZG", "wb_unemployment": "SL.UEM.TOTL.ZS"}
_COUNTRIES = {"CN", "US", "JP", "DE", "GB", "IN"}
MAX_RESPONSE_BYTES = 4_000_000
MAX_RECORDS = 10_000
REQUEST_TIMEOUT_SECONDS = 15.0
MIN_HOST_INTERVAL_SECONDS = 1.0
_next_request: dict[str, float] = {}
_rate_lock = threading.Lock()


class PublicDataSourceError(ValueError):
    """Sanitised failure suitable for the public ingestion status page."""

    def __init__(self, message: str, *, error_code: str = "invalid_payload"):
        super().__init__(message)
        self.error_code = error_code


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _plain(value: Any, limit: int = 12000) -> str:
    parser = _PlainText()
    parser.feed(str(value or "")[:MAX_RESPONSE_BYTES])
    return re.sub(r"\s+", " ", html.unescape(" ".join(parser.parts))).strip()[:limit]


def _utc(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise PublicDataSourceError("采集时间必须包含时区", error_code="invalid_context")
    if not 1900 <= now.year <= 2100:
        raise PublicDataSourceError("采集时间超出支持范围", error_code="invalid_context")
    return now.astimezone(timezone.utc)


def _xml(document: str) -> ET.Element:
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", document, re.I):
        raise PublicDataSourceError("不支持含外部定义的 XML")
    try:
        return ET.fromstring(document.lstrip("\ufeff\r\n "))
    except (ET.ParseError, ValueError):
        raise PublicDataSourceError("上游 XML 格式无效") from None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _record(source_id: str, upstream_id: str, title: str, now: datetime, *, source_url: str = "",
            period: str | None = None, published_at: str | None = None, region: str = "",
            symbols: list[str] | None = None, value: float | None = None, unit: str | None = None,
            text: str = "", metadata: dict | None = None) -> dict[str, Any]:
    source = _SOURCES[source_id]
    return {"source_id": source_id, "source": source["name"], "source_url": source_url or source["url"],
            "upstream_id": upstream_id, "category": source["category"], "title": title,
            "observed_at": now.isoformat(), "published_at": published_at, "period": period,
            "region": region, "symbols": symbols or [], "value": value, "unit": unit,
            "text": text, "metadata": metadata or {}}


def parse_ecb_fx(document: str, *, now: datetime | None = None) -> list[dict[str, Any]]:
    instant = _utc(now)
    root = _xml(document)
    if _local_name(root.tag) != "Envelope":
        raise PublicDataSourceError("上游不是欧洲央行参考汇率文档")
    rows = []
    for node in root.iter():
        if _local_name(node.tag) != "Cube" or "time" not in node.attrib:
            continue
        try:
            day = date.fromisoformat(node.attrib["time"])
        except ValueError:
            continue
        if day > instant.date():
            continue
        for quote in node:
            currency = quote.attrib.get("currency", "")
            rate = _number(quote.attrib.get("rate"))
            if _local_name(quote.tag) != "Cube" or not re.fullmatch(r"[A-Z]{3}", currency) or rate is None or rate <= 0:
                continue
            rows.append(_record("ecb_fx", f"EUR:{currency}:{day}", f"ECB 参考汇率 EUR/{currency}", instant,
                                period=day.isoformat(), region="EU", symbols=["EUR", currency], value=rate,
                                unit=f"{currency}/EUR", text=f"{day} 欧洲央行参考汇率：1 EUR = {rate:g} {currency}。官方参考值，不是实时成交报价。",
                                metadata={"frequency": "daily", "base_currency": "EUR", "quote_currency": currency,
                                          "value_status": "observed", "publication_time_known": False}))
    return rows


def _source_url(source_id: str, now: datetime) -> str:
    base = _SOURCES[source_id]["url"]
    if source_id in _WB_INDICATORS:
        return base + "?" + urlencode({"format": "json", "per_page": 1000, "date": f"{now.year - 9}:{now.year}"})
    return base


def parse_worldbank(document: str, source_id: str, *, now: datetime | None = None) -> list[dict[str, Any]]:
    instant = _utc(now)
    if source_id not in _WB_INDICATORS:
        raise PublicDataSourceError("未知世界银行指标", error_code="unknown_source")
    try:
        payload = json.loads(document)
    except (ValueError, TypeError):
        raise PublicDataSourceError("上游 JSON 格式无效") from None
    if not isinstance(payload, list) or len(payload) != 2 or not isinstance(payload[0], dict) or not isinstance(payload[1], list):
        raise PublicDataSourceError("世界银行响应结构无效")
    metadata, observations = payload
    if metadata.get("pages") != 1 or metadata.get("page") != 1 or metadata.get("total") != len(observations):
        raise PublicDataSourceError("世界银行响应分页不完整")
    indicator = _WB_INDICATORS[source_id]
    rows = []
    for observation in observations:
        if not isinstance(observation, dict):
            continue
        country, series = observation.get("country"), observation.get("indicator")
        if not isinstance(country, dict) or not isinstance(series, dict):
            continue
        region, period = country.get("id"), str(observation.get("date") or "")
        if region not in _COUNTRIES or series.get("id") != indicator or not re.fullmatch(r"\d{4}", period):
            continue
        if not instant.year - 9 <= int(period) <= instant.year:
            continue
        raw_value = observation.get("value")
        value = _number(raw_value)
        status = "missing" if raw_value is None else "observed" if value is not None else "invalid_numeric"
        title = f"{_plain(country.get('value'), 100)} · {_SOURCES[source_id]['name']}"
        text = f"{period} 年 {title}：{f'{value:g}%' if value is not None else '上游未提供有效数值'}。统计期不代表公布时间；本条为当前下载的数据版本。"
        rows.append(_record(source_id, f"{indicator}:{region}:{period}", title, instant,
                            source_url=_source_url(source_id, instant), period=period, region=region,
                            value=value, unit="%", text=text,
                            metadata={"frequency": "annual", "indicator": indicator, "indicator_name": _plain(series.get("value"), 300),
                                      "country_iso3": _plain(observation.get("countryiso3code"), 3),
                                      "dataset_updated_date": _plain(metadata.get("lastupdated"), 32) or None,
                                      "value_status": status, "publication_time_known": False,
                                      "methodology": "ILO modelled estimate" if source_id == "wb_unemployment" else None}))
    return rows


def _safe_link(source_id: str, value: str) -> str:
    base = _SOURCES[source_id]["url"]
    try:
        parsed = urlparse(value.strip())
        allowed = parsed.scheme == "https" and parsed.hostname == urlparse(base).hostname and not parsed.username and not parsed.password and parsed.port in {None, 443}
    except ValueError:
        allowed = False
    return value.strip() if allowed else base


def parse_policy_rss(document: str, source_id: str, *, now: datetime | None = None) -> list[dict[str, Any]]:
    instant = _utc(now)
    if source_id not in {"fed_policy", "ecb_publications"}:
        raise PublicDataSourceError("未知政策来源", error_code="unknown_source")
    root = _xml(document)
    if _local_name(root.tag) != "rss":
        raise PublicDataSourceError("上游不是 RSS 公告源")
    rows = []
    for item in root.iter("item"):
        title = _plain(item.findtext("title"), 1000)
        if not title:
            continue
        published_at = None
        raw_date = item.findtext("pubDate")
        if raw_date:
            try:
                moment = parsedate_to_datetime(raw_date)
                if moment.tzinfo is not None and moment.utcoffset() is not None:
                    moment = moment.astimezone(timezone.utc)
                    if moment > instant:
                        continue
                    published_at = moment.isoformat()
            except (ValueError, TypeError, OverflowError):
                pass
        link = _safe_link(source_id, item.findtext("link") or "")
        identity = link if link != _SOURCES[source_id]["url"] else f"{title}|{published_at or ''}"
        upstream_id = hashlib.sha256(identity.encode()).hexdigest()
        description = _plain(item.findtext("description"))
        rows.append(_record(source_id, upstream_id, title, instant, source_url=link,
                            published_at=published_at, region="US" if source_id == "fed_policy" else "EU",
                            text=title + ("\n" + description if description and description != title else ""),
                            metadata={"content_scope": "rss_title_and_summary", "publication_time_known": published_at is not None,
                                      "publisher_category": _plain(item.findtext("category"), 300) or None}))
    return rows


async def _wait_for_host(host: str) -> None:
    # A thread lock makes reservations safe even for callers with different event
    # loops; the actual wait is asynchronous and never holds the lock.
    with _rate_lock:
        now = time.monotonic()
        due = max(now, _next_request.get(host, now))
        _next_request[host] = due + MIN_HOST_INTERVAL_SECONDS
    if due > now:
        await asyncio.sleep(due - now)


async def fetch_source(source_id: str, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Fetch one fixed publisher endpoint without credentials or local writes.

    ``upstream_id`` is stable across refreshes. ``observed_at`` is acquisition
    metadata, not content; consumers must exclude it from semantic dedup hashes.
    The caller persists failed attempts separately and retains prior good data.
    """
    if source_id not in _SOURCES:
        raise PublicDataSourceError("未知公共数据来源", error_code="unknown_source")
    instant = _utc(now)
    url = _source_url(source_id, instant)
    await _wait_for_host(urlparse(url).hostname or "")
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=False,
                                     headers={"User-Agent": "StockViewAgent/1.0 (public economic data collection)",
                                              "Accept": "application/json, application/xml, text/xml, application/rss+xml"}) as client:
            async with client.stream("GET", url) as response:
                if response.status_code != 200:
                    raise PublicDataSourceError("公共数据来源暂不可用", error_code=f"http_{response.status_code}")
                length = response.headers.get("content-length")
                if length and length.isdigit() and int(length) > MAX_RESPONSE_BYTES:
                    raise PublicDataSourceError("公共数据响应过大", error_code="response_too_large")
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise PublicDataSourceError("公共数据响应过大", error_code="response_too_large")
                    chunks.append(chunk)
                document = b"".join(chunks).decode("utf-8-sig", errors="strict")
    except httpx.TimeoutException:
        raise PublicDataSourceError("公共数据请求超时", error_code="timeout") from None
    except httpx.HTTPError:
        raise PublicDataSourceError("公共数据网络请求失败", error_code="network_error") from None
    except UnicodeDecodeError:
        raise PublicDataSourceError("公共数据文本编码无效") from None
    if source_id == "ecb_fx":
        rows = parse_ecb_fx(document, now=instant)
    elif source_id in _WB_INDICATORS:
        rows = parse_worldbank(document, source_id, now=instant)
    else:
        rows = parse_policy_rss(document, source_id, now=instant)
    # Same upstream key with conflicting values is unsafe to silently overwrite.
    unique = {}
    for row in rows:
        key = row["upstream_id"]
        if key in unique and row != unique[key]:
            raise PublicDataSourceError("上游包含相互冲突的重复记录")
        unique[key] = row
    if not unique:
        raise PublicDataSourceError("来源未返回可验证的数据记录", error_code="empty_dataset")
    if len(unique) > MAX_RECORDS:
        raise PublicDataSourceError("来源记录数量超出上限", error_code="too_many_records")
    return list(unique.values())
