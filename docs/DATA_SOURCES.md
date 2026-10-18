# 数据源与 Agent 数据管线

本机安装与配置台步骤见 [LOCAL_SETUP.md](./LOCAL_SETUP.md)。

本文档说明后端数据能力，供 Transaction Agent、研究实验室、基本面 Research Agent、启动预热与脚本调用。

前端单页调用部分 API（见 `frontend/src/api.js`）；完整路由可通过 OpenAPI / 脚本访问。

首次启动只需选择大模型供应商并保存 API Key；免费公开行情默认使用。KlineShare、Tushare 等账号是高级可选项，不阻断首次配置。新账户由本地自动运行流程准备 5 只示例自选，已有或主动清空的列表保持不变。

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

### 来源自动维护

`source_autopilot.py` 将健康状态保存在研究账本，按**账户与日线/分钟能力**隔离。请求成功还须通过 OHLC、成交量、时间顺序与数值检查；最近 24 小时验证成功的内置来源优先。连续 3 次失败后进入从 5 分钟起的递增冷却，期间跳过，期满重新探测；成功后恢复。维护任务还轮转复测少量公开行情源，不替用户添加付费凭据或启用未知提供商。

新闻发现仅访问内置信任域名：人民网、中新网、新浪及金融界等站点，从页面的 RSS/Atom 声明寻找候选。请求与重定向均校验域名和公网地址，解析结果至少有两个有效条目及近期发布时间才加入；每日复核，连续 3 次失败后从活动清单停用。公开新闻定义跨账户共享，定义版本、证据哈希和路由/新增/停用事件保留在 `research.sqlite3`。

首页 `GET /api/autopilot/status` 提供来源健康摘要，`POST /api/autopilot/reconcile` 协调账户任务。该流程只维护内置适配器的调用顺序和经验证的来源定义，不生成执行任意爬虫代码、不自动安装适配器，也不执行 `git pull`。自动选择“健康”来源只说明接口数据可读，仍不能代替历史时点和严格回测质量检查。

**Transaction Agent** 每次 `GET /api/transaction-agent/views/{symbol}` 拉取 K 线、资金流、基本面快照（优先缓存）。

传入 `as_of=YYYY-MM-DD` 时按上海时区截取当日及之前的 K 线、个股和市场资金序列，`data_as_of` 返回实际最后一根 K 线日期。历史节点不使用当前估值、财务或经营讨论快照：报告期不能证明当时已公告，缺少可靠发布时间和可得时间的记录不能作为时点因子。缺失项会在策略详情的数据限制中说明；当前前复权因子与策略规则仍可能影响结果，因此不等同于严格的时点回测。

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

新闻同步与补充分析是长请求，耗时取决于新闻源和模型；前端超时均为 600 秒，开发代理超时为 900 秒。UI 每次补充分析最多处理 3 条，完成后可再次点击继续；前端 API 包装的默认批次同为 3 条，也支持显式指定数量。

路由前缀：`/api/news/*`。

### 新闻与财务修订留痕

`news.db` 的 `article_content_revisions` 按内容哈希保存初稿及内容变化，含正文、来源、发布时间、首次观测时间；原 `articles` 继续提供最新视图。Python `news_store.list_content_revisions(article_id, observed_before=...)` 可审阅本地观测历史。抓取时间变化或重复导入不会制造相同内容的新版本，原有模型解读历史单独保留。

`company_fundamentals.db` 的 `company_record_revisions` 保存基础资料、财务指标、经营讨论的完整原始行、内容与行哈希、来源文件、观测时间及源数据明确给出的公告/可用时间。Python `company_fundamentals_store.list_revisions(symbol, dataset=..., observed_before=...)` 支持按本地观测时间检查版本；最新查询接口保持不变。经营讨论正文也随版本保存。

缺少发布时间时留空，不把 `EndDate` / `Accper` 或导入时间当成公告时间；只有公告日期、没有时区与具体时间时也不补造可用时刻。旧数据库升级时保留当时仍存在的最新行，不能恢复升级前已被覆盖的内容。这些修订数据标记为解释和审计用途，不自动加入严格量价回测或宣称历史可得性已验证。

## LLM 与解读任务

| 能力 | 配置 | 说明 |
|------|------|------|
| 运行时模型 | `settings/llm`, `llm_runtime.json` | K 线解读、新闻分析、Research Agent |
| Kimi / OpenAI 兼容 | `KIMI_*`, `KLINE_ASSISTANT_*`, `OPENAI_*` | `config.py` 多级回退 |
| 收盘后 K 线批解读 | `kline_insight_scheduler` | 需 `ENABLE_SCHEDULER=true` 与 LLM Key |

路由：`/api/kline-learning/*`，供脚本与后台调度使用。

## 宏观 / 量化 / 信号（扩展 API）

以下后端 API 供脚本与 Agent 调用，单页不提供对应面板：

- `/api/macro/*` — Tushare 宏观、申万行业（`macro_tushare_service.py`）
- `/api/quant/*` — 回测与策略草案（`quant_backtest.py`）
- `/api/decision-signals/*` — 基于 Transaction Agent 输出的信号沉淀
- `/api/stock-picker-agents/*` — 自定义选股 Agent

## 研究实验室：持久行情与验证证据

前端 `/research` 与 `/api/research/*` 使用独立研究 worker。`research_data.py` 复用行情管线和图表缓存，将原始 JSON、带来源与时点的 Parquet、校验 manifest 保存为不可变批次；`research_store.py` 以 SQLite WAL 保存账户隔离的策略、实验、快照、任务和预算。旧快照保留原引用，新修订只影响新快照。

首次按 5 年日线采集；已有足够覆盖时转为近期增量，覆盖不足继续补采。离线缓存会标记陈旧，来源、复权口径或成交量单位变化会隔离分支。Baostock `adjustflag=3` 为不复权，成交量按股保存；不能与前复权数据无标记拼接。

当前行情提供者返回 OHLCV，并不自动证明完整的历史停牌/ST、涨跌停、证券存续、公司行动及信息可得时间。因此自动行情快照通常用于探索，不直接取得严格晋升资格。可信历史数据可经 `POST /api/research/import` 导入；完整字段、格式样例、覆盖检查及税前分红模拟限制见 [研究实验室说明](RESEARCH_LAB.md#严格数据与可信导入)。

GitHub 学习入口固定在 `research_generation.py`，读取 Qlib、RD-Agent、Freqtrade、vn.py 的指定公开文档，保留 commit、内容哈希和知识卡。资料只用于提出待验证假设，不运行远程代码，也不把仓库宣传或示例收益作为本地实验结论。

资产目录会扫描研究 SQLite 与所有不可变批次，但 JSON 写入前备份不是完整研究备份；恢复必须同时保留数据库、原始 JSON、Parquet 和 manifest，见 [备份与恢复](RESEARCH_LAB.md#备份与恢复)。

## 基本面 Research Agent（Python 接口）

`app/services/research_agent/` 提供基本面、估值、新闻、风险等研究流水线，与上述策略实验室是两个入口，数据来自：

- `research_agent/data_bundle.py`（K 线、Yahoo 基本面、issuer RAG）
- `issuer_rag_service` + 本地上传 `issuer_uploads/`
- 静态入口目录 `app/data/research_sources.json`

调用方式：Python `run_research(...)`。

## 雪球（可选）

`xueqiu_http.py` / `xueqiu_pipeline.py` 为可选补充源，提供 `GET /api/xueqiu/company` 与 `GET /api/xueqiu/bundle`。公司接口在雪球不可用时补充公开公司资料与本地新闻归档，并标明实际来源。默认自动访问雪球首页建立匿名会话，访客 Cookie 在内存缓存 15 分钟，到期后下次使用时重新获取；自动获取失败或接口拒绝后冷却 5 分钟。状态接口只展示会话状态，不返回 Cookie。匿名会话不能代替账号登录：若雪球要求登录或验证码，需要人工完成后通过管理员设置、`integrations.json` 的 `xueqiu_cookies` 或 `XUEQIU_COOKIES` 提供有效 Cookie。显式配置优先，登录凭据过期后仍需人工更新。

`company_updates.py` 在服务运行期间默认每 15 分钟刷新所有账户股票列表的标的并集；可见页面每 60 秒检查更新。配置为 `COMPANY_AUTO_REFRESH_ENABLED=true`、`COMPANY_REFRESH_INTERVAL_SEC=900`。公司资料持久缓存于 `backend/data/company_updates/`，失败时保留各部分上次成功内容与实际采集时间，并通过 `stale_fields` 标记旧资料。细节见 `backend/docs/XUEQIU_OPTIONAL.md`。

**雪球评论精选**位于新闻解读之后。`GET /api/xueqiu/comments` 读取账户隔离的快照，`POST /api/xueqiu/comments/refresh` 将最近 200 条或昨日讨论加入后台队列；`xueqiu_comments_pipeline` 负责限频采集、去重与逐批 LLM 筛选。自选股每日北京时间 09:00 更新前一天内容，受 `ENABLE_SCHEDULER` 与 `XUEQIU_COMMENTS_AUTO_REFRESH_ENABLED` 控制。结果与队列保存在 `xueqiu_comments.db`，错误时保留上次成功精选并显式标记。当前账户配置好大模型凭据即可入队，雪球会话在实际抓取时自动获取，无需先手填 Cookie。完整规则见 [雪球评论精选](../backend/docs/XUEQIU_OPTIONAL.md#雪球评论精选)。

## 启动预热（`ENABLE_STARTUP_DATA_CHECK`）

顺序见 `startup_data_check.py`：

1. 刷新 A 股列表（若过期）
2. 新闻源探测与 archive 同步
3. 预热上证指数 K 线
4. 轮转刷新股票列表内标的行情缓存
5. 刷新数据资产目录

手动触发：`POST /api/settings/data-center/refresh`（需管理员）。

多 worker 部署时仅在一个实例开启 `ENABLE_SCHEDULER` 与 `ENABLE_STARTUP_DATA_CHECK`。
