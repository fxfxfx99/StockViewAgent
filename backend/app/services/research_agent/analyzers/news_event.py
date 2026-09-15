"""新闻归类与情绪启发式（规则为主，避免虚构）。"""
from __future__ import annotations

import re
from typing import Any

_POS = re.compile(r"涨|增|超预|利好|突破|回购|分红|签约|增长|盈利|上调", re.I)
_NEG = re.compile(r"跌|减|低于预|利空|调查|诉讼|减持|亏损|下调|处罚|停牌|退市|风险", re.I)

_CATS = [
    ("业绩", r"业绩|财报|盈利|净利|营收|指引|EPS"),
    ("产品", r"产品|发布|新品|订单|量产"),
    ("监管", r"监管|证监会|交易所|问询|立案|处罚"),
    ("并购", r"收购|并购|重组|资产"),
    ("管理层", r"高管|董事|CEO|辞职|任命"),
    ("行业催化", r"行业|政策|补贴|景气"),
    ("诉讼", r"诉讼|仲裁|和解"),
    ("宏观影响", r"利率|通胀|美联储|关税|汇率"),
]


def _classify(title: str, summary: str) -> str:
    blob = f"{title} {summary}"
    for label, pat in _CATS:
        if re.search(pat, blob):
            return label
    return "其他"


def _sentiment(title: str, summary: str) -> str:
    blob = f"{title} {summary}"
    pn = len(_POS.findall(blob))
    nn = len(_NEG.findall(blob))
    if pn > nn + 1:
        return "正面"
    if nn > pn + 1:
        return "负面"
    return "中性"


def analyze_news(bundle: dict[str, Any]) -> dict[str, Any]:
    items = bundle.get("news") or []
    if not items:
        return {
            "narrative": "本地新闻库中未命中与该标的强相关条目（可能未入库或代码不匹配）。",
            "top_events": [],
            "by_category": {},
        }

    enriched: list[dict[str, Any]] = []
    by_cat: dict[str, int] = {}
    for it in items[:25]:
        title = str(it.get("title") or "")
        summary = str(it.get("summary") or "")[:400]
        cat = _classify(title, summary)
        sent = _sentiment(title, summary)
        by_cat[cat] = by_cat.get(cat, 0) + 1
        enriched.append(
            {
                "id": it.get("id"),
                "title": title,
                "published": it.get("published"),
                "published_ts": it.get("published_ts"),
                "source": it.get("source"),
                "link": it.get("link"),
                "category": cat,
                "sentiment": sent,
                "summary_excerpt": summary,
            }
        )

    lines = [f"本地库命中 {len(enriched)} 条相关新闻（节选分析）。"]
    for e in enriched[:8]:
        lines.append(f"- [{e['category']}/{e['sentiment']}] {e['title'][:80]}")

    return {
        "narrative": "\n".join(lines),
        "top_events": enriched,
        "by_category": by_cat,
    }
