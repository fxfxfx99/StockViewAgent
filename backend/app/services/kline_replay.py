"""Daily, unadjusted price replay with explicitly dated valuation information.

Current quote snapshots and the latest local CSV financial view are deliberately
not inputs: neither certifies what was known at a historical replay cursor.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
import math
import re
from typing import Any

import httpx

from app.services import eastmoney_market, kline_baostock, tushare_service
from app.services.market_time import SHANGHAI_TZ, market_date, market_timestamp
from app.storage import kline_bundle_cache
from app.storage.platform_store import kline_sdk_enabled

MAX_RANGE_DAYS = 3660
METRIC_DEFINITIONS = {
    "market_cap": "对应交易日总市值，单位元；缺少历史股本时不使用当前股本回算。",
    "profit_ttm": "优先以当时已公告的合并报表归母净利润计算 TTM；否则仅在历史市值及 PE 均存在时估算，并单独标记。",
    "pe_ttm": "数据源对应交易日的滚动市盈率；亏损或缺失时留空，不套用最新 PE。",
    "volume_ratio": "当日成交量 ÷ 前 5 根完整交易日线平均成交量；不是盘中量比，前 5 根不足或均量为零时留空。",
    "publication": "仅使用公告日期严格早于当前回放日期的财报；日期型公告缺少盘前/盘后时刻，统一从次日可用。",
}


class ReplayDataUnavailable(RuntimeError):
    pass


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y%m%d").date() if re.fullmatch(r"\d{8}", value) else date.fromisoformat(value)
    except ValueError:
        return None


def normalize_request(symbol: str, start_date: date | None, end_date: date | None) -> tuple[str, date, date, list[str]]:
    sym = symbol.strip().upper()
    if re.fullmatch(r"\d{6}", sym):
        suffix = "BJ" if sym.startswith(("4", "8", "92")) else "SS" if sym.startswith(("5", "6", "9")) else "SZ"
        sym = f"{sym}.{suffix}"
    if not re.fullmatch(r"\d{6}\.(SS|SH|SZ|BJ)", sym):
        raise ValueError("仅支持 A 股代码，例如 600519.SS、000001.SZ")
    sym = sym.replace(".SH", ".SS")
    now = datetime.now(SHANGHAI_TZ)
    # A provider may still be assembling today's daily close. Never replay an
    # unfinished daily candle as if the trader already knew its final outcome.
    completed = now.date() if (now.hour, now.minute) >= (16, 30) else now.date() - timedelta(days=1)
    end = min(end_date or completed, completed)
    start = start_date or end - timedelta(days=730)
    if start > end:
        raise ValueError("开始日期不得晚于结束日期或最近完整交易日")
    if start < date(1990, 1, 1) or (end - start).days > MAX_RANGE_DAYS:
        raise ValueError("单次复盘区间最多 10 年，开始日期须在 1990 年之后")
    warnings = []
    if end_date and end_date > completed:
        warnings.append(f"仅加载完整日线，结束日期已限制到 {completed.isoformat()}。")
    return sym, start, end, warnings


def _metric(value, unit, source, *, status="historical", note="", **extra):
    return {"value": value, "unit": unit, "status": status if value is not None else "missing",
            "source": source if value is not None else None, "note": note, **extra}


def _report_ttm(reports: list[dict], as_of: date) -> dict | None:
    """Choose revisions only AFTER their actual publication, then form TTM."""
    by_period: dict[date, tuple[date, float]] = {}
    for report in reports:
        period = _date(report.get("end_date"))
        announced = _date(report.get("ann_date"))
        actual = _date(report.get("f_ann_date"))
        if report.get("f_ann_date") not in (None, "") and actual is None:
            continue
        # A revision's actual date can be later than the nominal announcement.
        publication = max(announced, actual) if announced and actual else announced or actual
        profit = _number(report.get("n_income_attr_p"))
        if (str(report.get("report_type") or "1") != "1" or not period or not publication
                or publication >= as_of or period > as_of or profit is None
                or (period.month, period.day) not in {(3, 31), (6, 30), (9, 30), (12, 31)}):
            continue
        if period not in by_period or publication > by_period[period][0]:
            by_period[period] = (publication, profit)
    if not by_period:
        return None
    latest = max(by_period)
    publication, profit = by_period[latest]
    if latest.month == 12 and latest.day == 31:
        return {"value": profit, "report_period": latest.isoformat(), "published_at": publication.isoformat()}
    annual = date(latest.year - 1, 12, 31)
    prior = latest.replace(year=latest.year - 1)
    if annual not in by_period or prior not in by_period:
        return None
    return {"value": profit + by_period[annual][1] - by_period[prior][1],
            "report_period": latest.isoformat(),
            "published_at": max(publication, by_period[annual][0], by_period[prior][0]).isoformat()}


def build_replay(bundle: dict, symbol: str, start: date, end: date, *, daily_basic=None, reports=None) -> dict:
    """Pure transformation; later candles/reports cannot affect earlier rows."""
    if bundle.get("price_basis") != "raw" or bundle.get("volume_unit") != "shares":
        raise ValueError("复盘需要已确认不复权价格及股数单位的历史日线")
    source = str(bundle.get("source") or bundle.get("kline_source") or "unknown")
    valuations = daily_basic or {}
    previous_date = None
    history = []
    output = []
    for raw in bundle.get("candles") or []:
        stamp = _number(raw.get("t"))
        try:
            day = market_date(stamp) if stamp is not None else None
        except (ValueError, OverflowError, OSError):
            day = None
        values = {key: _number(raw.get(key)) for key in ("o", "h", "l", "c", "v")}
        if (day is None or any(value is None for value in values.values())
                or min(values[k] for k in ("o", "h", "l", "c")) <= 0 or values["v"] < 0
                or values["h"] < max(values["o"], values["c"], values["l"])
                or values["l"] > min(values["o"], values["c"])
                or (previous_date is not None and day <= previous_date)):
            raise ValueError("历史 K 线含无效价格、重复日期或日期顺序错误")
        previous_date = day
        if day > end:
            break
        row = {"t": market_timestamp(datetime.combine(day, datetime.min.time())), "date": day.isoformat(), **values}
        prev_close = _number(raw.get("pre_close"))
        if prev_close is None and history:
            prev_close = history[-1]["c"]
        pct = _number(raw.get("change_pct"))
        row["change_pct"] = pct if pct is not None else (100 * (row["c"] / prev_close - 1) if prev_close and prev_close > 0 else None)
        row["amplitude_pct"] = _number(raw.get("amplitude_pct"))
        if row["amplitude_pct"] is None and prev_close and prev_close > 0:
            row["amplitude_pct"] = 100 * (row["h"] - row["l"]) / prev_close
        amount = _number(raw.get("amount"))
        row["amount"] = amount if amount is not None and amount >= 0 else None
        row["turnover_rate"] = _number(raw.get("turnover_rate"))
        prior_volumes = [bar["v"] for bar in history[-5:]]
        average = sum(prior_volumes) / 5 if len(prior_volumes) == 5 else None
        row["volume_ratio"] = row["v"] / average if average and average > 0 else None
        daily = valuations.get(row["date"], {})
        market_cap = _number(daily.get("market_cap"))
        market_cap = market_cap if market_cap is not None and market_cap > 0 else None
        pe = _number(daily.get("pe_ttm", raw.get("pe_ttm")))
        pe = pe if pe is not None and pe > 0 else None
        if _number(daily.get("turnover_rate")) is not None:
            row["turnover_rate"] = _number(daily["turnover_rate"])
        fundamentals_source = daily.get("source") or source
        profit_info = _report_ttm(reports or [], day)
        if profit_info:
            profit_metric = _metric(profit_info.pop("value"), "CNY", "Tushare income", status="calculated",
                                    note="由当时已公告的合并报表归母净利润计算；报告期为滚动终点。", **profit_info)
        elif market_cap is not None and pe is not None:
            profit_metric = _metric(market_cap / pe, "CNY", fundamentals_source, status="estimated",
                                    note="历史总市值 ÷ 历史 PE(TTM) 估算，受数据源估值与舍入影响；不是原始财报数值。")
        else:
            profit_metric = _metric(None, "CNY", None, note="缺少当时已公告且足以计算 TTM 的财报或历史估值。")
        row.update(market_cap=market_cap, pe_ttm=pe, profit_ttm=profit_metric["value"])
        row["metrics"] = {
            "market_cap": _metric(market_cap, "CNY", fundamentals_source, note=METRIC_DEFINITIONS["market_cap"]),
            "profit_ttm": profit_metric,
            "pe_ttm": _metric(pe, "multiple", fundamentals_source, note=METRIC_DEFINITIONS["pe_ttm"]),
            "volume_ratio": _metric(row["volume_ratio"], "multiple", source, status="calculated", note=METRIC_DEFINITIONS["volume_ratio"]),
            "volume": _metric(row["v"], "shares", source),
            "amount": _metric(row["amount"], "CNY", source),
            "turnover_rate": _metric(row["turnover_rate"], "percent", fundamentals_source),
        }
        history.append(row)
        if day >= start:
            output.append(row)
    if not output:
        raise ValueError("所选区间没有可用历史日线，请调整日期或数据源")
    warnings = list(bundle.get("warnings") or [])
    if any(row["market_cap"] is None or row["pe_ttm"] is None for row in output):
        warnings.append("部分历史市值或 PE 缺失；可在设置中配置具有每日指标权限的 Tushare，缺失项保持为空。")
    if any(row["metrics"]["profit_ttm"]["status"] == "estimated" for row in output):
        warnings.append("部分盈利由历史市值与 PE 估算，表格中会标注“估算”；不作为财报原值。")
    warnings.append("日线为不复权价格；除权除息可能出现跳空。数据源历史修订未审计，适用于复盘练习。")
    return {"symbol": symbol, "name": str(bundle.get("name") or symbol), "interval": "1d", "candles": output,
            "source": source, "price_basis": "raw", "volume_unit": "shares", "currency": "CNY",
            "requested_start": start.isoformat(), "requested_end": end.isoformat(),
            "available_start": output[0]["date"], "available_end": output[-1]["date"],
            "warnings": list(dict.fromkeys(warnings)), "metric_definitions": METRIC_DEFINITIONS,
            "served_from_cache": False, "cache_stale": False}


async def _eastmoney_history(symbol: str, start: date, end: date) -> dict:
    params = {"secid": eastmoney_market.yahoo_symbol_to_secid(symbol), "ut": eastmoney_market.UT,
              "fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
              "klt": "101", "fqt": "0", "beg": start.strftime("%Y%m%d"), "end": end.strftime("%Y%m%d"), "lmt": "5000"}
    async with httpx.AsyncClient(timeout=httpx.Timeout(10, connect=5), headers=eastmoney_market._EM_HEADERS) as client:
        response = await eastmoney_market._get_with_retry(client, eastmoney_market.KLINE_URL, params, attempts=2)
        payload = response.json()
    data = payload.get("data") or {}
    lines = data.get("klines") or []
    candles = eastmoney_market._parse_kline_lines(lines, None)
    # These are per-date historical fields, never values from today's quote.
    extra = {}
    for line in lines:
        fields = line.split(",")
        if len(fields) >= 9:
            extra[fields[0][:10]] = {"amplitude_pct": _number(fields[7]), "change_pct": _number(fields[8])}
    for row in candles:
        row.update(extra.get(market_date(row["t"]).isoformat(), {}))
    if not candles:
        raise ReplayDataUnavailable("东方财富未返回所选区间的历史日线")
    return {"candles": candles, "name": data.get("name"), "source": "东方财富历史日线",
            "price_basis": "raw", "volume_unit": "shares"}


def _baostock_history(symbol: str, start: date, end: date) -> dict:
    bs = kline_baostock._ensure_baostock()
    fields = "date,open,high,low,close,volume,amount,adjustflag,turn,peTTM,pctChg,preclose,tradestatus"
    rows = kline_baostock._query_all(bs.query_history_k_data_plus(
        kline_baostock._yahoo_to_bs_code(symbol), fields, start_date=start.isoformat(), end_date=end.isoformat(),
        frequency="d", adjustflag="3"))
    # Suspended sessions are not steps in a trader's daily replay.
    rows = [row for row in rows if len(row) >= 13 and row[12] != "0"]
    candles = kline_baostock._rows_to_daily_candles(rows)
    by_day = {row[0]: row for row in rows}
    for candle in candles:
        row = by_day[market_date(candle["t"]).isoformat()]
        candle.update(turnover_rate=_number(row[8]), pe_ttm=_number(row[9]),
                      change_pct=_number(row[10]), pre_close=_number(row[11]))
    if not candles:
        raise ReplayDataUnavailable("Baostock 未返回所选区间的历史日线")
    return {"candles": candles, "source": "Baostock 历史日线", "price_basis": "raw", "volume_unit": "shares"}


def _tushare_history(symbol: str, start: date, end: date) -> dict:
    pro = tushare_service._get_pro()
    if pro is None:
        raise ReplayDataUnavailable("未配置 Tushare")
    code = tushare_service.yahoo_symbol_to_ts_code(symbol)
    params = {"ts_code": code, "start_date": start.strftime("%Y%m%d"), "end_date": end.strftime("%Y%m%d")}
    frame = pro.daily(**params)
    if frame is None or frame.empty:
        raise ReplayDataUnavailable("Tushare 未返回所选区间的日线")
    candles = []
    for row in frame.to_dict("records"):
        day = _date(row.get("trade_date"))
        if not day or row.get("ts_code") != code:
            continue
        candles.append({"t": market_timestamp(datetime.combine(day, datetime.min.time())),
                        **{key: _number(row.get(field)) for key, field in (("o", "open"), ("h", "high"), ("l", "low"), ("c", "close"))},
                        "v": (_number(row.get("vol")) * 100 if _number(row.get("vol")) is not None else None),
                        "amount": (_number(row.get("amount")) * 1000 if _number(row.get("amount")) is not None else None),
                        "change_pct": _number(row.get("pct_chg")), "pre_close": _number(row.get("pre_close"))})
    # Official units: daily vol is lots, amount is thousand CNY;
    # daily_basic total_mv is ten-thousand CNY (doc_id=27, doc_id=32).
    return {"candles": sorted(candles, key=lambda row: row["t"]), "source": "Tushare daily",
            "price_basis": "raw", "volume_unit": "shares"}


def _tushare_fundamentals(symbol: str, start: date, end: date) -> dict:
    pro = tushare_service._get_pro()
    if pro is None:
        return {"daily_basic": {}, "reports": [], "warnings": []}
    code = tushare_service.yahoo_symbol_to_ts_code(symbol)
    params = {"ts_code": code, "start_date": start.strftime("%Y%m%d"), "end_date": end.strftime("%Y%m%d")}
    daily, reports, warnings = {}, [], []
    try:
        frame = pro.daily_basic(**params, fields="ts_code,trade_date,total_mv,pe_ttm,turnover_rate")
        for row in ([] if frame is None else frame.to_dict("records")):
            day = _date(row.get("trade_date"))
            if day and row.get("ts_code") == code:
                cap = _number(row.get("total_mv"))
                daily[day.isoformat()] = {"market_cap": cap * 10000 if cap is not None else None,
                                         "pe_ttm": _number(row.get("pe_ttm")), "turnover_rate": _number(row.get("turnover_rate")),
                                         "source": "Tushare daily_basic"}
    except Exception:
        warnings.append("Tushare 历史每日指标暂不可用，请检查接口权限；对应指标留空。")
    try:
        frame = pro.income(ts_code=code, start_date=(start - timedelta(days=800)).strftime("%Y%m%d"),
                           end_date=end.strftime("%Y%m%d"), report_type="1",
                           fields="ts_code,ann_date,f_ann_date,end_date,report_type,n_income_attr_p")
        reports = [row for row in ([] if frame is None else frame.to_dict("records")) if row.get("ts_code") == code]
        # A single ticker's 10-year replay plus TTM warmup normally needs about
        # 50 consolidated quarterly records. Gate unusual large/revised result
        # sets conservatively: gateway-specific limits are not guaranteed.
        if len(reports) >= 100:
            warnings.append("财报返回记录较多，历史覆盖可能受数据接口行数限制；不足以组成 TTM 的时期保持为空或标注估算。")
    except Exception:
        warnings.append("Tushare 历史财报暂不可用；盈利仅在历史估值齐备时标注为估算。")
    return {"daily_basic": daily, "reports": reports, "warnings": warnings}


async def fetch_replay(symbol: str, start_date: date | None = None, end_date: date | None = None, *, user_id=0, refresh=False) -> dict:
    symbol, start, end, request_warnings = normalize_request(symbol, start_date, end_date)
    cache_symbol = f"REPLAY_{user_id}_{symbol}"
    cache_range = f"{start.isoformat()}_{end.isoformat()}"
    cached = kline_bundle_cache.load_bundle(cache_symbol, cache_range, "1d")
    if cached and not refresh:
        try:
            saved = datetime.fromisoformat(cached["saved_at"])
            age = (datetime.now(saved.tzinfo) - saved).total_seconds()
            if 0 <= age < 86400 and cached["payload"].get("price_basis") == "raw":
                return {**cached["payload"], "served_from_cache": True, "cache_stale": False, "cache_saved_at": cached["saved_at"],
                        "warnings": list(dict.fromkeys(cached["payload"].get("warnings", []) + request_warnings))}
        except (KeyError, ValueError, TypeError):
            pass
    warmup = start - timedelta(days=60)
    configured = tushare_service.is_configured()
    providers = []
    if configured:
        providers.append(("Tushare", lambda: asyncio.to_thread(_tushare_history, symbol, warmup, end)))
    providers.append(("东方财富", lambda: _eastmoney_history(symbol, warmup, end)))
    if kline_baostock.is_baostock_installed() and kline_sdk_enabled("baostock"):
        providers.append(("Baostock", lambda: asyncio.to_thread(_baostock_history, symbol, warmup, end)))
    bundle, failures = None, []
    for label, fetch in providers:
        try:
            candidate = await asyncio.wait_for(fetch(), timeout=24)
            build_replay(candidate, symbol, start, end)  # Validate before accepting a provider.
            bundle = candidate
            break
        except Exception:
            failures.append(label)
    if bundle is None:
        if cached and cached.get("payload", {}).get("candles") and cached["payload"].get("price_basis") == "raw":
            out = dict(cached["payload"])
            return {**out, "served_from_cache": True, "cache_stale": True, "cache_saved_at": cached.get("saved_at"),
                    "warnings": list(dict.fromkeys(out.get("warnings", []) + request_warnings + ["在线历史行情暂不可用，正在使用上次保存的复盘数据。"]))}
        raise ReplayDataUnavailable("所选区间历史行情暂不可用（" + "、".join(failures) + "），请稍后重试或检查行情源设置。")
    fundamentals = {"daily_basic": {}, "reports": [], "warnings": []}
    if configured:
        try:
            fundamentals = await asyncio.wait_for(asyncio.to_thread(_tushare_fundamentals, symbol, warmup, end), timeout=25)
        except Exception:
            fundamentals["warnings"].append("历史估值/财报请求超时；缺失指标保持为空。")
    bundle["warnings"] = list(bundle.get("warnings", [])) + fundamentals["warnings"] + request_warnings
    output = build_replay(bundle, symbol, start, end, daily_basic=fundamentals["daily_basic"], reports=fundamentals["reports"])
    try:
        kline_bundle_cache.save_bundle(cache_symbol, cache_range, "1d", output)
    except OSError:
        pass
    return output
