"""统一数据中心控制面：数据域、来源策略、持久化和更新状态。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.config import settings
from app.services import market_source_policy, startup_data_check
from app.storage import data_asset_manager, market_source_cache, news_store

DOMAINS: list[dict[str, Any]] = [
    {"id": "stock_universe", "name": "A股标的库", "primary": "东方财富", "fallbacks": ["百度", "新浪"], "ttl_hours": 24, "storage": "a_share_stocks.json"},
    {"id": "market_kline", "name": "行情与K线", "primary": "公开行情链", "fallbacks": ["Baostock", "Tushare", "Pytdx"], "ttl_hours": 12, "storage": "kline_bundle_cache/"},
    {"id": "capital_flow", "name": "资金流与市场扩展", "primary": "东方财富数据中心", "fallbacks": ["本地历史缓存"], "ttl_hours": 12, "storage": "market_history_cache/"},
    {"id": "news", "name": "新闻与快讯", "primary": "RSS/公开快讯", "fallbacks": ["本地新闻库"], "ttl_hours": 6, "storage": "news.db"},
    {"id": "company_profile", "name": "公司资料与财务", "primary": "东方财富F10/本地CSV", "fallbacks": ["本地公司数据库"], "ttl_hours": 720, "storage": "company_fundamentals.db"},
    {"id": "macro", "name": "宏观与行业", "primary": "Tushare/官方公开数据", "fallbacks": ["最近成功缓存"], "ttl_hours": 24, "storage": "tushare_daily_cache/"},
    {"id": "research", "name": "研究、策略与信号", "primary": "本系统分析流水线", "fallbacks": ["本地模板"], "ttl_hours": 0, "storage": "*.db"},
]


def _domain_asset_stats(items: list[dict[str, Any]], domain_id: str) -> dict[str, Any]:
    mapping = {
        "stock_universe": {"stock_universe"}, "market_kline": {"kline_bundle_cache", "market_source_cache"},
        "capital_flow": {"market_history_cache"}, "news": {"sqlite_db", "news_source_state"},
        "company_profile": {"company_profile_cache"}, "macro": {"tushare_daily_cache"},
        "research": {"kline_insights"},
    }
    selected = [x for x in items if x.get("kind") in mapping.get(domain_id, set())]
    if domain_id == "news": selected = [x for x in selected if x.get("key") in {"news", "cn_headline_sources"}]
    if domain_id == "research": selected += [x for x in items if x.get("key") in {"research_agent", "strategy_knowledge", "decision_signals", "quant"}]
    latest = max((str(x.get("updated_at") or "") for x in selected), default="")
    return {"files": len(selected), "size_bytes": sum(int(x.get("size_bytes") or 0) for x in selected), "last_saved_at": latest or None}


def snapshot() -> dict[str, Any]:
    assets = data_asset_manager.scan_data_assets()
    items = assets.get("items") or []
    startup = startup_data_check.load_state()
    domains = [{**d, "assets": _domain_asset_stats(items, d["id"])} for d in DOMAINS]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "startup_policy": {"enabled": settings.enable_startup_data_check, "background": True, "delay_sec": settings.startup_data_check_delay_sec},
        "refresh_running": startup_data_check.is_refresh_running(),
        "last_startup_refresh": startup,
        "domains": domains,
        "source_policy": market_source_policy.snapshot(),
        "source_health": market_source_cache.load_health(),
        "storage": {"totals": assets.get("totals"), "by_kind": assets.get("by_kind"), "managed_catalog": assets.get("catalog")},
        "news_archive": news_store.stats(),
    }
