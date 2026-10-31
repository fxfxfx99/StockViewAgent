"""Bounded public financial-report discovery and verified PDF downloads.

Primary: CNINFO's public disclosure site and its official static PDF host.
Fallback: Eastmoney's public announcement list and attachment-detail endpoints.
These are website interfaces, not contractual vendor APIs. No credentials,
captcha workarounds, arbitrary URL retrieval, scraping proxies or model calls.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime
import hashlib
import html
import json
import re
import threading
import time
import unicodedata
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
import weakref

import httpx

from app.services.market_time import SHANGHAI_TZ

MAX_PDF_BYTES = 30 * 1024 * 1024
MAX_METADATA_BYTES = 3 * 1024 * 1024
MAX_PAGES = 6
PAGE_SIZE = 100
REQUEST_INTERVAL_SECONDS = 1.0
CNINFO_STOCKS = "https://www.cninfo.com.cn/new/data/szse_stock.json"
CNINFO_QUERY = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
EASTMONEY_LIST = "https://np-anotice-stock.eastmoney.com/api/security/ann"
EASTMONEY_DETAIL = "https://np-cnotice-stock.eastmoney.com/api/content/ann"
_PERIOD_END = {"annual": "12-31", "half_year": "06-30", "q1": "03-31", "q3": "09-30"}
_ORG_CACHE = None
_ORG_LOCKS = weakref.WeakKeyDictionary()
_HOST_NEXT = {}
_RATE_LOCK = threading.Lock()
_MESSAGES = {
    "invalid_symbol": "仅支持与交易所匹配的六位A股代码.SS/SZ/BJ",
    "invalid_range": "报告年份、类型或公告日期范围无效",
    "unsafe_url": "来源链接不在财报官方域名和路径白名单内",
    "invalid_catalog_item": "报告目录信息无效或与来源记录不符",
    "source_unavailable": "公开财报来源暂不可用",
    "access_denied": "公开来源要求授权或拒绝访问，已停止该来源请求",
    "rate_limited": "公开来源限流，请稍后重试",
    "timeout": "公开财报来源请求超时",
    "invalid_response": "公开财报来源返回格式无效",
    "not_found": "公开来源未找到对应报告或原文",
    "too_large": "原文文件超过允许下载的大小上限",
    "invalid_pdf": "来源未返回有效的PDF原文",
    "redirect_limit": "来源重定向次数超过限制",
}


class SourceError(ValueError):
    def __init__(self, code):
        self.code = code if code in _MESSAGES else "source_unavailable"
        super().__init__(_MESSAGES[self.code])


def _symbol(value):
    if not isinstance(value, str):
        raise SourceError("invalid_symbol")
    value = value.strip().upper().replace(".SH", ".SS")
    if not re.fullmatch(r"\d{6}\.(SS|SZ|BJ)", value):
        raise SourceError("invalid_symbol")
    code, market = value.split(".")
    if not ((market == "SS" and code.startswith("6")) or (market == "SZ" and code.startswith(("0", "3")))
            or (market == "BJ" and code.startswith(("4", "8", "92")))):
        raise SourceError("invalid_symbol")
    return value


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise SourceError("invalid_range")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise SourceError("invalid_range") from None


def _today():
    return datetime.now(SHANGHAI_TZ).date()


def _title(value):
    if not isinstance(value, str) or len(value) > 3000:
        return ""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", html.unescape(re.sub(r"<[^>]*>", "", value)))).strip()


def classify_report(title):
    """Recognize a report period without treating related board letters as reports."""
    title = _title(title)
    match = re.search(r"((?:19|20)\d{2})年?(半年度|半年|年度|第一季度|一季度|第三季度|三季度)(报告|业绩预告|业绩快报)", title)
    if not match or any(word in title for word in ("子公司", "问询函", "监管函", "审核意见", "审议", "无法", "延期", "董事会工作", "监事会工作")):
        return None
    year = int(match[1])
    period = {"年度": "annual", "半年度": "half_year", "半年": "half_year", "第一季度": "q1", "一季度": "q1", "第三季度": "q3", "三季度": "q3"}[match[2]]
    suffix = title[match.end():]
    is_revision = any(word in title for word in ("修订", "修正", "更正", "补充", "更新"))
    if ("关于" in title and is_revision) or (is_revision and "公告" in suffix):
        kind = "correction"
    elif match[3] == "业绩预告":
        kind = "forecast"
    elif match[3] == "业绩快报":
        kind = "express"
    else:
        if "关于" in title or "公告" in suffix or "意见" in suffix:
            return None
        kind = "summary" if "摘要" in suffix else "full_report"
        # Parentheses are formatting, not a license to accept audit opinions or
        # other documents that merely mention a report in their title.
        residual = re.sub(r"[()]|全文|正文|摘要|中文版|中文|英文版|英文|修订稿|修订版|修订后|修订|修正后|修正|更正后|更正|更新后|更新|补充版", "", suffix)
        if residual:
            return None
    language = "en" if any(word in title.lower() for word in ("英文", "english")) else "zh"
    return {"report_year": year, "report_type": period, "report_period": f"{year}-{_PERIOD_END[period]}",
            "document_kind": kind, "is_revision": is_revision, "language": language,
            "is_interpretable": kind == "full_report" and language == "zh"}


def _safe_url(url, *, pdf=False):
    if not isinstance(url, str) or len(url) > 2500 or any(ord(char) < 33 or ord(char) == 127 for char in url):
        raise SourceError("unsafe_url")
    try:
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.port not in (None, 443) or parts.username or parts.password
                or parts.fragment or parts.netloc.lower() not in {parts.hostname, f"{parts.hostname}:443"}):
            raise ValueError
        host, path = parts.hostname, parts.path
        if pdf:
            if host == "static.cninfo.com.cn":
                if parts.query or not re.fullmatch(r"/finalpage/\d{4}-\d{2}-\d{2}/\d{6,20}\.PDF", path, re.I):
                    raise ValueError
                _date(path.split("/")[2])
            elif host == "pdf.dfcfw.com":
                if not re.fullmatch(r"/pdf/H2_AN\d{16,24}_\d{1,3}\.pdf", path, re.I):
                    raise ValueError
                if parts.query and not re.fullmatch(r"\d{10,16}\.pdf", parts.query):
                    raise ValueError
            else:
                raise ValueError
        else:
            endpoint = f"https://{host}{path}"
            allowed = {CNINFO_STOCKS: set(), CNINFO_QUERY: set(),
                       EASTMONEY_LIST: {"sr", "page_size", "page_index", "ann_type", "stock_list", "f_node", "s_node", "begin_time", "end_time", "client_source"},
                       EASTMONEY_DETAIL: {"art_code", "client_source", "page_index"}}
            if endpoint not in allowed or set(parse_qs(parts.query, keep_blank_values=True)) - allowed[endpoint]:
                raise ValueError
        return url
    except (ValueError, AttributeError):
        raise SourceError("unsafe_url") from None


async def _throttle(host):
    with _RATE_LOCK:
        now = time.monotonic()
        target = max(now, _HOST_NEXT.get(host, 0))
        _HOST_NEXT[host] = target + REQUEST_INTERVAL_SECONDS
    if target > now:
        await asyncio.sleep(target - now)


async def _request_bytes(method, url, *, data=None, params=None, pdf=False):
    if params:
        url += "?" + urlencode(params)
    _safe_url(url, pdf=pdf)
    maximum = MAX_PDF_BYTES if pdf else MAX_METADATA_BYTES
    try:
        async with asyncio.timeout(60 if pdf else 25):
            async with httpx.AsyncClient(timeout=httpx.Timeout(20, connect=8), follow_redirects=False,
                                         headers={"User-Agent": "StockViewAgent/1.0 (public financial disclosure reader)",
                                                  "Accept": "application/pdf" if pdf else "application/json",
                                                  "Referer": "https://www.cninfo.com.cn/" if "cninfo.com.cn" in urlsplit(url).hostname else "https://data.eastmoney.com/"}) as client:
                for redirect in range(4):
                    _safe_url(url, pdf=pdf)
                    await _throttle(urlsplit(url).hostname)
                    async with client.stream(method, url, data=data) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            if redirect == 3:
                                raise SourceError("redirect_limit")
                            location = response.headers.get("location")
                            if not location:
                                raise SourceError("invalid_response")
                            target = urljoin(url, location)
                            _safe_url(target, pdf=pdf)
                            # A PDF redirect cannot silently switch announcement.
                            if pdf and urlsplit(target).path.rsplit("/", 1)[-1].lower() != urlsplit(url).path.rsplit("/", 1)[-1].lower():
                                raise SourceError("unsafe_url")
                            url = target
                            if response.status_code == 303 or (method == "POST" and response.status_code in {301, 302}):
                                method, data = "GET", None
                            continue
                        if response.status_code in {401, 403}:
                            raise SourceError("access_denied")
                        if response.status_code == 429:
                            with _RATE_LOCK:
                                _HOST_NEXT[urlsplit(url).hostname] = max(_HOST_NEXT.get(urlsplit(url).hostname, 0), time.monotonic() + 60)
                            raise SourceError("rate_limited")
                        if response.status_code == 404:
                            raise SourceError("not_found")
                        if response.status_code != 200:
                            raise SourceError("source_unavailable")
                        length = response.headers.get("content-length")
                        if length and length.isdigit() and int(length) > maximum:
                            raise SourceError("too_large")
                        chunks, size = [], 0
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > maximum:
                                raise SourceError("too_large")
                            chunks.append(chunk)
                        content = b"".join(chunks)
                        content_type = response.headers.get("content-type", "").split(";", 1)[0].lower().strip()
                        if pdf and (content_type not in {"application/pdf", "application/octet-stream"} or not content.startswith(b"%PDF-")):
                            raise SourceError("invalid_pdf")
                        return content, url, content_type
    except (httpx.TimeoutException, TimeoutError):
        raise SourceError("timeout") from None
    except httpx.HTTPError:
        raise SourceError("source_unavailable") from None
    raise SourceError("source_unavailable")


async def _json_request(method, url, **kwargs):
    content, _, _ = await _request_bytes(method, url, **kwargs)
    try:
        data = json.loads(content)
        if not isinstance(data, dict):
            raise ValueError
        return data
    except (ValueError, TypeError, RecursionError):
        raise SourceError("invalid_response") from None


async def _org(code):
    global _ORG_CACHE
    lock = _ORG_LOCKS.setdefault(asyncio.get_running_loop(), asyncio.Lock())
    async with lock:
        if not _ORG_CACHE or time.monotonic() - _ORG_CACHE[0] > 86400:
            raw = await _json_request("GET", CNINFO_STOCKS)
            stocks = raw.get("stockList")
            if not isinstance(stocks, list) or not stocks:
                raise SourceError("invalid_response")
            mapping = {}
            for row in stocks:
                if isinstance(row, dict) and re.fullmatch(r"\d{6}", str(row.get("code", ""))) and re.fullmatch(r"[A-Za-z0-9]{3,40}", str(row.get("orgId", ""))):
                    mapping[row["code"]] = row["orgId"]
            _ORG_CACHE = (time.monotonic(), mapping)
        if code not in _ORG_CACHE[1]:
            raise SourceError("not_found")
        return _ORG_CACHE[1][code]


def _normalise_cninfo(row, symbol, org_id):
    if not isinstance(row, dict) or row.get("secCode") != symbol[:6] or row.get("orgId") != org_id:
        return None
    source_id = str(row.get("announcementId") or "")
    if not re.fullmatch(r"\d{6,20}", source_id):
        return None
    title = _title(row.get("announcementTitle"))
    classification = classify_report(title)
    if not classification:
        return None
    try:
        stamp = row["announcementTime"]
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
            return None
        stamp = stamp / 1000
        announced = datetime.fromtimestamp(stamp, SHANGHAI_TZ).date().isoformat()
        adjunct = row.get("adjunctUrl")
        if not isinstance(adjunct, str) or not adjunct.startswith("finalpage/"):
            return None
        pdf = _safe_url("https://static.cninfo.com.cn/" + adjunct, pdf=True)
        if urlsplit(pdf).path.rsplit("/", 1)[-1].lower() != source_id + ".pdf":
            return None
    except (SourceError, ValueError, OverflowError, OSError, KeyError):
        return None
    if classification["document_kind"] in {"full_report", "summary"} and announced < classification["report_period"]:
        return None
    detail = "https://www.cninfo.com.cn/new/disclosure/detail?" + urlencode({"stockCode": symbol[:6], "announcementId": source_id, "orgId": org_id, "announcementTime": announced})
    return {"source": "cninfo", "source_id": source_id, "symbol": symbol, "company_name": str(row.get("secName") or "")[:150],
            "title": title, **classification, "announcement_date": announced, "published_at": stamp,
            "announcement_url": detail, "pdf_url": pdf, "identity_key": f"cninfo:{symbol}:{source_id}"}


def _normalise_eastmoney(row, symbol):
    if not isinstance(row, dict):
        return None
    market = {"SS": "1", "SZ": "0", "BJ": "0"}[symbol[-2:]]
    if not isinstance(row.get("codes"), list):
        return None
    security = next((item for item in row["codes"] if isinstance(item, dict) and item.get("stock_code") == symbol[:6]
                     and str(item.get("market_code")) == market), None)
    source_id = str(row.get("art_code") or "")
    if not security or not re.fullmatch(r"AN\d{16,24}", source_id):
        return None
    title = _title(row.get("title"))
    classification = classify_report(title)
    if not classification:
        return None
    try:
        announced = _date(str(row.get("notice_date") or "")[:10]).isoformat()
        stamp = datetime.fromisoformat(announced).replace(tzinfo=SHANGHAI_TZ).timestamp()
    except (ValueError, SourceError):
        return None
    if classification["document_kind"] in {"full_report", "summary"} and announced < classification["report_period"]:
        return None
    return {"source": "eastmoney", "source_id": source_id, "symbol": symbol, "company_name": str(security.get("short_name") or "")[:150],
            "title": title, **classification, "announcement_date": announced, "published_at": stamp,
            "announcement_url": f"https://data.eastmoney.com/notices/detail/{symbol[:6]}/{source_id}.html", "pdf_url": None,
            "identity_key": f"eastmoney:{symbol}:{source_id}"}


async def _cninfo(symbol, start, end):
    org_id = await _org(symbol[:6])
    items, seen = [], set()
    for page in range(1, MAX_PAGES + 1):
        try:
            raw = await _json_request("POST", CNINFO_QUERY, data={"pageNum": page, "pageSize": PAGE_SIZE, "tabName": "fulltext",
                "column": {"SS": "sse", "SZ": "szse", "BJ": "bjse"}[symbol[-2:]], "stock": f"{symbol[:6]},{org_id}",
                "searchkey": "报告", "seDate": f"{start}~{end}", "isHLtitle": "false"})
            rows = raw.get("announcements")
            if rows is None and raw.get("totalAnnouncement") == 0:
                rows = []
            if not isinstance(rows, list):
                raise SourceError("invalid_response")
        except SourceError as exc:
            if items:
                return items, True, exc.code
            raise
        signatures = tuple(str(row.get("announcementId")) for row in rows if isinstance(row, dict))
        if signatures and signatures in seen:
            return items, True, None
        seen.add(signatures)
        items.extend(item for row in rows if (item := _normalise_cninfo(row, symbol, org_id)))
        if not raw.get("hasMore") and len(rows) < PAGE_SIZE:
            return items, False, None
        if not rows:
            return items, False, None
    return items, True, None


async def _eastmoney(symbol, start, end):
    items, seen = [], set()
    for page in range(1, MAX_PAGES + 1):
        try:
            raw = await _json_request("GET", EASTMONEY_LIST, params={"sr": -1, "page_size": PAGE_SIZE, "page_index": page,
                "ann_type": "A", "stock_list": symbol[:6], "f_node": 1, "s_node": 0,
                "begin_time": start, "end_time": end, "client_source": "web"})
            data = raw.get("data")
            if raw.get("success") not in (1, True) or not isinstance(data, dict) or not isinstance(data.get("list"), list):
                raise SourceError("invalid_response")
            total_hits = data.get("total_hits")
            if type(total_hits) is not int or total_hits < 0:
                raise SourceError("invalid_response")
        except SourceError as exc:
            if items:
                return items, True, exc.code
            raise
        rows = data["list"]
        signatures = tuple(str(row.get("art_code")) for row in rows if isinstance(row, dict))
        if signatures and signatures in seen:
            return items, True, None
        seen.add(signatures)
        items.extend(item for row in rows if (item := _normalise_eastmoney(row, symbol)))
        if len(rows) < PAGE_SIZE or page * PAGE_SIZE >= total_hits:
            return items, False, None
    return items, True, None


async def discover_reports(symbol, *, start_date=None, end_date=None, report_year=None, report_type=None, limit=30):
    symbol = _symbol(symbol)
    today = _today()
    try:
        default_start = today.replace(year=today.year - 3)
    except ValueError:
        default_start = today.replace(year=today.year - 3, day=28)
    start, end = _date(start_date) if start_date else default_start, _date(end_date) if end_date else today
    if (start > end or end > today or (end - start).days > 366 * 6 or type(limit) is not int or not 1 <= limit <= 100
            or (report_year is not None and (type(report_year) is not int or not 1990 <= report_year <= today.year))
            or (report_type is not None and report_type not in _PERIOD_END)):
        raise SourceError("invalid_range")
    sources, records, partial = [], [], False
    for source, fetch in (("cninfo", _cninfo), ("eastmoney", _eastmoney)):
        try:
            rows, truncated, error_code = await fetch(symbol, start.isoformat(), end.isoformat())
            valid = [row for row in rows if start.isoformat() <= row["announcement_date"] <= end.isoformat()
                     and (report_year is None or row["report_year"] == report_year)
                     and (report_type is None or row["report_type"] == report_type)]
            sources.append({"source": source, "status": "ok" if valid else "empty", "partial": truncated,
                            "error": _MESSAGES.get(error_code), "error_code": error_code})
            records.extend(valid)
            partial = partial or truncated
            if any(row["is_interpretable"] for row in valid) and not truncated:
                break
        except SourceError as exc:
            sources.append({"source": source, "status": "error", "error": str(exc), "error_code": exc.code})
        except (ValueError, TypeError, KeyError, AttributeError):
            sources.append({"source": source, "status": "error", "error": _MESSAGES["invalid_response"], "error_code": "invalid_response"})
    unique = {row["identity_key"]: row for row in records}
    ordered = sorted(unique.values(), key=lambda row: (not row["is_interpretable"], -row["published_at"], row["source"] != "cninfo", row["identity_key"]))
    return {"symbol": symbol, "items": ordered[:limit], "sources": sources, "partial": partial or len(ordered) > limit,
            "start_date": start.isoformat(), "end_date": end.isoformat(), "total_found": len(ordered)}


def _validate_item(item):
    if not isinstance(item, dict):
        raise SourceError("invalid_catalog_item")
    try:
        symbol = _symbol(item.get("symbol"))
        source, identifier = item["source"], item["source_id"]
        classification = classify_report(item["title"])
        if (source not in {"cninfo", "eastmoney"} or not isinstance(identifier, str) or not classification
                or item.get("identity_key") != f"{source}:{symbol}:{identifier}"
                or any(item.get(key) != classification[key] for key in ("report_period", "report_type", "document_kind"))):
            raise ValueError
        _date(item["announcement_date"])
        if source == "cninfo":
            if not re.fullmatch(r"\d{6,20}", identifier):
                raise ValueError
            url = _safe_url(item["pdf_url"], pdf=True)
            if urlsplit(url).hostname != "static.cninfo.com.cn" or urlsplit(url).path.rsplit("/", 1)[-1].lower() != identifier + ".pdf":
                raise ValueError
        elif not re.fullmatch(r"AN\d{16,24}", identifier):
            raise ValueError
        return symbol
    except (KeyError, TypeError, ValueError):
        raise SourceError("invalid_catalog_item") from None


async def download_report_pdf(item):
    """Download a server-catalogued record, never a URL provided by an API caller."""
    symbol = _validate_item(item)
    if item["source"] == "cninfo":
        url = item["pdf_url"]
    else:
        raw = await _json_request("GET", EASTMONEY_DETAIL, params={"art_code": item["source_id"], "client_source": "web", "page_index": 1})
        data = raw.get("data")
        market = {"SS": "1", "SZ": "0", "BJ": "0"}[symbol[-2:]]
        if not isinstance(data, dict) or data.get("art_code") != item["source_id"]:
            raise SourceError("invalid_catalog_item")
        if not isinstance(data.get("security"), list) or not any(isinstance(row, dict) and row.get("stock") == symbol[:6] and str(row.get("market_uni")) == market for row in data["security"]):
            raise SourceError("invalid_catalog_item")
        current = classify_report(data.get("notice_title"))
        if (not current or _title(data.get("notice_title")) != item["title"]
                or str(data.get("notice_date") or "")[:10] != item["announcement_date"]
                or any(current[key] != item[key] for key in ("report_period", "report_type", "document_kind"))):
            raise SourceError("invalid_catalog_item")
        url = data.get("attach_url_web")
        _safe_url(url, pdf=True)
        if urlsplit(url).hostname != "pdf.dfcfw.com" or not urlsplit(url).path.startswith(f"/pdf/H2_{item['source_id']}_"):
            raise SourceError("invalid_catalog_item")
    content, final_url, content_type = await _request_bytes("GET", url, pdf=True)
    return {"content": content, "sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content),
            "content_type": content_type, "url": final_url}
