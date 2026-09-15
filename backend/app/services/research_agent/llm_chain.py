"""Planner / 合成 / 风险复核 / 引用格式化（失败则返回 None，由流水线回退）。"""
from __future__ import annotations

import json
from typing import Any

from app.services.llm_client import chat_completion_sync
from app.services.research_agent.prompts import (
    SYSTEM_RESEARCH_AGENT,
    USER_CITATION_FORMATTER,
    USER_PLANNER,
    USER_RISK_REVIEWER,
    USER_SYNTHESIZER,
)


def _strip_json(s: str) -> str:
    t = (s or "").strip()
    if t.startswith("```"):
        parts = t.split("```")
        t = parts[1] if len(parts) > 1 else t
        if t.startswith("json"):
            t = t[4:].lstrip()
    return t.strip()


def _parse_json_obj(s: str | None) -> dict[str, Any] | None:
    if not s:
        return None
    try:
        data = json.loads(_strip_json(s))
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def run_planner(
    user_query: str,
    primary_symbol: str,
    primary_name: str,
    primary_market: str,
    compare_hint: str,
) -> dict[str, Any] | None:
    content = USER_PLANNER.format(
        user_query=user_query[:4000],
        primary_symbol=primary_symbol,
        primary_name=primary_name,
        primary_market=primary_market,
        compare_hint=compare_hint or "（无）",
    )
    raw, err = chat_completion_sync(
        [
            {"role": "system", "content": SYSTEM_RESEARCH_AGENT},
            {"role": "user", "content": content},
        ],
        temperature=0.15,
        timeout=60.0,
    )
    if err:
        return None
    return _parse_json_obj(raw)


def run_synthesizer(user_query: str, plan: dict[str, Any], facts: dict[str, Any]) -> dict[str, Any] | None:
    # 控制体积：新闻只留标题+摘要前 120 字
    slim = json.loads(json.dumps(facts, ensure_ascii=False))
    if isinstance(slim.get("news"), list):
        slim["news"] = [
            {k: x.get(k) for k in ("title", "published", "source", "link", "category", "sentiment")}
            | {"summary": (x.get("summary_excerpt") or "")[:120]}
            for x in (slim.get("news") or [])[:20]
        ]
    if isinstance(slim.get("issuer_chunks"), list):
        slim["issuer_chunks"] = [
            {"original_name": x.get("original_name"), "text": (x.get("text") or "")[:200]} for x in slim["issuer_chunks"][:8]
        ]

    content = USER_SYNTHESIZER.format(
        user_query=user_query[:4000],
        plan_json=json.dumps(plan, ensure_ascii=False)[:4000],
        facts_json=json.dumps(slim, ensure_ascii=False)[:24000],
    )
    raw, err = chat_completion_sync(
        [
            {"role": "system", "content": SYSTEM_RESEARCH_AGENT},
            {"role": "user", "content": content},
        ],
        temperature=0.25,
        timeout=120.0,
    )
    if err:
        return None
    return _parse_json_obj(raw)


def run_risk_reviewer(facts: dict[str, Any], memo_draft: dict[str, Any]) -> dict[str, Any] | None:
    content = USER_RISK_REVIEWER.format(
        facts_json=json.dumps(facts, ensure_ascii=False)[:12000],
        memo_draft_json=json.dumps(memo_draft, ensure_ascii=False)[:12000],
    )
    raw, err = chat_completion_sync(
        [
            {"role": "system", "content": SYSTEM_RESEARCH_AGENT},
            {"role": "user", "content": content},
        ],
        temperature=0.1,
        timeout=60.0,
    )
    if err:
        return None
    return _parse_json_obj(raw)


def run_citation_formatter(citations_raw: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    content = USER_CITATION_FORMATTER.format(citations_raw_json=json.dumps(citations_raw, ensure_ascii=False)[:16000])
    raw, err = chat_completion_sync(
        [
            {"role": "system", "content": SYSTEM_RESEARCH_AGENT},
            {"role": "user", "content": content},
        ],
        temperature=0.1,
        timeout=45.0,
    )
    if err:
        return None
    data = _parse_json_obj(raw)
    if not data:
        return None
    arr = data.get("citations")
    return arr if isinstance(arr, list) else None
