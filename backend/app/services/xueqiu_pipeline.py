"""
雪球数据流水线：拉取 → 解析 → 规整（模块无共享可变状态，便于并发调用方自控）。
"""
from __future__ import annotations

import html
import re
import time
from typing import Any

from app.services import company_public_sources, xueqiu_http

_YAHOO = re.compile(r"^(\d{6})\.(SS|SH|SZ|BJ)$", re.I)


def yahoo_to_xq_symbol(yahoo: str) -> str | None:
    """600519.SS -> SH600519；000001.SZ -> SZ000001；8xxxxx.BJ -> BJ8xxxxx"""
    s = (yahoo or "").strip().upper()
    m = _YAHOO.match(s)
    if not m:
        return None
    code, mkt = m.group(1).zfill(6), m.group(2).upper()
    if mkt in ("SS", "SH"):
        return f"SH{code}"
    if mkt == "SZ":
        return f"SZ{code}"
    if mkt == "BJ":
        return f"BJ{code}"
    return None


def stage_fetch_quote(xq_symbol: str) -> tuple[dict[str, Any] | None, str | None]:
    url = "https://stock.xueqiu.com/v5/stock/quote.json"
    params = {"symbol": xq_symbol, "extend": "detail"}
    ref = xueqiu_http.stock_page_referer(xq_symbol)
    data, err = xueqiu_http.request_json("GET", url, params=params, referer=ref)
    if err:
        return None, err
    if not isinstance(data, dict):
        return None, "quote 响应格式异常"
    return data, None


def _num(x: Any) -> float | None:
    try:
        if x is None:
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def stage_normalize_quote(raw: dict[str, Any]) -> dict[str, Any]:
    """抽取与前端展示相关的字段（与东财指标对照用）。"""
    d = raw.get("data") or raw
    if not isinstance(d, dict):
        return {}
    market = d.get("market") if isinstance(d.get("market"), dict) else {}
    quote = d.get("quote") if isinstance(d.get("quote"), dict) else {}
    # 部分字段在 quote 下，部分在根上，兼容两种结构
    src = {**d, **quote, **market}
    return {
        "name": (src.get("name") or src.get("stock_name") or "")[:64],
        "symbol_xq": (src.get("symbol") or src.get("code") or "")[:16],
        "current": _num(src.get("current")),
        "chg": _num(src.get("chg")),
        "percent": _num(src.get("percent")),
        "open": _num(src.get("open")),
        "high": _num(src.get("high")),
        "low": _num(src.get("low")),
        "volume": _num(src.get("volume")),
        "amount": _num(src.get("amount")),
        "turnover_rate": _num(src.get("turnover_rate")),
        "pe_ttm": _num(src.get("pe_ttm") or src.get("pe_lyr")),
        "pb": _num(src.get("pb")),
        "market_capital": _num(src.get("market_capital")),
        "float_market_capital": _num(src.get("float_market_capital")),
        "total_shares": _num(src.get("total_shares")),
        "float_shares": _num(src.get("float_shares")),
        "eps": _num(src.get("eps")),
        "navps": _num(src.get("navps")),
    }


def _strip_html(text: str) -> str:
    s = html.unescape(str(text or ""))
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def stage_fetch_company_f10(xq_symbol: str) -> tuple[dict[str, Any] | None, str | None]:
    """雪球 F10 公司简介（A 股 cn）。"""
    url = "https://stock.xueqiu.com/v5/stock/f10/cn/company.json"
    params = {"symbol": xq_symbol}
    ref = xueqiu_http.stock_page_referer(xq_symbol)
    data, err = xueqiu_http.request_json("GET", url, params=params, referer=ref, timeout_sec=10, max_retries=0)
    if err:
        return None, err
    if not isinstance(data, dict):
        return None, "公司简介响应格式异常"
    return data, None


def stage_normalize_company_f10(raw: dict[str, Any]) -> dict[str, Any]:
    d = raw.get("data") if isinstance(raw.get("data"), dict) else raw
    company = d.get("company") if isinstance(d.get("company"), dict) else d
    if not isinstance(company, dict):
        company = {}
    org_name = (
        company.get("org_name_cn")
        or company.get("org_name")
        or company.get("org_short_name_cn")
        or ""
    )
    industry = (
        company.get("affiliate_industry")
        or company.get("industry")
        or company.get("industry_name")
        or ""
    )
    intro = (
        company.get("org_cn_introduction")
        or company.get("brief_introduction")
        or company.get("introduction")
        or ""
    )
    name = (company.get("org_short_name_cn") or company.get("org_short_name") or "")[:64]
    return {
        "name": str(name).strip(),
        "org_name": str(org_name).strip(),
        "industry": str(industry).strip(),
        "intro": _strip_html(str(intro).strip()),
    }


def stage_fetch_major_events(xq_symbol: str, count: int = 15) -> tuple[list[dict[str, Any]], str | None]:
    """个股大事件时间轴（雪球 app 端 screener/event）。"""
    url = "https://stock.xueqiu.com/v5/stock/screener/event/list.json"
    params = {"symbol": xq_symbol, "page": 1, "size": min(max(1, count), 30)}
    ref = xueqiu_http.stock_page_referer(xq_symbol)
    data, err = xueqiu_http.request_json("GET", url, params=params, referer=ref, timeout_sec=10, max_retries=0)
    if err:
        return [], err
    if not isinstance(data, dict):
        return [], "大事件接口格式异常"
    block = data.get("data") if isinstance(data.get("data"), dict) else data
    # 显式空列表是有效结果；字段缺失或类型错误不能清空已有缓存。
    items = next(
        (container[key] for container, key in ((block, "items"), (block, "list"), (data, "list")) if key in container),
        None,
    )
    if not isinstance(items, list):
        return [], "大事件接口格式异常：缺少有效事件列表"
    out: list[dict[str, Any]] = []
    for it in items[:count]:
        if not isinstance(it, dict):
            continue
        title = _strip_html(it.get("title") or it.get("event_name") or "")[:240]
        message = _strip_html(it.get("message") or it.get("content") or it.get("description") or "")[:800]
        if not title and message:
            title = message[:120]
        created = it.get("created_at") or it.get("event_time") or it.get("timestamp")
        out.append(
            {
                "title": title,
                "message": message,
                "created_at": created,
                "event_type": (it.get("event_type") or it.get("type") or "")[:32],
            }
        )
    return out, None


def stage_fetch_stock_timeline(
    xq_symbol: str,
    *,
    source: str,
    count: int = 12,
) -> tuple[list[dict[str, Any]], str | None]:
    """个股资讯/公告时间线（api.xueqiu.com，与雪球个股页 tab 一致）。"""
    url = "https://api.xueqiu.com/statuses/stock_timeline.json"
    params = {
        "symbol_id": xq_symbol,
        "symbol": xq_symbol,
        "source": source,
        "count": min(max(1, count), 20),
        "page": 1,
        "sort": "alpha",
        "comment": "0",
        "hl": "0",
    }
    ref = xueqiu_http.stock_page_referer(xq_symbol)
    data, err = xueqiu_http.request_json("GET", url, params=params, referer=ref, timeout_sec=10, max_retries=0)
    if err:
        return [], err
    if not isinstance(data, dict):
        return [], "时间线接口格式异常"
    lst = data["list"] if "list" in data else data.get("statuses")
    if not isinstance(lst, list):
        return [], "时间线接口格式异常：缺少有效资讯列表"
    out: list[dict[str, Any]] = []
    for it in lst[:count]:
        if not isinstance(it, dict):
            continue
        title = _strip_html(it.get("title") or "")[:240]
        text = _strip_html(it.get("text") or it.get("description") or "")[:800]
        if not title and text:
            title = text[:120]
        created = it.get("created_at") or it.get("timeBefore")
        target = (it.get("target") or "").strip()
        url_item = f"https://xueqiu.com{target}" if target.startswith("/") else (target or None)
        sid = it.get("id")
        if not url_item and sid:
            url_item = f"https://xueqiu.com/{sid}"
        out.append(
            {
                "id": sid,
                "title": title,
                "text_excerpt": text,
                "created_at": created,
                "url": url_item,
                "source": source,
            }
        )
    return out, None


def stage_fetch_discussions(xq_symbol: str, count: int = 8) -> tuple[list[dict[str, Any]], str | None]:
    """个股相关帖子（讨论区），用于舆论补充。"""
    url = "https://xueqiu.com/statuses/search.json"
    params = {
        "q": xq_symbol,
        "symbol": xq_symbol,
        "count": min(max(1, count), 20),
        "page": 1,
        "sort": "time",
        "source": "all",
    }
    ref = xueqiu_http.stock_page_referer(xq_symbol)
    data, err = xueqiu_http.request_json("GET", url, params=params, referer=ref)
    if err:
        return [], err
    if not isinstance(data, dict):
        return [], "讨论接口格式异常"
    lst = data.get("list") or data.get("statuses") or []
    if not isinstance(lst, list):
        return [], None
    out: list[dict[str, Any]] = []
    for it in lst[:count]:
        if not isinstance(it, dict):
            continue
        uid = it.get("user_id")
        uname = ""
        u = it.get("user")
        if isinstance(u, dict):
            uname = (u.get("screen_name") or u.get("name") or "")[:64]
        title = (it.get("title") or "")[:200]
        text = (it.get("text") or it.get("description") or "")[:500]
        created = it.get("created_at") or it.get("timeBefore")
        sid = it.get("id")
        out.append(
            {
                "id": sid,
                "title": title,
                "text_excerpt": text.replace("\n", " ").strip(),
                "user": uname,
                "created_at": created,
                "url": f"https://xueqiu.com/{sid}" if sid else None,
            }
        )
    return out, None


def stage_fetch_hot_users(xq_symbol: str, count: int = 5) -> tuple[list[dict[str, Any]], str | None]:
    url = "https://xueqiu.com/recommend/user/stock_hot_user.json"
    params = {"symbol": xq_symbol, "start": 0, "count": min(max(1, count), 20)}
    ref = xueqiu_http.stock_page_referer(xq_symbol)
    data, err = xueqiu_http.request_json("GET", url, params=params, referer=ref)
    if err:
        return [], err
    users = data.get("users") if isinstance(data, dict) else None
    if not isinstance(users, list):
        return [], None
    out = []
    for u in users[:count]:
        if not isinstance(u, dict):
            continue
        out.append(
            {
                "name": (u.get("screen_name") or u.get("name") or "")[:64],
                "followers_count": u.get("followers_count"),
                "verified": u.get("verified"),
            }
        )
    return out, None


def stage_fetch_kline_daily_snippet(xq_symbol: str, days: int = 30) -> tuple[list[dict[str, Any]], str | None]:
    """最近 N 日日 K 摘要（与东财 K 线对照用，同源为雪球 chart API）。"""
    import time as _t

    end_ms = int(_t.time() * 1000)
    begin_ms = end_ms - max(5, min(days, 120)) * 86400 * 1000
    url = "https://stock.xueqiu.com/v5/stock/chart/kline.json"
    params = {
        "symbol": xq_symbol,
        "begin": str(begin_ms),
        "end": str(end_ms),
        "period": "day",
        "type": "before",
        "indicator": "kline",
    }
    ref = xueqiu_http.stock_page_referer(xq_symbol)
    data, err = xueqiu_http.request_json("GET", url, params=params, referer=ref)
    if err:
        return [], err
    if not isinstance(data, dict):
        return [], "K 线响应格式异常"
    chart = data.get("data") or data
    items = chart.get("item") or chart.get("items") if isinstance(chart, dict) else None
    if items is None and isinstance(chart, dict):
        items = (chart.get("chart") or {}).get("items") or chart.get("items")
    if not isinstance(items, list):
        return [], None
    col = chart.get("column") if isinstance(chart, dict) else None
    out: list[dict[str, Any]] = []
    for it in items[-days:]:
        if isinstance(it, dict):
            ts = it.get("timestamp")
            out.append(
                {
                    "timestamp": ts,
                    "open": _num(it.get("open")),
                    "high": _num(it.get("high")),
                    "low": _num(it.get("low")),
                    "close": _num(it.get("close") or it.get("close_price")),
                    "volume": _num(it.get("volume")),
                }
            )
        elif isinstance(it, (list, tuple)) and isinstance(col, list) and col:
            row = dict(zip(col, it))
            out.append(
                {
                    "timestamp": row.get("timestamp"),
                    "open": _num(row.get("open")),
                    "high": _num(row.get("high")),
                    "low": _num(row.get("low")),
                    "close": _num(row.get("close")),
                    "volume": _num(row.get("volume")),
                }
            )
    return out, None


def run_company_bundle(yahoo_symbol: str) -> dict[str, Any]:
    """公司信息栏：雪球优先，失败时使用公开资料并保留真实来源与连接状态。"""
    xq = yahoo_to_xq_symbol(yahoo_symbol)
    if not xq:
        return {
            "ok": False,
            "yahoo_symbol": yahoo_symbol,
            "xq_symbol": None,
            "cookies_configured": bool(xueqiu_http.effective_xueqiu_cookies()),
            "company": None,
            "major_events": [],
            "news": [],
            "errors": ["仅支持 A 股 Yahoo 格式代码，如 600519.SS"],
            "source": "https://xueqiu.com/",
            "stock_url": None,
        }

    cookies_configured = bool(xueqiu_http.effective_xueqiu_cookies())
    raw_company, c_err = stage_fetch_company_f10(xq)
    company = stage_normalize_company_f10(raw_company) if raw_company and not c_err else None
    if not c_err and not (company and any(company.values())):
        c_err = "雪球公司简介为空"
        company = None
    auth_status = xueqiu_http.auth_error_status(c_err)
    if auth_status:
        # 已确认登录不可用时，不重复请求同一会话下的其它接口。
        major_events, news, e_err, n_err = [], [], c_err, c_err
    else:
        major_events, e_err = stage_fetch_major_events(xq, count=15)
        auth_status = xueqiu_http.auth_error_status(e_err)
        if auth_status:
            news, n_err = [], e_err
        else:
            news, n_err = stage_fetch_stock_timeline(xq, source="自选股新闻", count=12)
            auth_status = xueqiu_http.auth_error_status(n_err)
    if not auth_status:
        auth_status = "unavailable" if c_err and e_err and n_err else "connected" if cookies_configured else "anonymous"

    now = int(time.time())
    sources = {"company": "雪球", "major_events": "雪球", "news": "雪球"}
    section_times = {field: now for field in sources}
    section_errors = {"company": c_err, "major_events": e_err, "news": n_err}
    stale_fields = []
    if c_err:
        company, sources["company"], section_times["company"], section_errors["company"] = company_public_sources.fetch_company(yahoo_symbol)
        if section_errors["company"] and company:
            stale_fields.append("company")
    if e_err or n_err:
        public_items, public_source, fetched_at, public_error = company_public_sources.fetch_news(
            yahoo_symbol, (company or {}).get("name") or None,
        )
        feed = [company_public_sources.as_feed_item(item) for item in public_items]
        if e_err:
            # 公告可作为事件补充；普通资讯不能冒充公司大事件。
            major_events = [item for item in feed if item["event_type"] == "公司公告"][:15]
        if n_err:
            news = [item for item in feed if item["event_type"] != "公司公告"][:12]
        for field, error in (("major_events", e_err), ("news", n_err)):
            if error:
                sources[field] = public_source
                section_times[field] = fetched_at
                section_errors[field] = public_error
                if public_error:
                    items = major_events if field == "major_events" else news
                    section_times[field] = max((item.get("fetched_at") or 0 for item in items), default=0) or None
                    if items:
                        stale_fields.append(field)

    messages = {
        "missing": "雪球接口需要登录，当前使用公开资料补充；可在配置台连接雪球。",
        "expired": "雪球登录已失效，当前使用公开资料补充；请在配置台更新 Cookie。",
        "unavailable": "雪球暂不可用，当前使用公开资料补充，将自动重试。",
    }
    return {
        "ok": bool(company or major_events or news),
        "yahoo_symbol": yahoo_symbol.strip().upper(),
        "xq_symbol": xq,
        "cookies_configured": cookies_configured,
        "auth_status": auth_status,
        "auth_message": messages.get(auth_status),
        "company": company,
        "major_events": major_events,
        "news": news,
        "errors": list(dict.fromkeys(str(error) for error in section_errors.values() if error)),
        "section_errors": section_errors,
        "sources": sources,
        "section_fetched_at": section_times,
        "stale_fields": stale_fields,
        "source": "https://xueqiu.com/",
        "stock_url": f"https://xueqiu.com/S/{xq}",
    }


def run_bundle(yahoo_symbol: str) -> dict[str, Any]:
    """
    聚合：行情 + 讨论摘要 + 热门用户。任一步失败仍返回其它成功部分与 errors 列表。
    """
    xq = yahoo_to_xq_symbol(yahoo_symbol)
    if not xq:
        return {
            "ok": False,
            "yahoo_symbol": yahoo_symbol,
            "xq_symbol": None,
            "cookies_configured": bool(xueqiu_http.effective_xueqiu_cookies()),
            "quote": None,
            "discussions": [],
            "hot_users": [],
            "kline_daily_snippet": [],
            "errors": ["仅支持 A 股 Yahoo 格式代码，如 600519.SS"],
            "source": "https://xueqiu.com/",
            "stock_url": None,
        }

    errors: list[str] = []

    def _add_err(msg: str | None) -> None:
        if msg and msg not in errors:
            errors.append(msg)

    raw_q, q_err = stage_fetch_quote(xq)
    quote_norm: dict[str, Any] | None = None
    _add_err(q_err)
    if raw_q and not q_err:
        quote_norm = stage_normalize_quote(raw_q)

    discussions, d_err = stage_fetch_discussions(xq)
    _add_err(d_err)

    hot_users, h_err = stage_fetch_hot_users(xq)
    _add_err(h_err)

    kline_snippet, k_err = stage_fetch_kline_daily_snippet(xq, days=30)
    _add_err(k_err)

    return {
        "ok": bool(quote_norm or discussions or hot_users or kline_snippet),
        "yahoo_symbol": yahoo_symbol.strip().upper(),
        "xq_symbol": xq,
        "cookies_configured": bool(xueqiu_http.effective_xueqiu_cookies()),
        "quote": quote_norm,
        "discussions": discussions,
        "hot_users": hot_users,
        "kline_daily_snippet": kline_snippet,
        "errors": errors,
        "source": "https://xueqiu.com/",
        "stock_url": f"https://xueqiu.com/S/{xq}",
    }
