import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from app.config import settings
from app.deps.auth import get_current_user, require_admin
from app.services import eastmoney_market, event_calendar_context, public_data_context, global_market, market_catalog, kline_learning_service as kls
from app.storage import kline_insight_store
from app.storage.users_store import UserRecord

router = APIRouter(prefix="/api/kline-learning", tags=["kline-learning"])


class PromptBody(BaseModel):
    """字段缺省为 None 时表示不覆盖该项（与 merge_prompt 配合）。"""

    system: str | None = None
    user_template: str | None = None
    market_system: str | None = None
    market_user_template: str | None = None
    strategies: dict[str, dict[str, str]] | None = None


class AnalyzeMarketBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    index_key: str = Field(default="SH_COMP", min_length=1, max_length=32)
    range_param: str = Field(default="1y", alias="range", pattern="^(3mo|6mo|1y|2y|5y)$")
    interval: str = Field(default="1d", pattern="^(1d|1wk|1mo)$")
    candle_rows: int = Field(default=35, ge=10, le=120)


class AnalyzeBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    symbol: str
    range_param: str = Field(default="1y", alias="range")
    interval: str = "1d"
    candle_rows: int = Field(default=35, ge=10, le=120)
    strategy: str = "comprehensive"
    prompt_modules: list[str] | None = None


class MarketChatBody(AnalyzeMarketBody):
    question: str = Field(min_length=2, max_length=2000)


@router.get("/materials")
def materials_list(_: Annotated[UserRecord, Depends(get_current_user)]):
    return {"items": kls.list_materials()}


@router.post("/materials")
async def materials_upload(
    _: Annotated[UserRecord, Depends(require_admin)],
    file: UploadFile = File(...),
    summary: str = Form(""),
):
    raw = await file.read()
    try:
        item = kls.add_material(raw, file.filename or "upload.txt", summary or None)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return item


@router.delete("/materials/{mid}")
def materials_delete(mid: str, _: Annotated[UserRecord, Depends(require_admin)]):
    if not kls.delete_material(mid):
        raise HTTPException(404, "未找到")
    return {"ok": True}


@router.get("/prompt")
def prompt_get(_: Annotated[UserRecord, Depends(get_current_user)]):
    return kls.get_prompt()


@router.put("/prompt")
def prompt_put(body: PromptBody, _: Annotated[UserRecord, Depends(require_admin)]):
    return kls.merge_prompt(
        system=body.system,
        user_template=body.user_template,
        market_system=body.market_system,
        market_user_template=body.market_user_template,
        strategies=body.strategies,
    )


@router.get("/major-indices")
def major_indices_list():
    return {"items": market_catalog.list_market_catalog()}


def _market_data_context(bundle):
    instrument = bundle.get("instrument") or {}
    return (
        f"【行情口径】市场={instrument.get('region', '')}；交易所={instrument.get('exchange', '')}；"
        f"类别={instrument.get('asset_class', '')}；单位={instrument.get('unit', '')}；"
        f"时区={instrument.get('timezone', 'Asia/Shanghai')}；来源={bundle.get('kline_source', '')}；"
        f"行情日期={bundle.get('data_as_of', '未知')}；历史回退={'是' if bundle.get('cache_stale') else '否'}。\n"
        f"{instrument.get('description', '')}\n"
        "以实际市场口径解读。历史回退不可描述为实时行情；缺失成交量不推断为零成交；"
        "连续期货存在换月和价差，区间价格变化不是可交易合约收益。"
    )


@router.post("/analyze-market")
async def analyze_market(body: AnalyzeMarketBody, _: Annotated[UserRecord, Depends(get_current_user)]):
    try:
        bundle = await global_market.fetch_market_kline(
            body.index_key, body.range_param, body.interval
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(502, "市场行情来源暂不可用，请稍后重试") from e

    candles = bundle.get("candles") or []
    metrics = bundle.get("metrics") or {}
    pr = kls.get_prompt()
    learning = kls.build_learning_context(max_total=4000)
    index_key = str(bundle.get("index_key") or body.index_key.strip().upper())
    index_label = str((bundle.get("instrument") or {}).get("label") or bundle.get("symbol") or index_key)
    try:
        user_msg = pr["market_user_template"].format(
            index_label=index_label,
            index_key=index_key,
            range_param=body.range_param,
            interval=body.interval,
            metrics=kls.escape_braces_for_str_format(kls.format_metrics(metrics)),
            candles_summary=kls.escape_braces_for_str_format(
                kls.format_candles_summary(candles, body.candle_rows, timezone_name=bundle.get("timezone", "Asia/Shanghai"))
            ),
            learning_context=kls.escape_braces_for_str_format(learning),
        )
    except KeyError as e:
        raise HTTPException(400, f"提示词模板占位符与参数不匹配: {e!s}") from e

    user_msg = _market_data_context(bundle) + "\n" + user_msg
    calendar = event_calendar_context.retrieve("kline", f"{index_label} 全球经济金融 利率 通胀 货币政策 风险")
    user_msg += event_calendar_context.prompt_evidence(calendar)
    public_data = public_data_context.retrieve(f"{index_label} 通胀 经济 汇率")
    user_msg = public_data_context.prompt_evidence(public_data) + user_msg
    analysis, err = await asyncio.to_thread(kls.llm_structured_interpret, pr["market_system"], user_msg)
    if err:
        raise HTTPException(502, err)
    text = kls.structured_analysis_markdown(analysis or kls.normalize_structured_analysis({}))
    return {
        "index_key": index_key,
        "index_label": index_label,
        "instrument": bundle.get("instrument"),
        "data_as_of": bundle.get("data_as_of"),
        "cache_stale": bool(bundle.get("cache_stale")),
        "kline_source": bundle.get("kline_source"),
        "source_url": bundle.get("source_url"),
        "cache_warning": bundle.get("cache_warning"),
        "range": body.range_param,
        "interval": body.interval,
        "interpretation": text,
        "analysis": analysis,
        "economic_calendar": calendar,
        "public_data": public_data,
        "model": settings.kline_llm_model,
    }


@router.post("/market-chat")
async def market_chat(body: MarketChatBody, _: Annotated[UserRecord, Depends(get_current_user)]):
    """基于所选指数的最新 K 线与指标回答简易行情问题。"""
    try:
        bundle = await global_market.fetch_market_kline(
            body.index_key, body.range_param, body.interval
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(502, "市场行情来源暂不可用，请稍后重试") from e

    candles = bundle.get("candles") or []
    metrics = bundle.get("metrics") or {}
    index_key = str(bundle.get("index_key") or body.index_key.strip().upper())
    index_label = str((bundle.get("instrument") or {}).get("label") or bundle.get("symbol") or index_key)
    context = (
        f"市场品种：{index_label}（{index_key}）\n"
        f"区间/周期：{body.range_param} / {body.interval}\n"
        f"指标：\n{kls.format_metrics(metrics)}\n"
        f"最近K线：\n{kls.format_candles_summary(candles, body.candle_rows, timezone_name=bundle.get('timezone', 'Asia/Shanghai'))}"
    )
    system = (
        "你是谨慎的中文市场行情解读助手。只基于提供的数据回答；先给结论，再列依据和风险。"
        "不要编造实时价格或新闻，不承诺收益，不给出确定性买卖指令。若数据不足要明确说明。"
    )
    user_msg = f"{_market_data_context(bundle)}\n{context}\n\n用户问题：{body.question.strip()}"
    calendar = event_calendar_context.retrieve("kline", f"{index_label} {body.question.strip()}")
    user_msg += event_calendar_context.prompt_evidence(calendar)
    public_data = public_data_context.retrieve(body.question.strip())
    user_msg = public_data_context.prompt_evidence(public_data) + user_msg
    text, err = await asyncio.to_thread(kls.llm_interpret, system, user_msg)
    if err:
        raise HTTPException(502, err)
    return {
        "answer": text,
        "economic_calendar": calendar,
        "public_data": public_data,
        "question": body.question.strip(),
        "index_key": index_key,
        "index_label": index_label,
        "instrument": bundle.get("instrument"),
        "data_as_of": bundle.get("data_as_of"),
        "cache_stale": bool(bundle.get("cache_stale")),
        "kline_source": bundle.get("kline_source"),
        "source_url": bundle.get("source_url"),
        "cache_warning": bundle.get("cache_warning"),
        "model": settings.kline_llm_model,
    }


@router.get("/insights/history")
def insight_history(
    user: Annotated[UserRecord, Depends(get_current_user)],
    symbol: str = Query(..., description="标的代码"),
):
    """仅当该标的曾手动成功解读过（opt_in）后返回历史条目；否则 dates 为空。"""
    sym = (symbol or "").strip().upper()
    if not sym:
        raise HTTPException(400, "缺少代码")
    return kline_insight_store.history_for_symbol(user.id, sym)


@router.post("/analyze")
async def analyze(body: AnalyzeBody, user: Annotated[UserRecord, Depends(get_current_user)]):
    sym = body.symbol.strip().upper()
    if not sym:
        raise HTTPException(400, "缺少代码")
    try:
        out = await kls.analyze_a_share_symbol(
            sym,
            range_param=body.range_param,
            interval=body.interval,
            candle_rows=body.candle_rows,
            strategy=body.strategy,
            prompt_modules=body.prompt_modules,
        )
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    except RuntimeError as e:
        raise HTTPException(502, str(e)) from e

    kline_insight_store.set_opt_in(user.id, sym, True)
    kline_insight_store.upsert_insight(
        user.id,
        sym,
        kline_insight_store.shanghai_report_date_str(),
        out.get("interpretation") or "",
        source="manual",
        model=str(out.get("model") or settings.kline_llm_model),
        economic_calendar=out.get("economic_calendar"),
        public_data=out.get("public_data"),
    )
    return out
