"""东方财富 PC_HSF10 公司概况（A 股）。"""
from __future__ import annotations

import random
import re
import time
from typing import Any

import httpx

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
_SURVEY = "https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/PageAjax"


def yahoo_symbol_to_em_code(symbol: str) -> str | None:
    s = (symbol or "").strip().upper()
    if "." not in s:
        return None
    code, suf = s.rsplit(".", 1)
    code = code.strip()
    if not re.fullmatch(r"\d{6}", code):
        return None
    if suf == "SS" or suf == "SH":
        return f"SH{code}"
    if suf == "SZ":
        return f"SZ{code}"
    if suf == "BJ":
        return f"BJ{code}"
    return None


def fetch_company_survey(
    symbol: str,
    *,
    timeout_sec: float | None = None,
    max_retries: int | None = None,
) -> tuple[dict[str, Any], str | None]:
    """
    返回 (auto 字段字典, 错误信息)。
    成功时错误为 None；默认超时 40 秒、连接超时 18 秒、重试 3 次，可由调用方调整。
    """
    em_code = yahoo_symbol_to_em_code(symbol)
    if not em_code:
        return (
            {"name": "", "org_name": "", "main_business": "", "industry": "", "intro": ""},
            "非 A 股 6 位代码格式（需 .SS/.SZ/.BJ），无法拉取东方财富 F10",
        )
    data = None
    last_err: Exception | None = None
    retryable = (
        httpx.RemoteProtocolError,
        httpx.ConnectError,
        httpx.ReadTimeout,
        httpx.ConnectTimeout,
    )
    timeout = httpx.Timeout(40.0, connect=18.0) if timeout_sec is None else httpx.Timeout(timeout_sec)
    retries = 3 if max_retries is None else max(0, max_retries)
    try:
        with httpx.Client(timeout=timeout, headers={"User-Agent": UA}) as client:
            for attempt in range(retries + 1):
                try:
                    r = client.get(_SURVEY, params={"code": em_code})
                    r.raise_for_status()
                    data = r.json()
                    break
                except retryable as e:
                    last_err = e
                    if attempt < retries:
                        time.sleep(0.45 + random.random() * 0.55)
                except httpx.HTTPStatusError as e:
                    last_err = e
                    if e.response.status_code >= 500 and attempt < retries:
                        time.sleep(0.45 + random.random() * 0.55)
                        continue
                    raise
    except Exception as e:  # noqa: BLE001
        return (
            {"name": "", "org_name": "", "main_business": "", "industry": "", "intro": ""},
            f"拉取失败：{e!s}",
        )
    if data is None and last_err is not None:
        return (
            {"name": "", "org_name": "", "main_business": "", "industry": "", "intro": ""},
            f"拉取失败：{last_err!s}",
        )
    if data is None:
        return (
            {"name": "", "org_name": "", "main_business": "", "industry": "", "intro": ""},
            "拉取失败：无响应",
        )
    jbzl = data.get("jbzl") if isinstance(data, dict) else None
    if not isinstance(jbzl, list) or not jbzl or not isinstance(jbzl[0], dict):
        return (
            {"name": "", "org_name": "", "main_business": "", "industry": "", "intro": ""},
            "东方财富未返回公司基本资料（可能代码无效或已退市）",
        )
    jb = jbzl[0]
    intro = (jb.get("ORG_PROFILE") or "").strip()
    scope = (jb.get("BUSINESS_SCOPE") or "").strip()
    industry = (jb.get("EM2016") or jb.get("INDUSTRYCSRC1") or "").strip()
    return (
        {
            "name": (jb.get("SECURITY_NAME_ABBR") or "").strip(),
            "org_name": (jb.get("ORG_NAME") or "").strip(),
            "main_business": scope[:4000],
            "industry": industry[:500],
            "intro": intro[:8000],
        },
        None,
    )
