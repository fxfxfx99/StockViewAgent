"""市场资金流向聚合：行业板块多周期 + 机构类型代理指标。

公开数据源对“国家队、游资、量化、散户”等没有统一实时字段。本模块只把可直连字段
直接呈现，其余分类以代理指标和置信度表达，避免伪精确。
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime
from typing import Any

import httpx

from app.services import market_extra_http
from app.services.market_time import SHANGHAI_TZ, market_date
from app.storage import market_history_cache, market_source_cache

logger = logging.getLogger(__name__)
_EAST_REQUEST_LOCK = asyncio.Lock()
_EAST_FAILURE_UNTIL = 0.0

_PERIOD_META = {
    "1d": {"label": "单日", "east_indicator": "今日", "rank_field": "main_net_inflow_1d"},
    "5d": {"label": "五日", "east_indicator": "5日", "rank_field": "main_net_inflow_5d"},
    "10d": {"label": "十日", "east_indicator": "10日", "rank_field": "main_net_inflow_10d"},
    "20d": {"label": "二十日", "east_indicator": "20日", "rank_field": "main_net_inflow_20d"},
}

_EAST_PERIOD_FIELDS = {
    # 东财板块资金流排名字段口径，与 AKShare stock_sector_fund_flow_rank 文档一致：
    # 今日支持主力/超大/大/中/小单净额与占比；5日、10日支持对应周期字段。
    "今日": {
        "change_pct": "f3",
        "main": "f62",
        "main_pct": "f184",
        "super": "f66",
        "super_pct": "f69",
        "large": "f72",
        "large_pct": "f75",
        "medium": "f78",
        "medium_pct": "f81",
        "small": "f84",
        "small_pct": "f87",
        "leader_name": "f128",
        "leader_code": "f140",
        "leader_change_pct": "f136",
    },
    "5日": {
        "change_pct": "f109",
        "main": "f164",
        "main_pct": "f165",
        "super": "f166",
        "super_pct": "f167",
        "large": "f168",
        "large_pct": "f169",
        "medium": "f170",
        "medium_pct": "f171",
        "small": "f172",
        "small_pct": "f173",
        "leader_name": "f128",
        "leader_code": "f140",
        "leader_change_pct": "f136",
    },
    "10日": {
        "change_pct": "f160",
        "main": "f174",
        "main_pct": "f175",
        "super": "f176",
        "super_pct": "f177",
        "large": "f178",
        "large_pct": "f179",
        "medium": "f180",
        "medium_pct": "f181",
        "small": "f182",
        "small_pct": "f183",
        "leader_name": "f128",
        "leader_code": "f140",
        "leader_change_pct": "f136",
    },
}

def _east_field_batches(fmap: dict[str, str]) -> tuple[str, list[str]]:
    """东财对长 fields 参数不稳定；核心排名保持三字段，其余分小批补齐。"""
    core = ",".join(("f12", "f14", fmap["main"]))
    optional = [
        "f124", fmap["change_pct"], fmap["main_pct"], fmap["leader_name"], fmap["leader_code"], fmap["leader_change_pct"],
        fmap["super"], fmap["super_pct"], fmap["large"], fmap["large_pct"],
        fmap["medium"], fmap["medium_pct"], fmap["small"], fmap["small_pct"],
    ]
    return core, [",".join(("f12", *optional[i : i + 4])) for i in range(0, len(optional), 4)]


async def _fetch_eastmoney_diff(params: dict[str, Any], fields: str) -> list[dict[str, Any]]:
    global _EAST_FAILURE_UNTIL
    async with _EAST_REQUEST_LOCK:
        if time.monotonic() < _EAST_FAILURE_UNTIL:
            raise RuntimeError("东方财富资金流接口处于短暂冷却期")
        return await _fetch_eastmoney_diff_unlocked(params, fields)


async def _fetch_eastmoney_diff_unlocked(params: dict[str, Any], fields: str) -> list[dict[str, Any]]:
    global _EAST_FAILURE_UNTIL
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            async with market_extra_http._async_client() as client:  # noqa: SLF001
                r = await client.get(
                    "https://push2.eastmoney.com/api/qt/clist/get",
                    params={**params, "fields": fields},
                    headers={"Referer": "https://data.eastmoney.com/bkzj/hy.html", "User-Agent": market_extra_http.UA},
                )
                r.raise_for_status()
                diff = ((r.json() or {}).get("data") or {}).get("diff") or []
                if isinstance(diff, dict):
                    return [diff[k] for k in sorted(diff, key=lambda x: int(x) if str(x).isdigit() else 0)]
                return diff if isinstance(diff, list) else []
        except (httpx.HTTPError, ValueError) as exc:
            last_error = exc
            if attempt == 0:
                await asyncio.sleep(0.6)
    if last_error is not None:
        _EAST_FAILURE_UNTIL = time.monotonic() + 120.0
        raise last_error
    return []


def _num(value: Any) -> float | None:
    if value in (None, "", "-", "--"):
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(x):
        return None
    return x


def _dt_from_f124(value: Any) -> str:
    try:
        ts = int(float(value))
    except (TypeError, ValueError):
        return datetime.now(SHANGHAI_TZ).date().isoformat()
    if ts <= 0:
        return datetime.now(SHANGHAI_TZ).date().isoformat()
    return market_date(ts).isoformat()


async def fetch_eastmoney_sector_fund_flow_rank(indicator: str = "今日", limit: int = 80) -> list[dict[str, Any]]:
    """东方财富行业板块资金流排名（行业资金流，fs=m:90+t:2）。"""
    fmap = _EAST_PERIOD_FIELDS.get(indicator)
    if not fmap:
        raise ValueError(f"不支持的板块资金周期: {indicator}")

    params = {
        "pn": 1,
        "pz": max(20, min(int(limit or 80), 200)),
        "po": 1,
        "np": 1,
        "fltt": 2,
        "invt": 2,
        "fid": fmap["main"],
        "fs": "m:90+t:2",
    }
    core_fields, optional_batches = _east_field_batches(fmap)
    rows = await _fetch_eastmoney_diff(params, core_fields)
    details: list[dict[str, Any]] = []
    for fields in optional_batches:
        try:
            details.extend(await _fetch_eastmoney_diff(params, fields))
        except Exception as exc:  # 主排名可用时，补充字段允许部分降级为空
            logger.info("行业资金流补充字段降级 indicator=%s fields=%s error=%s", indicator, fields, str(exc)[:300])
    detail_by_code: dict[str, dict[str, Any]] = {}
    for row in details:
        if isinstance(row, dict):
            detail_by_code.setdefault(str(row.get("f12") or ""), {}).update(row)
    rows = [{**row, **detail_by_code.get(str(row.get("f12") or ""), {})} for row in rows if isinstance(row, dict)]

    out: list[dict[str, Any]] = []
    for idx, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        out.append(
            {
                "rank": idx,
                "sector_code": str(row.get("f12") or ""),
                "sector_name": str(row.get("f14") or ""),
                "trade_date": _dt_from_f124(row.get("f124")),
                "period": indicator,
                "change_pct": _num(row.get(fmap["change_pct"])),
                "main_net_inflow": _num(row.get(fmap["main"])),
                "main_net_inflow_pct": _num(row.get(fmap["main_pct"])),
                "super_large_net_inflow": _num(row.get(fmap["super"])),
                "super_large_net_inflow_pct": _num(row.get(fmap["super_pct"])),
                "large_net_inflow": _num(row.get(fmap["large"])),
                "large_net_inflow_pct": _num(row.get(fmap["large_pct"])),
                "medium_net_inflow": _num(row.get(fmap["medium"])),
                "medium_net_inflow_pct": _num(row.get(fmap["medium_pct"])),
                "small_net_inflow": _num(row.get(fmap["small"])),
                "small_net_inflow_pct": _num(row.get(fmap["small_pct"])),
                "leader_name": str(row.get(fmap["leader_name"]) or ""),
                "leader_code": str(row.get(fmap["leader_code"]) or ""),
                "leader_change_pct": _num(row.get(fmap["leader_change_pct"])),
                "source": "eastmoney_push2_sector_fund_flow",
            }
        )
    if not out:
        raise ValueError("东方财富行业资金流未返回有效数据")
    return out


def _cache_key(period: str) -> str:
    return f"industry:{period}"


async def _sector_rank_with_cache(period: str, limit: int) -> dict[str, Any]:
    meta = _PERIOD_META[period]
    indicator = meta["east_indicator"]
    try:
        if indicator == "20日":
            rows = _derive_20d_from_cache(limit)
            source = "local_cache_derived_from_10d_or_daily"
            warning = "二十日为缓存/十日代理推导，待接入更完整历史源后会自动替换。"
        else:
            rows = await asyncio.wait_for(fetch_eastmoney_sector_fund_flow_rank(indicator, limit), timeout=28.0)
            source = "eastmoney_push2"
            warning = None
        cached = market_history_cache.merge_save_series("sector_fund_flow_rank", _cache_key(period), rows)
        market_source_cache.record_health(f"sector_fund_flow_{period}", ok=True, detail=source)
        return {
            "period": period,
            "label": meta["label"],
            "items": cached["items"][:limit],
            "cache_saved_at": cached["saved_at"],
            "served_from_cache": False,
            "source": source,
            "source_warning": warning,
        }
    except Exception as exc:  # noqa: BLE001
        err = str(exc) or exc.__class__.__name__
        logger.warning("行业资金流主源失败 period=%s error=%s", period, err[:500])
        market_source_cache.record_health(f"sector_fund_flow_{period}", ok=False, detail=err[:500])
        cached = market_history_cache.load_series("sector_fund_flow_rank", _cache_key(period))
        if cached:
            return {
                "period": period,
                "label": meta["label"],
                "items": (cached.get("items") or [])[:limit],
                "cache_saved_at": cached.get("saved_at"),
                "served_from_cache": True,
                "source": "local_cache",
                "source_warning": "实时行业资金流暂不可用，当前展示最近一次成功缓存。",
            }
        return {
            "period": period,
            "label": meta["label"],
            "items": [],
            "cache_saved_at": None,
            "served_from_cache": False,
            "source": "unavailable",
            "source_warning": "实时行业资金流暂不可用，且尚无可用缓存，请稍后重试。",
        }


def _derive_20d_from_cache(limit: int) -> list[dict[str, Any]]:
    ten = market_history_cache.load_series("sector_fund_flow_rank", _cache_key("10d")) or {}
    rows = []
    for row in ten.get("items") or []:
        if not isinstance(row, dict):
            continue
        out = dict(row)
        out["period"] = "20日"
        out["main_net_inflow"] = _num(row.get("main_net_inflow"))
        out["source"] = "local_cache_20d_proxy_from_10d"
        out["estimate"] = True
        rows.append(out)
    return rows[:limit]


def _by_sector(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(r.get("sector_code") or r.get("sector_name") or ""): r for r in rows if isinstance(r, dict)}


def _trend_reversal(periods: dict[str, dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    one = _by_sector(periods.get("1d", {}).get("items") or [])
    five = _by_sector(periods.get("5d", {}).get("items") or [])
    ten = _by_sector(periods.get("10d", {}).get("items") or [])
    rows: list[dict[str, Any]] = []
    for key, r1 in one.items():
        r5 = five.get(key) or {}
        r10 = ten.get(key) or {}
        v1 = _num(r1.get("main_net_inflow")) or 0.0
        v5 = _num(r5.get("main_net_inflow")) or 0.0
        v10 = _num(r10.get("main_net_inflow")) or 0.0
        if v1 > 0 and (v5 < 0 or v10 < 0):
            direction = "转强"
            score = abs(v1) + abs(min(v5, v10, 0))
        elif v1 < 0 and (v5 > 0 or v10 > 0):
            direction = "转弱"
            score = abs(v1) + abs(max(v5, v10, 0))
        else:
            continue
        rows.append(
            {
                "sector_code": r1.get("sector_code"),
                "sector_name": r1.get("sector_name"),
                "trade_date": r1.get("trade_date"),
                "direction": direction,
                "reversal_score": score,
                "main_net_inflow_1d": v1,
                "main_net_inflow_5d": v5,
                "main_net_inflow_10d": v10,
                "leader_name": r1.get("leader_name") or r5.get("leader_name") or r10.get("leader_name"),
                "source": "derived_from_eastmoney_sector_periods",
            }
        )
    rows.sort(key=lambda x: float(x.get("reversal_score") or 0), reverse=True)
    return rows[:limit]


def _sum_latest(rows: list[dict[str, Any]], field: str) -> float | None:
    values = [_num(r.get(field)) for r in rows if isinstance(r, dict)]
    values = [v for v in values if v is not None]
    if not values:
        return None
    return float(sum(values))


async def _institutional_proxy(limit: int) -> dict[str, Any]:
    errors: dict[str, str] = {}

    async def _safe(name: str, coro):
        try:
            return await coro
        except Exception as exc:  # noqa: BLE001
            errors[name] = str(exc)[:500]
            return []

    north, margin, hot = await asyncio.gather(
        _safe("north_flow_history", market_extra_http.fetch_north_flow_history()),
        _safe("margin", market_extra_http.fetch_securities_margin()),
        _safe("hot_rank_ths", market_extra_http.fetch_hot_rank_ths()),
    )
    if north:
        market_history_cache.merge_save_series("north_flow", "history", north)
    else:
        cached = market_history_cache.load_series("north_flow", "history")
        north = (cached or {}).get("items") or []
    if margin:
        market_history_cache.merge_save_series("margin", "market", margin)
    else:
        cached = market_history_cache.load_series("margin", "market")
        margin = (cached or {}).get("items") or []

    latest_north = north[0] if north else {}
    latest_margin = margin[0] if margin else {}
    net_north = _num(latest_north.get("net_tgt"))
    rz_change = _num(latest_margin.get("rzrqyecz"))
    hot_avg = None
    hot_values = [_num(x.get("hot_value")) for x in (hot or [])[: min(limit, 30)]]
    hot_values = [x for x in hot_values if x is not None]
    if hot_values:
        hot_avg = sum(hot_values) / len(hot_values)

    def signal(v: float | None, pos="流入", neg="流出") -> str:
        if v is None:
            return "待确认"
        if v > 0:
            return pos
        if v < 0:
            return neg
        return "中性"

    items = [
        {
            "type": "northbound_institution",
            "label": "北向/外资机构",
            "signal": signal(net_north),
            "net_flow_yuan": net_north,
            "confidence": "high" if net_north is not None else "low",
            "source": "eastmoney_mutual_deal_history",
            "description": "用沪股通+深股通净买入作为外资机构代理口径。",
        },
        {
            "type": "domestic_institution",
            "label": "境内机构/主力",
            "signal": "看行业大单共振",
            "net_flow_yuan": None,
            "confidence": "medium",
            "source": "eastmoney_sector_large_orders",
            "description": "用行业超大单+大单净额作为机构/主力代理，需结合行业排行表。",
        },
        {
            "type": "national_team",
            "label": "国家队/政策资金",
            "signal": "暂无实时直连",
            "net_flow_yuan": None,
            "confidence": "low",
            "source": "fallback_plan",
            "description": "公开实时接口通常不直接披露国家队流向；备用关注宽基ETF异常放量、中央汇金/证金公告、十大股东季报。",
        },
        {
            "type": "hot_money",
            "label": "游资",
            "signal": "热度升温" if hot_avg and hot_avg > 0 else "待确认",
            "net_flow_yuan": None,
            "confidence": "low",
            "source": "ths_hot_rank_proxy + dragon_tiger_future",
            "description": "用热股榜、题材热度和后续龙虎榜营业部识别做代理，实时结论仅作线索。",
        },
        {
            "type": "quant",
            "label": "量化资金",
            "signal": "待确认",
            "net_flow_yuan": None,
            "confidence": "low",
            "source": "turnover_and_order_size_proxy",
            "description": "公开接口难以直接区分量化，后续可接入高换手、成交额突变、ETF/指数增强持仓等代理指标。",
        },
        {
            "type": "retail",
            "label": "散户",
            "signal": signal(rz_change, "杠杆风险偏好上升", "杠杆风险偏好下降"),
            "net_flow_yuan": rz_change,
            "confidence": "medium" if rz_change is not None else "low",
            "source": "eastmoney_margin + sector_small_orders",
            "description": "用两融变化和小单净流入作为散户/杠杆情绪代理，不能等同真实账户类型。",
        },
    ]
    return {"items": items, "errors": errors or None}


async def get_market_fund_flow_dashboard(limit: int = 30) -> dict[str, Any]:
    limit = max(10, min(int(limit or 30), 100))
    p1, p5, p10 = await asyncio.gather(
        _sector_rank_with_cache("1d", limit),
        _sector_rank_with_cache("5d", limit),
        _sector_rank_with_cache("10d", limit),
    )
    periods = {"1d": p1, "5d": p5, "10d": p10}
    periods["20d"] = await _sector_rank_with_cache("20d", limit)
    reversal = {
        "period": "reversal",
        "label": "趋势逆转",
        "items": _trend_reversal(periods, limit),
        "source": "derived_from_1d_5d_10d",
    }
    institution = await _institutional_proxy(limit)
    sources = [
        {
            "name": "东方财富板块资金流",
            "role": "行业单日/五日/十日主源",
            "url": "https://data.eastmoney.com/bkzj/hy.html",
            "status": "ok" if any((periods[k].get("items") for k in ("1d", "5d", "10d"))) else "degraded",
        },
        {
            "name": "本地 market_history_cache",
            "role": "失败兜底与二十日代理",
            "url": "backend/data/market_history_cache",
            "status": "ok",
        },
        {
            "name": "东方财富北向/两融",
            "role": "机构与散户代理指标",
            "url": "https://data.eastmoney.com/zjlx/",
            "status": "degraded" if institution.get("errors") else "ok",
        },
        {
            "name": "同花顺热股榜",
            "role": "游资/题材热度备用线索",
            "url": "https://data.10jqka.com.cn/funds/hyzjl/",
            "status": "degraded" if (institution.get("errors") or {}).get("hot_rank_ths") else "ok",
        },
    ]
    return {
        "as_of": datetime.now().astimezone().isoformat(timespec="seconds"),
        "sector_flows": [periods["1d"], periods["5d"], periods["20d"], reversal],
        "sector_flows_extra": {"10d": periods["10d"]},
        "institutional_flows": institution,
        "source_health": market_source_cache.load_health() or {},
        "sources": sources,
        "methodology": [
            "行业板块至少展示单日、五日、二十日、趋势逆转；二十日缺少直连字段时以缓存/十日代理并标注。",
            "机构资金分类以公开可得字段做代理：北向、超大/大单、小单、两融、热度榜；国家队/量化/游资不做伪实时精确拆分。",
            "所有外部源失败时优先使用 backend/data/market_history_cache 的最近成功缓存，界面显示 served_from_cache/source_warning。",
        ],
    }
