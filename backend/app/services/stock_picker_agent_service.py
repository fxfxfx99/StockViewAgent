"""可配置选股 Agent 执行器：系统数据快照 + 自定义提示词 + 结构化输出。"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from app.services import llm_client, transaction_agent_service
from app.storage import stock_picker_agent_store


def _fallback_result(agent: dict[str, Any], bundles: list[dict[str, Any]], errors: list[dict[str, str]]) -> dict[str, Any]:
    definition = agent.get("definition") or {}
    minimum = float(definition.get("min_score") or 65)
    maximum = max(1, min(int(definition.get("max_selected") or 5), 20))
    ranked = []
    for bundle in bundles:
        strategies = bundle.get("strategies") or []
        best = max(strategies, key=lambda x: float(x.get("score") or 0), default={})
        score = float(best.get("score") or bundle.get("summary", {}).get("average_score") or 0)
        item = {
            "stock_code": bundle.get("symbol"), "stock_name": bundle.get("stock_name"),
            "total_score": round(score, 1), "grade": "B" if score >= 75 else "C",
            "selection_reasons": (best.get("reasons") or [])[:4], "risk_points": (best.get("risks") or [])[:4],
            "support_level": "数据不足", "action": "重点跟踪" if score >= 75 else "仅观察",
            "data_gaps": ["尾盘分钟成交与集合竞价字段不足，不能判断强承接"],
        }
        ranked.append(item)
    ranked.sort(key=lambda x: x["total_score"], reverse=True)
    selected = [x for x in ranked if x["total_score"] >= minimum][:maximum]
    return {
        "market_environment": {"level": "C", "market_stage": "数据不足", "main_theme": [], "summary": "未启用或未成功调用大模型，使用本地多策略信号做保守初筛。", "strategy": "降低仓位"},
        "selected_stocks": selected,
        "watchlist": [x for x in ranked if x not in selected][:maximum],
        "excluded_stocks": errors,
        "summary": {"selected_count": len(selected), "best_candidate": selected[0]["stock_code"] if selected else "无", "main_logic": "按本地行情、资金与多策略评分保守排序", "overall_risk": "高"},
        "risk_disclaimer": "部分关键数据缺失，本次评分置信度有限；结果仅供研究，不构成投资建议。",
        "execution_mode": "rule_fallback",
    }


async def run_agent(user_id: int, agent_id: str, symbols: list[str]) -> dict[str, Any]:
    agent = stock_picker_agent_store.get_agent(user_id, agent_id)
    if not agent: raise ValueError("选股 Agent 不存在")
    if not agent.get("enabled"): raise ValueError("选股 Agent 已停用")
    normalized = list(dict.fromkeys(str(s).strip().upper() for s in symbols if str(s).strip()))[:30]
    if not normalized: raise ValueError("请至少提供一个候选股票代码")

    semaphore = asyncio.Semaphore(4)
    async def collect(symbol: str):
        async with semaphore:
            try: return await transaction_agent_service.generate_strategy_views(symbol), None
            except Exception as exc: return None, {"stock_code": symbol, "reason": str(exc)[:300]}
    pairs = await asyncio.gather(*(collect(s) for s in normalized))
    bundles = [x for x, _ in pairs if x]
    errors = [e for _, e in pairs if e]
    fallback = _fallback_result(agent, bundles, errors)
    definition = agent.get("definition") or {}
    messages = [
        {"role": "system", "content": f"{definition.get('system_prompt', '')}\n\n{definition.get('output_instruction', '')}\n只返回合法 JSON 对象。"},
        {"role": "user", "content": "请按本 Agent 定义筛选以下系统数据。缺失字段必须明确标记，禁止补造：\n" + json.dumps({"agent_definition": definition, "candidate_data": bundles, "collection_errors": errors}, ensure_ascii=False)[:80_000]},
    ]
    parsed, llm_error = await asyncio.to_thread(llm_client.chat_completion_json_array, messages, temperature=0.15, timeout=180.0)
    result = parsed if isinstance(parsed, dict) else fallback
    result["execution_mode"] = "llm" if isinstance(parsed, dict) else "rule_fallback"
    if llm_error: result["llm_warning"] = llm_error[:500]
    result["agent_snapshot"] = {"id": agent["id"], "name": agent["name"], "updated_at": agent["updated_at"]}
    result["candidate_count"] = len(normalized)
    return stock_picker_agent_store.save_run(user_id, agent_id, normalized, result)
