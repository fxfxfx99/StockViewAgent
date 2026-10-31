"""Public market series with explicit provider identities and quotation units.

Sources: Yahoo world-indices and commodities directories, and provider quote
pages in each entry. Futures are rolling nearby contract series, not spot prices,
ETFs or total-return indices. Provider rolls/adjustments may differ.
"""
from __future__ import annotations

from copy import deepcopy
from urllib.parse import quote


def _yahoo(symbol: str) -> list[dict]:
    return [
        {"id": "yahoo", "symbol": symbol, "url": f"https://finance.yahoo.com/quote/{quote(symbol, safe='')}/"},
        {"id": "yahoo_query2", "symbol": symbol, "url": f"https://query2.finance.yahoo.com/v8/finance/chart/{quote(symbol, safe='')}"},
    ]


def _cn(key: str, label: str, code: str, exchange: str) -> dict:
    sh = exchange == "SSE"
    symbol = f"{code}.{'SS' if sh else 'SZ'}"
    return dict(key=key, label=label, region="CN", asset_class="equity_index", exchange=exchange,
                currency="CNY", unit="点", timezone="Asia/Shanghai", symbol=symbol,
                providers=[
                    {"id": "eastmoney", "symbol": f"{1 if sh else 0}.{code}", "url": f"https://quote.eastmoney.com/zs{code}.html"},
                    {"id": "tencent", "symbol": f"{'sh' if sh else 'sz'}{code}", "url": "https://gu.qq.com/"},
                    {"id": "sina", "symbol": f"{'sh' if sh else 'sz'}{code}", "url": "https://finance.sina.com.cn/"},
                    {"id": "baostock", "symbol": f"{'sh' if sh else 'sz'}.{code}", "url": "http://www.baostock.com/"},
                    *_yahoo(symbol)[:1],
                ], intervals=["1d", "1wk", "1mo"], volume_unit="股")


def _us(key: str, label: str, symbol: str, exchange: str, stooq: str | None = None) -> dict:
    providers = _yahoo(symbol)
    if stooq:
        providers.append({"id": "stooq", "symbol": stooq,
                          "url": f"https://stooq.com/q/?s={quote(stooq, safe='')}"})
    return dict(key=key, label=label, region="US", asset_class="equity_index", exchange=exchange,
                currency="USD", unit="点", timezone="America/New_York", symbol=symbol,
                providers=providers, intervals=["1d", "1wk", "1mo"], volume_unit="股")


def _future(key: str, label: str, symbol: str, exchange: str, unit: str) -> dict:
    return dict(key=key, label=label, region="GLOBAL", asset_class="futures", exchange=exchange,
                currency="USD", unit=unit, timezone="America/New_York", symbol=symbol,
                providers=_yahoo(symbol), intervals=["1d", "1wk", "1mo"], volume_unit="合约",
                series_type="nearby_continuous", price_adjustment="provider_defined",
                description="近月连续合约，会随到期换月；价格不是现货，换月和复权方式依数据源而异。")


MARKET_CATALOG: dict[str, dict] = {row["key"]: row for row in [
    _cn("SH_COMP", "上证指数", "000001", "SSE"),
    _cn("SZ_COMP", "深证成指", "399001", "SZSE"),
    _cn("HS300", "沪深300", "000300", "SSE"),
    _cn("STAR50", "科创50", "000688", "SSE"),
    _cn("CYB", "创业板指", "399006", "SZSE"),
    _cn("SSE50", "上证50", "000016", "SSE"),
    _us("US_NYSE", "纽交所综合指数", "^NYA", "NYSE"),
    _us("US_NASDAQ", "纳斯达克综合指数", "^IXIC", "NASDAQ", "^ndq"),
    _us("US_AMEX", "NYSE American 综合指数", "^XAX", "NYSE American"),
    _us("US_DJI", "道琼斯工业指数", "^DJI", "S&P DJI", "^dji"),
    _us("US_SP500", "标普500", "^GSPC", "S&P DJI", "^spx"),
    _us("US_NDX", "纳斯达克100", "^NDX", "NASDAQ"),
    _us("US_RUT", "罗素2000", "^RUT", "FTSE Russell"),
    _future("FUT_GOLD", "黄金期货（近月连续）", "GC=F", "COMEX", "美元/金衡盎司"),
    _future("FUT_SILVER", "白银期货（近月连续）", "SI=F", "COMEX", "美元/金衡盎司"),
    _future("FUT_WTI", "WTI原油期货（近月连续）", "CL=F", "NYMEX", "美元/桶"),
    _future("FUT_BRENT", "布伦特原油金融期货（近月连续）", "BZ=F", "NYMEX", "美元/桶"),
    _future("FUT_NATGAS", "天然气期货（近月连续）", "NG=F", "NYMEX", "美元/百万英热单位"),
    _future("FUT_COPPER", "铜期货（近月连续）", "HG=F", "COMEX", "美元/磅"),
]}


for _instrument in MARKET_CATALOG.values():
    _instrument["requires_token"] = False


def list_market_catalog() -> list[dict]:
    """Copies prevent a response consumer from mutating future requests."""
    return deepcopy(list(MARKET_CATALOG.values()))


def get_market_instrument(key: str) -> dict | None:
    return deepcopy(MARKET_CATALOG.get((key or "").strip().upper()))
