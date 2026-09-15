"""TransactionAgent 融合层：用本系统数据接口生成多策略交易观点。

第一版采用可解释的规则化评分，保证无需 LLM/RAG 索引也能稳定运行；
后续可在同一输入 bundle 上接入 TransactionAgent 的 YAML prompt 与 LLM 生成。
"""
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import settings
from app.services import kline_pipeline, market_extra_http, strategy_knowledge_service
from app.storage import company_fundamentals_store, kline_bundle_cache, market_history_cache

STRATEGIES: list[dict[str, str]] = [
    {"id": "trend_following_mid", "name": "中期趋势（右侧）", "timeframe": "1-8周", "style": "趋势跟踪派"},
    {"id": "short_term_trading", "name": "短线交易", "timeframe": "1-5天", "style": "快枪手"},
    {"id": "swing_trading", "name": "波段交易", "timeframe": "1-4周", "style": "波段猎人"},
    {"id": "mean_reversion", "name": "均值回归", "timeframe": "2-10天", "style": "统计套利派"},
    {"id": "value_investing", "name": "价值投资", "timeframe": "6-24个月", "style": "价值投资者"},
    {"id": "quality_growth", "name": "质量成长", "timeframe": "3-12个月", "style": "质量成长派"},
    {"id": "dividend_yield", "name": "高股息红利", "timeframe": "6-24个月", "style": "现金流投资者"},
    {"id": "event_driven", "name": "事件驱动", "timeframe": "1天-3周", "style": "事件套利者"},
    {"id": "flow_confluence", "name": "资金共振", "timeframe": "3天-4周", "style": "资金跟踪者"},
    {"id": "sector_rotation", "name": "行业轮动", "timeframe": "1-8周", "style": "中观配置者"},
    {"id": "thematic_growth", "name": "产业趋势（主题成长）", "timeframe": "2周-6个月", "style": "主题趋势研究员"},
    {"id": "mutual_fund_crowding", "name": "公募基金抱团", "timeframe": "1-6个月", "style": "机构抱团观察者"},
    {"id": "national_team_policy", "name": "国家队/政策资金", "timeframe": "1-12个月", "style": "政策资金跟踪者"},
    {"id": "hot_money_dragon_tiger", "name": "游资龙头", "timeframe": "1-10天", "style": "情绪龙头交易者"},
    {"id": "quant_liquidity", "name": "量化流动性", "timeframe": "1-15天", "style": "微观结构观察者"},
    {"id": "northbound_institution", "name": "北向机构配置", "timeframe": "2周-6个月", "style": "外资配置跟踪者"},
]

_STRATEGY_BY_ID = {s["id"]: s for s in STRATEGIES}
_STRATEGY_BY_NAME = {s["name"]: s for s in STRATEGIES}


def transaction_agent_resource_root() -> Path:
    return settings.data_dir.parent / "app" / "resources" / "transaction_agent"


def resources_status() -> dict[str, Any]:
    root = transaction_agent_resource_root()
    notes = root / "notes" / "strategy_kb_notes.md"
    return {
        "root": str(root),
        "exists": root.exists(),
        "merged": True,
        "notes": str(notes),
        "notes_exists": notes.exists(),
        "mode": "integrated_rule_based_data_aligned",
        "prompt_count": 0,
        "strategies": STRATEGIES,
    }


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return x


def _avg(vals: list[float]) -> float | None:
    vals = [x for x in vals if x is not None]
    return sum(vals) / len(vals) if vals else None


def _pct(a: float | None, b: float | None) -> float | None:
    if a is None or b in (None, 0):
        return None
    return (a - b) / abs(b) * 100.0


def _direction(score: int) -> str:
    if score >= 60:
        return "bullish"
    if score <= 40:
        return "bearish"
    return "neutral"


def _clamp_score(score: float) -> int:
    return int(max(0, min(100, round(score))))


def _safe_json_loads(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


def _compact_record(row: dict[str, Any] | None, *, text_limit: int = 800) -> dict[str, Any] | None:
    if not row:
        return None
    out = dict(row)
    out.pop("raw_json", None)
    text = out.get("mana_disc_anal")
    if isinstance(text, str) and len(text) > text_limit:
        out["mana_disc_anal"] = text[:text_limit] + "…"
    return out


def _latest_flow_summary(flow_items: list[dict[str, Any]]) -> dict[str, Any]:
    if not flow_items:
        return {"rows": 0}
    latest = flow_items[0] if isinstance(flow_items[0], dict) else {}
    main = _num(latest.get("main_net_inflow"))
    window = flow_items[:5]
    main_5d = sum((_num(r.get("main_net_inflow")) or 0.0) for r in window if isinstance(r, dict))
    sm_5d = sum((_num(r.get("sm_net_inflow")) or 0.0) for r in window if isinstance(r, dict))
    mid_5d = sum((_num(r.get("mid_net_inflow")) or 0.0) for r in window if isinstance(r, dict))
    lg_5d = sum((_num(r.get("lg_net_inflow")) or 0.0) for r in window if isinstance(r, dict))
    max_5d = sum((_num(r.get("max_net_inflow")) or 0.0) for r in window if isinstance(r, dict))
    return {
        "rows": len(flow_items),
        "latest_date": latest.get("date") or latest.get("trade_date"),
        "main_net_inflow": main,
        "main_net_inflow_5d": round(main_5d, 2),
        "small_net_inflow_5d": round(sm_5d, 2),
        "medium_net_inflow_5d": round(mid_5d, 2),
        "large_net_inflow_5d": round(lg_5d, 2),
        "super_large_net_inflow_5d": round(max_5d, 2),
        "small_net_inflow": _num(latest.get("sm_net_inflow")),
        "medium_net_inflow": _num(latest.get("mid_net_inflow")),
        "large_net_inflow": _num(latest.get("lg_net_inflow")),
        "super_large_net_inflow": _num(latest.get("max_net_inflow")),
        "latest": latest,
    }


def _latest_market_institution_summary() -> dict[str, Any]:
    north = market_history_cache.load_series("north_flow", "history") or {}
    margin = market_history_cache.load_series("margin", "market") or {}
    latest_north = (north.get("items") or [{}])[0] if isinstance(north.get("items"), list) else {}
    latest_margin = (margin.get("items") or [{}])[0] if isinstance(margin.get("items"), list) else {}
    return {
        "north_rows": len(north.get("items") or []),
        "north_latest_date": latest_north.get("trade_date"),
        "north_net_tgt": _num(latest_north.get("net_tgt")),
        "north_net_hgt": _num(latest_north.get("net_hgt")),
        "north_net_sgt": _num(latest_north.get("net_sgt")),
        "margin_rows": len(margin.get("items") or []),
        "margin_latest_date": latest_margin.get("trade_date"),
        "margin_balance_change": _num(latest_margin.get("rzrqyecz")),
        "source_note": "来自市场行情资金流缓存；无缓存时仅作低置信度代理。",
    }


async def collect_signal_bundle(symbol: str, as_of: str | None = None) -> dict[str, Any]:
    sym = symbol.strip().upper()
    as_of_day = (as_of or "").strip()[:10] or None
    if as_of_day:
        try:
            as_of_dt = datetime.strptime(as_of_day, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("as_of 须为 YYYY-MM-DD") from exc
    else:
        as_of_dt = None

    # 时间节点越早，需要越长的历史窗口，否则截断后会空窗
    range_param = "1y"
    if as_of_dt is not None:
        days_ago = max(0, (datetime.now() - as_of_dt).days)
        if days_ago > 1500:
            range_param = "max"
        elif days_ago > 700:
            range_param = "5y"
        elif days_ago > 300:
            range_param = "2y"
        else:
            range_param = "1y"

    try:
        kline = await kline_pipeline.fetch_a_share_kline_with_fallbacks(sym, range_param, "1d")
    except Exception as exc:
        cached = kline_bundle_cache.load_bundle(sym, range_param, "1d")
        if not cached:
            raise
        kline = kline_bundle_cache.merge_stale_response(cached, fetch_error=str(exc))
        kline["kline_source"] = "local_cache"
    candles = list(kline.get("candles") or [])
    if as_of_day:
        cutoff_ts = int(as_of_dt.timestamp()) + 24 * 3600 - 1
        candles = [c for c in candles if int(c.get("t") or 0) <= cutoff_ts]
        if not candles:
            raise ValueError(f"在 {as_of_day} 及之前没有可用 K 线数据（已尝试 range={range_param}）")
        kline = dict(kline)
        kline["candles"] = candles
        kline["data_as_of"] = as_of_day
    metrics = dict(kline.get("metrics") or {})
    fundamentals = company_fundamentals_store.latest_for_symbol(sym)
    flow_cached = market_history_cache.load_series("stock_capital_flow", sym)
    flow_items = list((flow_cached or {}).get("items") or [])
    if not flow_items:
        try:
            rows = await market_extra_http.fetch_stock_capital_flow(sym)
            flow_cached = market_history_cache.merge_save_series("stock_capital_flow", sym, rows)
            flow_items = list(flow_cached.get("items") or [])
        except Exception:
            flow_items = []
    if as_of_day and flow_items:
        flow_items = [
            row
            for row in flow_items
            if str(row.get("date") or row.get("trade_date") or "")[:10] <= as_of_day
        ]

    closes = [_num(c.get("c")) for c in candles]
    volumes = [_num(c.get("v")) for c in candles]
    latest_close = closes[-1] if closes else _num(metrics.get("latest_close"))
    prev_close = closes[-2] if len(closes) >= 2 else _num(metrics.get("previous_close"))
    ma5 = _avg([x for x in closes[-5:] if x is not None])
    ma20 = _avg([x for x in closes[-20:] if x is not None])
    ma60 = _avg([x for x in closes[-60:] if x is not None])
    vol5 = _avg([x for x in volumes[-5:] if x is not None])
    vol20 = _avg([x for x in volumes[-20:] if x is not None])
    high60 = max([x for x in closes[-60:] if x is not None], default=None)
    low60 = min([x for x in closes[-60:] if x is not None], default=None)
    change_5d = _pct(latest_close, closes[-6] if len(closes) >= 6 else None)
    change_20d = _pct(latest_close, closes[-21] if len(closes) >= 21 else None)
    change_60d = _pct(latest_close, closes[-61] if len(closes) >= 61 else None)
    vol_ratio = (vol5 / vol20) if vol5 is not None and vol20 not in (None, 0) else _num(metrics.get("volume_ratio"))

    financial_raw = _safe_json_loads((fundamentals.get("financial") or {}).get("raw_json"))
    basic = _compact_record(fundamentals.get("basic_info")) or {}
    financial = _compact_record(fundamentals.get("financial"))
    mda = _compact_record(fundamentals.get("mda"), text_limit=1000) or {}
    return {
        "symbol": sym,
        "stock_name": metrics.get("name") or basic.get("short_name") or sym,
        "kline_source": kline.get("kline_source"),
        "data_as_of": kline.get("data_as_of") or as_of_day,
        "as_of": as_of_day,
        "range": range_param,
        "metrics": metrics,
        "technical": {
            "latest_close": latest_close,
            "previous_close": prev_close,
            "change_1d_pct": _pct(latest_close, prev_close),
            "change_5d_pct": change_5d,
            "change_20d_pct": change_20d,
            "change_60d_pct": change_60d,
            "ma5": ma5,
            "ma20": ma20,
            "ma60": ma60,
            "price_above_ma20": latest_close is not None and ma20 is not None and latest_close > ma20,
            "price_above_ma60": latest_close is not None and ma60 is not None and latest_close > ma60,
            "ma20_above_ma60": ma20 is not None and ma60 is not None and ma20 > ma60,
            "volume_ratio_5d_20d": round(vol_ratio, 3) if vol_ratio is not None else None,
            "position_60d_pct": (
                round((latest_close - low60) / (high60 - low60) * 100, 2)
                if latest_close is not None and high60 is not None and low60 is not None and high60 > low60
                else None
            ),
            "high60": high60,
            "low60": low60,
        },
        "flow": _latest_flow_summary(flow_items),
        "market_institution": _latest_market_institution_summary(),
        "fundamentals": {
            "basic_info": basic,
            "financial": financial,
            "mda": mda,
            "raw_financial": financial_raw,
            "pe_ttm": metrics.get("pe_ttm"),
            "total_market_cap_yuan": metrics.get("total_market_cap_yuan"),
            "industry": (
                basic.get("industry_name_c")
                or basic.get("industry_name")
                or basic.get("industry_name_d")
                or (fundamentals.get("financial") or {}).get("industry_name_1")
            ),
            "mda_preview": (mda.get("mana_disc_anal") or "")[:500],
        },
    }


def _base_view(strategy: dict[str, str], score: float, confidence: float, viewpoint: str, reasons: list[str], risks: list[str], action: str, do_not: list[str], signals: dict[str, Any]) -> dict[str, Any]:
    s = _clamp_score(score)
    return {
        "strategy": strategy["name"],
        "strategy_id": strategy["id"],
        "direction": _direction(s),
        "score": s,
        "confidence": round(max(0.1, min(0.95, confidence)), 2),
        "viewpoint": viewpoint,
        "reasons": reasons[:4],
        "risks": risks[:3],
        "action_suggestion": action,
        "do_not_do": do_not[:4],
        "timeframe": strategy["timeframe"],
        "metadata": {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "model_version": "rule-based.transaction-agent-adapter.v1",
            "signal_inputs": signals,
            "rag_context": [],
        },
    }


def _compact_value(value: Any) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, float):
        return f"{value:.2f}"
    if isinstance(value, int):
        return str(value)
    return str(value)


def _signal_detail_lines(strategy_id: str, signals: dict[str, Any]) -> list[str]:
    technical = signals.get("technical") if isinstance(signals.get("technical"), dict) else signals
    flow = signals.get("flow") if isinstance(signals.get("flow"), dict) else {}
    fundamentals = signals.get("fundamentals") if isinstance(signals.get("fundamentals"), dict) else signals
    lines = [
        f"最新价 {_compact_value(technical.get('latest_close'))}，5日/20日/60日涨跌幅分别为 {_compact_value(technical.get('change_5d_pct'))}% / {_compact_value(technical.get('change_20d_pct'))}% / {_compact_value(technical.get('change_60d_pct'))}%。",
        f"均线状态：20日均线{'上方' if technical.get('price_above_ma20') else '下方或未知'}，60日均线{'上方' if technical.get('price_above_ma60') else '下方或未知'}，20日均线{'高于' if technical.get('ma20_above_ma60') else '未高于或未知'}60日均线。",
    ]
    if strategy_id in {"short_term_trading", "flow_confluence", "event_driven", "thematic_growth", "hot_money_dragon_tiger", "quant_liquidity"}:
        lines.append(
            f"资金与量能：5/20日量能比 {_compact_value(technical.get('volume_ratio_5d_20d'))}，主力5日净流入 {_compact_value(flow.get('main_net_inflow_5d'))}，资金流样本 {flow.get('rows') or 0} 条。"
        )
        lines.append(
            f"订单结构代理：超大单/大单/小单5日净流入分别为 {_compact_value(flow.get('super_large_net_inflow_5d'))} / {_compact_value(flow.get('large_net_inflow_5d'))} / {_compact_value(flow.get('small_net_inflow_5d'))}。"
        )
    if strategy_id in {"value_investing", "quality_growth", "dividend_yield", "thematic_growth", "sector_rotation", "mutual_fund_crowding", "national_team_policy", "northbound_institution"}:
        lines.append(
            f"基本面线索：行业 {fundamentals.get('industry') or '未知'}，PE(TTM) {_compact_value(fundamentals.get('pe_ttm'))}，本地经营讨论摘要{'可用' if fundamentals.get('mda_preview') else '缺失'}。"
        )
    if strategy_id in {"national_team_policy", "northbound_institution", "mutual_fund_crowding"}:
        market_inst = signals.get("market_institution") if isinstance(signals.get("market_institution"), dict) else {}
        lines.append(
            f"市场机构代理：北向最近净流入 {_compact_value(market_inst.get('north_net_tgt'))}，两融余额变化 {_compact_value(market_inst.get('margin_balance_change'))}，样本 {market_inst.get('north_rows') or 0}/{market_inst.get('margin_rows') or 0} 条。"
        )
    if strategy_id in {"swing_trading", "mean_reversion"}:
        lines.append(
            f"区间位置：60日位置 {_compact_value(technical.get('position_60d_pct'))}%，60日高低点 {_compact_value(technical.get('high60'))} / {_compact_value(technical.get('low60'))}。"
        )
    return lines


def _build_detailed_analysis(view: dict[str, Any], knowledge: list[dict[str, Any]]) -> dict[str, Any]:
    strategy_id = str(view.get("strategy_id") or "")
    signals = (view.get("metadata") or {}).get("signal_inputs") or {}
    knowledge_points: list[str] = []
    knowledge_risks: list[str] = []
    sources: list[dict[str, Any]] = []
    for item in knowledge:
        for point in item.get("key_points") or []:
            if point and point not in knowledge_points:
                knowledge_points.append(point)
        for risk in item.get("risks") or []:
            if risk and risk not in knowledge_risks:
                knowledge_risks.append(risk)
        sources.append(
            {
                "title": item.get("source_title") or item.get("title"),
                "publisher": item.get("publisher"),
                "url": item.get("url"),
                "quality_score": item.get("quality_score"),
            }
        )
    risk_controls = list(dict.fromkeys((view.get("risks") or []) + knowledge_risks))[:6]
    watch_items = list(dict.fromkeys((view.get("reasons") or []) + _signal_detail_lines(strategy_id, signals)))[:8]
    return {
        "headline": view.get("viewpoint"),
        "score_interpretation": f"评分 {view.get('score')}，置信度 {view.get('confidence')}；方向为 {view.get('direction')}，适用周期 {view.get('timeframe')}。",
        "signal_checks": watch_items,
        "knowledge_points": knowledge_points[:8],
        "risk_controls": risk_controls,
        "action_plan": view.get("action_suggestion"),
        "do_not_do": view.get("do_not_do") or [],
        "data_limitations": [
            "策略观点由本系统 K 线、资金流、财务/MD&A 与策略知识库综合生成；外部数据源不可用时会使用缓存或降低置信度。",
            "公开策略资料仅保存短摘录和结构化摘要，不保存版权书籍或研报全文。",
        ],
        "sources": sources[:8],
    }


def _apply_knowledge_context(view: dict[str, Any]) -> dict[str, Any]:
    knowledge = strategy_knowledge_service.query_strategy_knowledge(view["strategy"], limit=6)
    if not knowledge:
        out = dict(view)
        out["detailed_analysis"] = _build_detailed_analysis(out, [])
        return out
    out = dict(view)
    out["knowledge_context"] = [
        {
            "title": k.get("title"),
            "source_title": k.get("source_title"),
            "publisher": k.get("publisher"),
            "url": k.get("url"),
            "key_points": (k.get("key_points") or [])[:2],
            "risks": (k.get("risks") or [])[:1],
        }
        for k in knowledge
    ]
    meta = dict(out.get("metadata") or {})
    meta["knowledge_sources"] = len(knowledge)
    meta["rag_context"] = [k.get("summary") for k in knowledge if k.get("summary")]
    out["metadata"] = meta
    out["confidence"] = round(min(0.95, float(out.get("confidence") or 0.5) + min(len(knowledge), 3) * 0.03), 2)
    if knowledge:
        first_points = knowledge[0].get("key_points") or []
        if first_points:
            reasons = list(out.get("reasons") or [])
            reasons.append(f"策略知识库提示：{first_points[0]}")
            out["reasons"] = reasons[:4]
    out["detailed_analysis"] = _build_detailed_analysis(out, knowledge)
    return out


def _score_views(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    t = bundle["technical"]
    f = bundle["fundamentals"]
    flow = bundle["flow"]
    market_inst = bundle.get("market_institution") or {}
    pe = _num(f.get("pe_ttm"))
    market_cap = _num(f.get("total_market_cap_yuan"))
    pos = _num(t.get("position_60d_pct"))
    ch5 = _num(t.get("change_5d_pct")) or 0
    ch20 = _num(t.get("change_20d_pct")) or 0
    ch60 = _num(t.get("change_60d_pct")) or 0
    volr = _num(t.get("volume_ratio_5d_20d")) or 1
    main5 = _num(flow.get("main_net_inflow_5d")) or 0
    super5 = _num(flow.get("super_large_net_inflow_5d")) or 0
    large5 = _num(flow.get("large_net_inflow_5d")) or 0
    small5 = _num(flow.get("small_net_inflow_5d")) or 0
    north_net = _num(market_inst.get("north_net_tgt"))
    margin_change = _num(market_inst.get("margin_balance_change"))
    flow_pos = main5 > 0
    trend_good = bool(t.get("price_above_ma20")) + bool(t.get("price_above_ma60")) + bool(t.get("ma20_above_ma60"))
    industry = f.get("industry") or "所属行业"
    name = bundle.get("stock_name") or bundle.get("symbol")

    views: list[dict[str, Any]] = []
    strategy = _STRATEGY_BY_ID["trend_following_mid"]
    score = 35 + trend_good * 13 + (8 if ch20 > 0 else -8) + (5 if volr >= 1 else 0)
    views.append(_base_view(strategy, score, 0.72, f"{name}中期趋势以均线位置和20日表现为核心，当前更偏{('顺势跟随' if score >= 60 else '等待确认' if score > 40 else '防守')}。", [
        f"20日涨跌幅 {ch20:.2f}%，60日涨跌幅 {ch60:.2f}%。",
        f"价格{'站上' if t.get('price_above_ma20') else '未站上'}20日均线，{'站上' if t.get('price_above_ma60') else '未站上'}60日均线。",
        f"5/20日量能比约 {volr:.2f}。",
    ], ["趋势信号来自日线，若主源回退需关注数据口径。", "放量下跌会破坏右侧结构。"], "等待回踩不破关键均线或放量突破后再提高仓位。", ["不要在跌破20日均线后追高。", "不要忽视成交量萎缩的假突破。"], t))

    strategy = _STRATEGY_BY_ID["short_term_trading"]
    score = 50 + ch5 * 2 + (8 if volr > 1.2 else -4) + (8 if flow_pos else -8)
    views.append(_base_view(strategy, score, 0.62, f"{name}短线弹性取决于5日动量和资金认可度，当前信号{'偏活跃' if score >= 60 else '一般' if score > 40 else '偏弱'}。", [
        f"5日涨跌幅 {ch5:.2f}%。",
        f"量能比 {volr:.2f}，主力5日净流入 {main5:.0f}。",
    ], ["短线波动大，隔日资金撤退会快速改变结论。"], "只在量价同步时轻仓试错，跌破前低及时退出。", ["不要无止损追涨。", "不要把短线信号当长期逻辑。"], {"technical": t, "flow": flow}))

    strategy = _STRATEGY_BY_ID["swing_trading"]
    score = 45 + (20 if 30 <= (pos or 50) <= 80 else -10) + (10 if ch20 > 0 else -8) + (6 if trend_good >= 2 else 0)
    views.append(_base_view(strategy, score, 0.66, f"{name}波段位置在60日区间约 {pos if pos is not None else '未知'}%，更适合按区间做计划。", [
        f"60日区间低点 {t.get('low60') or '—'}，高点 {t.get('high60') or '—'}。",
        f"20日表现 {ch20:.2f}%，趋势确认项 {trend_good}/3。",
    ], ["接近区间高位时盈亏比下降。"], "靠近支撑分批观察，接近压力位降低预期。", ["不要在区间顶部重仓追入。", "不要无视支撑跌破。"], t))

    strategy = _STRATEGY_BY_ID["mean_reversion"]
    score = 50 + (18 if (pos is not None and pos < 25) else -10 if (pos is not None and pos > 85) else 3) + (-ch5 if ch5 < 0 else -min(ch5, 8))
    views.append(_base_view(strategy, score, 0.58, f"{name}均值回归视角关注短期偏离，当前位置{'有修复观察价值' if score >= 60 else '不够极端' if score > 40 else '不宜逆势接'}。", [
        f"60日位置 {pos if pos is not None else '未知'}%，5日涨跌幅 {ch5:.2f}%。",
        f"最新价 {t.get('latest_close') or '—'}，20日均线 {t.get('ma20') or '—'}。",
    ], ["强趋势下均值回归容易连续失效。"], "仅在缩量企稳后观察反弹，不预设底部。", ["不要在放量破位时逆势摊平。", "不要把反弹当反转。"], t))

    strategy = _STRATEGY_BY_ID["value_investing"]
    score = 55 + (12 if pe and pe < 25 else -8 if pe and pe > 60 else 0) + (8 if f.get("mda_preview") else -6)
    views.append(_base_view(strategy, score, 0.6, f"{name}价值视角主要看估值与业务稳定性，当前估值参考 PE(TTM)={pe if pe is not None else '未知'}。", [
        f"行业：{industry}，总市值 {f.get('total_market_cap_yuan') or '—'}。",
        f"本地基础库{'已有' if f.get('basic_info') else '缺少'}公司基本信息，{'已有' if f.get('mda_preview') else '缺少'}MD&A文本。",
    ], ["单一 PE 不能代表完整估值。", "2025数据可能包含未审/不同口径报告。"], "继续结合财报原文、现金流与行业景气确认安全边际。", ["不要只因低 PE 买入。", "不要忽略业绩质量和负债结构。"], f))

    strategy = _STRATEGY_BY_ID["quality_growth"]
    score = 50 + (10 if ch60 > 0 else -5) + (8 if f.get("mda_preview") else -5) + (5 if pe and pe < 80 else -5 if pe and pe > 120 else 0)
    views.append(_base_view(strategy, score, 0.55, f"{name}质量成长视角需要财务增长字段进一步确认，当前以趋势和经营描述做初筛。", [
        f"60日涨跌幅 {ch60:.2f}%，PE(TTM) {pe if pe is not None else '未知'}。",
        f"MD&A摘要：{(f.get('mda_preview') or '暂无')[:70]}",
    ], ["缺少收入/利润增速的标准化字段映射，质量成长结论偏初筛。"], "优先补充 ROE、营收增速、利润增速后再提高置信度。", ["不要仅凭题材判断成长质量。", "不要忽视高估值回撤风险。"], f))

    strategy = _STRATEGY_BY_ID["dividend_yield"]
    score = 45 + (12 if pe and pe < 20 else 0) + (8 if "银行" in str(industry) or "公用" in str(industry) else 0)
    views.append(_base_view(strategy, score, 0.45, f"{name}红利策略需要分红率字段支撑，当前仅能用行业和估值做低置信筛选。", [
        f"行业：{industry}，PE(TTM) {pe if pe is not None else '未知'}。",
        "当前数据接口尚未映射连续分红和股息率字段。",
    ], ["缺少股息率、派息率、自由现金流覆盖数据。"], "补充分红数据前只作为候选观察，不作为红利买入依据。", ["不要把低估值等同高股息。", "不要忽视分红不可持续风险。"], f))

    strategy = _STRATEGY_BY_ID["event_driven"]
    has_mda = bool(f.get("mda_preview"))
    score = 48 + (8 if abs(ch5) > 8 else 0) + (6 if has_mda else -4)
    views.append(_base_view(strategy, score, 0.5, f"{name}事件驱动视角当前依赖新闻/公告催化补充，价格异动仅提供线索。", [
        f"5日涨跌幅 {ch5:.2f}%，MD&A记录{'可用' if has_mda else '不可用'}。",
        "后续可接入本地新闻库与公告解析形成催化剂清单。",
    ], ["缺少明确事件日期和公告类型时，事件交易胜率低。"], "等待公告、业绩预告或产业催化明确后再制定事件窗口。", ["不要把单纯波动误判为事件催化。", "不要在事件落地后追逐兑现行情。"], {"technical": t, "fundamentals": f}))

    strategy = _STRATEGY_BY_ID["flow_confluence"]
    score = 50 + (18 if flow_pos else -14) + (8 if volr > 1.1 else -4) + (6 if ch20 > 0 else -6)
    views.append(_base_view(strategy, score, 0.7 if flow.get("rows") else 0.42, f"{name}资金共振显示主力5日净流入 {main5:.0f}，与量价{'形成一定配合' if score >= 60 else '尚未共振'}。", [
        f"资金流缓存 {flow.get('rows') or 0} 条，最近日期 {flow.get('latest_date') or '未知'}。",
        f"主力5日净流入 {main5:.0f}，量能比 {volr:.2f}。",
    ], ["资金流数据存在口径与时滞。", "单一路资金不能替代价格结构。"], "资金继续流入且价格不滞涨时才提高策略权重。", ["不要只看单日净流入。", "不要忽视放量滞涨背离。"], {"flow": flow, "technical": t}))

    strategy = _STRATEGY_BY_ID["sector_rotation"]
    score = 50 + (10 if ch60 > 0 else -8) + (8 if flow_pos else -4) + (5 if industry else -5)
    views.append(_base_view(strategy, score, 0.52, f"{name}行业轮动视角归属 {industry}，需要结合行业指数相对强弱进一步确认。", [
        f"个股60日表现 {ch60:.2f}%，资金5日净流入 {main5:.0f}。",
        f"本地基础库行业字段：{industry}。",
    ], ["尚未接入完整行业指数相对排名。"], "结合申万行业 K 线与资金流排名后再判断轮动强弱。", ["不要只凭个股走势推断行业轮动。", "不要忽略行业拥挤度。"], {"industry": industry, "technical": t, "flow": flow}))

    strategy = _STRATEGY_BY_ID["thematic_growth"]
    theme_blob = f"{industry} {f.get('mda_preview') or ''}"
    theme_keywords = ("AI", "人工智能", "算力", "数据", "芯片", "半导体", "机器人", "低空", "出海", "国产", "创新", "新能源", "储能")
    theme_hits = [kw for kw in theme_keywords if kw.lower() in theme_blob.lower()]
    score = 46 + min(len(theme_hits), 4) * 5 + (8 if ch60 > 0 else -5) + (6 if flow_pos else -4) + (5 if f.get("mda_preview") else -4)
    views.append(_base_view(strategy, score, 0.56, f"{name}主题成长视角关注产业叙事、政策/技术催化与趋势验证，当前主题线索{'较多' if theme_hits else '仍需补充'}。", [
        f"命中主题关键词：{('、'.join(theme_hits[:5]) if theme_hits else '暂无明确主题关键词')}。",
        f"60日表现 {ch60:.2f}%，主力5日净流入 {main5:.0f}。",
        f"行业/业务线索：{str(industry)[:24]}。",
    ], ["主题交易容易受情绪与拥挤度影响。", "叙事兑现前后波动会显著放大。"], "优先观察主题是否有持续订单、业绩或政策验证，再结合趋势与资金确认。", ["不要把热点词等同业绩兑现。", "不要在主题高潮期忽略估值和回撤。"], {"industry": industry, "mda_preview": f.get("mda_preview"), "technical": t, "flow": flow}))

    large_cap = bool(market_cap and market_cap >= 30_000_000_000)
    mid_large_cap = bool(market_cap and market_cap >= 10_000_000_000)
    stable_trend = trend_good >= 2 and ch20 > 0
    strategy = _STRATEGY_BY_ID["mutual_fund_crowding"]
    score = 42 + (14 if large_cap else 6 if mid_large_cap else -8) + (12 if stable_trend else -5) + (8 if super5 + large5 > 0 else -5) + (6 if pe and 10 <= pe <= 60 else -4 if pe and pe > 100 else 0)
    views.append(_base_view(strategy, score, 0.5, f"{name}公募基金抱团视角关注大市值、行业景气、趋势稳定和机构订单共振，当前抱团线索{'较强' if score >= 60 else '仍需持仓数据验证'}。", [
        f"总市值 {market_cap if market_cap is not None else '未知'}，行业 {industry}。",
        f"20日/60日涨跌幅 {ch20:.2f}% / {ch60:.2f}%，趋势确认项 {trend_good}/3。",
        f"超大单+大单5日净流入 {(super5 + large5):.0f}，PE(TTM) {pe if pe is not None else '未知'}。",
    ], ["当前未直连基金季报重仓明细，抱团仅为代理判断。", "抱团股在业绩或流动性拐点时可能出现一致性撤退。"], "优先等待基金持仓/机构调研/北向配置数据进一步确认，再把它作为中期策略权重。", ["不要把上涨后的大市值股自动等同公募抱团。", "不要忽视拥挤交易导致的踩踏风险。"], {"technical": t, "flow": flow, "fundamentals": f, "market_institution": market_inst}))

    policy_industry = any(x in str(industry) for x in ("银行", "证券", "保险", "非银", "能源", "电力", "通信", "半导体", "军工", "央企", "国企"))
    strategy = _STRATEGY_BY_ID["national_team_policy"]
    score = 40 + (14 if large_cap else -5) + (10 if policy_industry else 0) + (8 if ch60 >= 0 else -5) + (6 if north_net and north_net > 0 else -3 if north_net and north_net < 0 else 0)
    views.append(_base_view(strategy, score, 0.42, f"{name}国家队/政策资金视角更适合观察权重、金融、央国企或关键产业链标的，当前政策资金线索{'可观察' if score >= 55 else '偏弱或缺证据'}。", [
        f"行业/属性线索：{industry}，大市值属性{'成立' if large_cap else '不明显'}。",
        f"北向市场代理净流入 {north_net if north_net is not None else '未知'}，两融变化 {margin_change if margin_change is not None else '未知'}。",
        "国家队持仓通常来自定期股东披露，实时资金流只能作为弱代理。",
    ], ["国家队持仓披露滞后，不能用日内资金流直接确认。", "政策托底与个股上涨之间并非一一对应。"], "把它作为底部稳定性和政策方向观察项，等待十大股东、ETF放量或公告数据确认。", ["不要把大盘权重股都当成国家队买入。", "不要根据传闻做短线追高。"], {"technical": t, "flow": flow, "fundamentals": f, "market_institution": market_inst}))

    hot_money_setup = ch5 > 5 and volr > 1.2 and (pos or 0) > 55
    strategy = _STRATEGY_BY_ID["hot_money_dragon_tiger"]
    score = 38 + (18 if hot_money_setup else -4) + (10 if flow_pos else -8) + (8 if small5 < 0 and super5 + large5 > 0 else 0) + (6 if ch20 > 0 else -4)
    views.append(_base_view(strategy, score, 0.48 if flow.get("rows") else 0.34, f"{name}游资龙头视角关注短线强度、题材弹性、量能放大和龙虎榜席位，当前{'有情绪交易线索' if score >= 60 else '暂未形成典型游资结构'}。", [
        f"5日涨跌幅 {ch5:.2f}%，60日位置 {pos if pos is not None else '未知'}%，量能比 {volr:.2f}。",
        f"主力5日净流入 {main5:.0f}，小单5日净流入 {small5:.0f}。",
        "龙虎榜/知名营业部数据尚未接入个股级实时验证。",
    ], ["游资策略对情绪周期和隔日承接高度敏感。", "高位放量不一定是接力，也可能是兑现。"], "只有在题材、盘口承接和资金继续强化时短线观察，隔日不及预期应快速降权。", ["不要在连续加速后无计划追板。", "不要把单日大单流入当成游资锁仓。"], {"technical": t, "flow": flow, "fundamentals": f}))

    flow_divergence = (super5 + large5 > 0 and small5 > 0) or (super5 + large5 < 0 and small5 < 0)
    strategy = _STRATEGY_BY_ID["quant_liquidity"]
    score = 45 + (14 if volr > 1.5 else 6 if volr > 1.15 else -4) + (8 if abs(ch5) <= 12 else -5) + (7 if flow_divergence else 0) + (5 if 25 <= (pos or 50) <= 85 else -3)
    views.append(_base_view(strategy, score, 0.44, f"{name}量化流动性视角关注成交活跃、波动可控和订单结构分歧，当前{'适合做流动性观察' if score >= 58 else '量化线索一般'}。", [
        f"量能比 {volr:.2f}，5日涨跌幅 {ch5:.2f}%，60日位置 {pos if pos is not None else '未知'}%。",
        f"大单系5日净流入 {(super5 + large5):.0f}，小单5日净流入 {small5:.0f}。",
        "当前未接入逐笔/订单簿，量化策略只能用日线流动性代理。",
    ], ["高频/量化真实信号需要逐笔、盘口和成交分布，日线代理误差较大。"], "作为交易拥挤度和短周期波动提示，不单独作为方向判断。", ["不要把高换手自动理解为量化买入。", "不要用日线代理替代真实微观结构数据。"], {"technical": t, "flow": flow}))

    strategy = _STRATEGY_BY_ID["northbound_institution"]
    score = 45 + (10 if large_cap else -4) + (10 if ch20 > 0 else -6) + (8 if north_net and north_net > 0 else -6 if north_net and north_net < 0 else 0) + (5 if pe and pe < 60 else -3 if pe and pe > 100 else 0)
    views.append(_base_view(strategy, score, 0.46 if market_inst.get("north_rows") else 0.32, f"{name}北向机构配置视角关注大中市值、行业景气、估值可解释和外资市场风险偏好，当前{'有配置跟踪价值' if score >= 58 else '配置证据不足'}。", [
        f"北向市场净流入 {north_net if north_net is not None else '未知'}，缓存样本 {market_inst.get('north_rows') or 0} 条。",
        f"总市值 {market_cap if market_cap is not None else '未知'}，PE(TTM) {pe if pe is not None else '未知'}。",
        f"20日表现 {ch20:.2f}%，行业 {industry}。",
    ], ["当前是市场级北向代理，尚非个股沪深港通持股变动。", "外资配置会受汇率、全球风险偏好和指数权重影响。"], "接入个股北向持股后再提高置信度；当前只用于中期配置偏好的弱确认。", ["不要把北向市场净流入等同个股被外资买入。", "不要忽略汇率和海外风险偏好变化。"], {"technical": t, "flow": flow, "fundamentals": f, "market_institution": market_inst}))

    return [_apply_knowledge_context(v) for v in views]


async def generate_strategy_views(symbol: str, strategy: str | None = None, as_of: str | None = None) -> dict[str, Any]:
    bundle = await collect_signal_bundle(symbol, as_of=as_of)
    views = _score_views(bundle)
    if strategy:
        key = strategy.strip()
        want = _STRATEGY_BY_ID.get(key) or _STRATEGY_BY_NAME.get(key)
        if not want:
            raise ValueError(f"未知策略：{strategy}")
        views = [v for v in views if v["strategy_id"] == want["id"]]
    bullish = sum(1 for v in views if v["direction"] == "bullish")
    bearish = sum(1 for v in views if v["direction"] == "bearish")
    neutral = sum(1 for v in views if v["direction"] == "neutral")
    avg_score = round(sum(v["score"] for v in views) / len(views), 1) if views else 50
    return {
        "symbol": bundle["symbol"],
        "stock_name": bundle["stock_name"],
        "as_of": bundle.get("as_of"),
        "source": {
            "adapter": "TransactionAgent",
            "mode": "rule_based_data_aligned",
            "kline_source": bundle.get("kline_source"),
            "data_as_of": bundle.get("data_as_of"),
            "resources": resources_status(),
        },
        "signals": bundle,
        "strategies": views,
        "summary": {
            "bullish_count": bullish,
            "neutral_count": neutral,
            "bearish_count": bearish,
            "average_score": avg_score,
            "dominant_direction": _direction(_clamp_score(avg_score)),
            "strategy_count": len(views),
            "knowledge_item_count": strategy_knowledge_service.status().get("item_count", 0),
        },
    }
