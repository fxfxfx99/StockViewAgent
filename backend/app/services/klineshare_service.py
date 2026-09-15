"""KlineShare 行情 API（可选接入，见 https://data.klineshare.cn/docs）。"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any

import httpx

from app.config import settings

_DEFAULT_BASE = "https://data.klineshare.cn"
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_RANGE_ROWS = {"3mo": 80, "6mo": 150, "1y": 280, "2y": 550, "5y": 1300, "10y": 2600, "max": 5000}
_INTERVAL_PERIOD = {
    "1d": "1d",
    "daily": "1d",
    "day": "1d",
    "1wk": "1w",
    "1w": "1w",
    "week": "1w",
    "1mo": "1M",
    "1m": "1M",
    "month": "1M",
}


def effective_api_base() -> str:
    from app.storage.integrations_store import load_integrations

    data = load_integrations()
    b = (data.get("klineshare_api_base") or settings.klineshare_api_base or _DEFAULT_BASE).strip()
    return b.rstrip("/") or _DEFAULT_BASE


def effective_api_key() -> str:
    from app.security.user_context import current_user_id
    from app.storage.integrations_store import load_integrations
    from app.storage.user_credentials_store import load_user_credentials

    uid = current_user_id.get()
    if uid:
        personal = (load_user_credentials(uid).get("klineshare_api_key") or "").strip()
        if personal:
            return personal
    data = load_integrations()
    k = (data.get("klineshare_api_key") or "").strip()
    if k:
        return k
    return (settings.klineshare_api_key or "").strip()


def effective_market_data_provider() -> str:
    """klineshare | tushare | public | 空字符串表示尚未选择。"""
    from app.security.user_context import current_user_id
    from app.storage.user_credentials_store import load_user_credentials

    uid = current_user_id.get()
    if not uid:
        return ""
    raw = (load_user_credentials(uid).get("market_data_provider") or "").strip().lower()
    if raw in {"klineshare", "tushare", "public"}:
        return raw
    return ""


def is_configured() -> bool:
    return bool(effective_api_key())


def should_use_in_chain() -> bool:
    return effective_market_data_provider() == "klineshare" and is_configured()


def yahoo_symbol_to_klineshare(symbol: str) -> str | None:
    """600519.SS -> 600519.SH；000001.SZ -> 000001.SZ；920xxx.BJ -> 920xxx.BJ"""
    s = (symbol or "").strip().upper()
    if "." not in s:
        return None
    code, suf = s.rsplit(".", 1)
    code = code.zfill(6)
    if not code.isdigit():
        return None
    if suf in ("SS", "SH"):
        return f"{code}.SH"
    if suf == "SZ":
        return f"{code}.SZ"
    if suf == "BJ":
        return f"{code}.BJ"
    return None


def _num(v: Any) -> float | None:
    try:
        if v is None:
            return None
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return None
        return x
    except (TypeError, ValueError):
        return None


def _parse_ts(v: Any) -> int | None:
    if v is None:
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    if n > 1e12:
        return n // 1000
    if n > 1e9:
        return n
    return None


def _request(
    path: str,
    *,
    params: dict[str, Any] | None = None,
    require_key: bool = False,
) -> tuple[Any | None, str | None]:
    base = effective_api_base()
    url = f"{base}{path}"
    headers = {"User-Agent": _UA, "Accept": "application/json"}
    key = effective_api_key()
    if require_key and not key:
        return None, "未配置 KlineShare API Key"
    if key:
        headers["X-API-Key"] = key
    try:
        with httpx.Client(timeout=25.0, follow_redirects=True) as client:
            r = client.get(url, params=params, headers=headers)
            text = r.text
    except httpx.HTTPError as e:
        return None, str(e)
    try:
        data = r.json()
    except ValueError:
        return None, f"KlineShare 返回非 JSON（HTTP {r.status_code}）"
    if isinstance(data, dict):
        if data.get("success") is False:
            msg = (data.get("message") or data.get("error_description") or "请求失败")[:300]
            code = data.get("code") or data.get("error_code")
            return None, f"KlineShare {code}: {msg}" if code else msg
        if not r.is_success:
            return None, f"KlineShare HTTP {r.status_code}"
        return data, None
    if not r.is_success:
        return None, f"KlineShare HTTP {r.status_code}"
    return data, None


def health_check() -> tuple[bool, str]:
    data, err = _request("/v1/health", require_key=False)
    if err:
        return False, err
    if isinstance(data, dict) and data.get("success"):
        return True, "ok"
    return True, "reachable"


def test_connection() -> dict[str, Any]:
    ok, msg = health_check()
    out: dict[str, Any] = {
        "ok": ok and is_configured(),
        "health_ok": ok,
        "configured": is_configured(),
        "api_base": effective_api_base(),
        "message": msg,
    }
    if not is_configured():
        out["ok"] = False
        out["message"] = "请先配置 KlineShare API Key（用户中心申请）"
        return out
    sym = "600519.SH"
    data, err = _request(
        "/v1/kline",
        params={"symbol": sym, "period": "1d", "count": 3},
        require_key=True,
    )
    if err:
        out["ok"] = False
        out["message"] = err
        return out
    out["ok"] = True
    out["message"] = "连接成功"
    out["sample_symbol"] = sym
    block = data.get("data") if isinstance(data, dict) else None
    if isinstance(block, dict):
        klines = block.get("klines") or block.get("items") or block.get("list") or []
        if isinstance(klines, list):
            out["sample_rows"] = len(klines)
    return out


def _extract_klines(data: Any) -> list[Any]:
    if not isinstance(data, dict):
        return []
    block = data.get("data")
    if isinstance(block, dict):
        for key in ("klines", "items", "list", "candles"):
            val = block.get(key)
            if isinstance(val, list):
                return val
    for key in ("klines", "items", "list"):
        val = data.get(key)
        if isinstance(val, list):
            return val
    return []


def _row_to_candle(row: Any) -> dict[str, Any] | None:
    if isinstance(row, dict):
        ts = _parse_ts(row.get("timestamp") or row.get("time") or row.get("t") or row.get("date"))
        if ts is None and row.get("trade_date"):
            d = str(row.get("trade_date"))
            if len(d) == 8:
                try:
                    ts = int(datetime.strptime(d, "%Y%m%d").timestamp())
                except ValueError:
                    ts = None
        o = _num(row.get("open") or row.get("o"))
        h = _num(row.get("high") or row.get("h"))
        l = _num(row.get("low") or row.get("l"))
        c = _num(row.get("close") or row.get("c"))
        v = _num(row.get("volume") or row.get("vol") or row.get("v"))
        if ts is None or c is None:
            return None
        return {"t": ts, "o": o, "h": h, "l": l, "c": c, "v": v}
    if isinstance(row, (list, tuple)) and len(row) >= 5:
        ts = _parse_ts(row[0])
        if ts is None:
            return None
        return {
            "t": ts,
            "o": _num(row[1]),
            "h": _num(row[2]),
            "l": _num(row[3]),
            "c": _num(row[4]),
            "v": _num(row[5]) if len(row) > 5 else None,
        }
    return None


def fetch_kline_bundle(yahoo_symbol: str, range_param: str = "1y", interval: str = "1d") -> dict[str, Any]:
    """标准化为本系统 K 线 bundle（日线/周/月）。"""
    if not is_configured():
        raise RuntimeError("KlineShare API Key 未配置")
    ks_sym = yahoo_symbol_to_klineshare(yahoo_symbol)
    if not ks_sym:
        raise ValueError("无法转换为 KlineShare 代码")
    period = _INTERVAL_PERIOD.get((interval or "1d").lower())
    if not period:
        raise ValueError(f"KlineShare 暂不支持周期 {interval}")
    count = _RANGE_ROWS.get(range_param, 280)
    data, err = _request(
        "/v1/kline",
        params={"symbol": ks_sym, "period": period, "count": min(count, 5000)},
        require_key=True,
    )
    if err:
        raise RuntimeError(err)
    raw_rows = _extract_klines(data)
    candles: list[dict[str, Any]] = []
    for row in raw_rows:
        c = _row_to_candle(row)
        if c:
            candles.append(c)
    candles.sort(key=lambda x: x["t"])
    if len(candles) < 2:
        # 部分套餐走 v2
        data2, err2 = _request(
            "/v2/kline",
            params={"symbol": ks_sym, "period": period, "count": min(count, 5000)},
            require_key=True,
        )
        if not err2:
            for row in _extract_klines(data2):
                c = _row_to_candle(row)
                if c:
                    candles.append(c)
            candles.sort(key=lambda x: x["t"])
    if len(candles) < 2:
        raise ValueError("KlineShare K 线数据不足")
    return {
        "symbol": yahoo_symbol.upper(),
        "interval": interval if interval in {"1wk", "1mo"} else "1d",
        "range": range_param,
        "candles": candles,
        "kline_source": "klineshare",
        "data_as_of": candles[-1]["t"],
    }
