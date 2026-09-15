"""News relevance → gated potential impact. Failed output never becomes an AI opinion."""
from __future__ import annotations

import json
import re
import time
from threading import Event
from typing import Any

from pydantic import ValidationError

from app.config import settings
from app.schemas.analysis import ImpactResult, RelevanceResult
from app.schemas.news import analysis_content_hash, analysis_input_snapshot, clean_text
from app.services import a_share_stocks, analysis_context, llm_client, prompt_loader, news_watchlist_matcher
from app.storage import profile_store


class AnalysisCancelled(RuntimeError):
    """The in-flight request may finish, but no later stage or retry may start."""


class _OutputIssue(ValueError):
    """Only application-authored field paths and error types may reach a retry."""


def _provider_issue(error: str) -> str:
    if error in {"LLM 未返回合法 JSON", "LLM 响应格式无效", "LLM 返回空内容"}:
        return "$:invalid_json_or_empty_response"
    if error in {"LLM 服务返回 HTTP 401", "LLM 服务返回 HTTP 403"}:
        return "$:authentication_error"
    if error in {"LLM 请求超时", "LLM 网络请求失败"}:
        return "$:connection_error"
    return "$:provider_error"


def _validation_issue(error: ValidationError, model: type) -> str:
    """Omit values, arbitrary extra keys, validator messages and provider text."""
    schema = model.model_json_schema()
    fields = set(schema.get("properties", {}))
    for definition in schema.get("$defs", {}).values():
        fields.update(definition.get("properties", {}))
    safe = []
    for detail in error.errors(include_input=False, include_context=False, include_url=False)[:8]:
        parts = [str(part) if isinstance(part, int) else part if part in fields else "unknown_field"
                 for part in detail.get("loc", ())]
        kind = str(detail.get("type", "validation_error"))
        kind = kind if re.fullmatch(r"[a-z_]{1,80}", kind) else "validation_error"
        safe.append(f"{'.'.join(parts) or '$'}:{kind}")
    return "; ".join(safe) or "$:validation_error"


def _validate_evidence(rows: list[dict[str, Any]], payload: dict[str, Any]) -> None:
    sources = {source["source_id"]: source for source in payload.get("context", {}).get("evidence_sources", [])}
    for row in rows:
        references = row["evidence"]
        if not any(reference["source_id"] == "N0" for reference in references):
            raise _OutputIssue("evidence.source_id:current_news_required")
        for reference in references:
            source_id = reference["source_id"]
            source = sources.get(source_id)
            if source is None:
                raise _OutputIssue("evidence.source_id:unknown_source")
            if source_id.startswith("P") and source.get("stock_code") != row["stock_code"]:
                raise _OutputIssue("evidence.source_id:wrong_stock_profile")
            quote = " ".join(reference.get("quote", "").split())
            if source_id == "N0" and not quote:
                raise _OutputIssue("evidence.quote:current_news_quote_required")
            if quote and not any(quote in " ".join(str(source.get(field) or "").split()) for field in ("text", "title")):
                raise _OutputIssue("evidence.quote:not_in_source")


def _failure_reason(stage: str, error: str | None) -> str:
    detail = error or "模型输出未通过结构与证据校验"
    if detail in {"LLM 服务返回 HTTP 401", "LLM 服务返回 HTTP 403"}:
        detail += "，请检查 API Key 和模型访问权限"
    elif detail == "LLM 服务返回 HTTP 404":
        detail += "，请检查模型是否仍可用，以及接口地址和模型访问权限"
    elif detail in {"LLM 请求超时", "LLM 网络请求失败"}:
        detail += "，请检查服务连接后重试"
    return f"{stage}失败：{detail}。"


def _check_cancelled(cancel_event: Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise AnalysisCancelled("Analysis cancelled")


def _option(name: str, default: int) -> int:
    return int(getattr(settings, f"effective_{name}", getattr(settings, name, default)))


def _symbol_name(symbol: str) -> str:
    profile = profile_store.merge_display(profile_store.get_entry(symbol))
    name = str(profile.get("name") or "").strip()
    if not name:
        name = str((a_share_stocks.lookup_by_yahoo_symbol(symbol) or {}).get("name") or "").strip()
    return name


def _stock_context(symbol: str) -> dict[str, str]:
    """Supply only saved company context; empty fields explicitly remain unknown."""
    profile = profile_store.merge_display(profile_store.get_entry(symbol))
    return {
        "stock_code": symbol,
        "stock_name": clean_text(_symbol_name(symbol), 120),
        "org_name": clean_text(profile.get("org_name"), 200),
        "industry": clean_text(profile.get("industry"), 200),
        "main_business": clean_text(profile.get("main_business"), 1200),
    }


def _is_stale(item: dict[str, Any], *, max_age_days: int | None = None) -> bool:
    max_days = max_age_days if max_age_days is not None else _option("news_max_age_days", 7)
    if max_age_days is not None:
        # Backfills may widen the global age setting, but may not describe an
        # unknown or future publication date as news from the requested window.
        try:
            stamp = float(item.get("published_ts"))
            now = time.time()
            return not now - max_days * 86400 <= stamp <= now
        except (ValueError, TypeError, OverflowError):
            return True
    if max_days <= 0 or item.get("published_ts") is None:
        return False
    try:
        return float(item["published_ts"]) < time.time() - max_days * 86400
    except (ValueError, TypeError):
        return False


def _filter_by_max_age(items: list[dict[str, Any]], *, max_age_days: int | None = None) -> list[dict[str, Any]]:
    """Compatibility helper; analyze_batch itself preserves positional alignment."""
    return [item for item in items if not _is_stale(item, max_age_days=max_age_days)]


def _not_analyzed(symbol: str, name: str, status: str, message: str) -> dict[str, Any]:
    return {"stock_code": symbol, "stock_name": name, "status": status, "reason": message,
            "reasoning": "", "relevance_score": None, "relevance_type": None,
            "sentiment": None, "impact_direction": None, "impact_strength": None,
            "time_horizon": None, "confidence": None, "key_factors": [], "risk_points": [],
            "facts": [], "inferences": [], "uncertainties": [], "analyzed_at": time.time()}


def _validated_call(prompt: str, payload: dict[str, Any], model: type, symbols: list[str],
                    cancel_event: Event | None = None) -> tuple[list[dict[str, Any]] | None, str | None]:
    system = prompt_loader.load_prompt("system_prompt") + "\n\n" + prompt_loader.load_prompt(prompt)
    item_schema = model.model_json_schema()
    definitions = item_schema.pop("$defs", {})
    if model is ImpactResult:
        item_schema["properties"]["evidence"]["contains"] = {
            "type": "object", "required": ["source_id", "quote"],
            "properties": {"source_id": {"const": "N0"},
                           "quote": {"type": "string", "minLength": 1, "pattern": r"\S"}},
        }
    contract = {"type": "array", "items": item_schema, "minItems": len(symbols), "maxItems": len(symbols)}
    if definitions:
        contract["$defs"] = definitions
    system += ("\n\n应用输出契约（必须遵守；旧 Prompt 中的字段示例仅供参考）：\n"
               "每只输入股票必须且只能输出一个对象。仅输出满足以下 JSON Schema 的数组，不得添加字段。\n"
               + json.dumps(contract, ensure_ascii=False))
    if model is ImpactResult:
        system += ("\n证据规则：evidence 只能引用 context.evidence_sources 中实际存在的 source_id，必须包含当前新闻 N0；"
                   "N0 的 quote 必须非空，并逐字摘录本条新闻的 text 或 title；仅有标题时摘录标题。"
                   "P 开头的公司资料只可用于其 stock_code 对应股票。其他来源 quote 可为空；非空时必须逐字摘录该来源的 text 或 title，"
                   "不得拼接或编造引用、数字、日期。历史材料只作背景，不能冒充本次事件的最新进展。"
                   "填写明确的事件、传导环节、兑现条件、反证、后续观察与置信度理由；未知信息直接标注未知。")
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
    retries = max(0, min(_option("analysis_integrity_retries", 1), 3))
    failure = "模型输出未通过结构与证据校验"
    for attempt in range(retries + 1):
        _check_cancelled(cancel_event)
        data, error = llm_client.chat_completion_json_array(messages, temperature=0.2,
                                                          timeout=getattr(settings, "llm_timeout_seconds", 90),
                                                          cancel_event=cancel_event)
        _check_cancelled(cancel_event)
        if error:
            failure = llm_client.safe_error(error)
            issue = _provider_issue(failure)
            if not llm_client.is_retryable_error(failure):
                break
        else:
            try:
                if not isinstance(data, list) or len(data) != len(symbols):
                    raise _OutputIssue("$:wrong_result_count")
                rows = [model.model_validate(row).model_dump() for row in data]
                codes = [row["stock_code"] for row in rows]
                if set(codes) != set(symbols) or len(set(codes)) != len(codes):
                    raise _OutputIssue("stock_code:wrong_stock_mapping")
                if model is ImpactResult:
                    directions = {"positive": "bullish", "neutral": "neutral", "negative": "bearish"}
                    if any(directions[row["sentiment"]] != row["impact_direction"] for row in rows):
                        raise _OutputIssue("impact_direction:inconsistent_sentiment")
                    _validate_evidence(rows, payload)
                return rows, None
            except ValidationError as validation:
                issue = _validation_issue(validation, model)
            except _OutputIssue as validation:
                issue = str(validation)
            except (ValueError, TypeError):
                issue = "$:invalid_output"
            failure = f"模型输出未通过结构与证据校验（{issue}）"
        # The retry never incorporates arbitrary provider errors or invalid model output.
        if attempt < retries:
            messages[0] = {"role": "system", "content": system + "\n前次校验问题（仅字段路径与错误类型）：" + issue + "\n请根据原始输入重新输出完整结果。"}
    return None, failure


def _apply_quality_limit(result: dict[str, Any], stock: dict[str, str], context: dict[str, Any]) -> None:
    quality = context.get("quality", {})
    cap = max(0, min(int(quality.get("confidence_cap", 100)), 100))
    notes = list(quality.get("limitations") or [])
    if result.get("relevance_type") != "direct" and not stock.get("industry") and not stock.get("main_business"):
        cap = min(cap, 60)
        notes.append("未提供该公司的行业和主营业务资料，间接关联的传导链仍需核实。")
    result["confidence"] = min(result["confidence"], cap)
    if cap < 100:
        notes.append(f"根据当前材料质量，置信度上限为 {cap} 分。")
    result["quality_notes"] = list(dict.fromkeys(notes))


def _analyze_article(item: dict[str, Any], stocks: list[dict[str, str]],
                     cancel_event: Event | None = None, *, max_age_days: int | None = None) -> dict[str, Any]:
    _check_cancelled(cancel_event)
    symbols = [stock["stock_code"] for stock in stocks]
    names = {stock["stock_code"]: stock["stock_name"] for stock in stocks}
    status, message = "", ""
    if _is_stale(item, max_age_days=max_age_days):
        status = "stale"
        message = (f"新闻日期无法确认处于近 {max_age_days} 天内，保留历史资讯。" if max_age_days is not None
                   else "新闻已超出配置的分析时效，保留历史资讯。")
    elif not settings.any_llm_key_configured:
        status, message = "unavailable", "尚未配置大模型，新闻已归档，配置后可补充分析。"
    if status:
        return {"status": status, "related_stocks": [_not_analyzed(s, names[s], status, message) for s in symbols]}
    snapshot = analysis_input_snapshot(item)
    article = {"title": snapshot["title"], "content": snapshot["content_excerpt"],
               "published_at": snapshot["published_at"], "source": snapshot["source"],
               "source_hint": item.get("symbol_hint")}
    try:
        matches = news_watchlist_matcher.article_match_evidence(item, symbols)
        context = analysis_context.build_analysis_context(item, stocks)
        context["matching_evidence"] = matches
        _check_cancelled(cancel_event)
        saved_context = {"analysis_input": snapshot, "input_content_hash": analysis_content_hash(snapshot),
                         "analysis_context": context, "analysis_version": 2}
        relevance, relevance_error = _validated_call("news_relevance", {"news": article, "stocks": stocks, "context": context},
                                                     RelevanceResult, symbols, cancel_event)
        if relevance is None:
            return {"status": "error", "related_stocks": [
                {**_not_analyzed(s, names[s], "error", _failure_reason("相关性分析", relevance_error)), **saved_context}
                for s in symbols]}
        threshold = max(0, min(_option("news_relevance_threshold", 60), 100))
        qualified = [row for row in relevance if row["information_value"] and row["relevance_type"] != "weak"
                     and row["relevance_score"] > 0
                     and (row["relevance_score"] >= threshold or matches.get(row["stock_code"]))]
        context_by_symbol = {stock["stock_code"]: stock for stock in stocks}
        qualified_context = [{**row, **context_by_symbol[row["stock_code"]]} for row in qualified]
        impact, impact_error = _validated_call("stock_impact_analysis", {"news": article, "stocks": qualified_context, "context": context},
                                              ImpactResult, [row["stock_code"] for row in qualified], cancel_event) if qualified else ([], None)
        impacts = {row["stock_code"]: row for row in (impact or [])}
        output = []
        for row in relevance:
            symbol = row["stock_code"]
            if row in qualified and symbol in impacts:
                result = {**row, **impacts[symbol], "status": "analyzed"}
                _apply_quality_limit(result, context_by_symbol[symbol], context)
            elif row in qualified:
                result = {**_not_analyzed(symbol, names[symbol], "error", ""), **row,
                          "reason": _failure_reason("潜在影响分析", impact_error), "relevance_reason": row["reason"]}
            else:
                result = {**_not_analyzed(symbol, names[symbol], "irrelevant", row["reason"]), **row}
            result["stock_name"] = names[symbol]
            result["match_evidence"] = matches.get(symbol, [])
            result["relevance_threshold"] = threshold
            result["analyzed_at"] = time.time()
            result.update(saved_context)
            output.append(result)
        return {"status": "error" if any(row["status"] == "error" for row in output) else "analyzed",
                "related_stocks": output}
    except (OSError, ValueError):
        return {"status": "error", "related_stocks": [_not_analyzed(s, names[s], "error", "分析配置或输出无效，请检查 Prompt 文件。") for s in symbols]}


def analyze_batch(items: list[dict[str, Any]], watchlist: list[str], *,
                  cancel_event: Event | None = None, max_age_days: int | None = None) -> list[dict[str, Any]]:
    """One result per input, even for old/invalid news or missing model credentials."""
    _check_cancelled(cancel_event)
    if max_age_days is not None and not 1 <= max_age_days <= 30:
        raise ValueError("max_age_days must be between 1 and 30")
    symbols = list(dict.fromkeys(str(s).strip().upper() for s in watchlist if str(s).strip()))
    stocks = [_stock_context(symbol) for symbol in symbols]
    return [_analyze_article(item, stocks, cancel_event, max_age_days=max_age_days) for item in items]
