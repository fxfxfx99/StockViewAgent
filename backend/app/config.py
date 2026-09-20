from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_BACKEND_DIR = Path(__file__).resolve().parent.parent
_DATA_DIR = _BACKEND_DIR / "data"
_PROJECT_ROOT = _BACKEND_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    openai_api_key: str = ""
    openai_api_base: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"

    # K 线助手 / 市场研判大盘解读 / 新闻分析（优先于 OPENAI_*；兼容 OpenAI 协议）
    kline_assistant_api_key: str = ""
    kline_assistant_api_base: str = ""
    kline_assistant_model: str = ""

    # Kimi（Moonshot）OpenAI 兼容：仅填此项时默认 api_base=https://api.moonshot.cn/v1、model=moonshot-v1-32k（K 线解读上下文较长）
    kimi_api_key: str = ""
    kimi_api_base: str = "https://api.moonshot.cn/v1"
    kimi_model: str = "moonshot-v1-32k"

    # 合并后返回的最大条数（多源聚合）
    news_feed_max_items: int = 250

    # 新闻解读：仅分析 published_ts 在 N 天内的条目；0=不限制。
    news_max_age_days: int = 0
    news_relevance_threshold: int = 60

    # LLM 解读结果完整性：解析失败或字段缺失时额外重试次数（0=不重试）
    analysis_integrity_retries: int = 1

    # 使用 LiteLLM 统一路由（需 pip install litellm）；false 时仍走 OpenAI 兼容 HTTP
    use_litellm: bool = False
    # 留空则使用 openai/{OPENAI_MODEL 或运行时 model}
    litellm_model: str = ""

    # Tushare Pro：环境变量；可被 backend/data/integrations.json 中的 tushare_token 覆盖
    tushare_token: str = ""
    # 自定义数据网关（自建/镜像时填写；对应 pro._DataApi__http_url）
    tushare_data_api_url: str = ""

    # KlineShare 行情 API（可选，见 https://data.klineshare.cn/docs）
    klineshare_api_key: str = ""
    klineshare_api_base: str = "https://data.klineshare.cn"

    # 行情扩展 HTTP 代理（键名 adata_proxy_* 与历史配置兼容；限流/跨境时可开）
    adata_proxy_enabled: bool = False
    adata_proxy_ip: str = ""
    adata_proxy_url: str = ""

    # 可选本地行情库：free-stockdb Windows 本地 HTTP 服务（默认关闭；可作为 K 线兜底源）
    free_stockdb_enabled: bool = False
    free_stockdb_base_url: str = "http://127.0.0.1:7899"
    free_stockdb_timeout_sec: float = 3.0

    # A 股多源策略；也可在 backend/data/integrations.json 用同名小写键覆盖。
    # 可选值见 /api/market/source-health 的 source_policy. 默认保持多层降级链。
    market_kline_daily_chain: str = ""
    market_kline_minute_chain: str = ""
    market_stock_universe_chain: str = ""

    # 登录与权限（JWT）；生产务必设置 AUTH_JWT_SECRET
    auth_jwt_secret: str = "dev-change-me-stockviewagent"
    auth_token_ttl_hours: int = 72
    # false（默认）：本地开放模式，无 Token 时自动使用本地用户，便于 GitHub 自托管部署
    # true：强制登录，接口需 Bearer Token
    auth_required: bool = False
    # 浏览器默认仅允许 loopback 来源；局域网前端/部署域名填写完整来源，逗号分隔。
    # 对应主机也加入 Host 白名单；无 Origin 的可信主机 CLI/Agent 调用保持可用。
    cors_allowed_origins: str = ""
    # 首次启动且库中无用户时，用以下密码创建 admin / user（留空则分别为 admin123 / user123）
    auth_bootstrap_admin_password: str = ""
    auth_bootstrap_user_password: str = ""
    # 多 worker / 多副本部署时只允许独立 scheduler 实例开启，避免重复任务。
    enable_scheduler: bool = True
    # 启动后异步检查/预热外部数据源；不阻塞服务启动。
    enable_startup_data_check: bool = True
    startup_data_check_delay_sec: float = 5.0
    startup_data_check_watchlist_limit: int = 6
    startup_stock_list_ttl_hours: int = 24

    # 统一数据资产管理：backend/data/_managed 保存索引和 JSON 写入前备份
    enable_data_asset_backups: bool = True
    data_asset_max_backups: int = 5

    # 量化交易：实盘委托转发至本机代理（如 Windows + QMT/easytrader miniqmt）；留空则仅支持模拟撮合
    # 量化 API 预留：实盘转发尚未接入
    quant_agent_url: str = ""
    quant_agent_secret: str = ""

    # 雪球：从浏览器复制 Cookie（含登录态），否则接口多返回 400016；见 /api/xueqiu/bundle
    xueqiu_cookies: str = ""
    xueqiu_min_interval_sec: float = 1.2
    xueqiu_max_retries: int = 2
    xueqiu_timeout_sec: float = 25.0
    # 服务运行期间刷新所有账户股票列表的公共公司资料；多实例只开启一个。
    company_auto_refresh_enabled: bool = True
    company_refresh_interval_sec: int = 900

    @property
    def effective_tushare_token(self) -> str:
        """当前账户配置优先，其次平台配置和环境变量。"""
        from app.security.user_context import current_user_id
        from app.storage.integrations_store import load_integrations
        from app.storage.user_credentials_store import load_user_credentials

        uid = current_user_id.get()
        if uid:
            personal = (load_user_credentials(uid).get("tushare_token") or "").strip()
            if personal:
                return personal

        data = load_integrations()
        k = (data.get("tushare_token") or "").strip()
        if k:
            return k
        return (self.tushare_token or "").strip()

    @property
    def data_dir(self) -> Path:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        return _DATA_DIR

    @property
    def kline_learning_dir(self) -> Path:
        p = _PROJECT_ROOT / "kline_learning"
        (p / "files").mkdir(parents=True, exist_ok=True)
        return p

    @property
    def issuer_uploads_dir(self) -> Path:
        p = _PROJECT_ROOT / "issuer_uploads"
        (p / "files").mkdir(parents=True, exist_ok=True)
        return p

    @property
    def kline_llm_key(self) -> str:
        from app.security.user_context import current_user_id
        from app.storage.llm_runtime import load_runtime
        from app.storage.user_credentials_store import load_user_credentials

        uid = current_user_id.get()
        rt = load_user_credentials(uid).get("llm", {}) if uid else load_runtime()
        k = (rt.get("api_key") or "").strip()
        if uid or k:
            return k
        return (
            (self.kimi_api_key or "").strip()
            or (self.kline_assistant_api_key or "").strip()
            or (self.openai_api_key or "").strip()
        )

    @property
    def kline_llm_key_backup(self) -> str:
        """控制台保存的备用 Key（仅 llm_runtime.json，不与环境变量混用）。"""
        from app.security.user_context import current_user_id
        from app.storage.llm_runtime import load_runtime
        from app.storage.user_credentials_store import load_user_credentials

        uid = current_user_id.get()
        rt = load_user_credentials(uid).get("llm", {}) if uid else load_runtime()
        return (rt.get("api_key_backup") or "").strip()

    @property
    def kline_llm_fallback_key(self) -> str:
        """不同提供商的故障切换 Key；与同提供商备用 Key 分开。"""
        from app.security.user_context import current_user_id
        from app.storage.user_credentials_store import load_user_credentials

        uid = current_user_id.get()
        if not uid:
            return ""
        return (load_user_credentials(uid).get("llm", {}).get("fallback_api_key") or "").strip()

    @property
    def kline_llm_fallback_base(self) -> str:
        from app.security.user_context import current_user_id
        from app.storage.llm_runtime import normalize_openai_api_base
        from app.storage.user_credentials_store import load_user_credentials

        uid = current_user_id.get()
        if not uid:
            return ""
        raw = load_user_credentials(uid).get("llm", {}).get("fallback_api_base") or ""
        return normalize_openai_api_base(str(raw).strip())

    @property
    def kline_llm_fallback_model(self) -> str:
        from app.security.user_context import current_user_id
        from app.services.llm_provider_presets import infer_from_api_base
        from app.services.llm_runtime_unify import resolve_chat_model_id
        from app.storage.user_credentials_store import load_user_credentials

        uid = current_user_id.get()
        if not uid:
            return ""
        rt = load_user_credentials(uid).get("llm", {})
        base = self.kline_llm_fallback_base
        inf = infer_from_api_base(base) if base else None
        return resolve_chat_model_id(str(rt.get("fallback_model") or "").strip(), inf) if base else ""

    @property
    def effective_openai_key(self) -> str:
        """与 K 线助手共用解析后的主 Key（含页面保存）；不含备用。"""
        return self.kline_llm_key

    @property
    def any_llm_key_configured(self) -> bool:
        """主 Key 或备用 Key 或环境变量其一有值即视为已配置。"""
        return bool(
            (self.kline_llm_key or "").strip()
            or (self.kline_llm_key_backup or "").strip()
            or (self.kline_llm_fallback_key or "").strip()
        )

    def _news_option(self, name: str, default: int, lower: int, upper: int) -> int:
        from app.storage.integrations_store import load_integrations

        news = load_integrations().get("news")
        raw = news.get(name, default) if isinstance(news, dict) else default
        try:
            return max(lower, min(upper, int(raw)))
        except (ValueError, TypeError, OverflowError):
            return default

    @property
    def effective_news_relevance_threshold(self) -> int:
        return self._news_option("relevance_threshold", self.news_relevance_threshold, 0, 100)

    @property
    def effective_news_max_age_days(self) -> int:
        return self._news_option("max_age_days", self.news_max_age_days, 0, 365)

    @property
    def effective_analysis_integrity_retries(self) -> int:
        return self._news_option("integrity_retries", self.analysis_integrity_retries, 0, 3)

    @property
    def kline_llm_base(self) -> str:
        from app.security.user_context import current_user_id
        from app.storage.llm_runtime import load_runtime, normalize_openai_api_base
        from app.storage.user_credentials_store import load_user_credentials

        uid = current_user_id.get()
        rt = load_user_credentials(uid).get("llm", {}) if uid else load_runtime()
        b = (rt.get("api_base") or "").strip()
        if b:
            return normalize_openai_api_base(b)
        if (self.kimi_api_key or "").strip() and not (self.kline_assistant_api_base or "").strip():
            return normalize_openai_api_base(
                (self.kimi_api_base or "https://api.moonshot.cn/v1").strip()
            )
        b = (self.kline_assistant_api_base or self.openai_api_base or "https://api.openai.com/v1").strip()
        return normalize_openai_api_base(b)

    @property
    def kline_embedding_api_base(self) -> str:
        """RAG /embeddings 使用的 OpenAI 兼容根；未单独配置时与 kline_llm_base 相同。"""
        from app.security.user_context import current_user_id
        from app.storage.llm_runtime import load_runtime, normalize_openai_api_base
        from app.storage.user_credentials_store import load_user_credentials

        uid = current_user_id.get()
        rt = load_user_credentials(uid).get("llm", {}) if uid else load_runtime()
        eb = (rt.get("embedding_api_base") or "").strip()
        if eb:
            return normalize_openai_api_base(eb)
        return self.kline_llm_base

    @property
    def kline_llm_model(self) -> str:
        """与保存时 unify 一致：运行时解析营销名（如 kimi 2.5）为合法 API model id，避免仅改 Key 仍请求错误模型。"""
        from app.security.user_context import current_user_id
        from app.services.llm_provider_presets import infer_from_api_base
        from app.services.llm_runtime_unify import resolve_chat_model_id
        from app.storage.llm_runtime import load_runtime
        from app.storage.user_credentials_store import load_user_credentials

        uid = current_user_id.get()
        rt = load_user_credentials(uid).get("llm", {}) if uid else load_runtime()
        base = self.kline_llm_base
        inf = infer_from_api_base(base) if base else None

        m = (rt.get("model") or "").strip()
        if m:
            return resolve_chat_model_id(m, inf)
        if (self.kline_assistant_model or "").strip():
            return resolve_chat_model_id(self.kline_assistant_model.strip(), inf)
        if (self.kimi_api_key or "").strip():
            return resolve_chat_model_id((self.kimi_model or "moonshot-v1-32k").strip(), inf)
        return resolve_chat_model_id((self.openai_model or "gpt-4o-mini").strip(), inf)


settings = Settings()
