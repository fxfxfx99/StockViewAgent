"""策略知识采集与管理。

合规约束：
- 公开网页仅保存短摘录和结构化笔记，不复制整篇文章；
- 书籍、付费研报仅登记书目/链接/人工摘要，不保存版权正文；
- 所有来源保留 URL、publisher、策略标签与抓取状态，便于审计和更新。
"""
from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any

import httpx

from app.config import settings
from app.storage import strategy_knowledge_store as store

_MAX_EXCERPT_CHARS = 6000
_HTTP_TIMEOUT = httpx.Timeout(25.0, connect=8.0)
_UA = "StockViewAgent/1.0 strategy knowledge updater (+local research assistant)"


PUBLIC_SOURCES: list[dict[str, Any]] = [
    {
        "id": "investopedia_trend_trading_indicators",
        "title": "Trend Trading Indicators",
        "url": "https://www.investopedia.com/articles/active-trading/041814/four-most-commonlyused-indicators-trend-trading.asp",
        "source_type": "web_article",
        "publisher": "Investopedia",
        "language": "en",
        "strategy_tags": ["中期趋势（右侧）", "波段交易"],
        "priority": 80,
        "copyright_note": "Public web article; store metadata and short excerpt only.",
    },
    {
        "id": "investopedia_mean_reversion",
        "title": "Mean Reversion",
        "url": "https://www.investopedia.com/terms/m/meanreversion.asp",
        "source_type": "web_article",
        "publisher": "Investopedia",
        "language": "en",
        "strategy_tags": ["均值回归"],
        "priority": 80,
        "copyright_note": "Public web article; store metadata and short excerpt only.",
    },
    {
        "id": "investopedia_value_investing",
        "title": "Value Investing",
        "url": "https://www.investopedia.com/terms/v/valueinvesting.asp",
        "source_type": "web_article",
        "publisher": "Investopedia",
        "language": "en",
        "strategy_tags": ["价值投资"],
        "priority": 80,
        "copyright_note": "Public web article; store metadata and short excerpt only.",
    },
    {
        "id": "ishares_quality_investing",
        "title": "What is Quality Investing?",
        "url": "https://www.ishares.com/us/investor-education/investment-strategies/what-is-quality-investing",
        "source_type": "web_article",
        "publisher": "iShares",
        "language": "en",
        "strategy_tags": ["质量成长"],
        "priority": 78,
        "copyright_note": "Public web page; store metadata and short excerpt only.",
    },
    {
        "id": "investopedia_dividend_yield",
        "title": "Dividend Yield",
        "url": "https://www.investopedia.com/terms/d/dividendyield.asp",
        "source_type": "web_article",
        "publisher": "Investopedia",
        "language": "en",
        "strategy_tags": ["高股息红利"],
        "priority": 78,
        "copyright_note": "Public web article; store metadata and short excerpt only.",
    },
    {
        "id": "investopedia_event_driven",
        "title": "Event-Driven Strategy",
        "url": "https://www.investopedia.com/terms/e/eventdriven.asp",
        "source_type": "web_article",
        "publisher": "Investopedia",
        "language": "en",
        "strategy_tags": ["事件驱动"],
        "priority": 76,
        "copyright_note": "Public web article; store metadata and short excerpt only.",
    },
    {
        "id": "investopedia_momentum",
        "title": "Momentum",
        "url": "https://www.investopedia.com/terms/m/momentum.asp",
        "source_type": "web_article",
        "publisher": "Investopedia",
        "language": "en",
        "strategy_tags": ["短线交易", "资金共振", "中期趋势（右侧）"],
        "priority": 76,
        "copyright_note": "Public web article; store metadata and short excerpt only.",
    },
    {
        "id": "investopedia_obv",
        "title": "On-Balance Volume",
        "url": "https://www.investopedia.com/terms/o/onbalancevolume.asp",
        "source_type": "web_article",
        "publisher": "Investopedia",
        "language": "en",
        "strategy_tags": ["资金共振"],
        "priority": 76,
        "copyright_note": "Public web article; store metadata and short excerpt only.",
    },
    {
        "id": "wikipedia_sector_rotation",
        "title": "Sector rotation",
        "url": "https://en.wikipedia.org/wiki/Sector_rotation",
        "source_type": "encyclopedia",
        "publisher": "Wikipedia",
        "language": "en",
        "strategy_tags": ["行业轮动"],
        "priority": 68,
        "copyright_note": "CC BY-SA source; retain URL and use short excerpt/summary.",
    },
    {
        "id": "wikipedia_factor_investing",
        "title": "Factor investing",
        "url": "https://en.wikipedia.org/wiki/Factor_investing",
        "source_type": "encyclopedia",
        "publisher": "Wikipedia",
        "language": "en",
        "strategy_tags": ["价值投资", "质量成长", "短线交易", "中期趋势（右侧）"],
        "priority": 68,
        "copyright_note": "CC BY-SA source; retain URL and use short excerpt/summary.",
    },
    {
        "id": "wikipedia_quality_investing",
        "title": "Quality investing",
        "url": "https://en.wikipedia.org/wiki/Quality_investing",
        "source_type": "encyclopedia",
        "publisher": "Wikipedia",
        "language": "en",
        "strategy_tags": ["质量成长", "价值投资"],
        "priority": 75,
        "copyright_note": "CC BY-SA source; retain URL and use short excerpt/summary.",
    },
    {
        "id": "wikipedia_trend_following",
        "title": "Trend following",
        "url": "https://en.wikipedia.org/wiki/Trend_following",
        "source_type": "encyclopedia",
        "publisher": "Wikipedia",
        "language": "en",
        "strategy_tags": ["中期趋势（右侧）", "波段交易"],
        "priority": 68,
        "copyright_note": "CC BY-SA source; retain URL and use short excerpt/summary.",
    },
    {
        "id": "arxiv_dynamic_momentum_learning",
        "title": "Trend-Following Strategies via Dynamic Momentum Learning",
        "url": "https://arxiv.org/abs/2106.08420",
        "source_type": "research_paper",
        "publisher": "arXiv",
        "authors": "Bruno P. C. Levy, Hedibert F. Lopes",
        "year": "2021",
        "language": "en",
        "strategy_tags": ["中期趋势（右侧）", "短线交易", "波段交易"],
        "priority": 88,
        "copyright_note": "arXiv abstract/page; store metadata and short excerpt only.",
    },
    {
        "id": "arxiv_factor_momentum",
        "title": "Is Factor Momentum More than Stock Momentum?",
        "url": "https://arxiv.org/abs/2009.04824",
        "source_type": "research_paper",
        "publisher": "arXiv",
        "authors": "Antoine Falck, Adam Rej, David Thesmar",
        "year": "2020",
        "language": "en",
        "strategy_tags": ["短线交易", "中期趋势（右侧）", "行业轮动"],
        "priority": 88,
        "copyright_note": "arXiv abstract/page; store metadata and short excerpt only.",
    },
    {
        "id": "arxiv_value_quant_convergence",
        "title": "Quant Convergence: Bridging Classical Value Investing and Modern Factor Models",
        "url": "https://arxiv.org/abs/2606.24575",
        "source_type": "research_paper",
        "publisher": "arXiv",
        "authors": "Augusto Eiji Yamazaki, Hugo Garrido-Lestache Belinchon",
        "year": "2026",
        "language": "en",
        "strategy_tags": ["价值投资", "质量成长"],
        "priority": 86,
        "copyright_note": "arXiv abstract/page; store metadata and short excerpt only.",
    },
    {
        "id": "arxiv_style_allocation_2026",
        "title": "Continuous Timing Signals for Growth-Defensive Style Allocation",
        "url": "https://arxiv.org/abs/2605.20636",
        "source_type": "research_paper",
        "publisher": "arXiv",
        "authors": "Zheli Xiong",
        "year": "2026",
        "language": "en",
        "strategy_tags": ["行业轮动", "质量成长", "价值投资"],
        "priority": 84,
        "copyright_note": "arXiv abstract/page; store metadata and short excerpt only.",
    },
    {
        "id": "arxiv_ai_premium_2026",
        "title": "AI Premium",
        "url": "https://arxiv.org/abs/2606.30583",
        "source_type": "research_paper",
        "publisher": "arXiv",
        "authors": "Nicola Borri, Yukun Liu, Aleh Tsyvinski",
        "year": "2026",
        "language": "en",
        "strategy_tags": ["产业趋势（主题成长）", "质量成长", "行业轮动"],
        "priority": 88,
        "copyright_note": "arXiv abstract/page; store metadata and short excerpt only.",
    },
    {
        "id": "brookings_china_ai_strategy_2026",
        "title": "Competing AI strategies for the US and China",
        "url": "https://www.brookings.edu/articles/competing-ai-strategies-for-the-us-and-china/",
        "source_type": "research_article",
        "publisher": "Brookings Institution",
        "year": "2026",
        "language": "en",
        "strategy_tags": ["产业趋势（主题成长）", "行业轮动"],
        "priority": 78,
        "copyright_note": "Public web article; store metadata and short excerpt only.",
    },
    {
        "id": "barings_china_2026_themes",
        "title": "Hong Kong & China Stock Markets: Investment Case for 2026",
        "url": "https://www.barings.com/en-us/guest/perspectives/viewpoints/hong-kong-china-stock-markets-investment-case-for-2026-publicequities-vwpt",
        "source_type": "market_outlook",
        "publisher": "Barings",
        "year": "2026",
        "language": "en",
        "strategy_tags": ["产业趋势（主题成长）", "行业轮动", "价值投资"],
        "priority": 74,
        "copyright_note": "Public web outlook; store metadata and short excerpt only.",
    },
    {
        "id": "wikipedia_momentum_investing",
        "title": "Momentum investing",
        "url": "https://en.wikipedia.org/wiki/Momentum_investing",
        "source_type": "encyclopedia",
        "publisher": "Wikipedia",
        "language": "en",
        "strategy_tags": ["短线交易", "中期趋势（右侧）"],
        "priority": 66,
        "copyright_note": "CC BY-SA source; retain URL and use short excerpt/summary.",
    },
    {
        "id": "aqr_understanding_factor_investing",
        "title": "Understanding Factor Investing",
        "url": "https://funds.aqr.com/Insights/Strategies/Understanding-Factor-Investing",
        "source_type": "research_article",
        "publisher": "AQR Funds",
        "language": "en",
        "strategy_tags": ["中期趋势（右侧）", "价值投资", "质量成长", "短线交易", "行业轮动"],
        "priority": 82,
        "copyright_note": "Public educational page; store metadata and short excerpt only.",
    },
    {
        "id": "aqr_fact_fiction_momentum",
        "title": "Fact, Fiction and Momentum Investing",
        "url": "https://www.aqr.com/Insights/Research/Journal-Article/Fact-Fiction-and-Momentum-Investing",
        "source_type": "research_article",
        "publisher": "AQR Capital Management",
        "authors": "Cliff Asness, Andrea Frazzini, Ronen Israel, Tobias J. Moskowitz",
        "year": "2014",
        "language": "en",
        "strategy_tags": ["中期趋势（右侧）", "短线交易", "波段交易"],
        "priority": 88,
        "copyright_note": "Public research landing page; store metadata and short excerpt only.",
    },
    {
        "id": "aqr_value_momentum_everywhere",
        "title": "Value and Momentum Everywhere",
        "url": "https://www.aqr.com/Insights/Research/Journal-Article/Value-and-Momentum-Everywhere",
        "source_type": "research_article",
        "publisher": "AQR Capital Management",
        "year": "2013",
        "language": "en",
        "strategy_tags": ["价值投资", "中期趋势（右侧）", "短线交易", "行业轮动"],
        "priority": 86,
        "copyright_note": "Public research landing page; store metadata and short excerpt only.",
    },
    {
        "id": "ssga_factor_based_investing",
        "title": "Factor-based investing: Targeting the real drivers of return",
        "url": "https://www.ssga.com/au/en_gb/intermediary/insights/education/factor-based-investing",
        "source_type": "education",
        "publisher": "State Street Global Advisors",
        "language": "en",
        "strategy_tags": ["价值投资", "质量成长", "中期趋势（右侧）", "高股息红利", "行业轮动"],
        "priority": 80,
        "copyright_note": "Public education page; store metadata and short excerpt only.",
    },
    {
        "id": "ssga_dividend_investing",
        "title": "What is dividend investing? Understanding how it works",
        "url": "https://www.ssga.com/us/en/intermediary/resources/education/what-is-dividend-investing-understanding-how-it-works",
        "source_type": "education",
        "publisher": "State Street Global Advisors",
        "language": "en",
        "strategy_tags": ["高股息红利", "价值投资"],
        "priority": 82,
        "copyright_note": "Public education page; store metadata and short excerpt only.",
    },
    {
        "id": "fidelity_sector_rotation_strategies",
        "title": "Sector Rotation Strategies",
        "url": "https://www.fidelity.com/learning-center/trading-investing/markets-sectors/intro-sector-rotation-strats",
        "source_type": "education",
        "publisher": "Fidelity Investments",
        "language": "en",
        "strategy_tags": ["行业轮动", "产业趋势（主题成长）"],
        "priority": 82,
        "copyright_note": "Public education page; store metadata and short excerpt only.",
    },
    {
        "id": "fidelity_sector_investment_strategies",
        "title": "Sector Investment Strategies",
        "url": "https://www.fidelity.com/sector-investing/sector-investment-strategies",
        "source_type": "education",
        "publisher": "Fidelity Investments",
        "language": "en",
        "strategy_tags": ["行业轮动", "产业趋势（主题成长）"],
        "priority": 76,
        "copyright_note": "Public education page; store metadata and short excerpt only.",
    },
    {
        "id": "cfa_improving_value_investing",
        "title": "Improving the Odds of Value Investing",
        "url": "https://rpc.cfainstitute.org/blogs/enterprising-investor/2021/improving-the-odds-of-value-investing",
        "source_type": "research_article",
        "publisher": "CFA Institute",
        "language": "en",
        "strategy_tags": ["价值投资", "质量成长", "行业轮动"],
        "priority": 82,
        "copyright_note": "Public article; store metadata and short excerpt only.",
    },
    {
        "id": "cfa_factor_breakdown_sectors",
        "title": "What's in a Factor? A Breakdown by Sectors",
        "url": "https://rpc.cfainstitute.org/blogs/enterprising-investor/2018/whats-in-a-factor-a-breakdown-by-sectors",
        "source_type": "research_article",
        "publisher": "CFA Institute",
        "language": "en",
        "strategy_tags": ["价值投资", "质量成长", "中期趋势（右侧）", "行业轮动"],
        "priority": 80,
        "copyright_note": "Public article; store metadata and short excerpt only.",
    },
    {
        "id": "cfa_factor_premiums",
        "title": "Factor Premiums: An Eternal Feature of Financial Markets",
        "url": "https://rpc.cfainstitute.org/blogs/enterprising-investor/2024/factor-premiums-an-eternal-feature-of-financial-markets",
        "source_type": "research_article",
        "publisher": "CFA Institute",
        "language": "en",
        "strategy_tags": ["价值投资", "中期趋势（右侧）", "短线交易", "质量成长"],
        "priority": 80,
        "copyright_note": "Public article; store metadata and short excerpt only.",
    },
    {
        "id": "interactive_brokers_mean_reversion",
        "title": "Mean Reversion Strategies: Introduction, Trading, Strategies and More",
        "url": "https://www.interactivebrokers.com/campus/ibkr-quant-news/mean-reversion-strategies-introduction-trading-strategies-and-more-part-i/",
        "source_type": "education",
        "publisher": "Interactive Brokers Campus",
        "language": "en",
        "strategy_tags": ["均值回归", "短线交易"],
        "priority": 76,
        "copyright_note": "Public education page; store metadata and short excerpt only.",
    },
    {
        "id": "questdb_mean_reversion",
        "title": "Mean Reversion Trading Strategies",
        "url": "https://questdb.com/glossary/mean-reversion-trading-strategies/",
        "source_type": "education",
        "publisher": "QuestDB",
        "language": "en",
        "strategy_tags": ["均值回归"],
        "priority": 70,
        "copyright_note": "Public glossary page; store metadata and short excerpt only.",
    },
    {
        "id": "analystprep_event_driven",
        "title": "Event-Driven Strategies & Merger Arbitrage",
        "url": "https://analystprep.com/study-notes/cfa-level-2/event-driven-strategies-merger-arbitrage/",
        "source_type": "education",
        "publisher": "AnalystPrep",
        "language": "en",
        "strategy_tags": ["事件驱动"],
        "priority": 74,
        "copyright_note": "Public education page; store metadata and short excerpt only.",
    },
    {
        "id": "gmo_event_driven_strategy",
        "title": "Event-Driven Strategy",
        "url": "https://www.gmo.com/americas/product-index-page/alternatives/event-driven-strategy/",
        "source_type": "strategy_overview",
        "publisher": "GMO",
        "language": "en",
        "strategy_tags": ["事件驱动"],
        "priority": 70,
        "copyright_note": "Public strategy overview; store metadata and short excerpt only.",
    },
    {
        "id": "public_swing_trading",
        "title": "What is swing trading?",
        "url": "https://public.com/learn/swing-trading",
        "source_type": "education",
        "publisher": "Public.com",
        "language": "en",
        "strategy_tags": ["波段交易", "短线交易"],
        "priority": 68,
        "copyright_note": "Public education page; store metadata and short excerpt only.",
    },
    {
        "id": "eastmoney_dragon_tiger_board",
        "title": "龙虎榜数据中心",
        "url": "https://data.eastmoney.com/stock/tradedetail.html",
        "source_type": "data_portal",
        "publisher": "东方财富",
        "language": "zh",
        "strategy_tags": ["游资龙头", "事件驱动", "短线交易"],
        "priority": 82,
        "copyright_note": "Public data portal; store metadata and short excerpt only.",
    },
    {
        "id": "eastmoney_institution_seat_tracking",
        "title": "机构席位追踪",
        "url": "https://data.eastmoney.com/stock/jgstatistic.html",
        "source_type": "data_portal",
        "publisher": "东方财富",
        "language": "zh",
        "strategy_tags": ["游资龙头", "公募基金抱团", "北向机构配置"],
        "priority": 78,
        "copyright_note": "Public data portal; store metadata and short excerpt only.",
    },
    {
        "id": "eastmoney_central_huijin_holder",
        "title": "中央汇金投资有限责任公司持股列表",
        "url": "https://data.eastmoney.com/gdfx/shareholder/10061230.html",
        "source_type": "data_portal",
        "publisher": "东方财富",
        "language": "zh",
        "strategy_tags": ["国家队/政策资金"],
        "priority": 84,
        "copyright_note": "Public shareholder data portal; store metadata and short excerpt only.",
    },
    {
        "id": "eastmoney_csf_holder",
        "title": "中国证券金融股份有限公司持股列表",
        "url": "https://data.eastmoney.com/gdfx/shareholder/10196008.html",
        "source_type": "data_portal",
        "publisher": "东方财富",
        "language": "zh",
        "strategy_tags": ["国家队/政策资金"],
        "priority": 84,
        "copyright_note": "Public shareholder data portal; store metadata and short excerpt only.",
    },
    {
        "id": "akshare_fund_hold_and_lhb_catalog",
        "title": "AKShare 股票数据：基金持股与龙虎榜接口目录",
        "url": "https://akshare-hh.readthedocs.io/en/latest/data/stock/stock.html",
        "source_type": "data_documentation",
        "publisher": "AKShare",
        "language": "zh",
        "strategy_tags": ["公募基金抱团", "游资龙头", "北向机构配置"],
        "priority": 80,
        "copyright_note": "Open documentation; store metadata and short excerpt only.",
    },
    {
        "id": "ths_dragon_tiger_hot_money",
        "title": "龙虎榜：知名游资与活跃营业部",
        "url": "https://data.10jqka.com.cn/market/longhu/",
        "source_type": "data_portal",
        "publisher": "同花顺",
        "language": "zh",
        "strategy_tags": ["游资龙头", "短线交易"],
        "priority": 78,
        "copyright_note": "Public data portal; store metadata and short excerpt only.",
    },
    {
        "id": "eastmoney_northbound_flow",
        "title": "东方财富北向资金与资金流向",
        "url": "https://data.eastmoney.com/zjlx/",
        "source_type": "data_portal",
        "publisher": "东方财富",
        "language": "zh",
        "strategy_tags": ["北向机构配置", "资金共振", "公募基金抱团"],
        "priority": 78,
        "copyright_note": "Public data portal; store metadata and short excerpt only.",
    },
    {
        "id": "quant_microstructure_metadata",
        "title": "量化流动性与微观结构代理框架",
        "source_type": "internal_methodology",
        "publisher": "StockViewAgent",
        "language": "zh",
        "strategy_tags": ["量化流动性"],
        "priority": 82,
        "access_policy": "metadata_only",
        "copyright_note": "Internal methodology note; no external copyrighted text.",
    },
    {
        "id": "classic_security_analysis",
        "title": "Security Analysis",
        "source_type": "book_metadata",
        "publisher": "McGraw-Hill",
        "authors": "Benjamin Graham, David Dodd",
        "year": "1934",
        "strategy_tags": ["价值投资"],
        "priority": 72,
        "access_policy": "metadata_only",
        "copyright_note": "Book metadata and strategy notes only; do not store copyrighted full text.",
    },
    {
        "id": "classic_intelligent_investor",
        "title": "The Intelligent Investor",
        "source_type": "book_metadata",
        "publisher": "Harper",
        "authors": "Benjamin Graham",
        "year": "1949",
        "strategy_tags": ["价值投资", "高股息红利"],
        "priority": 72,
        "access_policy": "metadata_only",
        "copyright_note": "Book metadata and strategy notes only; do not store copyrighted full text.",
    },
    {
        "id": "classic_reminiscences_stock_operator",
        "title": "Reminiscences of a Stock Operator",
        "source_type": "book_metadata",
        "publisher": "George H. Doran Company",
        "authors": "Edwin Lefevre",
        "year": "1923",
        "strategy_tags": ["短线交易", "中期趋势（右侧）", "波段交易"],
        "priority": 66,
        "access_policy": "metadata_only",
        "copyright_note": "Book metadata and strategy notes only; check local jurisdiction before storing full text.",
    },
]

BOOK_NOTES: list[dict[str, Any]] = [
    {
        "source_id": "classic_security_analysis",
        "strategy": "价值投资",
        "title": "安全边际与资产质量",
        "summary": "价值策略应把估值、资产质量和偿债安全放在同一个框架内，低估值必须结合业务质量与财务稳健性确认。",
        "key_points": ["安全边际优先于短期预测", "资产负债表质量影响估值折扣", "避免只因便宜而买入低质量资产"],
        "risks": ["会计口径失真", "价值陷阱", "估值修复周期过长"],
        "quality_score": 0.82,
    },
    {
        "source_id": "classic_intelligent_investor",
        "strategy": "价值投资",
        "title": "防御型投资者与情绪纪律",
        "summary": "长期价值判断需要将市场报价视为机会而非指令，避免被短期价格波动牵引。",
        "key_points": ["市场先生框架强调情绪独立", "分散与安全边际降低永久损失", "估值纪律比预测更重要"],
        "risks": ["过度自信", "忽视组合分散", "把价格下跌误判为便宜"],
        "quality_score": 0.82,
    },
    {
        "source_id": "classic_reminiscences_stock_operator",
        "strategy": "中期趋势（右侧）",
        "title": "顺势与止损纪律",
        "summary": "趋势策略重视跟随已经证明自己的价格方向，并在判断错误时快速退出。",
        "key_points": ["趋势确认后再行动", "亏损时缩小暴露", "避免频繁对抗价格方向"],
        "risks": ["追涨后趋势反转", "缺少止损规则", "把噪声当趋势"],
        "quality_score": 0.72,
    },
    {
        "source_id": "eastmoney_dragon_tiger_board",
        "strategy": "游资龙头",
        "title": "龙虎榜与情绪龙头",
        "summary": "游资策略应把龙虎榜席位、题材强度、成交额放大和隔日承接结合起来，单一大单流入不能证明游资锁仓。",
        "key_points": ["龙虎榜用于验证异动背后的席位结构", "短线龙头需要题材、强度、流动性和承接共同确认", "知名营业部上榜后仍需观察隔日竞价和成交额"],
        "risks": ["高位一致性加速后容易兑现", "席位数据滞后且可能多账户拆分", "弱市接力失败会快速回撤"],
        "quality_score": 0.82,
    },
    {
        "source_id": "eastmoney_central_huijin_holder",
        "strategy": "国家队/政策资金",
        "title": "国家队持仓披露与政策资金边界",
        "summary": "国家队策略应以中央汇金、证金等定期股东披露和宽基ETF异常放量为证据，不能把日内资金流直接等同政策资金买入。",
        "key_points": ["国家队确认通常来自十大股东、ETF持仓或公告", "权重金融、央国企和关键产业链更容易成为观察对象", "政策资金更多体现稳定性和底部托底线索"],
        "risks": ["持仓披露存在季度滞后", "政策托底不等于个股趋势反转", "传闻驱动容易造成追高"],
        "quality_score": 0.84,
    },
    {
        "source_id": "akshare_fund_hold_and_lhb_catalog",
        "strategy": "公募基金抱团",
        "title": "基金重仓与抱团识别",
        "summary": "公募抱团需要基金季报重仓、机构调研、行业景气和大市值趋势共振。未接入持仓明细时只能使用大市值、趋势稳定和机构订单做代理。",
        "key_points": ["基金抱团应以基金持仓集中度和持续增配验证", "大市值、流动性和业绩可解释性是抱团的基础条件", "抱团增强时常表现为趋势平稳、回撤较浅和机构订单持续"],
        "risks": ["季报披露滞后", "抱团拥挤后业绩低预期会触发集中撤退", "代理信号容易把普通趋势股误判为抱团"],
        "quality_score": 0.8,
    },
    {
        "source_id": "eastmoney_northbound_flow",
        "strategy": "北向机构配置",
        "title": "北向配置代理",
        "summary": "北向机构配置应区分市场级北向净流入和个股级持股变动。没有个股持股时，只能作为风险偏好与风格偏好的弱确认。",
        "key_points": ["北向资金适合观察外资风险偏好和大中市值配置", "需结合行业、估值和趋势，而非只看单日净流入", "个股级持股变化比市场级净流入更有解释力"],
        "risks": ["汇率和海外风险偏好会改变北向流向", "市场级数据不能证明个股被买入", "指数权重调整会干扰配置判断"],
        "quality_score": 0.78,
    },
    {
        "source_id": "quant_microstructure_metadata",
        "strategy": "量化流动性",
        "title": "量化流动性代理规则",
        "summary": "量化流动性策略在缺少逐笔和盘口时，只能使用换手、成交额、波动、量价分歧和订单大小结构做代理。",
        "key_points": ["成交活跃但波动可控更适合作为流动性观察对象", "大单与小单同向或背离可提示短周期拥挤度", "量化信号应服务于交易拥挤度和执行风险判断"],
        "risks": ["日线代理无法还原真实高频订单", "高换手可能来自分歧兑现而非量化买入", "盘口结构变化会让日线结论滞后"],
        "quality_score": 0.82,
    },
]


def seed_sources() -> dict[str, Any]:
    for source in PUBLIC_SOURCES:
        store.upsert_source(source)
    for note in BOOK_NOTES:
        source = next((s for s in PUBLIC_SOURCES if s["id"] == note["source_id"]), {})
        store.upsert_item(
            {
                **note,
                "url": source.get("url"),
                "content_excerpt": "",
            }
        )
    return store.status()


def _strip_html(text: str) -> str:
    text = re.sub(r"(?is)<script.*?>.*?</script>", " ", text)
    text = re.sub(r"(?is)<style.*?>.*?</style>", " ", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _summarize_excerpt(text: str, strategy: str, title: str) -> dict[str, Any]:
    lower = text.lower()
    points: list[str] = []
    risks: list[str] = []
    strategy_lower = strategy.lower()
    is_trend_like = strategy in {"中期趋势（右侧）", "短线交易", "波段交易", "资金共振"} or any(
        x in strategy_lower for x in ("趋势", "短线", "波段", "资金")
    )
    is_value_like = strategy in {"价值投资", "高股息红利"} or "价值" in strategy_lower or "红利" in strategy_lower
    is_quality_like = strategy == "质量成长" or "质量" in strategy_lower or "成长" in strategy_lower
    is_sector_like = strategy == "行业轮动" or "行业" in strategy_lower or "轮动" in strategy_lower
    is_theme_like = strategy == "产业趋势（主题成长）" or "主题" in strategy_lower or "产业" in strategy_lower
    is_mean_like = strategy == "均值回归" or "均值" in strategy_lower or "回归" in strategy_lower
    is_event_like = strategy == "事件驱动" or "事件" in strategy_lower or "催化" in strategy_lower
    is_swing_like = strategy == "波段交易" or "波段" in strategy_lower
    is_flow_like = strategy == "资金共振" or "资金" in strategy_lower
    is_fund_crowding_like = strategy == "公募基金抱团" or "公募" in strategy_lower or "抱团" in strategy_lower
    is_policy_like = strategy == "国家队/政策资金" or "国家队" in strategy_lower or "政策" in strategy_lower
    is_hot_money_like = strategy == "游资龙头" or "游资" in strategy_lower or "龙虎榜" in strategy_lower
    is_quant_like = strategy == "量化流动性" or "量化" in strategy_lower or "流动性" in strategy_lower
    is_northbound_like = strategy == "北向机构配置" or "北向" in strategy_lower or "外资" in strategy_lower
    if is_trend_like and any(x in lower for x in ("moving average", "trend", "momentum", "breakout")):
        points.append("趋势/动量类策略应结合价格方向、均线或突破信号确认。")
    if is_trend_like and any(x in lower for x in ("risk", "loss", "drawdown", "crash", "volatility")):
        risks.append("动量和趋势策略需要控制回撤、反转和波动放大的风险。")
    if is_value_like and any(x in lower for x in ("value", "intrinsic", "margin of safety", "valuation")):
        points.append("价值策略关注估值、内在价值与安全边际。")
    if is_quality_like and any(x in lower for x in ("quality", "roe", "debt", "earnings")):
        points.append("质量策略关注盈利能力、负债水平和盈利稳定性。")
    if strategy == "高股息红利" and any(x in lower for x in ("dividend", "yield", "payout")):
        points.append("红利策略需要同时检查股息率、派息率和现金流覆盖。")
    if strategy == "高股息红利" and any(x in lower for x in ("debt", "cash flow", "declining cash", "sustainability")):
        risks.append("红利策略不能只看股息率，要警惕高派息率、负债融资分红和现金流恶化。")
    if is_sector_like and any(x in lower for x in ("sector", "cycle", "rotation")):
        points.append("行业轮动策略需要结合经济周期、相对强弱和资金迁移。")
    if is_theme_like and any(x in lower for x in ("ai", "technology", "innovation", "theme", "policy", "industrial", "productivity")):
        points.append("主题成长策略需要跟踪产业趋势、政策催化、技术扩散和商业化验证。")
    if is_mean_like and any(x in lower for x in ("mean reversion", "z-score", "standard deviation", "bollinger", "average")):
        points.append("均值回归策略应识别相对历史均值的偏离程度，并等待偏离收敛或波动回落。")
    if is_mean_like and any(x in lower for x in ("trend", "stop", "risk", "volatility")):
        risks.append("强趋势或波动状态切换会让均值回归连续失效，需要严格止损和仓位控制。")
    if is_event_like and any(x in lower for x in ("event-driven", "merger", "acquisition", "restructuring", "buyback", "bankruptcy", "corporate event")):
        points.append("事件驱动策略需要明确事件类型、时间窗口、成功概率和失败时的价格回撤。")
    if is_event_like and any(x in lower for x in ("uncertainty", "outcome", "risk", "spread", "failure")):
        risks.append("事件交易收益高度依赖结果兑现，需跟踪公告进展、监管审批和预期差。")
    if is_swing_like and any(x in lower for x in ("swing trading", "risk management", "technical", "short-term", "stop")):
        points.append("波段交易应把入场位置、止损、目标位和持有周期写成计划，避免临盘漂移。")
    if is_swing_like and any(x in lower for x in ("discipline", "risk", "overnight", "volatility")):
        risks.append("波段交易承受隔夜与跳空风险，仓位和止损必须先于入场确定。")
    if is_flow_like and any(x in lower for x in ("flow", "volume", "momentum", "liquidity")):
        points.append("资金共振策略需要同时观察资金流、成交量和价格方向是否同步。")
    if is_fund_crowding_like and any(x in lower for x in ("fund", "holding", "institution", "基金", "持仓", "重仓", "机构")):
        points.append("公募基金抱团需要基金重仓、行业景气、流动性和趋势稳定共同验证。")
    if is_fund_crowding_like:
        risks.append("基金持仓披露滞后，代理信号必须降低置信度并警惕拥挤撤退。")
    if is_policy_like and any(x in lower for x in ("huijin", "central", "shareholder", "汇金", "证金", "国家队", "股东")):
        points.append("国家队/政策资金应优先用汇金、证金、ETF和十大股东披露确认。")
    if is_policy_like:
        risks.append("国家队数据多为定期披露，日内资金流不能直接证明政策资金买入。")
    if is_hot_money_like and any(x in lower for x in ("dragon", "tiger", "龙虎榜", "营业部", "hot money", "游资")):
        points.append("游资龙头策略需要结合龙虎榜席位、题材强度、成交额和隔日承接。")
    if is_hot_money_like:
        risks.append("游资交易高度依赖情绪周期，高位接力失败会快速回撤。")
    if is_quant_like and any(x in lower for x in ("liquidity", "turnover", "volume", "microstructure", "流动性", "换手", "量化")):
        points.append("量化流动性策略需要观察成交活跃度、波动、订单结构和短周期拥挤度。")
    if is_quant_like:
        risks.append("没有逐笔和盘口时，量化信号只能作为低置信代理。")
    if is_northbound_like and any(x in lower for x in ("northbound", "mutual", "沪股通", "深股通", "北向", "外资")):
        points.append("北向机构配置要区分市场级净流入与个股级持股变化。")
    if is_northbound_like:
        risks.append("北向流向受汇率、海外风险偏好和指数权重影响。")
    if not points:
        points.append(f"{strategy} 可从该来源提取定义、信号口径和风险约束。")
    if not risks:
        risks.append("公开资料只提供通用框架，落到 A 股仍需结合交易制度、流动性和数据口径。")
    summary = f"{title} 为 {strategy} 提供公开策略框架，可用于补充信号解释、风险条件和不做条件。"
    return {"summary": summary, "key_points": points[:4], "risks": risks[:3]}


def _strategy_profile(strategy: str) -> dict[str, Any]:
    strategy_lower = strategy.lower()
    labels: list[str] = [strategy]
    if strategy in {"中期趋势（右侧）", "短线交易", "波段交易", "资金共振"} or any(
        x in strategy_lower for x in ("趋势", "短线", "波段", "资金")
    ):
        labels.extend(["趋势", "动量", "突破", "均线", "资金"])
    if strategy in {"价值投资", "高股息红利"} or "价值" in strategy_lower or "红利" in strategy_lower:
        labels.extend(["价值", "估值", "内在价值", "安全边际", "红利", "股息", "现金流"])
    if strategy == "质量成长" or "质量" in strategy_lower or "成长" in strategy_lower:
        labels.extend(["质量", "成长", "盈利", "ROE", "负债", "稳定性"])
    if strategy == "行业轮动" or "行业" in strategy_lower or "轮动" in strategy_lower:
        labels.extend(["行业", "轮动", "周期", "相对强弱"])
    if strategy == "产业趋势（主题成长）" or "主题" in strategy_lower or "产业" in strategy_lower:
        labels.extend(["产业", "主题", "AI", "科技", "创新", "政策", "趋势", "商业化", "催化"])
    if strategy == "均值回归":
        labels.extend(["均值", "回归", "偏离", "修复"])
    if strategy == "事件驱动":
        labels.extend(["事件", "催化", "公告", "窗口"])
    if strategy == "公募基金抱团":
        labels.extend(["公募", "基金", "抱团", "重仓", "机构", "持仓", "调研", "流动性"])
    if strategy == "国家队/政策资金":
        labels.extend(["国家队", "政策", "汇金", "证金", "股东", "ETF", "央企", "金融"])
    if strategy == "游资龙头":
        labels.extend(["游资", "龙虎榜", "营业部", "龙头", "情绪", "接力", "题材"])
    if strategy == "量化流动性":
        labels.extend(["量化", "流动性", "换手", "成交", "波动", "订单", "盘口"])
    if strategy == "北向机构配置":
        labels.extend(["北向", "外资", "沪股通", "深股通", "机构", "配置", "持股"])
    return {"labels": labels}


def _filter_strategy_notes(strategy: str, points: list[str], risks: list[str]) -> tuple[list[str], list[str]]:
    labels = _strategy_profile(strategy)["labels"]

    def keep(text: str) -> bool:
        return any(label and label.lower() in text.lower() for label in labels)

    kept_points = [p for p in points if keep(p)]
    kept_risks = [r for r in risks if keep(r)]
    if not kept_points:
        kept_points = [f"{strategy} 可从该来源提取定义、信号口径和风险约束。"]
    if not kept_risks:
        kept_risks = ["公开资料只提供通用框架，落到 A 股仍需结合交易制度、流动性和数据口径。"]
    return kept_points, kept_risks


def ingest_local_transaction_agent_kb() -> dict[str, Any]:
    seed_sources()
    notes = settings.data_dir.parent / "app" / "resources" / "transaction_agent" / "notes" / "strategy_kb_notes.md"
    count = 0
    store.upsert_source(
        {
            "id": "transaction_agent_local_notes",
            "title": "TransactionAgent local strategy notes",
            "url": str(notes),
            "source_type": "local_notes",
            "publisher": "StockViewAgent TransactionAgent",
            "language": "zh",
            "strategy_tags": [s["name"] for s in _strategy_sources()],
            "priority": 70,
            "copyright_note": "Bundled local strategy notes for Transaction Agent.",
        }
    )
    if notes.exists():
        text = notes.read_text(encoding="utf-8", errors="ignore")
        for source in PUBLIC_SOURCES:
            for strategy in source.get("strategy_tags") or []:
                if strategy not in text:
                    continue
                idx = text.find(f"## {strategy}")
                excerpt = text[idx : idx + 2500] if idx >= 0 else text[:2000]
                store.upsert_item(
                    {
                        "source_id": "transaction_agent_local_notes",
                        "strategy": strategy,
                        "title": f"{strategy} 本地策略笔记",
                        "summary": f"TransactionAgent 本地知识库中整理的 {strategy} 关键概念、A股特化和常见错误。",
                        "key_points": _summarize_excerpt(excerpt, strategy, "本地策略笔记")["key_points"],
                        "risks": _summarize_excerpt(excerpt, strategy, "本地策略笔记")["risks"],
                        "content_excerpt": excerpt[:_MAX_EXCERPT_CHARS],
                        "url": str(notes),
                        "quality_score": 0.7,
                    }
                )
                count += 1
    return {"items_upserted": count, **store.status()}


def _strategy_sources() -> list[dict[str, str]]:
    from app.services.transaction_agent_service import STRATEGIES

    return STRATEGIES


def synthesize_strategy(strategy: str) -> dict[str, Any]:
    seed_sources()
    items = [item for item in store.query_strategy(strategy, limit=12) if item.get("source_id") != "strategy_knowledge_synthesis"]
    source_id = "strategy_knowledge_synthesis"
    store.upsert_source(
        {
            "id": source_id,
            "title": "Strategy knowledge synthesis",
            "source_type": "synthesis",
            "publisher": "StockViewAgent",
            "language": "zh",
            "strategy_tags": [strategy],
            "priority": 90,
            "copyright_note": "Derived notes from registered metadata, local notes and short public excerpts.",
        }
    )
    points: list[str] = []
    risks: list[str] = []
    source_titles: list[str] = []
    for item in items:
        source_titles.append(item.get("source_title") or item.get("title") or "")
        item_points, item_risks = _filter_strategy_notes(strategy, item.get("key_points") or [], item.get("risks") or [])
        for point in item_points:
            if point and point not in points:
                points.append(point)
        for risk in item_risks:
            if risk and risk not in risks:
                risks.append(risk)
    if not points:
        points = [f"{strategy} 暂无足够外部知识，先使用本地 TransactionAgent 规则。"]
    if not risks:
        risks = ["公开资料不足时降低策略置信度，避免过度拟合单一信号。"]
    summary = (
        f"{strategy} 强化摘要：综合 {len(items)} 条知识项，优先用于补充信号解释、"
        "不做条件、风险提示和评分置信度。"
    )
    store.upsert_item(
        {
            "source_id": source_id,
            "strategy": strategy,
            "title": f"{strategy} 强化规则摘要",
            "summary": summary,
            "key_points": points[:8],
            "risks": risks[:5],
            "content_excerpt": "\n".join([f"- {x}" for x in points[:8] + risks[:5]]),
            "quality_score": 0.92 if len(items) >= 3 else 0.72,
        }
    )
    return {
        "strategy": strategy,
        "knowledge_items_used": len(items),
        "source_titles": list(dict.fromkeys(x for x in source_titles if x))[:10],
        "key_points": points[:8],
        "risks": risks[:5],
    }


def strengthen_all_strategies() -> dict[str, Any]:
    out = []
    for s in _strategy_sources():
        out.append(synthesize_strategy(s["name"]))
    return {"items": out, **store.status()}


async def refresh_public_sources(*, limit: int = 8, retry_forbidden: bool = False) -> dict[str, Any]:
    seed_sources()
    run_id = store.start_refresh_run()
    checked = 0
    upserted = 0
    errors: list[dict[str, Any]] = []
    sources = []
    for source in store.list_sources():
        if not source.get("url") or source.get("access_policy") == "metadata_only":
            continue
        if not str(source.get("url") or "").startswith(("http://", "https://")):
            continue
        if (
            not retry_forbidden
            and source.get("status") == "error"
            and "403" in str(source.get("fetch_error") or "")
        ):
            continue
        sources.append(source)
    sources = sources[: max(1, int(limit))]
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, headers={"User-Agent": _UA}, follow_redirects=True) as client:
        for source in sources:
            checked += 1
            try:
                resp = await client.get(source["url"])
                resp.raise_for_status()
                excerpt = _strip_html(resp.text)[:_MAX_EXCERPT_CHARS]
                tags = source.get("strategy_tags") or []
                for strategy in tags:
                    note = _summarize_excerpt(excerpt, strategy, source["title"])
                    store.upsert_item(
                        {
                            "source_id": source["id"],
                            "strategy": strategy,
                            "title": source["title"],
                            "summary": note["summary"],
                            "key_points": note["key_points"],
                            "risks": note["risks"],
                            "content_excerpt": excerpt,
                            "url": source["url"],
                            "quality_score": min(0.9, 0.55 + float(source.get("priority") or 50) / 200.0),
                        }
                    )
                    upserted += 1
                store.mark_source_checked(source["id"], ok=True)
            except Exception as exc:  # noqa: BLE001
                err = str(exc)[:800]
                errors.append({"source_id": source.get("id"), "error": err})
                store.mark_source_checked(source["id"], ok=False, error=err)
    store.finish_refresh_run(
        run_id,
        status_value="ok" if not errors else "warning",
        sources_checked=checked,
        items_upserted=upserted,
        errors=errors,
    )
    synth = strengthen_all_strategies()
    return {
        "run_id": run_id,
        "sources_checked": checked,
        "items_upserted": upserted,
        "synthesized_strategies": len(synth.get("items") or []),
        "errors": errors,
        **store.status(),
    }


def query_strategy_knowledge(strategy: str, *, limit: int = 5) -> list[dict[str, Any]]:
    seed_sources()
    rows = store.query_strategy(strategy, limit=limit)
    for row in rows:
        points, risks = _filter_strategy_notes(strategy, row.get("key_points") or [], row.get("risks") or [])
        row["key_points"] = points
        row["risks"] = risks
    return rows


def status() -> dict[str, Any]:
    seed_sources()
    return store.status()
