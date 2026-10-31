"""Auditable weekly cross-asset history and monthly clock inputs.

All market rows are actual observations, labelled with their completed Friday
bucket. Rates, prices, adjusted ETF prices and indices remain distinct kinds.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import json
import math
import time
from urllib.parse import quote

from app.config import settings
from app.storage import sqlite_bootstrap


def _asset(identifier, name, asset_class, symbol, *, kind="index", unit="点", note=""):
    return {"id": identifier, "name": name, "asset_class": asset_class, "kind": kind,
            "unit": unit, "source": "Yahoo Finance", "source_symbol": symbol,
            "source_url": f"https://finance.yahoo.com/quote/{quote(symbol, safe='')}/history/",
            "is_proxy": bool(note), "proxy_note": note, "frequency": "W-FRI",
            "price_basis": "adjusted_close" if kind == "etf" else "close"}


ASSET_CATALOG = [
    _asset("sp500", "标普500", "equity", "^GSPC"),
    _asset("nasdaq", "纳斯达克综合", "equity", "^IXIC"),
    _asset("dow", "道琼斯工业", "equity", "^DJI"),
    _asset("russell2000", "罗素2000", "equity", "^RUT"),
    _asset("ftse100", "英国富时100", "equity", "^FTSE"),
    _asset("dax", "德国DAX", "equity", "^GDAXI"),
    _asset("nikkei225", "日经225", "equity", "^N225"),
    _asset("hangseng", "香港恒生", "equity", "^HSI"),
    _asset("sse", "上证综指", "equity", "000001.SS"),
    _asset("csi300", "沪深300", "equity", "000300.SS"),
    _asset("treasury10y", "美国10年国债收益率", "bond", "^TNX", kind="yield", unit="%",
           note="10年美国国债收益率代理；不是债券价格或总回报指数，不能将利率升幅理解为债券收益。"),
    _asset("treasury_long", "美国长期国债 · TLT代理", "bond", "TLT", kind="etf", unit="USD",
           note="iShares 20年以上美国国债ETF复权收盘价代理；含Yahoo分红/拆分调整，不是原始债券指数。基金成立前留空。"),
    _asset("investment_grade", "美国投资级信用债 · LQD代理", "credit", "LQD", kind="etf", unit="USD",
           note="iShares投资级公司债ETF复权收盘价代理；不是原始债券指数。基金成立前留空。"),
    _asset("high_yield", "美国高收益债 · HYG代理", "credit", "HYG", kind="etf", unit="USD",
           note="iShares高收益公司债ETF复权收盘价代理；不是原始债券指数。基金成立前留空。"),
    _asset("gsci", "标普GSCI商品指数", "commodity", "^SPGSCI"),
    _asset("gold", "黄金期货价格", "commodity", "GC=F", kind="futures_price", unit="USD/盎司",
           note="Yahoo黄金期货近月连续报价代理；合约切换会造成跳跃，不是现货或可投资总回报指数。"),
    _asset("silver", "白银期货价格", "commodity", "SI=F", kind="futures_price", unit="USD/盎司",
           note="Yahoo白银期货近月连续报价代理；合约切换会造成跳跃，不是现货或可投资总回报指数。"),
    _asset("crude_wti", "WTI原油期货价格", "commodity", "CL=F", kind="futures_price", unit="USD/桶",
           note="Yahoo WTI期货近月连续报价代理；可出现负价格，合约切换会造成跳跃，不是总回报指数。"),
    _asset("usd", "美元指数", "currency", "DX-Y.NYB"),
    _asset("cash3m", "美国3个月国库券贴现率", "cash", "^IRX", kind="yield", unit="%",
           note="3个月美国国库券年化贴现率代理；不是现金累计回报指数，不进行净值或累计收益推算。"),
]
# DAX is itself a performance index, unlike the other equity price indices.
# Its methodology disclosure is not an instrument substitution/proxy.
next(item for item in ASSET_CATALOG if item["id"] == "dax").update(
    proxy_note="DAX通常为含股息再投资的绩效指数；与多数股指价格口径不同，跨指数收益比较需留意。",
    price_basis="performance_index", is_proxy=False)
next(item for item in ASSET_CATALOG if item["id"] == "csi300").update(
    source="腾讯财经", source_symbol="sh000300", source_url="https://gu.qq.com/sh000300/zs")
next(item for item in ASSET_CATALOG if item["id"] == "sse").update(
    source="腾讯财经", source_symbol="sh000001", source_url="https://gu.qq.com/sh000001/zs")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS merrill_assets (
 id TEXT PRIMARY KEY, metadata TEXT NOT NULL, requested_from TEXT, requested_to TEXT,
 last_synced_at REAL, last_error TEXT);
CREATE TABLE IF NOT EXISTS merrill_weekly (
 asset_id TEXT NOT NULL, date TEXT NOT NULL, close REAL NOT NULL, observation_date TEXT NOT NULL,
 PRIMARY KEY(asset_id,date));
CREATE TABLE IF NOT EXISTS merrill_macro (
 series_id TEXT NOT NULL,date TEXT NOT NULL,value REAL NOT NULL,PRIMARY KEY(series_id,date));
CREATE TABLE IF NOT EXISTS merrill_state (key TEXT PRIMARY KEY,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS merrill_analyses (
 user_id INTEGER NOT NULL,region TEXT NOT NULL,payload TEXT NOT NULL,updated_at REAL NOT NULL,
 PRIMARY KEY(user_id,region));
"""


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


@contextmanager
def connection():
    with sqlite_bootstrap.connection(settings.data_dir / "merrill.sqlite3", _SCHEMA) as db:
        yield db


def init_db():
    with connection() as db:
        for item in ASSET_CATALOG:
            db.execute("INSERT INTO merrill_assets(id,metadata) VALUES(?,?) "
                       "ON CONFLICT(id) DO UPDATE SET metadata=excluded.metadata", (item["id"], _json(item)))


def _day(value):
    return date.fromisoformat(str(value)[:10])


def _finite(value):
    if isinstance(value, bool) or value is None:
        raise ValueError("观测必须为有限数值")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("观测必须为有限数值")
    return number


def completed_friday(today=None):
    """Only include Friday after UTC has entered Saturday (all markets closed)."""
    today = today or datetime.now(timezone.utc).date()
    if isinstance(today, datetime):
        today = today.astimezone(timezone.utc).date()
    offset = (today.weekday() - 4) % 7
    return today - timedelta(days=offset or 7)


def requested_start(today=None):
    today = today or datetime.now(timezone.utc).date()
    try:
        return today.replace(year=today.year - 50)
    except ValueError:
        return today.replace(year=today.year - 50, day=28)


def upsert_weekly(identifier, rows, *, requested_from=None, requested_to=None, last_error=None):
    """UPSERT an observation batch atomically; an error never deletes old data."""
    init_db()
    clean = {}
    cutoff = completed_friday()
    for row in rows:
        week = _day(row["date"])
        observation = _day(row.get("observation_date", row["date"]))
        if week.weekday() != 4 or week > cutoff or not week - timedelta(days=4) <= observation <= week:
            raise ValueError("仅接受已完成交易周的真实观测")
        close = _finite(row.get("close", row.get("value")))
        # The latest actual observation wins duplicate buckets. Never invent days.
        if week not in clean or observation >= clean[week][2]:
            clean[week] = (week, close, observation)
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM merrill_assets WHERE id=?", (identifier,)).fetchone() is None:
            raise ValueError("未知资产序列")
        db.executemany("""INSERT INTO merrill_weekly VALUES(?,?,?,?)
          ON CONFLICT(asset_id,date) DO UPDATE SET close=excluded.close,observation_date=excluded.observation_date
          WHERE excluded.observation_date >= merrill_weekly.observation_date""",
                       [(identifier, week.isoformat(), value, observed.isoformat()) for week, value, observed in clean.values()])
        db.execute("""UPDATE merrill_assets SET requested_from=COALESCE(?,requested_from),
          requested_to=COALESCE(?,requested_to),last_synced_at=?,last_error=? WHERE id=?""",
                   (requested_from, requested_to, time.time(), str(last_error)[:500] if last_error else None, identifier))
    return len(clean)


def record_error(identifier, error, *, requested_from=None, requested_to=None):
    # Keep last successful sync timestamp and data when the source is unavailable.
    init_db()
    with connection() as db:
        db.execute("""UPDATE merrill_assets SET last_error=?,requested_from=COALESCE(?,requested_from),
          requested_to=COALESCE(?,requested_to) WHERE id=?""",
                   (str(error)[:500], requested_from, requested_to, identifier))


def catalog():
    init_db()
    by_id = {}
    with connection() as db:
        for row in db.execute("""SELECT a.*,MIN(w.date) available_from,MAX(w.date) available_to,COUNT(w.date) row_count
          FROM merrill_assets a LEFT JOIN merrill_weekly w ON w.asset_id=a.id GROUP BY a.id"""):
            item = {**json.loads(row["metadata"]), **{key: row[key] for key in row.keys() if key not in {"metadata", "id"}}}
            count = item["row_count"]
            start = _day(item["requested_from"]) if item["requested_from"] else requested_start()
            end = _day(item["requested_to"]) if item["requested_to"] else completed_friday()
            first_friday = start + timedelta(days=(4 - start.weekday()) % 7)
            expected = max(0, (end - first_friday).days // 7 + 1)
            present = db.execute("SELECT COUNT(*) FROM merrill_weekly WHERE asset_id=? AND date>=? AND date<=?",
                                 (item["id"], start.isoformat(), end.isoformat())).fetchone()[0]
            span_expected = ((_day(item["available_to"]) - _day(item["available_from"])).days // 7 + 1) if count else 0
            item.update(expected_weeks=expected, missing_weeks=max(0, expected - present),
                        internal_missing_weeks=max(0, span_expected - count),
                        coverage_ratio=round(present / expected, 4) if expected else 0,
                        status="error" if item["last_error"] else "ready" if count else "pending")
            item["coverage_note"] = ("按来源真实可用历史保存；成立前、源不提供与缺失周均留空，未拼接或前填。"
                                     if present < expected else "请求区间所有周均有真实日观测；每周取最后可用交易日。")
            by_id[item["id"]] = item
    return [by_id[item["id"]] for item in ASSET_CATALOG]


def get_series(ids, start=None, end=None):
    if isinstance(ids, str):
        ids = [ids]
    known = {item["id"]: item for item in ASSET_CATALOG}
    ids = list(dict.fromkeys(ids))
    if any(identifier not in known for identifier in ids):
        raise ValueError("未知资产序列")
    start = _day(start).isoformat() if start else "0001-01-01"
    end = _day(end).isoformat() if end else "9999-12-31"
    if start > end:
        raise ValueError("开始日期不能晚于结束日期")
    with connection() as db:
        return [{**known[identifier], "points": [dict(row) for row in db.execute(
            "SELECT date,close,observation_date FROM merrill_weekly WHERE asset_id=? AND date>=? AND date<=? ORDER BY date",
            (identifier, start, end))]} for identifier in ids]


def save_macro(identifier, rows):
    if identifier not in {"INDPRO", "CPIAUCSL", "CN_INDUSTRY_YOY", "CN_CPI_YOY", "CN_PMI"}:
        raise ValueError("未知宏观序列")
    clean = {}
    today = datetime.now(timezone.utc).date()
    for row in rows:
        day = _day(row["date"])
        if day.day != 1 or day >= today.replace(day=1):
            raise ValueError("宏观观测必须使用已结束月份的首日日期")
        clean[day.isoformat()] = _finite(row["value"])
    with connection() as db:
        db.executemany("INSERT INTO merrill_macro VALUES(?,?,?) ON CONFLICT(series_id,date) DO UPDATE SET value=excluded.value",
                       [(identifier, day, value) for day, value in clean.items()])
    return len(clean)


def get_macro():
    result = {key: [] for key in ("INDPRO", "CPIAUCSL", "CN_INDUSTRY_YOY", "CN_CPI_YOY", "CN_PMI")}
    with connection() as db:
        for row in db.execute("SELECT * FROM merrill_macro ORDER BY series_id,date"):
            result.setdefault(row["series_id"], []).append({"date": row["date"], "value": row["value"]})
    return result


def get_state(key, default=None):
    with connection() as db:
        row = db.execute("SELECT payload FROM merrill_state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default


def set_state(key, value):
    with connection() as db:
        db.execute("INSERT INTO merrill_state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload", (key, _json(value)))


def _analysis_key(user_id, region):
    if type(user_id) is not int or user_id < 1 or region not in {"US", "CN"}:
        raise ValueError("账户或区域无效")
    return user_id, region


def get_analysis(user_id, region="US"):
    with connection() as db:
        row = db.execute("SELECT payload FROM merrill_analyses WHERE user_id=? AND region=?", _analysis_key(user_id, region)).fetchone()
        return json.loads(row[0]) if row else None


def save_analysis(user_id, payload, region="US"):
    with connection() as db:
        db.execute("INSERT INTO merrill_analyses VALUES(?,?,?,?) "
                   "ON CONFLICT(user_id,region) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at",
                   (*_analysis_key(user_id, region), _json(payload), time.time()))
