"""可配置的 A 股数据源策略。

默认策略参考 a-stock-data / adata 的多源思路：公开 HTTP 源与本地/SDK 源分层，失败后按不同风控面降级。
"""
from __future__ import annotations

from typing import Any

from app.config import settings
from app.storage.integrations_store import load_integrations

DEFAULT_KLINE_DAILY_CHAIN = (
    "eastmoney",
    "tencent",
    "baidu",
    "free_stockdb",
    "baostock",
    "sina",
    "klineshare",
    "tushare",
    "pytdx",
)
DEFAULT_KLINE_MINUTE_CHAIN = (
    "eastmoney",
    "tencent_minute",
    "baostock_minute",
    "pytdx_minute",
)
DEFAULT_STOCK_UNIVERSE_CHAIN = (
    "eastmoney",
    "baidu",
    "sina",
)

_ALIASES = {
    "em": "eastmoney",
    "eastmoney_clist": "eastmoney",
    "qq": "tencent",
    "tencent_fqkline": "tencent",
    "baidu_gushitong": "baidu",
    "stockdb": "free_stockdb",
    "free-stockdb": "free_stockdb",
    "tencent_mkline": "tencent_minute",
    "qq_minute": "tencent_minute",
    "baostock_kline": "baostock",
    "baostock_m": "baostock_minute",
    "pytdx_kline": "pytdx",
    "tushare_pro": "tushare",
    "klineshare_api": "klineshare",
    "pytdx_m": "pytdx_minute",
}

_KNOWN = {
    *DEFAULT_KLINE_DAILY_CHAIN,
    *DEFAULT_KLINE_MINUTE_CHAIN,
    *DEFAULT_STOCK_UNIVERSE_CHAIN,
}


def _split_chain(raw: Any, default: tuple[str, ...]) -> list[str]:
    if isinstance(raw, str):
        items = [x.strip() for x in raw.replace(";", ",").split(",")]
    elif isinstance(raw, (list, tuple)):
        items = [str(x).strip() for x in raw]
    else:
        items = []
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        key = _ALIASES.get(item.lower(), item.lower())
        if key in _KNOWN and key not in seen:
            out.append(key)
            seen.add(key)
    return out or list(default)


def _setting(name: str, default: str = "") -> str:
    return str(getattr(settings, name, default) or "").strip()


def _chain(key: str, env_name: str, default: tuple[str, ...]) -> list[str]:
    data = load_integrations()
    return _split_chain(data.get(key) or _setting(env_name), default)


def _inject_optional_provider(chain: list[str]) -> list[str]:
    """按用户选择的行情提供商，将 KlineShare / Tushare 提前到公开源之后。"""
    from app.services import klineshare_service, tushare_service

    provider = klineshare_service.effective_market_data_provider()
    out = [x for x in chain if x not in {"klineshare", "tushare"}]
    if provider == "klineshare" and klineshare_service.is_configured():
        if out and out[0] == "eastmoney":
            return [out[0], "klineshare"] + out[1:]
        return ["klineshare"] + out
    if provider == "tushare" and tushare_service.is_configured():
        if out and out[0] == "eastmoney":
            return [out[0], "tushare"] + out[1:]
        return ["tushare"] + out
    return out


def kline_daily_chain() -> list[str]:
    return _inject_optional_provider(
        _chain("market_kline_daily_chain", "market_kline_daily_chain", DEFAULT_KLINE_DAILY_CHAIN)
    )


def kline_minute_chain() -> list[str]:
    return _chain("market_kline_minute_chain", "market_kline_minute_chain", DEFAULT_KLINE_MINUTE_CHAIN)


def stock_universe_chain() -> list[str]:
    return _chain("market_stock_universe_chain", "market_stock_universe_chain", DEFAULT_STOCK_UNIVERSE_CHAIN)


def snapshot() -> dict[str, Any]:
    return {
        "kline_daily_chain": kline_daily_chain(),
        "kline_minute_chain": kline_minute_chain(),
        "stock_universe_chain": stock_universe_chain(),
        "override_keys": [
            "MARKET_KLINE_DAILY_CHAIN / integrations.market_kline_daily_chain",
            "MARKET_KLINE_MINUTE_CHAIN / integrations.market_kline_minute_chain",
            "MARKET_STOCK_UNIVERSE_CHAIN / integrations.market_stock_universe_chain",
        ],
        "known_sources": sorted(_KNOWN),
    }
