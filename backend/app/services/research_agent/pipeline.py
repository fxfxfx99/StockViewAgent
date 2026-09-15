"""投研 Agent 端到端流水线。"""
from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any

from app.services.research_agent.analyzers.filing_like import analyze_filings
from app.services.research_agent.analyzers.fundamental import analyze_fundamentals
from app.services.research_agent.analyzers.news_event import analyze_news
from app.services.research_agent.analyzers.price_action import analyze_price_action
from app.services.research_agent.analyzers.risk import analyze_risks
from app.services.research_agent.analyzers.valuation import analyze_valuation
from app.services.research_agent.data_bundle import collect_for_symbol
from app.services.research_agent.llm_chain import (
    run_citation_formatter,
    run_planner,
    run_risk_reviewer,
    run_synthesizer,
)
from app.services.research_agent.memo_markdown import build_markdown_memo, build_programmatic_citations
from app.services.research_agent.models import (
    AgentRunLog,
    Citation,
    DeepDive,
    OnePager,
    ResearchPlan,
    ResearchReport,
    ResearchRunRecord,
    SummaryCard,
)
from app.services.research_agent.symbol_resolve import (
    ResolvedSymbol,
    extract_comparison_fragment,
    resolve_primary,
)
from app.storage import research_agent_store


def _default_plan(r: ResolvedSymbol, user_query: str, secondary: str | None) -> dict[str, Any]:
    return {
        "primary_ticker": r.symbol,
        "secondary_ticker": secondary,
        "company_name": r.display_name,
        "intent": user_query[:200],
        "tasks": [
            "price_action_analysis",
            "financial_analysis",
            "news_analysis",
            "valuation_analysis",
            "risk_analysis",
            "final_memo",
        ],
        "time_horizon": "medium",
        "comparison_mode": bool(secondary),
        "language": "zh",
        "data_sources_planned": ["yahoo_or_eastmoney", "news_sqlite", "tushare_optional", "issuer_rag"],
    }


def _template_report(
    plan: dict[str, Any],
    price: dict[str, Any],
    fund: dict[str, Any],
    val: dict[str, Any],
    news: dict[str, Any],
    fil: dict[str, Any],
    risk: dict[str, Any],
) -> dict[str, Any]:
    r1m = price.get("returns_1m")
    stance = "中性"
    if r1m is not None:
        if r1m > 8:
            stance = "偏积极"
        elif r1m < -8:
            stance = "偏谨慎"

    lc = price.get("latest_close")
    price_s = str(lc) if lc is not None else "未获取"

    return {
        "summary_card": {
            "company_name": plan.get("company_name") or plan.get("primary_ticker"),
            "ticker": plan.get("primary_ticker"),
            "price": price_s,
            "currency": None,
            "change_1m_pct": r1m,
            "stance": stance,
            "confidence": "低",
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        },
        "one_pager": {
            "one_line": "基于有限公开数据与本地库的自动化研究摘要，结论克制，需人工复核。",
            "core_logic": [
                price.get("narrative", "")[:200] + ("…" if len(price.get("narrative", "")) > 200 else ""),
                fund.get("narrative", "")[:200] + ("…" if len(fund.get("narrative", "")) > 200 else ""),
                news.get("narrative", "")[:200] + ("…" if len(news.get("narrative", "")) > 200 else ""),
            ],
            "main_risks": (risk.get("items") or [])[:5],
            "watch_signals": [
                "下一季财报与指引是否兑现当前预期",
                "主要竞品份额与价格战变化",
                "监管与宏观流动性对相关估值倍数的影响",
            ],
        },
        "deep_dive": {
            "price_action": price.get("narrative") or "—",
            "fundamentals": fund.get("narrative") or "—",
            "filings_notes": fil.get("narrative") or "—",
            "news_events": news.get("narrative") or "—",
            "valuation": val.get("narrative") or "—",
            "risk_list": risk.get("narrative") or "—",
            "bull_bear": "看多：数据改善与预期差；看空：增长放缓与估值回撤。需结合证据逐项验证。",
        },
        "disclaimer": "内容仅供研究参考，不构成投资建议。",
    }


def _merge_risk_review(memo: dict[str, Any], review: dict[str, Any] | None) -> None:
    if not review:
        return
    sc = memo.setdefault("summary_card", {})
    if review.get("stance"):
        sc["stance"] = review["stance"]
    if review.get("confidence"):
        sc["confidence"] = review["confidence"]
    op = memo.setdefault("one_pager", {})
    extra = review.get("extra_risks") or []
    if extra and isinstance(extra, list):
        mr = list(op.get("main_risks") or [])
        for x in extra:
            if x and x not in mr:
                mr.append(str(x))
        op["main_risks"] = mr[:8]


def _cite_from_dict(c: dict[str, Any], idx: int) -> Citation:
    st = c.get("source_type") or "other"
    if st not in ("news", "filing", "market", "profile", "financial", "llm_inference", "other"):
        st = "other"
    return Citation(
        id=str(c.get("id") or f"cite-{idx}"),
        title=str(c.get("title") or "")[:500],
        source_type=st,
        time=c.get("time"),
        url=c.get("url"),
        source_label=str(c.get("source_label") or "")[:300],
        excerpt=str(c.get("excerpt") or "")[:800],
    )


def _dict_to_report(d: dict[str, Any], citations: list[dict]) -> ResearchReport:
    sc = d.get("summary_card") or {}
    op = d.get("one_pager") or {}
    dd = d.get("deep_dive") or {}
    cites: list[Citation] = []
    for i, c in enumerate(citations):
        if isinstance(c, dict):
            cites.append(_cite_from_dict(c, i + 1))
    return ResearchReport(
        summary_card=SummaryCard(
            company_name=str(sc.get("company_name") or ""),
            ticker=str(sc.get("ticker") or ""),
            price=str(sc.get("price")) if sc.get("price") is not None else None,
            currency=sc.get("currency"),
            change_1m_pct=sc.get("change_1m_pct") if isinstance(sc.get("change_1m_pct"), (int, float)) else None,
            stance=sc.get("stance") if sc.get("stance") in ("偏积极", "中性", "偏谨慎") else "中性",
            confidence=sc.get("confidence") if sc.get("confidence") in ("低", "中", "高") else "中",
            updated_at=str(sc.get("updated_at") or ""),
        ),
        one_pager=OnePager(
            one_line=str(op.get("one_line") or ""),
            core_logic=[str(x) for x in (op.get("core_logic") or []) if x],
            main_risks=[str(x) for x in (op.get("main_risks") or []) if x],
            watch_signals=[str(x) for x in (op.get("watch_signals") or []) if x],
        ),
        deep_dive=DeepDive(
            price_action=str(dd.get("price_action") or ""),
            fundamentals=str(dd.get("fundamentals") or ""),
            filings_notes=str(dd.get("filings_notes") or ""),
            news_events=str(dd.get("news_events") or ""),
            valuation=str(dd.get("valuation") or ""),
            risk_list=str(dd.get("risk_list") or ""),
            bull_bear=str(dd.get("bull_bear") or ""),
        ),
        citations=cites,
        disclaimer=str(d.get("disclaimer") or "内容仅供研究参考，不构成投资建议。"),
        markdown_memo="",
        raw_llm_used=False,
    )


async def run_research(user_id: int, user_query: str) -> ResearchRunRecord:
    t0 = time.time()
    steps: list[dict[str, Any]] = []
    log = AgentRunLog(steps=[], data_sources_used=[], errors=[], started_at=t0, finished_at=t0)

    q = (user_query or "").strip()
    if not q:
        raise ValueError("empty query")

    # A: 标的
    resolved, rerr = resolve_primary(q)
    if not resolved:
        steps.append({"name": "任务理解与标的识别", "status": "error", "detail": rerr or "无法解析标的"})
        log.steps = steps
        log.errors.append(rerr or "无法解析标的")
        raise ValueError(rerr or "无法识别股票代码或公司名称，请补充代码或更完整简称")

    steps.append(
        {
            "name": "任务理解与标的识别",
            "status": "ok",
            "detail": f"{resolved.symbol} ({resolved.display_name}) [{resolved.market}] — {resolved.resolution_note}",
        }
    )

    compare_hint = extract_comparison_fragment(q)
    secondary: str | None = None
    if compare_hint:
        r2, _ = resolve_primary(compare_hint)
        if r2 and r2.symbol != resolved.symbol:
            secondary = r2.symbol
            steps.append(
                {
                    "name": "对比标的识别",
                    "status": "ok",
                    "detail": f"{r2.symbol} ({r2.display_name})",
                }
            )

    plan_dict = run_planner(q, resolved.symbol, resolved.display_name, resolved.market, compare_hint or "")
    if not plan_dict:
        plan_dict = _default_plan(resolved, q, secondary)
        steps.append({"name": "研究计划（Planner）", "status": "degraded", "detail": "LLM 不可用或解析失败，使用启发式计划"})
    else:
        steps.append({"name": "研究计划（Planner）", "status": "ok", "detail": json.dumps(plan_dict, ensure_ascii=False)[:800]})
        if plan_dict.get("secondary_ticker"):
            secondary = str(plan_dict["secondary_ticker"]).strip().upper() or secondary
        if plan_dict.get("comparison_mode") and not secondary and compare_hint:
            r2, _ = resolve_primary(compare_hint)
            if r2:
                secondary = r2.symbol

    plan = ResearchPlan.model_validate(
        {
            "primary_ticker": plan_dict.get("primary_ticker") or resolved.symbol,
            "secondary_ticker": plan_dict.get("secondary_ticker") or secondary,
            "company_name": plan_dict.get("company_name") or resolved.display_name,
            "intent": plan_dict.get("intent") or q[:200],
            "tasks": plan_dict.get("tasks")
            or [
                "price_action_analysis",
                "financial_analysis",
                "news_analysis",
                "valuation_analysis",
                "risk_analysis",
                "final_memo",
            ],
            "time_horizon": plan_dict.get("time_horizon") or "medium",
            "comparison_mode": bool(plan_dict.get("comparison_mode")),
            "language": plan_dict.get("language") or "zh",
            "data_sources_planned": plan_dict.get("data_sources_planned") or [],
        }
    )

    # B: 采集
    bundle = await collect_for_symbol(
        plan.primary_ticker,
        q,
        include_secondary=plan.secondary_ticker,
    )
    clog = bundle.get("collection_log") or {}
    log.data_sources_used = list(dict.fromkeys(log.data_sources_used + (clog.get("data_sources_used") or [])))
    log.errors.extend(clog.get("errors") or [])
    steps.append(
        {
            "name": "数据采集",
            "status": "ok" if bundle.get("kline") else "degraded",
            "detail": f"数据源: {', '.join(clog.get('data_sources_used') or [])}; 错误: {len(clog.get('errors') or [])}",
        }
    )

    price = analyze_price_action(bundle)
    fund = analyze_fundamentals(bundle)
    val = analyze_valuation(bundle)
    news = analyze_news(bundle)
    fil = analyze_filings(bundle)
    risk = analyze_risks(bundle, price, val, news)

    steps.append({"name": "分析模块", "status": "ok", "detail": "Price / Fundamental / Valuation / News / Filing / Risk"})

    facts: dict[str, Any] = {
        "price": {k: price.get(k) for k in ("returns_5d", "returns_1m", "returns_3m", "returns_1y", "volatility_annual_approx", "narrative")},
        "fundamentals": fund.get("narrative"),
        "valuation": val.get("narrative"),
        "news": news.get("top_events"),
        "filings": fil.get("narrative"),
        "risks": risk.get("items"),
        "secondary": bundle.get("secondary"),
        "errors": clog.get("errors"),
    }

    memo_draft = _template_report(plan_dict, price, fund, val, news, fil, risk)
    raw_llm = False
    synth = run_synthesizer(q, plan_dict, {**facts, "news": news.get("top_events") or [], "issuer_chunks": bundle.get("issuer_chunks") or []})
    if synth:
        memo_draft = synth
        raw_llm = True
        steps.append({"name": "备忘录合成（LLM）", "status": "ok", "detail": "已生成结构化 JSON 备忘录"})
    else:
        steps.append({"name": "备忘录合成（LLM）", "status": "degraded", "detail": "回退至规则模板"})

    review = run_risk_reviewer(facts, memo_draft)
    if review:
        _merge_risk_review(memo_draft, review)
        steps.append({"name": "风险复核（LLM）", "status": "ok", "detail": json.dumps(review, ensure_ascii=False)[:600]})
    else:
        steps.append({"name": "风险复核（LLM）", "status": "skipped", "detail": "未调用或失败"})

    cite_raw = build_programmatic_citations(bundle, news, price)
    cite_fmt = run_citation_formatter(cite_raw)
    citations_list = cite_fmt if cite_fmt else cite_raw
    if cite_fmt:
        steps.append({"name": "引用格式化（LLM）", "status": "ok", "detail": f"{len(citations_list)} 条"})
    else:
        steps.append({"name": "引用格式化（LLM）", "status": "degraded", "detail": "使用程序化引用"})

    memo_draft["citations"] = citations_list
    md = build_markdown_memo(memo_draft, plan_dict, q)

    report = _dict_to_report(memo_draft, citations_list)
    report.markdown_memo = md
    report.raw_llm_used = raw_llm

    log.steps = steps
    log.finished_at = time.time()

    resolved_payload = {
        "symbol": resolved.symbol,
        "display_name": resolved.display_name,
        "market": resolved.market,
        "resolution_note": resolved.resolution_note,
    }
    payload = {
        "plan": plan.model_dump(),
        "report": report.model_dump(),
        "log": log.model_dump(),
        "resolved": resolved_payload,
    }
    rid, created_at = research_agent_store.insert_run(user_id, q, payload)

    return ResearchRunRecord(
        id=rid,
        user_id=user_id,
        query=q,
        plan=plan,
        report=report,
        log=log,
        created_at=created_at,
        resolved=resolved_payload,
    )
