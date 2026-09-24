import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import settings
from app.middlewares.error_handler import ErrorHandlerMiddleware, register_exception_handlers
from app.middlewares.trusted_origin import TrustedOriginMiddleware
from app.routers import (
    admin,
    analysis,
    auth,
    decision_signals,
    kline_learning,
    macro,
    market,
    news,
    quant,
    settings_integrations,
    settings_llm,
    stock_picker_agents,
    stocks,
    transaction_agent,
    watchlist,
    xueqiu,
)
from app.storage import company_fundamentals_store, decision_signal_store, news_store, stock_picker_agent_store, strategy_knowledge_store, tech_media_store, users_store

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.services import (
        company_updates,
        kline_insight_scheduler,
        startup_data_check,
        xueqiu_comments_scheduler,
        xueqiu_comments_service,
    )
    from app.storage import data_asset_manager

    scheduler_task: asyncio.Task | None = None
    startup_check_task: asyncio.Task | None = None
    company_updates_task: asyncio.Task | None = None
    comments_worker_task = asyncio.create_task(xueqiu_comments_service.worker_loop())
    comments_scheduler_task: asyncio.Task | None = None
    data_asset_manager.refresh_catalog_from_disk()
    if settings.enable_scheduler:
        scheduler_task = asyncio.create_task(kline_insight_scheduler.kline_insight_scheduler_loop())
        if settings.xueqiu_comments_auto_refresh_enabled:
            comments_scheduler_task = asyncio.create_task(xueqiu_comments_scheduler.scheduler_loop())
    if settings.enable_startup_data_check:
        startup_check_task = asyncio.create_task(startup_data_check.delayed_startup_data_check())
    if settings.company_auto_refresh_enabled:
        company_updates_task = asyncio.create_task(company_updates.company_updates_loop())
    yield
    for task in (scheduler_task, startup_check_task, company_updates_task, comments_worker_task, comments_scheduler_task):
        if task is not None:
            task.cancel()
    if scheduler_task is not None:
        try:
            await scheduler_task
        except asyncio.CancelledError:
            pass
    if startup_check_task is not None:
        try:
            await asyncio.wait_for(startup_check_task, timeout=3.0)
        except asyncio.CancelledError:
            pass
        except asyncio.TimeoutError:
            logger.warning("启动数据源检查取消超时，跳过等待以便服务快速退出")
    if company_updates_task is not None:
        try:
            await asyncio.wait_for(company_updates_task, timeout=3.0)
        except asyncio.CancelledError:
            pass
        except asyncio.TimeoutError:
            logger.warning("公司资料更新取消超时，跳过等待以便服务快速退出")
    for task in (comments_worker_task, comments_scheduler_task):
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=3.0)
            except asyncio.CancelledError:
                pass
            except asyncio.TimeoutError:
                logger.warning("雪球评论任务取消超时，任务将在租约到期后恢复")


app = FastAPI(
    title="StockViewAgent API",
    version="1.0.0",
    lifespan=lifespan,
    openapi_tags=[
        {"name": "watchlist", "description": "股票列表（`watchlist.json`、导入、行情摘要、公司资料）"},
        {"name": "macro", "description": "市场研判 · 宏观与行业（Tushare）"},
        {
            "name": "market",
            "description": "A股多源行情与统一缓存；日线按可配置公开源链降级并可用 Tushare/Pytdx 兜底，分钟走东财/腾讯/Baostock/Pytdx；MyTT 指标 /mytt/*",
        },
        {"name": "kline-learning", "description": "K 线学习资料与个股/大盘解读"},
        {"name": "news", "description": "多源新闻聚合与本地 SQLite 归档"},
        {"name": "analysis", "description": "新闻影响分析等"},
        {"name": "auth", "description": "登录与当前用户"},
        {"name": "admin", "description": "管理员：用户、权限、平台开关"},
        {"name": "settings", "description": "LLM 与集成配置（需登录；写入限管理员）"},
        {"name": "stocks", "description": "全市场股票搜索补全"},
        {"name": "transaction-agent", "description": "TransactionAgent 多策略交易观点（数据接口已对齐本系统）"},
        {"name": "quant", "description": "量化交易：策略制定、模板化回测、绩效报告"},
        {"name": "decision-signals", "description": "决策信号生命周期与 1/3/5/10 日后验复盘"},
        {"name": "stock-picker-agents", "description": "可新增、编辑、启停和执行的用户自定义选股 Agent"},
        {"name": "xueqiu", "description": "雪球可选补充：公司信息、讨论摘要等（需 Cookie 或匿名 warm-up）"},
    ],
)

app.add_middleware(
    TrustedOriginMiddleware,
    allowed_origins=settings.cors_allowed_origins,
)
app.add_middleware(ErrorHandlerMiddleware)

news_store.init_db()
tech_media_store.init_db()
users_store.init_db()
company_fundamentals_store.init_db()
strategy_knowledge_store.init_db()
decision_signal_store.init_db()
stock_picker_agent_store.init_db()

register_exception_handlers(app)

app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(watchlist.router)
app.include_router(analysis.router)
app.include_router(news.router)
app.include_router(market.router)
app.include_router(macro.router)
app.include_router(kline_learning.router)
app.include_router(settings_llm.router)
app.include_router(settings_integrations.router)
app.include_router(stocks.router)
app.include_router(transaction_agent.router)
app.include_router(decision_signals.router)
app.include_router(stock_picker_agents.router)
app.include_router(quant.router)
app.include_router(xueqiu.router)


@app.get("/api/health")
def health():
    return {"status": "ok", "app": "StockViewAgent"}


@app.get("/api/ready")
def ready():
    """容器编排就绪探针：确认应用及用户数据库可读。"""
    from app.services import startup_data_check

    users_store.list_users()
    return {"status": "ready", "startup_data_check": startup_data_check.load_state() or None}
