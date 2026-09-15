"""
A 股快讯：多备用源注册表 + 每日健康探测 + 状态持久化。

- 东财 7×24：同一接口不同 fastColumn 作互为备份（102/103/104）。
- RSS：人民网财经、中新网财经等为新浪失效时的备用；支持 data 目录覆盖配置扩展 URL。
- 每天（Asia/Shanghai 日历日）首次请求时跑一轮轻量 probe，更新可用性；站点变更时可人工改覆盖 JSON。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import feedparser
import httpx

from app.config import settings

_CN = ZoneInfo("Asia/Shanghai")
_STATE_FILE = "cn_headline_sources_state.json"
_OVERRIDE_FILE = "cn_headline_sources_override.json"
_SCHEMA = 1

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
_EM_URL = "https://np-weblist.eastmoney.com/comm/web/getFastNewsList"

_maint_lock = asyncio.Lock()


def _state_path() -> Path:
    return settings.data_dir / _STATE_FILE


def _override_path() -> Path:
    return settings.data_dir / _OVERRIDE_FILE


def _default_state() -> dict[str, Any]:
    return {
        "schema": _SCHEMA,
        "last_maintenance_calendar_date": "",
        "source_health": {},
        "notes": [],
    }


def _load_state() -> dict[str, Any]:
    p = _state_path()
    if not p.exists():
        return _default_state()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return _default_state()
        raw.setdefault("schema", _SCHEMA)
        raw.setdefault("last_maintenance_calendar_date", "")
        raw.setdefault("source_health", {})
        if not isinstance(raw["source_health"], dict):
            raw["source_health"] = {}
        raw.setdefault("notes", [])
        return raw
    except (OSError, json.JSONDecodeError):
        return _default_state()


def _save_state(state: dict[str, Any]) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    state["schema"] = _SCHEMA
    p.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_override_rss() -> list[dict[str, Any]]:
    p = _override_path()
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        rss = raw.get("rss") if isinstance(raw, dict) else None
        if not isinstance(rss, list):
            return []
        out: list[dict[str, Any]] = []
        for i, row in enumerate(rss):
            if not isinstance(row, dict):
                continue
            url = str(row.get("url") or "").strip()
            if not url or "example.com" in url:
                continue
            out.append(
                {
                    "id": str(row.get("id") or f"override_{i}").strip() or f"override_{i}",
                    "label": str(row.get("label") or "自定义 RSS").strip(),
                    "url": url,
                    "referer": str(row.get("referer") or "").strip(),
                    "priority": int(row.get("priority", 80)),
                }
            )
        return out
    except (OSError, json.JSONDecodeError, ValueError):
        return []


def load_custom_rss_sources() -> list[dict[str, Any]]:
    """返回用户配置的附加 RSS 源，供控制台展示。"""
    return _load_override_rss()


def save_custom_rss_sources(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """校验并保存附加 RSS 源；保存后会自动进入快讯抓取与每日健康探测。"""
    if len(rows) > 50:
        raise ValueError("自定义 RSS 最多 50 个")

    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"第 {i + 1} 项必须是对象")
        source_id = str(row.get("id") or f"custom_{i + 1}").strip()
        label = str(row.get("label") or "自定义 RSS").strip()
        url = str(row.get("url") or "").strip()
        parsed = urlparse(url)
        if not source_id or source_id in seen_ids:
            raise ValueError(f"第 {i + 1} 项 id 为空或重复")
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"第 {i + 1} 项不是有效的 HTTP(S) RSS 地址")
        seen_ids.add(source_id)
        normalized.append(
            {
                "id": source_id,
                "label": label,
                "url": url,
                "referer": str(row.get("referer") or "").strip(),
                "priority": max(1, min(int(row.get("priority", 80)), 999)),
            }
        )

    p = _override_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "_comment": "控制台维护的自定义 RSS；priority 越小越优先。",
        "rss": normalized,
    }
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return normalized


def builtin_source_rows() -> list[dict[str, Any]]:
    """内置源：priority 越小越优先合并展示。"""
    rows: list[dict[str, Any]] = []
    for col, pr in (("102", 5), ("103", 6), ("104", 7)):
        rows.append(
            {
                "id": f"em724_fc{col}",
                "kind": "eastmoney_json",
                "label": f"东方财富 · 7×24（栏目{col}）",
                "priority": pr,
                "fast_column": col,
            }
        )
    # 新浪（部分环境解析条数为 0，保留作备用链；稳定 id 便于 state 记录）
    for lab, url in [
        ("新浪 · 股票要闻", "https://rss.sina.com.cn/roll/stock/hot_roll.xml"),
        ("新浪 · 财经滚动", "https://rss.sina.com.cn/roll/finance/hot_roll.xml"),
        ("新浪 · 财经焦点", "https://rss.sina.com.cn/news/allnews/finance.xml"),
    ]:
        sid = f"sina_{hashlib.md5(lab.encode()).hexdigest()[:12]}"
        rows.append(
            {
                "id": sid,
                "kind": "rss",
                "label": lab,
                "url": url,
                "referer": "https://finance.sina.com.cn/",
                "priority": 40,
            }
        )
    # 实测可用的宏观/财经 RSS，作新浪失效备份
    rows.append(
        {
            "id": "rss_people_finance",
            "kind": "rss",
            "label": "人民网 · 财经",
            "url": "http://www.people.com.cn/rss/finance.xml",
            "referer": "http://finance.people.cn/",
            "priority": 15,
        }
    )
    rows.append(
        {
            "id": "rss_chinanews_finance",
            "kind": "rss",
            "label": "中新网 · 财经",
            "url": "https://www.chinanews.com.cn/rss/finance.xml",
            "referer": "https://www.chinanews.com.cn/",
            "priority": 18,
        }
    )
    rows.append(
        {
            "id": "rss_jrj_finance",
            "kind": "rss",
            "label": "金融界 · 财经",
            "url": "https://rss.jrj.com.cn/rss/finance.xml",
            "referer": "https://finance.jrj.com.cn/",
            "priority": 22,
        }
    )
    for o in _load_override_rss():
        rows.append(
            {
                "id": o["id"],
                "kind": "rss",
                "label": o["label"],
                "url": o["url"],
                "referer": o["referer"],
                "priority": o["priority"],
            }
        )
    return rows


async def _probe_em(client: httpx.AsyncClient, fast_column: str) -> int:
    params = {
        "client": "web",
        "biz": "web_724",
        "fastColumn": fast_column,
        "sortEnd": "0",
        "pageSize": "8",
        "req_trace": "1",
    }
    try:
        r = await client.get(_EM_URL, params=params)
        r.raise_for_status()
        data = r.json()
        inner = (data or {}).get("data") if isinstance(data, dict) else None
        if not isinstance(inner, dict):
            return 0
        lst = inner.get("fastNewsList") or []
        return len(lst) if isinstance(lst, list) else 0
    except (httpx.HTTPError, json.JSONDecodeError, TypeError, ValueError):
        return 0


async def _probe_rss(client: httpx.AsyncClient, url: str, referer: str) -> int:
    headers: dict[str, str] = {}
    if referer:
        headers["Referer"] = referer
    try:
        r = await client.get(url, follow_redirects=True, headers=headers or None)
        if r.status_code != 200:
            return 0
        d = feedparser.parse(r.content)
        return len(getattr(d, "entries", None) or [])
    except httpx.HTTPError:
        return 0


async def _run_maintenance_body() -> dict[str, Any]:
    """探测所有内置源并写入 state（按上海日历日只跑一次）。"""
    today = datetime.now(_CN).date().isoformat()
    state = _load_state()
    if state.get("last_maintenance_calendar_date") == today:
        return {"skipped": True, "reason": "already_today", "calendar_date": today}

    rows = builtin_source_rows()
    health: dict[str, Any] = dict(state.get("source_health") or {})
    notes: list[str] = []

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(22.0, connect=12.0),
        headers={"User-Agent": UA, "Referer": "https://kuaixun.eastmoney.com/"},
    ) as client:
        for row in rows:
            sid = row["id"]
            kind = row["kind"]
            n = 0
            if kind == "eastmoney_json":
                n = await _probe_em(client, row["fast_column"])
            elif kind == "rss":
                n = await _probe_rss(client, row["url"], row.get("referer") or "")
            prev = health.get(sid) if isinstance(health.get(sid), dict) else {}
            fail_days = int(prev.get("fail_days") or 0)
            if n > 0:
                fail_days = 0
            else:
                fail_days = min(fail_days + 1, 30)
            health[sid] = {
                "last_probe_entries": n,
                "last_ok": n > 0,
                "fail_days": fail_days,
                "updated_ts": int(time.time()),
                "label": row.get("label"),
            }
            await asyncio.sleep(0.08)

    ok_n = sum(1 for h in health.values() if isinstance(h, dict) and h.get("last_ok"))
    if ok_n == 0:
        notes.append("今日探测：所有内置源均未解析到条目，仍将尝试完整抓取（可能为临时网络问题）。")

    state["source_health"] = health
    state["last_maintenance_calendar_date"] = today
    state["notes"] = notes[-5:]
    _save_state(state)
    return {
        "skipped": False,
        "calendar_date": today,
        "sources_probed": len(rows),
        "healthy_count": ok_n,
        "notes": notes,
    }


async def run_daily_maintenance_if_due() -> dict[str, Any]:
    async with _maint_lock:
        return await _run_maintenance_body()


def source_enabled(source_id: str, kind: str) -> bool:
    """东财栏目始终尝试；RSS 连续多日探测为 0 则暂停以省流量，紧急回退时会无视。"""
    if kind == "eastmoney_json":
        return True
    st = _load_state()
    h = (st.get("source_health") or {}).get(source_id)
    if not isinstance(h, dict):
        return True
    fail_days = int(h.get("fail_days") or 0)
    return fail_days < 5


def sources_for_fetch(include_disabled_rss: bool = False) -> list[dict[str, Any]]:
    rows = sorted(builtin_source_rows(), key=lambda r: (r["priority"], r["id"]))
    if include_disabled_rss:
        return rows
    return [r for r in rows if source_enabled(r["id"], r["kind"])]


def maintenance_ledger() -> dict[str, Any]:
    st = _load_state()
    return {
        "last_maintenance_calendar_date": st.get("last_maintenance_calendar_date"),
        "source_health": st.get("source_health"),
        "override_file": str(_override_path()),
        "state_file": str(_state_path()),
        "notes": st.get("notes"),
    }


def write_override_template() -> None:
    """生成覆盖配置模板（若不存在）；默认 rss 为空，避免请求无效示例域名。"""
    p = _override_path()
    if p.exists():
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    sample = {
        "_comment": "在此添加自定义 RSS：priority 越小越优先；站点变更时改 url 即可。",
        "rss": [],
    }
    p.write_text(json.dumps(sample, ensure_ascii=False, indent=2), encoding="utf-8")
