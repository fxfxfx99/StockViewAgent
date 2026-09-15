"""基本面快照：A 股 Tushare basic+daily；全球 Yahoo fundamentals；缺失则明确标注。"""
from __future__ import annotations

from typing import Any


def analyze_fundamentals(bundle: dict[str, Any]) -> dict[str, Any]:
    pack = bundle.get("tushare_pack") or {}
    basic = pack.get("basic") or {}
    daily = pack.get("daily_recent") or []
    yf = bundle.get("yahoo_fundamentals") or {}
    metrics = bundle.get("kline_metrics") or {}

    lines: list[str] = []

    if basic:
        lines.append(
            f"A 股基础信息（Tushare stock_basic）: 名称相关字段 {basic.get('name') or '—'}；"
            f"行业 {basic.get('industry') or '未披露'}；上市日期 {basic.get('list_date') or '未披露'}。"
        )
    elif yf.get("trailing_pe") is not None or yf.get("market_cap") is not None:
        lines.append(
            f"市场快照（Yahoo）: 市值口径数据存在但细节未完全展开；"
            f"营收同比增速字段 revenue_growth={yf.get('revenue_growth')}（无则未披露）。"
        )
    else:
        lines.append("未获取结构化基本面库（Tushare 未配置或非 A 股无扩展字段），财务深度分析受限。")

    if daily:
        lines.append("最近若干交易日日线（Tushare daily，节选）:")
        for row in daily[:5]:
            lines.append(
                f"  日期 {row.get('trade_date')}: 收 {row.get('close')} "
                f"额 {row.get('amount') or '—'} 换手 {row.get('turnover_rate') or '—'}"
            )
    else:
        if metrics.get("latest_volume") is not None:
            lines.append(
                f"行情侧量能: 最近成交量 {metrics.get('latest_volume')}；"
                f"换手率 {metrics.get('latest_turnover_rate') or '未计算'}。"
            )

    pe_ttm = metrics.get("pe_ttm")
    if pe_ttm is not None:
        lines.append(f"行情快照含市盈率 TTM（东财/链路透传）: 约 {pe_ttm}（请以交易所披露为准）。")

    return {
        "narrative": "\n".join(lines),
        "basic_fields": basic,
        "daily_recent": daily,
        "yahoo_fundamentals": yf,
        "pe_ttm_quote": pe_ttm,
    }
