"""Evidence-backed, stock-specific features built without network or model calls.

Quotes are observations, rule-extracted exposures are review candidates, and
conditional business mechanisms are never presented as a stock-price forecast.
Historical snapshots require evidence observed by their cutoff; report periods
alone do not establish publication/availability.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import statistics
from datetime import date, datetime, time, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.config import settings
from app.services.stock_code_utils import normalize_symbol

SHANGHAI = ZoneInfo("Asia/Shanghai")
SCHEMA_VERSION = 1
CATEGORIES = ("business", "product", "geography", "customer", "supplier", "cost", "financial", "market")
_CATEGORY_LABELS = {"business": "业务构成", "product": "产品与产能", "geography": "地区与外汇", "customer": "客户与需求", "supplier": "供应链", "cost": "成本结构", "financial": "财务约束", "market": "市场行为"}
# These identify quoted candidate passages, not verified exposure amounts.
_TOPIC_RULES = {
    "product": (r"产品|产能|销量|产量|生产线|型号|研发|主营业务", "产品与产能披露"),
    "geography": (r"海外|境外|出口|进口|地区|区域|外币|汇率|美元|欧洲|北美|东南亚", "地区与币种披露"),
    "customer": (r"客户|订单|需求|经销商|消费者|应收账款", "客户与需求披露"),
    "supplier": (r"供应商|供应链|采购|上游|供货", "供应链披露"),
    "cost": (r"原材料|燃料|电价|能源成本|人工成本|运输成本|采购成本|成本占比", "成本项目披露"),
    "financial": (r"负债|借款|现金流|毛利率|净利率|资本开支|偿债|利息|套期保值", "财务约束披露"),
}
_FACTORS = (
    ("input_cost", "原材料及能源价格", ("cost", "supplier"), r"原材料|采购成本|燃料|电价|能源成本", ["原材料", "采购价格", "涨价", "降价", "电价", "油价"],
     "若公司为该投入品的净购买方且不能转嫁或对冲，采购价上涨会压缩单位毛利；下降反向。", ["确认具体投入品及采购占比", "确认合同重定价、库存和套保期限"], ["售价同步上调", "库存成本滞后", "套保收益抵消"], ["投入品采购价", "单位成本", "毛利率"]),
    ("customer_demand", "客户订单与终端需求", ("customer",), r"客户|订单|需求|经销商", ["订单", "需求", "销量", "客户", "采购", "消费"],
     "若事件影响披露的客户或终端用途，订单变化可通过销量、产能利用率和经营杠杆影响利润。", ["确认事件对象与披露客户或用途一致", "确认收入占比、订单兑现率及价格变化"], ["订单取消或延迟", "价格竞争", "替代客户补偿"], ["新增订单", "交付量", "应收账款回款"]),
    ("fx_trade", "汇率与跨境贸易", ("geography",), r"海外|境外|出口|进口|外币|汇率|美元", ["汇率", "人民币", "美元", "关税", "出口", "进口", "制裁", "贸易"],
     "贸易限制可影响披露地区的销量或采购；汇率对收入、成本、外币负债和套保的净效应需分别计算。", ["确认销售或采购目的地、结算币种及净敞口", "确认关税承担方与套保比例"], ["本地化生产", "自然对冲", "汇率套保", "转口或替代市场"], ["地区收入", "净外币敞口", "关税及贸易规则"]),
    ("funding_cost", "融资成本与偿债约束", ("financial",), r"负债|借款|偿债|利息", ["利率", "降息", "加息", "融资", "债务", "信用"],
     "若存在待重定价或待再融资的净有息负债，融资利率上升可能增加利息支出；负债币种和期限决定传导速度。", ["区分有息负债、经营负债和净现金", "确认固定或浮动利率及到期结构"], ["固定利率锁定", "存款利息增加", "债务提前偿还"], ["有息负债余额", "融资利率", "现金流覆盖率"]),
    ("capacity_supply", "产能投放与供给", ("product",), r"产能|生产线|产量|扩产", ["产能", "投产", "停产", "扩产", "供给", "限产"],
     "新增有效产能只有在需求和良率支持时才增加利润；停产、过剩或折旧增加可能抵消销量收益。", ["确认实际投产日期、良率与利用率", "确认需求、售价和新增折旧"], ["供给过剩压价", "投产延迟", "良率不足"], ["产能利用率", "量价变化", "折旧与资本开支"]),
)


_SCOPE_CATALOG = {
    "input_cost": ("碳酸锂", "氢氧化锂", "锂", "电解铜", "铜箔", "铜", "铝", "镍", "钴", "锌", "铁矿石", "钢材", "钢铁", "原油", "石油", "天然气", "煤炭", "焦煤", "焦炭", "电力", "电价", "硅料", "硅片", "多晶硅", "棉花", "玉米", "大豆", "橡胶", "纸浆", "糖", "燃料油"),
    "fx_trade": ("美元", "欧元", "日元", "港币", "英镑", "美国", "欧洲", "欧盟", "日本", "北美", "东南亚", "印度", "俄罗斯", "中东", "南美", "非洲"),
    "customer_demand": ("汽车", "新能源汽车", "消费电子", "手机", "半导体", "光伏", "风电", "房地产", "基建", "家电", "白酒", "医疗", "数据中心", "电网", "储能", "动力电池", "航空"),
    "capacity_supply": ("汽车", "动力电池", "储能", "光伏", "硅料", "硅片", "半导体", "晶圆", "钢铁", "水泥", "煤炭", "铜", "铝", "白酒"),
    "funding_cost": ("人民币", "美元", "欧元", "日元", "港币", "浮动利率", "固定利率"),
}


def _scope_terms(factor_id: str, features: list) -> list[str]:
    text = " ".join(str(feature["value"]) for feature in features)
    found = [term for term in _SCOPE_CATALOG.get(factor_id, ()) if term in text]
    # Keep the most specific term, e.g. 碳酸锂 rather than also emitting 锂.
    return [term for term in found if not any(term != other and term in other for other in found)]


def _timestamp(value: Any) -> datetime | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, timezone.utc)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _cutoff(value: Any) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, date) and not isinstance(value, datetime):
        return datetime.combine(value, time.max, SHANGHAI).astimezone(timezone.utc)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value)):
        return datetime.combine(date.fromisoformat(str(value)), time.max, SHANGHAI).astimezone(timezone.utc)
    parsed = _timestamp(value.isoformat() if isinstance(value, datetime) else value)
    if parsed is None:
        raise ValueError("as_of 须为 YYYY-MM-DD 或带时区时间戳")
    return parsed


def _dict(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


def _get(raw: dict, *keys: str) -> Any:
    for key in keys:
        if raw.get(key) not in (None, ""):
            return raw[key]
    return None


def _identifier(*parts: Any) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:20]


def _raw(record: dict) -> dict:
    return {**_dict(record.get("raw_json")), **_dict(record.get("raw")), **record}


def _source(record: dict, kind: str, cutoff: datetime, historical: bool) -> dict | None:
    raw = _raw(record)
    observed = _get(raw, "observed_at", "updated_at", "first_observed_at", "content_observed_at", "fetched_at", "saved_at")
    observed_dt = _timestamp(observed)
    published = _get(raw, "published_at", "published_ts", "PublishedAt", "PublicationDate", "AnnouncementDate", "AnnDate", "Annodt", "FI_T1.Annodt", "公告日期")
    published_dt = _timestamp(published)
    report = _get(raw, "report_date", "EndDate", "Enddate", "Accper", "FI_T1.Accper")
    if observed_dt and observed_dt > cutoff or published_dt and published_dt > cutoff:
        return None
    cutoff_date = cutoff.astimezone(SHANGHAI).date().isoformat()
    # Period dates never substitute for availability, but impossible future
    # periods/publication dates are still excluded from current factual records.
    if report and str(report)[:10] > cutoff_date:
        return None
    if published and re.match(r"^\d{4}-\d{2}-\d{2}", str(published)) and str(published)[:10] > cutoff_date:
        return None
    if historical and observed_dt is None:
        return None
    sid = "sf:" + _identifier(kind, raw.get("source_path") or raw.get("url") or raw.get("id"), raw.get("content_hash") or raw.get("row_hash") or json.dumps(raw, ensure_ascii=False, sort_keys=True, default=str))
    return {"id": sid, "source_id": sid, "kind": kind, "title": str(_get(raw, "title", "source_name", "source_path") or kind),
            "url": raw.get("url") or raw.get("link"), "source_path": raw.get("source_path"),
            "report_date": str(report) if report else None, "published_at": published,
            "observed_at": observed_dt.isoformat() if observed_dt else None,
            "as_of": str(report or published or observed or "") or None,
            "availability": "locally_observed" if observed_dt else "unknown", "historical_availability_verified": bool(observed_dt),
            "quote": ""}


def _evidence(source: dict, quote: str) -> dict:
    return {"source_id": source["id"], "quote": quote, "as_of": source["as_of"]}


def _feature(source: dict, category: str, label: str, value: Any, *, quote: str, status: str = "reported", unit: str | None = None, missing: list | None = None) -> dict:
    return {"id": "feature:" + _identifier(category, label, quote if status == "candidate" and category != "financial" else ""), "category": category, "name": label, "label": label,
            "value": value, "unit": unit, "status": status, "as_of": source["as_of"],
            "source_ids": [source["id"]], "evidence": [_evidence(source, quote)], "missing": missing or []}


def _financial_features(record: dict, source: dict) -> list[dict]:
    raw = _raw(record)
    metadata = {"stkcd", "symbol", "shortname", "accper", "enddate", "typrep", "source", "source_type", "indcd1", "indnme1", "report_date", "report_type", "unit", "currency", "textualsimilarity", "id"}
    result = []
    for key, value in raw.items():
        if key.split(".")[-1].lower() in metadata or re.search(r"(?:date|time|_at|_id|code|_unit|\.unit|单位)$", key, re.I) or isinstance(value, (bool, dict, list)):
            continue
        string = str(value).strip().replace(",", "")
        percentage = string.endswith("%")
        try:
            number = float(string[:-1] if percentage else string)
        except (ValueError, TypeError):
            continue
        if not math.isfinite(number):
            continue
        unit = "%" if percentage else _get(raw, key + "_unit", key + ".unit")
        coded = bool(re.search(r"(?:^|\.)F\d+[A-Z]?$", key))
        missing = (["字段语义待字典映射"] if coded else []) + ([] if unit else ["源数据未提供单位，禁止推断或换算"])
        result.append(_feature(source, "financial", key, number, quote=f"{key}: {value}", unit=str(unit) if unit else None, status="candidate" if coded else "reported", missing=missing))
    return result[:80]


def _clean_bars(candles: list | None, cutoff: datetime) -> list[tuple[str, float]]:
    by_day = {}
    end = cutoff.astimezone(SHANGHAI).date().isoformat()
    for row in candles or []:
        if not isinstance(row, dict):
            continue
        stamp = _timestamp(row.get("t"))
        day = stamp.astimezone(SHANGHAI).date().isoformat() if stamp else str(_get(row, "date", "trade_date") or "")[:10]
        if re.fullmatch(r"\d{8}", day):
            day = f"{day[:4]}-{day[4:6]}-{day[6:]}"
        try:
            date.fromisoformat(day)
            close = float(_get(row, "c", "close"))
        except (TypeError, ValueError):
            continue
        # Never consume an unfinished daily bar.
        available = datetime.combine(date.fromisoformat(day), time(15, 0), SHANGHAI).astimezone(timezone.utc)
        if day <= end and available <= cutoff and math.isfinite(close) and close > 0:
            by_day[day] = close
    return sorted(by_day.items())[-1500:]


def _market_stats(bars: list, benchmark: list, metadata: dict) -> dict:
    result = {"status": "available" if len(bars) >= 2 else "missing", "sample_size": max(0, len(bars) - 1),
              "window_start": bars[0][0] if bars else None, "window_end": bars[-1][0] if bars else None,
              "benchmark": metadata.get("benchmark_symbol"), "adjustment": metadata.get("adjustment") or "unknown",
              "return_20d_pct": None, "return_60d_pct": None, "return_120d_pct": None,
              "volatility_60d_pct": None, "max_drawdown_120d_pct": None, "beta": None, "beta_sample_size": 0,
              "limitations": ["统计描述历史价格，不证明因果或预测未来收益；未扣除交易成本。", "窗口按本股有效日线计数，停牌或缺测可能改变日历跨度。"]}
    if result["adjustment"] == "unknown":
        result["limitations"].append("缓存复权口径未知，分红送转可能污染收益、波动和事件反应。")
    for window in (20, 60, 120):
        if len(bars) > window:
            result[f"return_{window}d_pct"] = round((bars[-1][1] / bars[-window - 1][1] - 1) * 100, 4)
    returns = [bars[i][1] / bars[i - 1][1] - 1 for i in range(1, len(bars))]
    if len(returns) >= 20:
        result["volatility_60d_pct"] = round(statistics.stdev(returns[-60:]) * math.sqrt(252) * 100, 4)
        result["volatility_sample_size"] = min(60, len(returns))
    if bars:
        peak, drawdown = bars[-121][1] if len(bars) >= 121 else bars[0][1], 0.0
        for _, close in bars[-121:]:
            peak = max(peak, close)
            drawdown = min(drawdown, close / peak - 1)
        result["max_drawdown_120d_pct"] = round(drawdown * 100, 4)
    bench = dict(benchmark)
    paired = []
    for i in range(max(1, len(bars) - 120), len(bars)):
        prev, cur = bars[i - 1], bars[i]
        if prev[0] in bench and cur[0] in bench:
            paired.append((cur[1] / prev[1] - 1, bench[cur[0]] / bench[prev[0]] - 1))
    result["beta_sample_size"] = len(paired)
    if len(paired) >= 30:
        xs, ys = [p[1] for p in paired], [p[0] for p in paired]
        variance = statistics.variance(xs)
        if variance > 0:
            result["beta"] = round(sum((x - statistics.mean(xs)) * (y - statistics.mean(ys)) for x, y in zip(xs, ys)) / (len(xs) - 1) / variance, 4)
    return result


def _event_reactions(events: list, bars: list, benchmark: list, links: list, sources: list, cutoff: datetime, historical: bool) -> dict:
    items, skipped = [], 0
    bench = dict(benchmark)
    for event in events[:300]:
        source = _source(event, "news", cutoff, historical)
        published = _timestamp(_get(event, "published_at", "published_ts"))
        if source is None or published is None or published > cutoff:
            skipped += 1
            continue
        day = published.astimezone(SHANGHAI).date().isoformat()
        # A deliberately conservative post-event window: event-day close to
        # subsequent session close. It excludes same-day immediate repricing.
        prior = [i for i, (d, _) in enumerate(bars) if d <= day]
        if not prior:
            skipped += 1
            continue
        start = prior[-1]
        quote = str(event.get("title") or "") + " " + str(event.get("summary") or event.get("content") or "")[:1600]
        matches = [link["id"] for link in links if any(keyword in quote for keyword in link["event_keywords"])
                   and (not link.get("scope_terms") or any(term in quote for term in link["scope_terms"]))]
        source["quote"] = quote.strip()
        sources.append(source)
        for window in (1, 5, 20):
            if start + window >= len(bars):
                continue
            base, end = bars[start], bars[start + window]
            stock_return = (end[1] / base[1] - 1) * 100
            benchmark_return = (bench[end[0]] / bench[base[0]] - 1) * 100 if base[0] in bench and end[0] in bench else None
            items.append({"event_id": str(event.get("id") or source["id"]), "title": str(event.get("title") or ""), "event_date": day,
                          "window_sessions": window, "window_start": base[0], "window_end": end[0], "stock_return_pct": round(stock_return, 4),
                          "benchmark_return_pct": round(benchmark_return, 4) if benchmark_return is not None else None,
                          "excess_return_pct": round(stock_return - benchmark_return, 4) if benchmark_return is not None else None,
                          "baseline": "event_day_or_prior_close", "factor_ids": matches, "factor_match_status": "keyword_candidate",
                          "source_ids": [source["id"]], "evidence": [_evidence(source, quote.strip())]})
    groups = []
    for link in links:
        for window in (1, 5, 20):
            values = [row["excess_return_pct"] for row in items if link["id"] in row["factor_ids"] and row["window_sessions"] == window and row["excess_return_pct"] is not None]
            if values:
                groups.append({"factor_id": link["id"], "window_sessions": window, "sample_size": len(values),
                               "mean_excess_return_pct": round(statistics.mean(values), 4), "median_excess_return_pct": round(statistics.median(values), 4),
                               "validation_status": "descriptive_only"})
    return {"status": "available" if items else "insufficient_data", "sample_size": len({row["event_id"] for row in items}),
            "windows": [1, 5, 20], "items": items, "by_factor": groups, "excluded_events": skipped,
            "limitations": ["仅使用明确关联该股票的本地新闻；不含完整事件总体。", "事件日或之前收盘为起点，观察后续交易日，未测量事件日即时反应。", "因子匹配仅为关键词候选；同期事件、选择偏差及重叠窗口未消除，不构成因果验证。", "基准缺失时只报告个股收益，不将其当成超额收益。", "复权口径以行情来源为准，未知时不能据此建立涨跌预测。", "窗口按本股有效日线计数，停牌或缺测会改变日历跨度。"]}


def build_from_inputs(symbol: str, *, fundamentals: dict | None = None, profile: dict | None = None,
                      candles: list | None = None, benchmark_candles: list | None = None,
                      events: list | None = None, as_of: Any = None, market_source: dict | None = None) -> dict:
    """Pure builder. Every input must belong to ``symbol``; no external I/O.

    fundamentals accepts basic_info/financial/mda dictionaries (or lists).
    Event inputs must already have a verified direct stock association.
    Unknown-date evidence is available only for a present-day snapshot.
    """
    stock = normalize_symbol(symbol)
    if not stock:
        raise ValueError("无效股票代码")
    cutoff, historical = _cutoff(as_of), as_of is not None
    features, sources, missing, name = [], [], [], ""
    fundamentals, profile, metadata = fundamentals or {}, profile or {}, market_source or {}
    for kind in ("basic_info", "financial", "mda"):
        supplied = fundamentals.get(kind) or []
        records = supplied if isinstance(supplied, list) else [supplied]
        eligible = [(record, _source(record, kind, cutoff, historical)) for record in records if isinstance(record, dict)]
        eligible = [(record, source) for record, source in eligible if source]
        if not eligible:
            if records:
                missing.append(f"{kind} 有记录，但截至快照时间尚不可用或缺少可核验观测时间")
            continue
        record, source = max(eligible, key=lambda item: (str(item[1].get("report_date") or ""), str(item[1].get("observed_at") or "")))
        raw = _raw(record)
        name = name or str(_get(raw, "short_name", "ShortName", "FI_T1.ShortName") or "")
        if kind == "financial":
            built = _financial_features(record, source)
            features.extend(built)
            source["quote"] = "\n".join(f["evidence"][0]["quote"] for f in built)
        else:
            text = str(_get(raw, "main_business", "MAINBUSSINESS", "MainBusiness", "mana_disc_anal", "ManaDiscAnal") or "")
            source["quote"] = text[:24000]
            if text:
                if kind == "basic_info":
                    features.append(_feature(source, "business", "主营业务原文", text[:4000], quote=text[:4000]))
                for sentence in re.split(r"(?<=[。！？；\n])", text[:120000]):
                    sentence = sentence.strip()
                    if len(sentence) < 8:
                        continue
                    for category, (pattern, label) in _TOPIC_RULES.items():
                        if re.search(pattern, sentence) and sum(f["category"] == category and source["id"] in f["source_ids"] for f in features) < 12:
                            features.append(_feature(source, category, label, sentence[:1200], quote=sentence[:1200], status="candidate", missing=["规则定位的披露片段；暴露主体、金额和净方向须复核"]))
            industry = _get(raw, "industry_name_c", "IndustryNameC", "industry_name_1", "IndustryName1")
            if industry and kind == "basic_info":
                source["quote"] += "\n披露行业分类: " + str(industry)
                features.append(_feature(source, "business", "披露行业分类", str(industry), quote=str(industry)))
        sources.append(source)
    profile_sources = [
        {"kind": "profile_auto", "auto": profile.get("auto"), "observed_at": profile.get("em_fetched_at"), "source_name": "东方财富公司资料缓存"},
        {"kind": "profile_manual", "auto": profile.get("manual"), "observed_at": None, "source_name": "用户补充资料"},
        *(profile.get("sources") or []),
    ]
    for entry in profile_sources:
        block = entry.get("auto")
        if not isinstance(block, dict) or not block:
            continue
        origin = entry.get("kind") or "profile_auto"
        record = {**block, "observed_at": entry.get("observed_at"), "title": entry.get("source_name"), "source_path": entry.get("source_path")}
        source = _source(record, origin, cutoff, historical)
        if source is None:
            continue
        name = name or str(block.get("name") or "")
        quote = str(block.get("main_business") or block.get("intro") or "")[:6000]
        if quote:
            source["quote"] = quote
            features.append(_feature(source, "business", "公司资料中的业务描述", quote, quote=quote, status="candidate", missing=["缓存描述需与最新公告核对"]))
            for category, (pattern, label) in _TOPIC_RULES.items():
                if re.search(pattern, quote):
                    features.append(_feature(source, category, label, quote, quote=quote, status="candidate", missing=["缓存描述需与最新公告核对"]))
            sources.append(source)
    # Deduplicate identical quoted facts, keeping the first (disclosure preferred).
    features = list({feature["id"]: feature for feature in reversed(features)}.values())[::-1]
    links = []
    for factor_id, label, categories, pattern, keywords, mechanism, conditions, counters, watch in _FACTORS:
        matched = [f for f in features if f["category"] in categories and re.search(pattern, str(f["value"]))
                   and not re.search(r"(?:不存在|不涉及|未开展|不从事|没有|不具有|无)[^。；，]{0,12}(?:" + pattern + ")", str(f["value"]))]
        if not matched:
            continue
        scope = _scope_terms(factor_id, matched)
        if factor_id == "input_cost":
            keywords = ["原材料", "采购价格", "采购成本", "涨价", "降价", *scope]
        source_ids = list(dict.fromkeys(sid for f in matched for sid in f["source_ids"]))
        links.append({"id": factor_id, "factor_id": factor_id, "factor": label, "name": label, "label": label,
                      "feature_ids": [f["id"] for f in matched], "exposure_feature_ids": [f["id"] for f in matched],
                      "source_ids": source_ids, "event_keywords": keywords, "scope_terms": scope, "scope_status": "quoted_candidate" if scope else "unidentified", "mechanism": mechanism, "status": "conditional",
                      "direction": {"operating": "conditional", "price": "unknown", "description": "经营传导为有条件假设；股价方向尚未知"},
                      "conditions": conditions, "counter_factors": counters + ["市场已提前反映该事件", "估值或风险溢价反向变化"],
                      "watch_points": watch, "missing": ["净暴露金额或比例未验证", "市场预期差未量化", "因果与样本外验证尚未完成"],
                      "evidence": [ref for f in matched for ref in f["evidence"]][:12]})
    market_observed = _timestamp(metadata.get("saved_at"))
    if historical and market_observed and market_observed > cutoff:
        candles, benchmark_candles = [], []
        missing.append("行情缓存晚于历史截止时间，未用于历史统计")
    bars, benchmark = _clean_bars(candles, cutoff), _clean_bars(benchmark_candles, cutoff)
    market = _market_stats(bars, benchmark, metadata)
    if bars:
        market_record = {"id": metadata.get("source_id") or stock, "title": "本地日线缓存", "observed_at": metadata.get("saved_at"), "report_date": bars[-1][0]}
        source = _source(market_record, "market", cutoff, False)
        if source:
            source["quote"] = f"{stock}: {bars[0][0]} 至 {bars[-1][0]}，{len(bars)} 根有效日线；复权={market['adjustment']}"
            sources.append(source)
            for metric, label in (("return_20d_pct", "近 20 交易日收益"), ("volatility_60d_pct", "近 60 交易日年化波动率"), ("beta", "相对基准 Beta")):
                if market[metric] is not None:
                    features.append(_feature(source, "market", label, market[metric], quote=source["quote"], status="observed", unit="%" if metric != "beta" else None, missing=market["limitations"]))
    reactions = _event_reactions(events or [], bars, benchmark, links, sources, cutoff, historical)
    categories = {category: {"label": _CATEGORY_LABELS[category], "status": "present" if any(f["category"] == category for f in features) else "missing", "count": sum(f["category"] == category for f in features)} for category in CATEGORIES}
    missing.extend(_CATEGORY_LABELS[category] + "缺少可用证据" for category, coverage in categories.items() if not coverage["count"])
    if not benchmark:
        missing.append("缺少同期间基准行情，不能计算市场调整后反应")
    if not reactions["sample_size"]:
        missing.append("缺少有完整后续价格窗口的直接关联事件")
    pending = sum(feature["status"] == "candidate" for feature in features)
    if pending:
        missing.append(f"{pending} 项为规则提取或字段语义待确认的候选，尚未完成证据复核")
    if any(source["availability"] == "unknown" for source in sources):
        missing.append("部分本地资料缺少观测时间，不能用于历史时点验证")
    if market["adjustment"] == "unknown" and bars:
        missing.append("日线复权口径未知，收益统计需核对除权除息")
    sources = list({source["id"]: source for source in sources}.values())
    for source in sources:
        source["text"] = source.get("quote") or ""
        source["source"] = source["kind"]
    return {"schema_version": SCHEMA_VERSION, "stock_code": stock, "name": name or stock, "as_of": cutoff.isoformat(),
            "features": features, "factor_links": links, "evidence_sources": sources,
            "coverage": {"status": "empty" if not features else "ready" if not missing else "partial", "categories": categories,
                         "missing": missing, "warnings": ["片段提取为规则候选；行业标签本身不生成股价结论。", "每周重建只更新已采集证据；资料缺口保持可见。"],
                         "source_count": len(sources), "feature_count": len(features), "pending_count": pending, "source_errors": []},
            "stats": {"market_behavior": market, "event_reactions": reactions, "historical_cutoff_applied": historical,
                      "builder": "local_evidence_rules_v1", "model_calls": 0}}


@lru_cache(maxsize=128)
def _read_json_cached(path: str, mtime_ns: int, size: int) -> dict:
    # Stat-keyed cache keeps repeated all-market and benchmark reads bounded.
    parsed = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("Expected a source JSON object")
    return parsed


def _read_json(path: Path, *, source_errors: list | None = None, error_code: str = "json_read_failed") -> dict:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return {}  # Missing sources are coverage gaps, not failed refreshes.
    except OSError:
        if source_errors is not None:
            source_errors.append(error_code)
        return {}
    try:
        return _read_json_cached(str(path), stat.st_mtime_ns, stat.st_size)
    except (OSError, ValueError):
        # A file observed above but removed mid-read is a failed source read.
        if source_errors is not None:
            source_errors.append(error_code)
        return {}


def _source_json_object(value: Any) -> dict:
    parsed = value if isinstance(value, dict) else json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("Expected a source JSON object")
    return parsed


def _read_fundamentals(path: Path, symbol: str, cutoff: datetime) -> dict:
    result = {}
    if not path.exists():
        return result
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for kind, table in (("basic_info", "company_basic_records"), ("financial", "financial_indicator_records"), ("mda", "management_discussion_records")):
            if "company_record_revisions" in tables:
                rows = conn.execute("SELECT * FROM company_record_revisions WHERE symbol=? AND dataset=? AND julianday(observed_at)<=julianday(?) ORDER BY julianday(observed_at) DESC,id DESC LIMIT 200", (symbol.split(".")[0], kind, cutoff.isoformat())).fetchall()
                # Latest observed revision of each period; source filtering in
                # the pure builder rejects future report/publication dates.
                seen, values = set(), []
                for row in rows:
                    record = dict(row)
                    _source_json_object(record["raw_json"])
                    if record["logical_key"] not in seen:
                        seen.add(record["logical_key"])
                        values.append(record)
                if values:
                    result[kind] = values
                    continue
            if table in tables:
                result[kind] = [dict(row) for row in conn.execute(f"SELECT * FROM {table} WHERE symbol=? ORDER BY report_date DESC LIMIT 30", (symbol.split(".")[0],))]
                for record in result[kind]:
                    _source_json_object(record["raw_json"])
    return result


def _read_market(directory: Path, symbol: str, cutoff: datetime, historical: bool, *, source_errors: list | None = None, error_code: str = "market_json_read_failed") -> tuple[list, dict]:
    candidates = []
    common_paths = [directory / f"{symbol}_{window}_1d.json" for window in ("1y", "2y", "5y", "max", "6mo", "3mo", "1mo")]
    paths = [path for path in common_paths if path.is_file()]
    if not paths and directory.exists():
        paths = list(directory.glob(f"{symbol}_*_1d.json"))
    for path in paths:
        entry = _read_json(path, source_errors=source_errors, error_code=error_code)
        observed = _timestamp(entry.get("saved_at"))
        if historical and (observed is None or observed > cutoff):
            continue
        payload = _dict(entry.get("payload"))
        rows = payload.get("candles") or []
        if isinstance(rows, list) and rows:
            candidates.append((len(_clean_bars(rows, cutoff)), str(entry.get("saved_at") or ""), rows,
                               {"saved_at": entry.get("saved_at"), "source_id": path.name, "adjustment": payload.get("adjustment") or payload.get("adjust"), "provider": payload.get("kline_source")}))
    if not candidates:
        return [], {}
    _, _, rows, metadata = max(candidates, key=lambda candidate: (candidate[0], candidate[1]))
    return rows, metadata


def _direct_event(row: dict, symbol: str) -> bool:
    related = row.get("related_codes") or []
    if isinstance(related, str):
        try:
            related = json.loads(related)
        except ValueError:
            related = []
    if not isinstance(related, list):
        related = []
    candidates = [*related, row.get("symbol_hint")]
    return any(normalize_symbol(str(code or "")) == symbol for code in candidates)


def _read_events(path: Path, symbol: str, cutoff: datetime) -> list:
    if not path.exists():
        return []
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "articles" not in tables:
            return []
        columns = {row[1] for row in conn.execute("PRAGMA table_info(articles)")}
        where = "published_ts<=?"
        args: list = [cutoff.timestamp()]
        related_sql = " OR related_codes LIKE ?" if "related_codes" in columns else ""
        where += " AND (symbol_hint IN (?, ?)" + related_sql + ")"
        args.extend([symbol, symbol.split(".")[0]])
        if related_sql:
            args.append("%" + symbol.split(".")[0] + "%")
        rows = conn.execute(f"SELECT * FROM articles WHERE {where} ORDER BY published_ts DESC LIMIT 600", args).fetchall()
        events = []
        for row in rows:
            record = dict(row)
            if not _direct_event(record, symbol):
                continue
            if "article_content_revisions" in tables:
                revision = conn.execute("SELECT * FROM article_content_revisions WHERE article_id=? AND first_observed_at<=? ORDER BY first_observed_at DESC,id DESC LIMIT 1", (record["id"], cutoff.timestamp())).fetchone()
                if revision:
                    record = {**_source_json_object(revision["snapshot_json"]), "id": record["id"], "observed_at": revision["first_observed_at"]}
                elif _timestamp(record.get("content_observed_at") or record.get("saved_at")) and _timestamp(record.get("content_observed_at") or record.get("saved_at")) > cutoff:
                    continue
            if _direct_event(record, symbol):
                events.append(record)
    return events[:300]


def build_snapshot(symbol: str, *, as_of: Any = None) -> dict:
    """Read existing local sources only: no DB creation, writes or paid models."""
    stock = normalize_symbol(symbol)
    if not stock:
        raise ValueError("无效股票代码")
    cutoff = _cutoff(as_of)
    root = settings.data_dir
    warnings, source_errors = [], []
    try:
        fundamentals = _read_fundamentals(root / "company_fundamentals.db", stock, cutoff)
    except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
        fundamentals = {}
        warnings.append("财务资料读取失败: " + type(exc).__name__)
        source_errors.append("fundamentals_read_failed")
    profiles = _read_json(root / "watchlist_profiles.json", source_errors=source_errors, error_code="profiles_json_read_failed")
    profile = dict(_dict(profiles.get(stock)))
    universe = _dict(_dict(_read_json(root / "company_basic_universe.json", source_errors=source_errors, error_code="company_universe_json_read_failed").get("symbols")).get(stock))
    updates = _read_json(root / "company_updates" / f"{stock}.json", source_errors=source_errors, error_code="company_update_json_read_failed")
    bundle = _dict(updates.get("bundle"))
    profile["sources"] = [
        {"kind": "universe_company", "auto": universe.get("auto"), "observed_at": universe.get("em_fetched_at"), "source_name": "东方财富全市场公司资料", "source_path": "company_basic_universe.json"},
        {"kind": "company_update", "auto": bundle.get("company"), "observed_at": _dict(bundle.get("section_fetched_at")).get("company"), "source_name": _dict(bundle.get("sources")).get("company") or "公司资料补充缓存", "source_path": f"company_updates/{stock}.json"},
    ]
    candles, metadata = _read_market(root / "kline_bundle_cache", stock, cutoff, as_of is not None, source_errors=source_errors, error_code="stock_market_json_read_failed")
    # Prefer CSI 300 where present; absence is explicitly recorded.
    benchmark, benchmark_symbol = [], None
    for code in ("INDEX_csi300", "INDEX_HS300", "INDEX_SH_COMP"):
        benchmark, _ = _read_market(root / "kline_bundle_cache", code, cutoff, as_of is not None, source_errors=source_errors, error_code="benchmark_market_json_read_failed")
        if benchmark:
            benchmark_symbol = code
            break
    metadata["benchmark_symbol"] = benchmark_symbol
    try:
        events = _read_events(Path(getattr(settings, "database_path", root / "news.db")), stock, cutoff)
    except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
        events = []
        warnings.append("事件资料读取失败: " + type(exc).__name__)
        source_errors.append("events_read_failed")
    snapshot = build_from_inputs(stock, fundamentals=fundamentals, profile=profile, candles=candles,
                                 benchmark_candles=benchmark, events=events, as_of=as_of, market_source=metadata)
    snapshot["coverage"]["warnings"].extend(warnings)
    snapshot["coverage"]["source_errors"] = list(dict.fromkeys(source_errors))
    return snapshot
