"""Public, key-free history collection for the two Merrill clock views.

Yahoo market observations are reduced from daily to completed W-FRI buckets;
FRED and NBS (via Eastmoney) macro observations retain their monthly frequency.
No missing observation is interpolated, carried forward, or replaced by an ETF
without an explicit catalogue label. HTTP failures preserve all saved history.
"""
from __future__ import annotations

import asyncio
import csv
from datetime import date, datetime, timedelta, timezone
import hashlib
import io
import math
import time
from urllib.parse import quote
from urllib.request import getproxies
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from app.storage import merrill_store as store

ASSETS = store.ASSET_CATALOG
MACRO_CATALOG = {
    "INDPRO": {"name": "美国工业生产指数", "unit": "2017=100", "frequency": "monthly", "region": "US",
               "source": "Federal Reserve / FRED", "source_url": "https://fred.stlouisfed.org/series/INDPRO",
               "kind": "level", "note": "季调月度指数；历史数据会修订，不是实时历史版本。"},
    "CPIAUCSL": {"name": "美国CPI", "unit": "1982-1984=100", "frequency": "monthly", "region": "US",
                 "source": "BLS / FRED", "source_url": "https://fred.stlouisfed.org/series/CPIAUCSL",
                 "kind": "level", "note": "季调月度指数；同比由同月12个月前指数计算。"},
    "CN_INDUSTRY_YOY": {"name": "中国规模以上工业增加值同比", "unit": "%", "frequency": "monthly", "region": "CN",
                        "source": "国家统计局 / 东方财富", "source_url": "https://data.eastmoney.com/cjsj/gyzjz.html",
                        "official_url": "https://www.stats.gov.cn/sj/zxfb/", "kind": "yoy",
                        "note": "当月同比；不把1—2月合并值作为2月单月；缺失月份留空。"},
    "CN_CPI_YOY": {"name": "中国CPI同比", "unit": "%", "frequency": "monthly", "region": "CN",
                   "source": "国家统计局 / 东方财富", "source_url": "https://data.eastmoney.com/cjsj/cpi.html",
                   "official_url": "https://www.stats.gov.cn/sj/zxfb/", "kind": "yoy",
                   "note": "全国居民消费价格当月同比，直接使用同比值，不再次同比转换。"},
    "CN_PMI": {"name": "中国制造业PMI", "unit": "PMI点", "frequency": "monthly", "region": "CN",
               "source": "国家统计局 / 东方财富", "source_url": "https://data.eastmoney.com/cjsj/pmi.html",
               "official_url": "https://www.stats.gov.cn/sj/zxfb/", "kind": "level",
               "note": "官方制造业采购经理指数月度水平；50为荣枯线，不是增长同比或工业产出指数。"},
}


class SourceUnavailable(RuntimeError):
    pass


def _number(value):
    if value is None or isinstance(value, bool) or str(value).strip() in {"", ".", "-", "--"}:
        return None
    try:
        number = float(value)
    except (ValueError, TypeError):
        return None
    return number if math.isfinite(number) else None


def aggregate_weekly(rows, *, today=None):
    """Last finite actual weekday observation per completed Friday, no fill."""
    cutoff = store.completed_friday(today)
    buckets = {}
    for row in rows:
        try:
            observed = date.fromisoformat(str(row["date"])[:10])
        except (ValueError, TypeError, KeyError):
            continue
        value = _number(row.get("close", row.get("value")))
        if value is None or observed.weekday() > 4:
            continue
        week = observed + timedelta(days=4 - observed.weekday())
        if week > cutoff:
            continue
        existing = buckets.get(week)
        if existing is None or observed.isoformat() >= existing["observation_date"]:
            buckets[week] = {"date": week.isoformat(), "close": value, "observation_date": observed.isoformat()}
    return [buckets[key] for key in sorted(buckets)]


def parse_yahoo(payload, *, adjusted=False, kind="index"):
    chart = payload.get("chart") or {}
    if chart.get("error") or not chart.get("result"):
        raise SourceUnavailable("Yahoo没有返回该序列的历史数据")
    item = chart["result"][0]
    try:
        tz = ZoneInfo(item.get("meta", {}).get("exchangeTimezoneName") or "UTC")
    except ZoneInfoNotFoundError:
        tz = timezone.utc
    indicators = item.get("indicators") or {}
    quotes = indicators.get("quote") or [{}]
    # ETF history MUST have adjusted close; falling back would change the series.
    values = ((indicators.get("adjclose") or [{}])[0].get("adjclose") if adjusted
              else quotes[0].get("close"))
    stamps = item.get("timestamp") or []
    if values is None or len(values) != len(stamps):
        raise SourceUnavailable("Yahoo返回数据列不完整，未切换价格口径")
    result = []
    for stamp, value in zip(stamps, values):
        number = _number(value)
        if number is None or (kind in {"index", "etf"} and number <= 0):
            continue
        try:
            observed = datetime.fromtimestamp(stamp, tz=timezone.utc).astimezone(tz).date()
        except (TypeError, ValueError, OverflowError, OSError):
            continue
        result.append({"date": observed.isoformat(), "close": number})
    return result


def parse_fred_csv(text, series_id):
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    if not reader.fieldnames or series_id not in reader.fieldnames:
        raise SourceUnavailable("FRED未返回预期CSV数据列")
    date_field = "observation_date" if "observation_date" in reader.fieldnames else "DATE"
    if date_field not in reader.fieldnames:
        raise SourceUnavailable("FRED日期列缺失")
    rows = {}
    for row in reader:
        number = _number(row.get(series_id))
        try:
            observed = date.fromisoformat(row[date_field])
        except (ValueError, TypeError, KeyError):
            continue
        if number is not None:
            rows[observed.isoformat()] = number
    return [{"date": day, "value": rows[day]} for day in sorted(rows)]


def parse_china_macro(payload, series_id):
    data = (payload.get("result") or {}).get("data")
    if payload.get("success") is not True or not isinstance(data, list):
        raise SourceUnavailable("中国宏观来源未返回有效数据")
    field = {"CN_INDUSTRY_YOY": "BASE_SAME", "CN_CPI_YOY": "NATIONAL_SAME", "CN_PMI": "MAKE_INDEX"}[series_id]
    result = {}
    for row in data:
        number = _number(row.get(field))
        try:
            observed = date.fromisoformat(str(row.get("REPORT_DATE"))[:10])
        except ValueError:
            continue
        if number is None:
            continue
        # The official industrial January-February release is a combined rate.
        # Eastmoney labels it February; do not silently treat it as a single month.
        if series_id == "CN_INDUSTRY_YOY" and observed.month in {1, 2}:
            continue
        result[observed.replace(day=1).isoformat()] = number
    return [{"date": day, "value": result[day]} for day in sorted(result)]


def parse_federal_reserve_ip(text):
    """G.17 ip_sa.txt: B50001 is the seasonally adjusted total production index."""
    rows = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 3 or fields[0] != '"B50001"':
            continue
        try:
            year = int(fields[1])
        except ValueError:
            continue
        for month, raw in enumerate(fields[2:14], start=1):
            value = _number(raw)
            if value is not None and value > 0:
                rows.append({"date": date(year, month, 1).isoformat(), "value": value})
    if not rows:
        raise SourceUnavailable("美联储G.17没有返回B50001总工业生产数据")
    return rows


def parse_bls_cpi(payload):
    if payload.get("status") != "REQUEST_SUCCEEDED":
        raise SourceUnavailable("BLS公开CPI请求失败或达到服务限额")
    rows = []
    for series in (payload.get("Results") or {}).get("series", []):
        if series.get("seriesID") != "CUSR0000SA0":
            continue
        for observation in series.get("data", []):
            period = observation.get("period", "")
            if len(period) != 3 or not period.startswith("M") or not period[1:].isdigit():
                continue
            month = int(period[1:])
            value = _number(observation.get("value"))
            if month not in range(1, 13) or value is None or value <= 0:
                continue
            try:
                day = date(int(observation["year"]), month, 1)
            except (ValueError, TypeError, KeyError):
                continue
            rows.append({"date": day.isoformat(), "value": value})
    return rows


async def _get(client, url, *, params=None):
    for attempt in range(2):
        try:
            response = await client.get(url, params=params)
            response.raise_for_status()
            if len(response.content) > 12_000_000:
                raise SourceUnavailable("来源响应超过允许大小")
            return response
        except httpx.HTTPStatusError as exc:
            raise SourceUnavailable(f"公共来源HTTP {exc.response.status_code}") from None
        except httpx.HTTPError as exc:
            if attempt == 0:
                await asyncio.sleep(0.4)
                continue
            message = "公共来源连接超时" if isinstance(exc, httpx.TimeoutException) else "公共来源网络连接失败"
            raise SourceUnavailable(message) from None


async def fetch_csi300(client, start, end):
    return await fetch_china_index(client, "sh000300", start, end, first_year=2005)


async def fetch_china_index(client, symbol, start, end, *, first_year):
    rows, hashes = [], []
    # CSI 300 launched in 2005; Shanghai Composite in 1990. Tencent limits each request to 640 observations,
    # so use two-year daily windows rather than truncating the requested history.
    first_year = max(first_year, start.year)
    for year in range(first_year, end.year + 1, 2):
        lower = max(start, date(year, 1, 1))
        upper = min(end, date(year + 1, 12, 31))
        response = await _get(client, "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
                              params={"param": f"{symbol},day,{lower},{upper},640,qfq"})
        try:
            payload = response.json()
            data = (payload.get("data") or {}).get(symbol) or {}
            daily = data.get("day") or data.get("qfqday") or []
            if payload.get("code") != 0 or not daily:
                raise SourceUnavailable("腾讯中国指数历史数据缺失")
        except (ValueError, TypeError, AttributeError):
            raise SourceUnavailable("腾讯中国指数历史数据格式异常") from None
        for row in daily:
            if not isinstance(row, list) or len(row) < 3:
                continue
            value = _number(row[2])
            if value is not None and value > 0 and lower.isoformat() <= str(row[0]) <= upper.isoformat():
                rows.append({"date": str(row[0]), "close": value})
        hashes.append(hashlib.sha256(response.content).hexdigest())
    if not rows:
        raise SourceUnavailable("腾讯中国指数请求区间没有历史观测")
    return rows, hashlib.sha256("".join(hashes).encode()).hexdigest()


async def fetch_market(client, asset, start, end):
    if asset["id"] == "csi300":
        return await fetch_csi300(client, start, end)
    if asset["id"] == "sse":
        return await fetch_china_index(client, "sh000001", start, end, first_year=1990)
    # period2 is exclusive. Include the entire Friday across exchange timezones.
    first = int(datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc).timestamp())
    last = int(datetime.combine(end + timedelta(days=2), datetime.min.time(), tzinfo=timezone.utc).timestamp())
    response = await _get(client, "https://query1.finance.yahoo.com/v8/finance/chart/" + quote(asset["source_symbol"], safe=""),
                          params={"period1": first, "period2": last, "interval": "1d", "events": "div,splits", "includeAdjustedClose": "true"})
    try:
        rows = parse_yahoo(response.json(), adjusted=asset["kind"] == "etf", kind=asset["kind"])
    except (ValueError, TypeError, KeyError):
        raise SourceUnavailable("Yahoo历史数据格式异常") from None
    rows = [row for row in rows if start.isoformat() <= row["date"] <= end.isoformat()]
    if not rows:
        raise SourceUnavailable("请求区间没有可用历史日数据")
    return rows, hashlib.sha256(response.content).hexdigest()


async def fetch_macro(client, series_id, start, end):
    provenance = {}
    if series_id in {"INDPRO", "CPIAUCSL"}:
        try:
            response = await _get(client, "https://fred.stlouisfed.org/graph/fredgraph.csv",
                                  params={"id": series_id, "cosd": start.isoformat(), "coed": end.isoformat()})
            rows = parse_fred_csv(response.text, series_id)
            digest = hashlib.sha256(response.content).hexdigest()
        except SourceUnavailable as exc:
            provenance["fallback_reason"] = str(exc)
            if series_id == "INDPRO":
                url = "https://www.federalreserve.gov/releases/g17/ipdisk/ip_sa.txt"
                response = await _get(client, url)
                rows = parse_federal_reserve_ip(response.text)
                digest = hashlib.sha256(response.content).hexdigest()
                provenance.update(source="Federal Reserve G.17", source_url=url, source_symbol="B50001")
            else:
                url = "https://api.bls.gov/publicAPI/v2/timeseries/data/CUSR0000SA0"
                rows, body_hashes = [], []
                # Anonymous BLS API permits ten calendar years per request.
                for first_year in range(start.year, end.year + 1, 10):
                    response = await _get(client, url, params={"startyear": first_year, "endyear": min(first_year + 9, end.year)})
                    try:
                        batch = parse_bls_cpi(response.json())
                    except (ValueError, TypeError, KeyError):
                        raise SourceUnavailable("BLS CPI数据格式异常") from None
                    if not batch:
                        raise SourceUnavailable("BLS CPI请求年份没有月度观测")
                    rows.extend(batch)
                    body_hashes.append(hashlib.sha256(response.content).hexdigest())
                digest = hashlib.sha256("".join(body_hashes).encode()).hexdigest()
                provenance.update(source="BLS Public Data API", source_url=url, source_symbol="CUSR0000SA0")
    else:
        report = {"CN_INDUSTRY_YOY": "RPT_ECONOMY_INDUS_GROW", "CN_CPI_YOY": "RPT_ECONOMY_CPI", "CN_PMI": "RPT_ECONOMY_PMI"}[series_id]
        rows, body_hashes = [], []
        for page in range(1, 11):
            response = await _get(client, "https://datacenter-web.eastmoney.com/api/data/v1/get",
                                  params={"reportName": report, "columns": "ALL", "pageSize": 500,
                                          "pageNumber": page, "sortColumns": "REPORT_DATE", "sortTypes": -1})
            try:
                payload = response.json()
                rows.extend(parse_china_macro(payload, series_id))
                pages = int((payload.get("result") or {}).get("pages") or 1)
            except (ValueError, TypeError, KeyError):
                raise SourceUnavailable("中国宏观来源数据格式异常") from None
            body_hashes.append(hashlib.sha256(response.content).hexdigest())
            if page >= pages:
                break
        if pages > 10:
            raise SourceUnavailable("宏观来源返回异常分页数量")
    # A period label is not a release date. Retain only published, completed months.
    this_month = datetime.now(timezone.utc).date().replace(day=1)
    rows = sorted((row for row in rows if start.replace(day=1).isoformat() <= row["date"] < this_month.isoformat()), key=lambda row: row["date"])
    if not rows:
        raise SourceUnavailable("来源没有可用月度观测")
    if series_id not in {"INDPRO", "CPIAUCSL"}:
        digest = hashlib.sha256("".join(body_hashes).encode()).hexdigest()
    return rows, digest, provenance


def _error(exc):
    # Provider bodies and proxy URLs may contain credentials; only safe messages persist.
    return str(exc)[:300] if isinstance(exc, SourceUnavailable) else "来源处理失败（" + type(exc).__name__ + "）"


async def sync_all(full=False, *, asset_ids=None, macro_ids=None):
    """Refresh all public series; the caller owns the cross-process worker lock.

    Daily levels incrementally recheck the last five weeks. Adjusted ETF prices
    and macro data always refetch available history so corporate actions and
    statistical revisions are reflected consistently throughout the database.
    """
    assets = [item for item in ASSETS if asset_ids is None or item["id"] in asset_ids]
    macros = [identifier for identifier in MACRO_CATALOG if macro_ids is None or identifier in macro_ids]
    if asset_ids is not None and set(asset_ids) - {item["id"] for item in ASSETS}:
        raise ValueError("未知资产序列")
    if macro_ids is not None and set(macro_ids) - set(MACRO_CATALOG):
        raise ValueError("未知宏观序列")
    store.init_db()
    started = time.time()
    start, end = store.requested_start(), store.completed_friday()
    status = {"status": "running", "count": 0, "series_count": 0, "macro_count": 0,
              "errors": [], "started_at": started, "finished_at": None,
              "requested_from": start.isoformat(), "requested_to": end.isoformat(),
              "scope": "all" if asset_ids is None and macro_ids is None else "selected",
              "requested_series": len(assets), "requested_macro": len(macros)}
    store.set_state("sync_status", status)
    existing = {item["id"]: item for item in store.catalog()}
    macro_meta = store.get_state("macro_metadata", {})
    limit = asyncio.Semaphore(3)
    # Respect the user's OS/environment HTTP proxy; do not expose its URL.
    proxy = getproxies().get("https") or getproxies().get("http")
    if proxy and not proxy.startswith(("http://", "https://")):
        proxy = None
    async with httpx.AsyncClient(timeout=httpx.Timeout(35, connect=15), follow_redirects=True,
                                 proxy=proxy, headers={"User-Agent": "Mozilla/5.0 StockViewAgent/1.0"}) as client:
        async def market(asset):
            async with limit:
                identifier = asset["id"]
                last = existing[identifier].get("available_to")
                fetch_start = (max(start, date.fromisoformat(last) - timedelta(days=35))
                               if last and not full and asset["kind"] != "etf" else start)
                try:
                    rows, digest = await fetch_market(client, asset, fetch_start, end)
                    weekly = aggregate_weekly(rows)
                    if not weekly:
                        raise SourceUnavailable("没有已完成交易周的观测")
                    count = store.upsert_weekly(identifier, weekly, requested_from=start.isoformat(), requested_to=end.isoformat())
                    store.set_state("source_snapshot:" + identifier, {"sha256": digest, "retrieved_at": time.time(),
                                    "source": asset["source"], "source_symbol": asset["source_symbol"],
                                    "source_url": asset["source_url"], "price_basis": asset["price_basis"],
                                    "daily_observations": len(rows), "fetched_from": fetch_start.isoformat(), "fetched_to": end.isoformat()})
                    status["count"] += count
                    status["series_count"] += 1
                except Exception as exc:
                    message = _error(exc)
                    store.record_error(identifier, message, requested_from=start.isoformat(), requested_to=end.isoformat())
                    status["errors"].append({"id": identifier, "error": message})
                store.set_state("sync_status", status)

        async def macro(identifier):
            async with limit:
                item = {**MACRO_CATALOG[identifier], **macro_meta.get(identifier, {})}
                try:
                    rows, digest, provenance = await fetch_macro(client, identifier, start, end)
                    count = store.save_macro(identifier, rows)
                    item.pop("fallback_reason", None)
                    item.pop("source_symbol", None)
                    item.update(MACRO_CATALOG[identifier], **provenance)
                    item.update(last_error=None, last_synced_at=time.time(), row_count=count,
                                available_from=rows[0]["date"], available_to=rows[-1]["date"], sha256=digest)
                    status["macro_count"] += 1
                except Exception as exc:
                    message = _error(exc)
                    item["last_error"] = message
                    status["errors"].append({"id": identifier, "error": message})
                macro_meta[identifier] = item
                store.set_state("macro_metadata", macro_meta)
                store.set_state("sync_status", status)

        await asyncio.gather(*(market(item) for item in assets), *(macro(identifier) for identifier in macros))
    successful = status["series_count"] + status["macro_count"]
    status.update(status="succeeded" if not status["errors"] else "partial" if successful else "failed", finished_at=time.time())
    store.set_state("sync_status", status)
    return status
