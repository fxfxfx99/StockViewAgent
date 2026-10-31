"""
市场研判（宏观与行业）：Tushare Pro 指数/外汇/现货金/国际指数等 K 线数据。
需配置 TUSHARE_TOKEN（或 integrations.json tushare_token）。

接口与字段参考（与平台文档一致）：
- 指数日线 index_daily — doc_id=95（指数行情）；指数列表 index_basic — doc_id=94
- 外汇日线 fx_daily — doc_id=179
- 上海金现货 sge_daily — doc_id=285
- 国际指数 index_global — doc_id=211（布伦特等，积分要求较高）
- 申万行业日线 sw_daily — doc_id=327（单日可拉全市场指数 ts_code/name 作下拉列表）
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from app.config import settings
from app.services.market_time import SHANGHAI_TZ, market_timestamp
from app.services.tushare_service import _cell, _get_pro, is_configured
from app.storage.tushare_daily_cache import (
    fresh_payload,
    load_entry,
    merge_stale_indices,
    merge_stale_kline,
    save_payload,
)

MACRO_CACHE_TTL_SECONDS = 15 * 60
SW_KLINE_CACHE_TTL_SECONDS = 15 * 60
SW_INDICES_CACHE_TTL_SECONDS = 60 * 60

_RANGE_DAYS: dict[str, int] = {
    "3mo": 92,
    "6mo": 184,
    "1y": 370,
    "2y": 740,
    "5y": 1850,
}

# 展示用 id -> Tushare 调用说明（ts_code / 接口名）
MACRO_SERIES: list[dict[str, Any]] = [
    {
        "id": "gold_sge",
        "name": "上海金 Au99.99",
        "short": "黄金",
        "api": "sge_daily",
        "ts_code": "Au99.99",
        "tushare_doc_id": 285,
        "note": "元/克；现货日行情",
    },
    {
        "id": "usdcnh",
        "name": "美元/人民币（离岸）",
        "short": "USD/CNH",
        "api": "fx_daily",
        "ts_code": "USDCNH.FXCM",
        "tushare_doc_id": 179,
        "note": "FXCM 报价；bid_* 作 OHLC",
    },
    {
        "id": "sh",
        "name": "上证综指",
        "short": "上证",
        "api": "index_daily",
        "ts_code": "000001.SH",
        "tushare_doc_id": 95,
        "index_basic_doc_id": 94,
    },
    {
        "id": "sz",
        "name": "深证成指",
        "short": "深证",
        "api": "index_daily",
        "ts_code": "399001.SZ",
        "tushare_doc_id": 95,
        "index_basic_doc_id": 94,
    },
    {
        "id": "cyb",
        "name": "创业板指",
        "short": "创业板",
        "api": "index_daily",
        "ts_code": "399006.SZ",
        "tushare_doc_id": 95,
        "index_basic_doc_id": 94,
    },
    {
        "id": "brent",
        "name": "布伦特原油（国际指数）",
        "short": "布伦特",
        "api": "index_global",
        "ts_code": "BRENT",
        "tushare_doc_id": 211,
        "note": "index_global；需较高积分，若报错请查阅 doc_id=211",
    },
]

_SERIES_BY_ID: dict[str, dict[str, Any]] = {x["id"]: x for x in MACRO_SERIES}


def catalog() -> list[dict[str, Any]]:
    return list(MACRO_SERIES)


def _trade_date_to_ts(s: str) -> int:
    """YYYYMMDD -> 上海日历日 12:00 的时间戳（与现有 K 线展示一致）。"""
    if not re.fullmatch(r"\d{8}", s):
        raise ValueError("trade_date 须为有效的 YYYYMMDD 日期")
    dt = datetime.strptime(s, "%Y%m%d").replace(hour=12, minute=0, second=0)
    return market_timestamp(dt)


def _range(value: str) -> str:
    result = (value or "1y").strip().lower()
    if result not in _RANGE_DAYS:
        raise ValueError(f"range 须为 {', '.join(_RANGE_DAYS)} 之一")
    return result


def _text(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def _nonnegative(value: Any) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else 0.0
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _provider_error(error: Exception) -> str:
    """Provider exceptions sometimes include URLs or the request token."""
    message = str(error).lower()
    if any(term in message for term in ("token", "权限", "积分", "permission", "unauthorized")):
        return "Tushare 请求失败，请检查 Token、积分与接口权限"
    if any(term in message for term in ("timeout", "timed out", "超时")):
        return "Tushare 请求超时，请稍后重试"
    return "Tushare 请求失败或返回数据不可用，请稍后重试"


def _sample(df: pd.DataFrame) -> dict[str, Any]:
    result = {}
    for key, value in df.iloc[-1].to_dict().items():
        value = _cell(value)
        if value is None or pd.isna(value):
            value = None
        elif isinstance(value, float) and not math.isfinite(value):
            value = None
        result[str(key)] = value
    return result


def _df_to_candles(df: pd.DataFrame, kind: str) -> list[dict[str, Any]]:
    if df is None or df.empty or "trade_date" not in df.columns:
        return []
    out: dict[int, dict[str, Any]] = {}
    for _, row in df.iterrows():
        try:
            ts = _trade_date_to_ts(_text(row.get("trade_date")))
        except (ValueError, TypeError, OverflowError, OSError):
            continue
        if kind == "fx":
            o = row.get("bid_open")
            h = row.get("bid_high")
            low = row.get("bid_low")
            c = row.get("bid_close")
        else:
            o, h, low, c = row.get("open"), row.get("high"), row.get("low"), row.get("close")
        try:
            o, h, low, c = float(o), float(h), float(low), float(c)
        except (TypeError, ValueError, OverflowError):
            continue
        if not all(math.isfinite(value) and value > 0 for value in (o, h, low, c)):
            continue
        if low > min(o, c) or h < max(o, c) or low > h:
            continue
        out[ts] = {
                "t": ts,
                "o": round(o, 6),
                "h": round(h, 6),
                "l": round(low, 6),
                "c": round(c, 6),
                "v": int(_nonnegative(row.get("vol"))),
                "amount": round(_nonnegative(row.get("amount")), 2),
            }
    return [out[stamp] for stamp in sorted(out)]


def _fetch_macro_kline_live(meta: dict[str, Any], range_param: str) -> dict[str, Any]:
    days = _RANGE_DAYS[_range(range_param)]
    end = datetime.now(SHANGHAI_TZ)
    start = end - timedelta(days=days)
    start_s = start.strftime("%Y%m%d")
    end_s = end.strftime("%Y%m%d")

    pro = _get_pro()
    if pro is None:
        raise ValueError("Tushare 初始化失败")

    api = meta["api"]
    code = meta["ts_code"]
    df: pd.DataFrame | None = None
    eff_code = code
    try:
        if api == "index_daily":
            df = pro.index_daily(ts_code=code, start_date=start_s, end_date=end_s)
        elif api == "fx_daily":
            df = pro.fx_daily(ts_code=code, start_date=start_s, end_date=end_s)
        elif api == "sge_daily":
            df = pro.sge_daily(ts_code=code, start_date=start_s, end_date=end_s)
        elif api == "index_global":
            last_err: Exception | None = None
            for cand in dict.fromkeys((code, "UKOIL", "BRENT")):
                try:
                    tdf = pro.index_global(ts_code=cand, start_date=start_s, end_date=end_s)
                    if tdf is not None and not tdf.empty:
                        df = tdf
                        eff_code = cand
                        break
                except Exception as e:
                    last_err = e
                    continue
            if df is None and last_err is not None:
                raise last_err
        else:
            raise ValueError(f"未实现的 api: {api}")
    except Exception as e:
        raise ValueError(_provider_error(e)) from None

    if df is None or df.empty:
        return {
            "series_id": meta["id"],
            "name": meta["name"],
            "ts_code": code,
            "tushare_api": api,
            "range": range_param,
            "candles": [],
            "raw_fields": [],
            "warning": "该区间无返回数据（可能停牌、权限不足或代码变更）",
        }

    kind = "fx" if api == "fx_daily" else "ohlc"
    candles = _df_to_candles(df, kind)
    raw_cols = [str(c) for c in df.columns.tolist()]
    sample_json = _sample(df)

    return {
        "series_id": meta["id"],
        "name": meta["name"],
        "short": meta.get("short"),
        "ts_code": eff_code,
        "tushare_api": api,
        "tushare_doc_id": meta.get("tushare_doc_id"),
        "index_basic_doc_id": meta.get("index_basic_doc_id"),
        "note": meta.get("note"),
        "range": range_param,
        "candles": candles,
        "raw_fields": raw_cols,
        "last_bar_sample": sample_json,
    }


def fetch_macro_kline(series_id: str, range_param: str = "1y") -> dict[str, Any]:
    meta = _SERIES_BY_ID.get((series_id or "").strip().lower())
    if not meta:
        raise ValueError(f"未知 series_id，可选：{list(_SERIES_BY_ID.keys())}")

    rng = _range(range_param)
    cache_key = f"{meta['id']}_{rng}"
    token = (settings.effective_tushare_token or "").strip()
    ent = load_entry("macro_kline", cache_key)
    has_cache = bool(ent and (ent.get("payload") or {}).get("candles"))

    if not is_configured():
        if has_cache:
            return merge_stale_kline(ent, current_token="", fetch_error=None, no_token=True)
        raise ValueError("未配置 Tushare：请在控制台 → API 密钥中保存当前账户的 Token")

    cached = fresh_payload(ent, current_token=token, ttl_seconds=MACRO_CACHE_TTL_SECONDS) if has_cache else None
    if cached is not None:
        return cached
    try:
        out = _fetch_macro_kline_live(meta, rng)
        out["cache_stale"] = False
        out["cache_hit"] = False
        out["cache_ttl_seconds"] = MACRO_CACHE_TTL_SECONDS
        if not out.get("candles"):
            if has_cache:
                return merge_stale_kline(ent, current_token=token, fetch_error="本次未返回有效行情，保留最近成功缓存", no_token=False)
            out.setdefault("warning", "该区间未返回有效行情")
            return out
        save_payload("macro_kline", cache_key, out, token)
        return out
    except Exception as e:
        err = _provider_error(e)
        if has_cache:
            return merge_stale_kline(ent, current_token=token, fetch_error=err, no_token=False)
        raise ValueError(err) from None


def _find_recent_sw_daily_trade_date(pro: Any) -> str:
    """向前回溯若干自然日，找到 sw_daily 有数据的一日（用于拉取当日全市场行业指数列表）。"""
    d = datetime.now(SHANGHAI_TZ)
    for _ in range(14):
        td = d.strftime("%Y%m%d")
        try:
            df = pro.sw_daily(trade_date=td, fields="ts_code,name")
            if df is not None and not df.empty:
                return td
        except Exception as e:
            # Credentials/network failures do not improve by issuing the same
            # paid request for another 44 dates.
            raise ValueError(_provider_error(e)) from None
        d -= timedelta(days=1)
    raise ValueError("无法取得申万行业列表：近期无返回数据")


def fetch_sw_daily_indices(trade_date: str | None = None) -> dict[str, Any]:
    """单日 sw_daily 返回全部行业指数 ts_code + name，供前端下拉。"""
    td_in = (trade_date or "").strip()
    if td_in:
        try:
            _trade_date_to_ts(td_in)
        except (ValueError, TypeError, OverflowError, OSError):
            raise ValueError("trade_date 须为有效的 YYYYMMDD 日期") from None
    cache_key = td_in if td_in else "latest"
    token = (settings.effective_tushare_token or "").strip()
    ent = load_entry("sw_indices", cache_key)
    has_cache = bool(ent and (ent.get("payload") or {}).get("items"))

    if not is_configured():
        if has_cache:
            return merge_stale_indices(ent, current_token="", fetch_error=None, no_token=True)
        raise ValueError("未配置 Tushare：请在控制台 → API 密钥中保存当前账户的 Token")

    cached = fresh_payload(ent, current_token=token, ttl_seconds=SW_INDICES_CACHE_TTL_SECONDS) if has_cache else None
    if cached is not None:
        return cached
    try:
        pro = _get_pro()
        if pro is None:
            raise ValueError("Tushare 初始化失败")
        td = td_in
        if not td:
            td = _find_recent_sw_daily_trade_date(pro)
        df = pro.sw_daily(trade_date=td, fields="ts_code,name")
        by_code: dict[str, dict[str, str]] = {}
        if df is not None and not df.empty:
            for _, row in df.iterrows():
                code = _text(row.get("ts_code")).upper()
                name = _text(row.get("name"))
                if re.fullmatch(r"\d{6}\.SI", code):
                    by_code[code] = {"ts_code": code, "name": name or code}
        items = [by_code[code] for code in sorted(by_code)]
        out = {"trade_date": td, "items": items, "tushare_doc_id": 327,
               "cache_stale": False, "cache_hit": False, "cache_ttl_seconds": SW_INDICES_CACHE_TTL_SECONDS}
        if not items:
            if has_cache:
                return merge_stale_indices(ent, current_token=token, fetch_error="本次未返回有效行业列表，保留最近成功缓存", no_token=False)
            out["warning"] = "该日无有效申万行业指数数据"
            return out
        save_payload("sw_indices", cache_key, out, token)
        return out
    except Exception as e:
        err = _provider_error(e)
        if has_cache:
            return merge_stale_indices(ent, current_token=token, fetch_error=err, no_token=False)
        raise ValueError(err) from None


def fetch_sw_daily_kline(ts_code: str, range_param: str = "1y") -> dict[str, Any]:
    """申万行业指数日线 K 线（sw_daily，按 ts_code + 区间）。"""
    code = (ts_code or "").strip().upper()
    if not re.fullmatch(r"\d{6}\.SI", code):
        raise ValueError("ts_code 须为六位申万行业指数代码.SI，例如 801010.SI")
    rng = _range(range_param)
    cache_key = f"{code}_{rng}"
    token = (settings.effective_tushare_token or "").strip()
    ent = load_entry("sw_daily_kline", cache_key)
    has_cache = bool(ent and (ent.get("payload") or {}).get("candles"))

    if not is_configured():
        if has_cache:
            return merge_stale_kline(ent, current_token="", fetch_error=None, no_token=True)
        raise ValueError("未配置 Tushare：请在控制台 → API 密钥中保存当前账户的 Token")

    cached = fresh_payload(ent, current_token=token, ttl_seconds=SW_KLINE_CACHE_TTL_SECONDS) if has_cache else None
    if cached is not None:
        return cached
    try:
        days = _RANGE_DAYS[rng]
        end = datetime.now(SHANGHAI_TZ)
        start = end - timedelta(days=days)
        start_s = start.strftime("%Y%m%d")
        end_s = end.strftime("%Y%m%d")

        pro = _get_pro()
        if pro is None:
            raise ValueError("Tushare 初始化失败")
        df = pro.sw_daily(ts_code=code, start_date=start_s, end_date=end_s)
        if df is None or df.empty:
            out = {
                "ts_code": code,
                "name": code,
                "tushare_api": "sw_daily",
                "tushare_doc_id": 327,
                "range": rng,
                "candles": [],
                "raw_fields": [],
                "warning": "该区间无返回数据（权限不足、代码错误或暂无行情）",
            }
            out["cache_stale"] = False
            out["cache_hit"] = False
            out["cache_ttl_seconds"] = SW_KLINE_CACHE_TTL_SECONDS
            if has_cache:
                return merge_stale_kline(ent, current_token=token, fetch_error="本次未返回有效行情，保留最近成功缓存", no_token=False)
            return out

        candles = _df_to_candles(df, "ohlc")
        raw_cols = [str(c) for c in df.columns.tolist()]
        sample_json = _sample(df)
        name = ""
        if "name" in df.columns and len(df):
            try:
                name = _text(df.iloc[-1].get("name"))
            except Exception:
                name = ""

        out = {
            "ts_code": code,
            "name": name or code,
            "tushare_api": "sw_daily",
            "tushare_doc_id": 327,
            "range": rng,
            "candles": candles,
            "raw_fields": raw_cols,
            "last_bar_sample": sample_json,
        }
        out["cache_stale"] = False
        out["cache_hit"] = False
        out["cache_ttl_seconds"] = SW_KLINE_CACHE_TTL_SECONDS
        if not candles:
            if has_cache:
                return merge_stale_kline(ent, current_token=token, fetch_error="本次未返回有效行情，保留最近成功缓存", no_token=False)
            out["warning"] = "该区间未返回有效行情"
            return out
        save_payload("sw_daily_kline", cache_key, out, token)
        return out
    except Exception as e:
        err = _provider_error(e)
        if has_cache:
            return merge_stale_kline(ent, current_token=token, fetch_error=err, no_token=False)
        raise ValueError(err) from None
