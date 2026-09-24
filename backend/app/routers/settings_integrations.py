"""
统一查看/更新：大模型（OpenAI 兼容）与各数据源的凭证与说明。

GET  不返回完整密钥，仅 masked 与来源；PUT 可一并更新 Tushare 与 LLM（与 PUT /api/settings/llm 等价字段）。
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.config import settings
from app.deps.auth import get_current_user, require_admin
from app.storage.integrations_store import load_integrations, merge_integrations
from app.storage.platform_store import load_platform
from app.storage.user_credentials_store import load_user_credentials, merge_user_credentials
from app.storage.users_store import UserRecord

from app.routers.settings_llm import llm_public_dict

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _suffix(key: str | None) -> str | None:
    if not key or len(key) < 6:
        return None
    return key[-4:]


def _adata_block() -> dict[str, Any]:
    data = load_integrations()
    return {
        "proxy_enabled": bool(
            str(data.get("adata_proxy_enabled", "")).lower() in ("1", "true", "yes")
            or data.get("adata_proxy_enabled") is True
            or settings.adata_proxy_enabled
        ),
        "proxy_ip_configured": bool((data.get("adata_proxy_ip") or settings.adata_proxy_ip or "").strip()),
        "proxy_url_configured": bool((data.get("adata_proxy_url") or settings.adata_proxy_url or "").strip()),
        "env_keys": ["ADATA_PROXY_ENABLED", "ADATA_PROXY_IP", "ADATA_PROXY_URL"],
        "integrations_keys": ["adata_proxy_enabled", "adata_proxy_ip", "adata_proxy_url"],
    }


def _xueqiu_block() -> dict[str, Any]:
    data = load_integrations()
    raw = (data.get("xueqiu_cookies") or "").strip()
    env = False
    try:
        from app.config import settings as _s

        env = bool((_s.xueqiu_cookies or "").strip())
    except Exception:
        env = False
    n = len(raw) if raw else 0
    return {
        "cookies_configured": n > 0 or env,
        "integrations_key": "xueqiu_cookies",
        "priority_hint": "integrations.json 中 xueqiu_cookies 非空时优先于环境变量 XUEQIU_COOKIES；见 backend/docs/INTEGRATIONS_AND_CACHE.md",
        "hint": "默认自动获取并缓存雪球匿名 Cookie，无需填写。仅当雪球要求登录或验证时，由管理员登录后补充登录 Cookie；勿提交到 git",
    }


def _tushare_block(user_id: int) -> dict[str, Any]:
    personal_tok = (load_user_credentials(user_id).get("tushare_token") or "").strip()
    if personal_tok:
        src = "personal"
        effective = personal_tok
    else:
        platform_tok = (load_integrations().get("tushare_token") or "").strip()
        env_tok = (settings.tushare_token or "").strip()
        effective = platform_tok or env_tok
        src = "platform" if platform_tok else "environment" if env_tok else "none"
    return {
        "configured": bool(effective),
        "token_source": src,
        "token_suffix": _suffix(effective) if effective else None,
        "fallback_role": "可选接入：宏观/行业与 K 线兜底（配置台中选择 Tushare 时启用）",
        "env_key": "TUSHARE_TOKEN",
        "documentation_url": "https://tushare.pro/document/2",
        "optional": True,
    }


def _klineshare_block(user_id: int) -> dict[str, Any]:
    from app.services import klineshare_service

    personal = (load_user_credentials(user_id).get("klineshare_api_key") or "").strip()
    if personal:
        src = "personal"
        effective = personal
    else:
        platform_key = (load_integrations().get("klineshare_api_key") or "").strip()
        env_key = (settings.klineshare_api_key or "").strip()
        effective = platform_key or env_key
        src = "platform" if platform_key else "environment" if env_key else "none"
    return {
        "configured": bool(effective),
        "key_source": src,
        "key_suffix": _suffix(effective) if effective else None,
        "api_base": klineshare_service.effective_api_base(),
        "documentation_url": "https://data.klineshare.cn/docs",
        "recommended": True,
        "role": "推荐可选行情源：A 股 K 线、实时行情等（在配置台选择 KlineShare 时优先于公开源链）",
        "env_keys": ["KLINESHARE_API_KEY", "KLINESHARE_API_BASE"],
        "integrations_keys": ["klineshare_api_key", "klineshare_api_base"],
    }


def _market_data_block(user_id: int) -> dict[str, Any]:
    from app.services import klineshare_service, tushare_service

    provider = klineshare_service.effective_market_data_provider()
    configured = False
    if provider == "public":
        configured = True
    elif provider == "klineshare":
        configured = klineshare_service.is_configured()
    elif provider == "tushare":
        configured = tushare_service.is_configured()
    return {
        "provider": provider or "unset",
        "configured": configured,
        "options": [
            {
                "id": "klineshare",
                "label": "KlineShare（推荐）",
                "documentation_url": "https://data.klineshare.cn/docs",
            },
            {"id": "tushare", "label": "Tushare Pro", "documentation_url": "https://tushare.pro/document/2"},
            {
                "id": "public",
                "label": "仅公开源（东财/腾讯等，无需 Key）",
                "documentation_url": "https://github.com",
            },
        ],
    }


# 无密钥的公开/爬虫类数据源说明（便于前端一页展示）
PUBLIC_DATA_SOURCES: list[dict[str, Any]] = [
    {
        "id": "eastmoney_market",
        "name": "东方财富行情",
        "category": "http_public",
        "authentication": "none",
        "description": "A 股 K 线、快照、涨跌停等（push2 / push2his）",
        "documentation_url": "https://quote.eastmoney.com/",
        "notes": "无需 Token；见 GET /api/market/sources",
    },
    {
        "id": "market_http_extras",
        "name": "行情扩展（直连公开 HTTP）",
        "category": "http_public",
        "authentication": "none",
        "description": "腾讯 fqkline、新浪 getKLineData、腾讯 mkline 分钟兜底；东财数据中心与同花顺部分榜单/ETF 等；MyTT 见 /api/market/mytt",
        "notes": "无第三方行情 SDK；可选代理 ADATA_PROXY_* 或 integrations.json adata_proxy_*",
    },
    {
        "id": "eastmoney_clist",
        "name": "东方财富股票列表",
        "category": "http_public",
        "authentication": "none",
        "description": "全市场代码与简称，用于搜索补全",
        "notes": "push2.eastmoney.com clist；见 /api/stocks/search",
    },
    {
        "id": "eastmoney_f10",
        "name": "东方财富 F10 公司资料",
        "category": "scrape_html_json",
        "authentication": "none",
        "description": "公司简介、主营业务等（股票列表公司资料区）",
    },
    {
        "id": "cn_headlines_rss",
        "name": "财经快讯（东财栏目 + RSS）",
        "category": "rss_json",
        "authentication": "none",
        "description": "人民网/中新网等 RSS 与东财快讯列表",
        "notes": "见 GET /api/news/cn-headlines/sources",
    },
    {
        "id": "rss_global",
        "name": "RSS 聚合",
        "category": "rss",
        "authentication": "none",
        "description": "全局与标的相关 RSS，入库新闻库",
    },
    {
        "id": "klineshare_api",
        "name": "KlineShare 行情 API",
        "category": "api_key_optional",
        "authentication": "api_key",
        "description": "可选 A 股 K 线/行情增强（配置台推荐接入）",
        "documentation_url": "https://data.klineshare.cn/docs",
        "notes": "在配置台填写 API Key 并选择 KlineShare 为行情提供商",
    },
    {
        "id": "tushare_pro",
        "name": "Tushare Pro（可选）",
        "category": "api_token_optional",
        "authentication": "token",
        "description": "宏观/行业与 K 线兜底",
        "documentation_url": "https://tushare.pro/document/2",
        "notes": "在配置台选择 Tushare 为行情提供商时启用",
    },
    {
        "id": "baostock_sdk",
        "name": "Baostock（Python SDK）",
        "category": "sdk_optional",
        "authentication": "none",
        "description": "K 线兜底：日/周/月与前复权；分钟 5/15/30/60",
        "documentation_url": "http://www.baostock.com/mainContent?file=pythonAPI.md",
        "notes": "pip install baostock；管理员可在平台开关中禁用",
    },
    {
        "id": "pytdx_sdk",
        "name": "Pytdx（通达信协议）",
        "category": "sdk_optional",
        "authentication": "none",
        "description": "K 线链末位兜底；连接公网行情前置",
        "documentation_url": "https://pytdx-docs.readthedocs.io/zh-cn/latest/pytdx_hq/",
        "notes": "pip install pytdx；管理员可在平台开关中禁用",
    },
]


def integrations_payload(user_id: int) -> dict:
    """统一入口：大模型状态 + 行情提供商 + 各数据源说明 + 平台开关。"""
    return {
        "llm": llm_public_dict(user_id),
        "market_data": _market_data_block(user_id),
        "klineshare": _klineshare_block(user_id),
        "tushare": _tushare_block(user_id),
        "xueqiu": _xueqiu_block(),
        "adata": _adata_block(),
        "platform": load_platform(),
        "public_data_sources": PUBLIC_DATA_SOURCES,
        "storage": {
            "llm_runtime_file": "backend/data/llm_runtime.json",
            "integrations_file": "backend/data/integrations.json",
            "platform_file": "backend/data/platform.json",
            "env_hint": "backend/.env 中 OPENAI_* / KLINE_* / KLINESHARE_* / TUSHARE_TOKEN / AUTH_*",
        },
    }


def setup_status_payload(user_id: int) -> dict[str, Any]:
    from app.services import klineshare_service, tushare_service

    llm_ok = settings.any_llm_key_configured
    market = _market_data_block(user_id)
    market_ok = bool(market.get("configured"))
    provider = market.get("provider") or "unset"
    hints: list[str] = []
    if not llm_ok:
        hints.append("配置大模型 API Key、Base 与 Model（OpenAI 兼容）")
    if not market_ok:
        if provider == "unset":
            hints.append("选择行情提供商：推荐 KlineShare，或 Tushare / 仅公开源")
        elif provider == "klineshare":
            hints.append("填写并测试 KlineShare API Key")
        elif provider == "tushare":
            hints.append("填写并测试 Tushare Token")
    return {
        "ready": llm_ok and market_ok,
        "llm_configured": llm_ok,
        "market_data_configured": market_ok,
        "market_data_provider": provider,
        "klineshare_configured": klineshare_service.is_configured(),
        "tushare_configured": tushare_service.is_configured(),
        "onboarding_title": "首次使用请先完成配置",
        "onboarding_message": "请先自行配置大模型 API 与行情信息接口，再使用 K 线解读、Transaction Agent 等能力。",
        "onboarding_hints": hints,
        "config_console_path": "/setup",
        "docs": {
            "klineshare": "https://data.klineshare.cn/docs",
            "llm": "配置台 · 大模型",
        },
    }


@router.get("/integrations")
def get_integrations(user: Annotated[UserRecord, Depends(get_current_user)]):
    return integrations_payload(user.id)


@router.get("/setup-status")
def get_setup_status(user: Annotated[UserRecord, Depends(get_current_user)]):
    """首次打开引导：大模型 + 行情接口是否已配置。"""
    return setup_status_payload(user.id)


@router.get("/data-center")
def get_data_center(_: Annotated[UserRecord, Depends(get_current_user)]):
    from app.services import data_center
    return data_center.snapshot()


@router.post("/data-center/refresh")
def refresh_data_center(_: Annotated[UserRecord, Depends(require_admin)]):
    from app.services import startup_data_check
    return startup_data_check.schedule_background_refresh()


class LLMPatch(BaseModel):
    api_key: str | None = None
    api_base: str | None = None
    model: str | None = None
    embedding_model: str | None = None
    embedding_api_base: str | None = None


class IntegrationsUpdate(BaseModel):
    xueqiu_cookies: str | None = Field(
        default=None,
        description="可选的雪球登录 Cookie，优先于自动匿名会话；空字符串清除 integrations 中的值",
    )
    tushare_token: str | None = Field(
        default=None,
        description="传入则写入当前用户隔离凭据；空字符串清除个人凭据并回退平台或环境 Token",
    )
    klineshare_api_key: str | None = Field(
        default=None,
        description="KlineShare API Key；空字符串清除个人凭据",
    )
    market_data_provider: str | None = Field(
        default=None,
        description="行情提供商：klineshare | tushare | public",
    )
    llm: LLMPatch | None = Field(default=None, description="与 PUT /api/settings/llm 相同字段")
    adata_proxy_enabled: bool | None = Field(default=None, description="是否启用行情扩展 HTTP 代理")
    adata_proxy_ip: str | None = Field(default=None, description="host:port；空字符串清除文件中的值")
    adata_proxy_url: str | None = Field(default=None, description="代理池 URL；空字符串清除")


@router.post("/integrations/test-klineshare")
def test_klineshare(user: Annotated[UserRecord, Depends(get_current_user)]):
    from app.services import klineshare_service

    result = klineshare_service.test_connection()
    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=result.get("message") or "KlineShare 连接失败")
    return {"ok": True, **result}


@router.post("/integrations/test-tushare")
def test_tushare(user: Annotated[UserRecord, Depends(get_current_user)]):
    from app.services import tushare_service
    row, err = tushare_service.fetch_stock_basic("000001.SZ")
    if err or not row:
        raise HTTPException(status_code=502, detail=f"Tushare 连接失败：{err or '无返回'}")
    return {"ok": True, "source": _tushare_block(user.id)["token_source"], "sample": {"ts_code": row.get("ts_code"), "name": row.get("name")}}


@router.put("/integrations")
def put_integrations(body: IntegrationsUpdate, user: Annotated[UserRecord, Depends(get_current_user)]):
    patch = body.model_dump(exclude_unset=True)
    llm_update = None
    if body.llm is not None:
        lm = body.llm.model_dump(exclude_unset=True)
        if lm:
            from app.security.llm_endpoints import validate_llm_endpoints
            from app.services.llm_runtime_unify import unify_llm_runtime_fields

            current = dict(load_user_credentials(user.id).get("llm", {}))
            current.update(lm)
            validate_llm_endpoints(current)
            llm_update = unify_llm_runtime_fields(current)
    platform_keys = {
        "xueqiu_cookies",
        "adata_proxy_enabled",
        "adata_proxy_ip",
        "adata_proxy_url",
    }
    if user.role != "admin" and platform_keys.intersection(patch):
        raise HTTPException(status_code=403, detail="全局平台集成仅管理员可修改")
    if "xueqiu_cookies" in patch:
        merge_integrations({"xueqiu_cookies": patch["xueqiu_cookies"]})
    if "tushare_token" in patch:
        merge_user_credentials(user.id, {"tushare_token": patch["tushare_token"]})
    if "klineshare_api_key" in patch:
        merge_user_credentials(user.id, {"klineshare_api_key": patch["klineshare_api_key"]})
    if "market_data_provider" in patch:
        raw = (patch["market_data_provider"] or "").strip().lower()
        if raw and raw not in {"klineshare", "tushare", "public"}:
            raise HTTPException(status_code=400, detail="market_data_provider 须为 klineshare、tushare 或 public")
        merge_user_credentials(user.id, {"market_data_provider": raw or None})

    adata_keys = ("adata_proxy_enabled", "adata_proxy_ip", "adata_proxy_url")
    adata_patch = {k: patch[k] for k in adata_keys if k in patch}
    if adata_patch:
        merge_integrations(adata_patch)

    if llm_update is not None:
        merge_user_credentials(user.id, {"llm": llm_update})

    return integrations_payload(user.id)
