# 数据源与 Agent 数据管线

本机安装与配置台步骤见 [LOCAL_SETUP.md](./LOCAL_SETUP.md)。

本文档说明 **单页 UI 之外仍保留的后端数据能力**：供 Transaction Agent、Research Agent、启动预热与后续扩展使用。

前端单页仅调用子集 API（见 `frontend/src/api.js`）；其余路由仍可通过 OpenAPI / 脚本直接访问。

## 配置优先级（通用）

| 层级 | 位置 | 说明 |
|------|------|------|
| 1 | 用户 LLM 凭证（`user_credentials_store`） | 控制台保存的 API Key |
| 2 | `backend/data/integrations.json` | Tushare、雪球 Cookie、行情链、代理等 |
| 3 | `backend/.env` | 见 `backend/.env.example` |
| 4 | 代码默认值 | `app/config.py` |

详见 `backend/docs/INTEGRATIONS_AND_CACHE.md`。

## 行情与 K 线

| 能力 | 服务 / 路由 | 配置 |
|------|-------------|------|
| 日线 / 分钟 K 线降级链 | `kline_pipeline.py`, `market_source_policy.py` | `MARKET_KLINE_*_CHAIN` 或 integrations.json |
| 东财 / 腾讯 / 百度等 | `eastmoney_market.py`, `market_extra_http.py` | `ADATA_PROXY_*` |
| Tushare 兜底 | `tushare_service.py` | `TUSHARE_TOKEN` |
| Baostock / Pytdx / Yahoo | 链内 fallback | — |
| free-stockdb | `free_stockdb_http.py` | `FREE_STOCKDB_*` |

自检：`GET /api/market/source-health`。链路说明：`backend/docs/KLINE_DATA_SOURCES.md`。

**Transaction Agent** 每次 `GET /api/transaction-agent/views/{symbol}` 拉取 K 线、资金流、基本面快照（优先缓存）。

## 股票列表与公司资料

| 能力 | 路由 / 存储 | 说明 |
|------|-------------|------|
| A 股检索库 | `POST /api/stocks/refresh` → `a_share_stocks.json` | 启动任务按 TTL 刷新；**搜索合并东财全表 + 上传 CSV**，东财优先覆盖简称 |
| 全市场 F10 缓存 | `company_basic_universe.json` | 管理员 `POST /api/admin/company-universe/refresh` |
| 股票列表 profiles | `watchlist` profiles 接口 | F10 + 新闻摘要 + RAG 向量化（完整 refresh） |
| 单标的详情 | `GET /api/watchlist/stock-detail` | 板块、快照、简介补全 |

## 新闻与事件（Agent 侧）

| 能力 | 服务 | 存储 |
|------|------|------|
| RSS + A 股快讯 + 36氪/虎嗅 | `news_feed_sync.py` | `news.db` |
| 东财个股公告/资讯 | `eastmoney_stock_news.py` | 并入 RSS 拉取与 `sync-archive-feeds` |
| 共用 HTTP 重试 | `news_fetch_http.py` | RSS/快讯/东财 F10 |
| 列表标的匹配 | `news_watchlist_matcher.py` | 写入 archive 时打标 |
| LLM 新闻解读 | 两阶段：相关性 → 潜在影响（`impact_analyzer.py` + `app/prompts/`） | `article_stock_analysis` |
| 启动同步 | `startup_data_check` → `sync_all_sources_to_archive` | — |

单页 UI 在 K 线与策略观点下方提供 **相关新闻解读**：`GET /api/news/archive`（`scope=analysis|pending`）、`POST /api/news/sync-archive-feeds`、`POST /api/news/analyze-symbol-archive`。

路由前缀：`/api/news/*`。

## LLM 与解读任务

| 能力 | 配置 | 说明 |
|------|------|------|
| 运行时模型 | `settings/llm`, `llm_runtime.json` | K 线解读、新闻分析、Research Agent |
| Kimi / OpenAI 兼容 | `KIMI_*`, `KLINE_ASSISTANT_*`, `OPENAI_*` | `config.py` 多级回退 |
| 收盘后 K 线批解读 | `kline_insight_scheduler` | 需 `ENABLE_SCHEDULER=true` 与 LLM Key |

路由：`/api/kline-learning/*`（UI 已移除，scheduler 仍可用）。

## 宏观 / 量化 / 信号（扩展 API）

以下模块 **无单页 UI**，后端保留供脚本或后续面板：

- `/api/macro/*` — Tushare 宏观、申万行业（`macro_tushare_service.py`）
- `/api/quant/*` — 回测与策略草案（`quant_backtest.py`）
- `/api/decision-signals/*` — 基于 Transaction Agent 输出的信号沉淀
- `/api/stock-picker-agents/*` — 自定义选股 Agent

## Research Agent（无 HTTP 路由）

`app/services/research_agent/` 提供完整研究流水线（基本面、估值、新闻、风险等），数据来自：

- `research_agent/data_bundle.py`（K 线、Yahoo 基本面、issuer RAG）
- `issuer_rag_service` + 本地上传 `issuer_uploads/`
- 静态入口目录 `app/data/research_sources.json`

调用方式：Python `run_research(...)` 或后续挂载 API。

## 雪球（可选）

`xueqiu_http.py` / `xueqiu_pipeline.py` 为可选补充源；配置 `XUEQIU_COOKIES` 或 integrations.json。当前 **无独立 HTTP 路由**（见 `backend/docs/XUEQIU_OPTIONAL.md`）。

## 启动预热（`ENABLE_STARTUP_DATA_CHECK`）

顺序见 `startup_data_check.py`：

1. 刷新 A 股列表（若过期）
2. 新闻源探测与 archive 同步
3. 预热上证指数 K 线
4. 轮转刷新股票列表内标的行情缓存
5. 刷新数据资产目录

手动触发：`POST /api/settings/data-center/refresh`（需管理员）。

多 worker 部署时仅在一个实例开启 `ENABLE_SCHEDULER` 与 `ENABLE_STARTUP_DATA_CHECK`。
