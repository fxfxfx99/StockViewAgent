"""研究底稿 Markdown 导出。"""
from __future__ import annotations

from datetime import datetime
from typing import Any


def build_markdown_memo(report: dict[str, Any], plan: dict[str, Any], query: str) -> str:
    sc = report.get("summary_card") or {}
    op = report.get("one_pager") or {}
    dd = report.get("deep_dive") or {}
    cites = report.get("citations") or []

    lines: list[str] = []
    lines.append(f"# 买方投研备忘录（底稿）\n")
    lines.append(f"> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
    lines.append(f"> 用户问题: {query}\n")
    lines.append(f"> 计划标的: {plan.get('primary_ticker') or sc.get('ticker')} {plan.get('company_name') or ''}\n")
    lines.append("\n---\n")
    lines.append("## 摘要卡\n")
    p = sc.get("price")
    p_disp = str(p) if p is not None else "—"
    lines.append(
        f"- 标的: {sc.get('company_name')} / {sc.get('ticker')}\n"
        f"- 价格: {p_disp} {sc.get('currency') or ''}\n"
        f"- 近一月涨跌: {sc.get('change_1m_pct')}\n"
        f"- 总体判断: {sc.get('stance')}（置信度 {sc.get('confidence')}）\n"
        f"- 更新时间: {sc.get('updated_at')}\n"
    )
    lines.append("\n## 一页结论\n")
    lines.append(f"**一句话**: {op.get('one_line')}\n")
    lines.append("\n**核心逻辑**:\n")
    for x in op.get("core_logic") or []:
        lines.append(f"- {x}\n")
    lines.append("\n**主要风险**:\n")
    for x in op.get("main_risks") or []:
        lines.append(f"- {x}\n")
    lines.append("\n**跟踪信号**:\n")
    for x in op.get("watch_signals") or []:
        lines.append(f"- {x}\n")

    lines.append("\n---\n## 深度分析\n")
    for title, key in [
        ("价格表现", "price_action"),
        ("基本面", "fundamentals"),
        ("财报与公告", "filings_notes"),
        ("新闻与事件", "news_events"),
        ("估值", "valuation"),
        ("风险", "risk_list"),
        ("多空对照", "bull_bear"),
    ]:
        body = dd.get(key) or "—"
        lines.append(f"### {title}\n\n{body}\n\n")

    lines.append("---\n## 证据引用\n")
    for c in cites:
        lines.append(
            f"- **{c.get('id')}** [{c.get('source_type')}] {c.get('title')}\n"
            f"  - 时间: {c.get('time')}\n"
            f"  - 来源: {c.get('source_label')}\n"
            f"  - 摘要: {c.get('excerpt')}\n"
            f"  - 链接: {c.get('url') or '—'}\n"
        )

    disc = report.get("disclaimer") or "内容仅供研究参考，不构成投资建议。"
    lines.append(f"\n---\n\n*{disc}*\n")
    return "".join(lines)


def build_programmatic_citations(
    bundle: dict[str, Any],
    news_analysis: dict[str, Any],
    price: dict[str, Any],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    n = 0
    metrics = bundle.get("kline_metrics") or {}
    if metrics:
        n += 1
        out.append(
            {
                "id": f"market-{n}",
                "title": f"行情快照 {bundle.get('primary_symbol')}",
                "source_type": "market",
                "time": None,
                "url": None,
                "source_label": str(metrics.get("data_source") or "kline_pipeline / Yahoo"),
                "excerpt": (
                    f"收盘 {metrics.get('latest_close')} 涨跌幅 {metrics.get('change_pct')}% "
                    f"近源 {metrics.get('data_source') or ''}"
                )[:500],
            }
        )

    for e in (news_analysis.get("top_events") or [])[:15]:
        n += 1
        ts = e.get("published_ts")
        tss = None
        if ts:
            try:
                tss = datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
            except (TypeError, ValueError, OSError):
                tss = str(e.get("published") or "")
        out.append(
            {
                "id": f"news-{n}",
                "title": str(e.get("title") or "")[:200],
                "source_type": "news",
                "time": tss,
                "url": e.get("link"),
                "source_label": str(e.get("source") or "本地新闻库"),
                "excerpt": str(e.get("summary_excerpt") or "")[:400],
            }
        )

    for i, c in enumerate(bundle.get("issuer_chunks") or [], 1):
        out.append(
            {
                "id": f"filing-{i}",
                "title": str(c.get("original_name") or c.get("doc_id") or "上传文件"),
                "source_type": "filing",
                "time": None,
                "url": None,
                "source_label": "本地上传资料 RAG",
                "excerpt": str(c.get("text") or "")[:400],
            }
        )

    return out
