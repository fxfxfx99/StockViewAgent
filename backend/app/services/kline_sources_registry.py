"""K 线数据源备案（与 docs/KLINE_DATA_SOURCES.md 一致，供 API 与代码只读引用）。"""

from __future__ import annotations

# 日/周/月兜底顺序（kline_source 值）
DAILY_FALLBACK_CHAIN: tuple[str, ...] = (
    "eastmoney",
    "tencent_fallback",
    "free_stockdb_local",
    "baostock_fallback",
    "sina_fallback",
    "pytdx_fallback",
)

# 分钟线兜底顺序
MINUTE_FALLBACK_CHAIN: tuple[str, ...] = (
    "eastmoney",
    "tencent_mkline_fallback",
    "baostock_minute_fallback",
    "pytdx_minute_fallback",
)

KLINE_REGISTRY: dict[str, object] = {
    "daily_plus_intervals": ("1d", "5d", "1wk", "1mo"),
    "minute_intervals": ("1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h"),
    "daily_fallback_chain": DAILY_FALLBACK_CHAIN,
    "minute_fallback_chain": MINUTE_FALLBACK_CHAIN,
    "docs_path": "backend/docs/KLINE_DATA_SOURCES.md",
    "references": {
        "pytdx": "https://pytdx-docs.readthedocs.io/zh-cn/latest/",
        "baostock_index": "http://www.baostock.com/mainContent?file=indexData.md",
        "eastmoney_kline": "https://push2his.eastmoney.com/api/qt/stock/kline/get",
        "free_stockdb_local": "http://127.0.0.1:7899/?cmd=get&t=日k:600519:20260701<20260715（可选本地服务）",
    },
}
