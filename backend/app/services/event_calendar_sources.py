"""Bounded, credential-free economic release calendars from their publishers.

Dates come from the current upstream document, never a generated recurrence.
Calendar-only sources do not establish an actual result or market consensus.
HTML is parsed as inert text; no upstream script or URL is executed/followed.
Importance is a local editorial priority (1-3), not an upstream forecast.
HTML publishers do not expose a stable release identifier. Date-based identity
is conservative: a reschedule may leave both observed plans in storage. Missing
rows cannot prove cancellation, and ordinal matching can conflate new releases.
"""
from __future__ import annotations

import hashlib
import html
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import httpx


SOURCE_CATALOG = (
    {"id": "fed_fomc", "name": "美联储 FOMC 官方会议日历", "url": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm", "region": "US", "currency": "USD", "timezone": "America/New_York", "official": True, "format": "html", "note": "会议结束日；官网未列具体时刻时显示日期待定。政策声明链接仅证明已发布，不推断利率结果。"},
    {"id": "ecb_policy", "name": "欧洲央行官方会议日历", "url": "https://www.ecb.europa.eu/press/calendars/mgcgc/html/index.en.html", "region": "EU", "currency": "EUR", "timezone": "Europe/Berlin", "official": True, "format": "html", "note": "货币政策会议与发布会；不包含市场预期和政策结果数值。"},
    {"id": "boj_policy", "name": "日本银行官方会议日历", "url": "https://www.boj.or.jp/en/mopo/mpmsche_minu/index.htm", "region": "JP", "currency": "JPY", "timezone": "Asia/Tokyo", "official": True, "format": "html", "note": "会议结束日、展望报告、意见摘要和会议纪要；会议结果时刻未确定时保留全天标记。"},
    {"id": "cn_nbs", "name": "中国国家统计局官方发布日程", "url": "https://www.stats.gov.cn/sj/fbrc/bnxxfb/", "region": "CN", "currency": "CNY", "timezone": "Asia/Shanghai", "official": True, "format": "html", "note": "年度统计发布计划：CPI/PPI、PMI、国民经济、工业、社零与投资；可能调整，日程不含实际值。"},
    {"id": "us_bls", "name": "美国劳工统计局官方发布日历", "url": "https://www.bls.gov/schedule/news_release/bls.ics", "region": "US", "currency": "USD", "timezone": "America/New_York", "official": True, "format": "ics", "note": "就业、CPI/PPI等发布安排；官网可能限制访问（HTTP 403），失败保留可诊断状态，不生成替代数据。"},
)
_SOURCES = {item["id"]: item for item in SOURCE_CATALOG}
MAX_RESPONSE_BYTES = 2_000_000
_MONTHS = {name[:3].lower(): n for n, name in enumerate(("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"), 1)}


class CalendarSourceError(ValueError):
    """An upstream request or response cannot provide a trustworthy calendar."""

    def __init__(self, message: str, *, error_code: str = "invalid_calendar"):
        super().__init__(message)
        self.error_code = error_code


def _text(value: str) -> str:
    value = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", value, flags=re.I | re.S)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value))).strip()


def _category(title: str) -> str:
    lower = title.lower()
    for category, terms in (
        ("central_bank", ("fomc", "monetary policy", "governing council", "利率", "货币政策")),
        ("inflation", ("consumer price", "producer price", "inflation", "cpi", "ppi", "消费价格", "生产者价格")),
        ("employment", ("employment", "job", "labor", "labour", "unemployment", "工资", "就业")),
        ("trade", ("import", "export", "trade", "进出口", "贸易")),
        ("growth", ("gdp", "product", "retail", "国民经济", "采购经理", "工业", "投资", "零售", "增加值", "房地产", "能源", "收支")),
    ):
        if any(term in lower for term in terms):
            return category
    return "other"


def _event(source_id: str, title: str, local_day: date, *, upstream_id: str = "", clock: time | None = None,
           description: str = "", source_url: str = "", status: str = "scheduled", category: str = "",
           importance: int = 3, local_timezone: str = "") -> dict[str, Any]:
    meta = _SOURCES[source_id]
    tzname = local_timezone or meta["timezone"]
    moment = datetime.combine(local_day, clock or time.min, ZoneInfo(tzname))
    identity = upstream_id or hashlib.sha256(f"{source_id}|{local_day}|{title}".encode()).hexdigest()[:24]
    return {
        "source_id": source_id, "source": meta["name"], "source_url": source_url or meta["url"],
        "upstream_id": identity, "title": title, "description": description,
        "region": meta["region"], "currency": meta["currency"], "category": category or _category(title),
        "importance": importance, "scheduled_at": moment.astimezone(timezone.utc).isoformat(),
        "timezone": tzname, "scheduled_date": local_day.isoformat(), "all_day": clock is None,
        "status": status, "actual": None, "forecast": None, "previous": None, "unit": None,
    }


def _same_host_url(source_id: str, href: str) -> str:
    base = _SOURCES[source_id]["url"]
    target = urljoin(base, html.unescape(href))
    parsed = urlparse(target)
    return target if parsed.scheme == "https" and parsed.hostname == urlparse(base).hostname else base


def parse_fed_calendar(document: str) -> list[dict[str, Any]]:
    """Read the official year heading and each meeting's month/date cells."""
    headings = list(re.finditer(r"(20\d{2})\s+FOMC Meetings", document))
    rows = []
    for index, heading in enumerate(headings):
        year = int(heading.group(1))
        section = document[heading.end():headings[index + 1].start() if index + 1 < len(headings) else len(document)]
        starts = list(re.finditer(r'<div\b[^>]*class=["\'][^"\']*\bfomc-meeting\b[^"\']*["\'][^>]*>', section))
        # A word boundary also matches fomc-meeting__month; require a complete class token.
        starts = [m for m in starts if "fomc-meeting" in re.search(r'class=["\']([^"\']*)', m.group()).group(1).split()]
        for position, start in enumerate(starts):
            block = section[start.end():starts[position + 1].start() if position + 1 < len(starts) else len(section)]
            month_match = re.search(r'<div\b[^>]*class=["\'][^"\']*fomc-meeting__month[^"\']*["\'][^>]*>(.*?)</div>', block, re.S)
            date_match = re.search(r'<div\b[^>]*class=["\'][^"\']*fomc-meeting__date[^"\']*["\'][^>]*>(.*?)</div>', block, re.S)
            if not month_match or not date_match:
                continue
            month_text, date_text = _text(month_match.group(1)), _text(date_match.group(1))
            months = re.findall(r"[A-Za-z]+", month_text)
            days = re.findall(r"\d{1,2}", date_text)
            if not months or not days or months[-1][:3].lower() not in _MONTHS:
                continue
            try:
                day = date(year, _MONTHS[months[-1][:3].lower()], int(days[-1]))
            except ValueError:
                continue
            statement = re.search(r'href=["\']([^"\']*/newsevents/pressreleases/monetary\d{8}[a-z]\.htm)["\']', block)
            rows.append(_event("fed_fomc", "美联储 FOMC 货币政策会议（结束日）", day,
                               upstream_id=f"fomc-{day.isoformat()}", category="central_bank",
                               description=f"官方会议日期：{month_text} {date_text}, {year}。" + ("含经济预测摘要（SEP）。" if "*" in date_text else "") + "具体公布时刻未由本日历提供。",
                               source_url=_same_host_url("fed_fomc", statement.group(1)) if statement else "",
                               status="released" if statement else "scheduled"))
    return rows


def parse_ecb_calendar(document: str) -> list[dict[str, Any]]:
    rows = []
    for raw_date, raw_description in re.findall(r"<dt\b[^>]*>(.*?)</dt>\s*<dd\b[^>]*>(.*?)</dd>", document, re.S | re.I):
        description = _text(raw_description)
        lower = description.lower()
        if "non-monetary" in lower or not ("monetary policy" in lower or "press conference" in lower):
            continue
        try:
            day = datetime.strptime(_text(raw_date), "%d/%m/%Y").date()
        except ValueError:
            continue
        if "day 1" in lower:
            title, importance, key = "欧洲央行货币政策会议（首日）", 2, "day1"
        elif "monetary policy" in lower:
            title, importance, key = "欧洲央行货币政策会议及新闻发布会", 3, "decision"
        else:
            title, importance, key = "欧洲央行新闻发布会", 3, "press"
        rows.append(_event("ecb_policy", title, day, upstream_id=f"ecb-{day}-{key}",
                           description=description, importance=importance, category="central_bank"))
    return rows


def _english_day(value: str, default_year: int) -> date | None:
    """BOJ cell dates; use the last meeting day, excluding PDF file sizes."""
    text = re.sub(r"\[PDF[^]]*]", "", _text(value), flags=re.I)
    explicit_year = re.search(r"\b(20\d{2})\b", text)
    year = int(explicit_year.group(1)) if explicit_year else default_year
    text = re.sub(r"\([^)]*\)|\b20\d{2}\b", "", text)
    months = re.findall(r"[A-Za-z]+", text)
    days = re.findall(r"\d{1,2}", text)
    if not months or not days or months[-1][:3].lower() not in _MONTHS:
        return None
    try:
        return date(year, _MONTHS[months[-1][:3].lower()], int(days[-1]))
    except ValueError:
        return None


def parse_boj_calendar(document: str) -> list[dict[str, Any]]:
    rows = []
    has_release_time = "8:50 a.m." in _text(document)
    for table in re.findall(r"<table\b[^>]*>(.*?)</table>", document, re.S | re.I):
        caption = re.search(r"<caption\b[^>]*>.*?(20\d{2}).*?</caption>", table, re.S | re.I)
        if not caption or "Date of MPM" not in table:
            continue
        year = int(caption.group(1))
        for raw_row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", table, re.S | re.I):
            cells = re.findall(r"<td\b[^>]*>(.*?)</td>", raw_row, re.S | re.I)
            if len(cells) != 4:
                continue
            meeting_day = _english_day(cells[0], year)
            if not meeting_day:
                continue
            for index, (label, key) in enumerate((("日本银行货币政策会议（结束日）", "meeting"), ("日本银行经济与物价展望报告", "outlook"), ("日本银行货币政策会议意见摘要", "opinions"), ("日本银行货币政策会议纪要", "minutes"))):
                day = _english_day(cells[index], year)
                if not day:
                    continue
                href = re.search(r'href=["\']([^"\']+)["\']', cells[index])
                rows.append(_event("boj_policy", label, day, upstream_id=f"boj-{meeting_day}-{key}",
                                   clock=time(8, 50) if index >= 2 and has_release_time else None,
                                   description=f"对应货币政策会议结束日 {meeting_day}；官方日期：{_text(cells[index])}",
                                   source_url=_same_host_url("boj_policy", href.group(1)) if href else "",
                                   status="released" if href else "scheduled", category="central_bank",
                                   importance=3 if index <= 1 else 2))
    return rows


def _expanded_table(table: str) -> list[list[str]]:
    """Expand the publisher's row/col spans so missing months cannot shift data."""
    grid: dict[tuple[int, int], str] = {}
    row_count = 0
    for r, raw_row in enumerate(re.findall(r"<tr\b[^>]*>(.*?)</tr>", table, re.I | re.S)):
        row_count = r + 1
        c = 0
        for attrs, content in re.findall(r"<t[dh]\b([^>]*)>(.*?)</t[dh]>", raw_row, re.I | re.S):
            while (r, c) in grid:
                c += 1
            span = {}
            for name in ("rowspan", "colspan"):
                match = re.search(rf'{name}\s*=\s*["\']?(\d+)', attrs, re.I)
                span[name] = min(int(match.group(1)), 50) if match else 1
            for dr in range(span["rowspan"]):
                for dc in range(span["colspan"]):
                    grid[r + dr, c + dc] = _text(content)
            c += span["colspan"]
    width = max((c for _, c in grid), default=-1) + 1
    return [[grid.get((r, c), "") for c in range(width)] for r in range(row_count)]


def parse_nbs_calendar(document: str) -> list[dict[str, Any]]:
    year_match = re.search(r"(20\d{2})年国家统计局主要统计信息发布日程", _text(document))
    if not year_match:
        return []
    year, rows = int(year_match.group(1)), []
    for table in re.findall(r"<table\b[^>]*>(.*?)</table>", document, re.S | re.I):
        grid = _expanded_table(table)
        if not grid or grid[0][:2] != ["序号", "内容"]:
            continue
        month_columns = {c: int(m.group(1)) for c, value in enumerate(grid[0]) if (m := re.fullmatch(r"(\d{1,2})月", value))}
        for index, cells in enumerate(grid[1:], 1):
            if len(cells) < 3 or not cells[0].isdigit():
                continue
            for column, month in month_columns.items():
                # A subsequent time row has no day/weekday pair, and is skipped.
                days = re.findall(r"(\d{1,2})\s*/\s*[一二三四五六日天]", cells[column])
                clock_match = re.search(r"(\d{1,2})[:：](\d{2})", grid[index + 1][column]) if index + 1 < len(grid) else None
                for raw_day in days:
                    try:
                        day = date(year, month, int(raw_day))
                        clock = time(int(clock_match.group(1)), int(clock_match.group(2))) if clock_match else None
                    except ValueError:
                        continue
                    important = any(term in cells[1] for term in ("国民经济", "采购经理", "居民消费价格", "工业生产者价格"))
                    rows.append(_event("cn_nbs", cells[1], day, upstream_id=f"nbs-{day}-{cells[1]}", clock=clock,
                                       description="国家统计局年度发布计划，日期可能调整；日程本身不代表实际已发布。",
                                       importance=3 if important else 2))
    return rows


def _ics_text(value: str) -> str:
    return re.sub(r"\\([nN,;\\])", lambda m: "\n" if m.group(1).lower() == "n" else m.group(1), value).strip()


def parse_bls_calendar(document: str) -> list[dict[str, Any]]:
    """Support RFC5545 folding, local TZID, UTC, dates and explicit cancellation."""
    if "BEGIN:VCALENDAR" not in document:
        return []
    unfolded = re.sub(r"\r?\n[ \t]", "", document)
    rows = []
    for block in re.findall(r"BEGIN:VEVENT\r?\n(.*?)END:VEVENT", unfolded, re.S):
        fields: dict[str, tuple[str, str]] = {}
        for line in block.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            fields[key.split(";", 1)[0].upper()] = (key, _ics_text(value))
        title = fields.get("SUMMARY", ("", ""))[1]
        raw_key, raw_time = fields.get("DTSTART", ("", ""))
        if not title or not raw_time:
            continue
        tzname = "America/New_York"
        tzmatch = re.search(r'TZID=["\']?([^;"\']+)', raw_key)
        if tzmatch:
            tzname = {"US/Eastern": "America/New_York", "US-Eastern": "America/New_York",
                      "Eastern Standard Time": "America/New_York"}.get(tzmatch.group(1), tzmatch.group(1))
        try:
            if re.fullmatch(r"\d{8}", raw_time):
                moment, clock = datetime.strptime(raw_time, "%Y%m%d"), None
            elif raw_time.endswith("Z"):
                moment = datetime.strptime(raw_time, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tzname))
                clock = moment.time()
            else:
                moment = datetime.strptime(raw_time, "%Y%m%dT%H%M%S")
                clock = moment.time()
            status = "cancelled" if fields.get("STATUS", ("", ""))[1].upper() == "CANCELLED" else "scheduled"
            href = fields.get("URL", ("", ""))[1]
            category = _category(title)
            rows.append(_event("us_bls", title, moment.date(), upstream_id=fields.get("UID", ("", ""))[1],
                               clock=clock, description=fields.get("DESCRIPTION", ("", ""))[1],
                               source_url=_same_host_url("us_bls", href) if href else "", status=status, local_timezone=tzname,
                               category=category, importance=3 if category in {"inflation", "employment"} else 2))
        except (ValueError, KeyError):
            continue
    return rows


_PARSERS = {"fed_fomc": parse_fed_calendar, "ecb_policy": parse_ecb_calendar,
            "boj_policy": parse_boj_calendar, "cn_nbs": parse_nbs_calendar, "us_bls": parse_bls_calendar}


async def fetch_source(source_id: str, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Fetch fixed HTTPS sources, fail explicitly on access/format errors.

    Retain fourteen days of recent events and one year ahead. A valid page with
    no events inside that window may return []; an unrecognized page never does.
    """
    if source_id not in _SOURCES:
        raise CalendarSourceError(f"Unknown calendar source: {source_id}", error_code="unknown_source")
    meta = _SOURCES[source_id]
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0), follow_redirects=False,
                                     headers={"User-Agent": "StockViewAgent-Calendar/1.0", "Accept": "text/calendar,text/html;q=0.9"}) as client:
            async with client.stream("GET", meta["url"]) as response:
                response.raise_for_status()
                length = response.headers.get("content-length", "")
                if length.isdigit() and int(length) > MAX_RESPONSE_BYTES:
                    raise CalendarSourceError("Calendar response exceeds size limit", error_code="response_too_large")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_RESPONSE_BYTES:
                        raise CalendarSourceError("Calendar response exceeds size limit", error_code="response_too_large")
                document = data.decode("utf-8-sig", errors="replace")
    except httpx.HTTPStatusError as exc:
        code = f"http_{exc.response.status_code}"
        raise CalendarSourceError(f"{source_id}: HTTP {exc.response.status_code}", error_code=code) from exc
    except httpx.TimeoutException as exc:
        raise CalendarSourceError(f"{source_id}: request timed out", error_code="timeout") from exc
    except httpx.HTTPError as exc:
        raise CalendarSourceError(f"{source_id}: network request failed", error_code="network_error") from exc
    events = _PARSERS[source_id](document)
    if not events:
        raise CalendarSourceError(f"{source_id}: no recognizable calendar events; upstream format changed or access was blocked")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    today = current.astimezone(ZoneInfo(meta["timezone"])).date()
    start, end = today - timedelta(days=14), today + timedelta(days=370)
    unique = {item["upstream_id"]: item for item in events if start.isoformat() <= item["scheduled_date"] <= end.isoformat()}
    return sorted(unique.values(), key=lambda item: (item["scheduled_at"], item["upstream_id"]))
