# 海外新闻聚合

顶部 **工具 → 海外快讯**（`/news`）提供跨股票的海外财经新闻列表。默认随 API 后端启动持续采集，不需要先打开页面、添加自选股或配置大模型。停止 API 服务、电脑关机或休眠后不会继续抓取；恢复运行后按已保存的来源状态续跑。

## 使用

1. 启动项目：`./start.sh`。
2. 打开 **海外快讯**，按来源或关键词筛选，点击标题访问原文。
3. 展开来源状态，检查实际入口、最后成功时间、下一次重试及错误原因。来源暂时不可用时保留上次抓取的新闻。

后端来源轮询默认间隔 120 秒，调度循环最多每 30 秒检查是否到期；前端每 15 秒读取本地归档。页面刷新、分页与显示条数不会改变后台采集频率。发布时间来自源数据，本地采集时间单独保存；未提供发布时间的条目不会被伪造成刚发布的消息。首次启动且来源不可达时显示空状态，不填充演示新闻。

## 来源与时效

聚合 Reuters、Bloomberg、Benzinga、WSJ、FT 和 CNBC 的公开 RSS、公开新闻索引或已配置的授权接口。页面显示每个来源当前使用的传输方式和入口。Google News 新闻索引属于第三方索引，会有抓取、收录和刷新延迟，不能作为媒体终端直连。来源返回较旧内容时，即便请求成功，也应结合条目的发布时间判断时效。

公开模式保存标题、短摘要及原文链接，不下载付费文章全文。Reuters Connect、Bloomberg 终端及 Dow Jones 等商业实时资讯需要对应授权；本实现没有将公开 RSS 标为这些终端的实时数据。Benzinga 支持已授权的 News API，未配置 Key 时使用公开入口；是否覆盖某个频道取决于订阅权限。

本次核验的公开入口：

| 来源 | 入口 |
| --- | --- |
| Reuters | Google News 中 Reuters 域名的近七日新闻索引，非官方直连 |
| Bloomberg | [Markets RSS](https://www.bloomberg.com/feeds/markets/news.rss) |
| Benzinga | [公开 RSS](https://www.benzinga.com/feed)；[授权 News API 文档](https://docs.benzinga.com/api-reference/news-api/get-news-items) |
| WSJ | [Dow Jones Markets RSS](https://feeds.content.dowjones.io/public/rss/RSSMarketsMain) |
| FT | [Markets RSS](https://www.ft.com/markets?format=rss) |
| CNBC | [Top News RSS](https://www.cnbc.com/id/100003114/device/rss/rss.html) |

WSJ 旧 `feeds.a.dj.com` 地址曾返回正常 XML 但内容停留在 2025 年，已移出默认链。公开源的 TTL、缓存与更新频率由媒体决定；轮询更快不能保证更快收到新消息。商业接入范围分别见 [Reuters Connect](https://reutersagency.com/content-delivery-platforms/reuters-connect/) 与 [Bloomberg News Feed](https://www.bloomberg.com/distribution/products/news/)。

## 配置

在 **工具 → 数据中心 → 自动采集配置** 中，`overseas_enabled` 和 `overseas_poll_interval_sec` 控制海外自动采集开关与间隔，保存后在后续循环生效，无需重新打开新闻页面。暂停后保留循环，重新开启可继续执行。海外循环独立于中央来源队列的 `enabled` 开关；页面显示设置也不承担后台采集设置的作用。

以下环境配置控制服务是否启动、首次运行的间隔、请求超时与授权连接；修改环境后须重启 API 服务。已持久保存的页面配置优先决定运行时轮询间隔。

```dotenv
OVERSEAS_NEWS_ENABLED=true
OVERSEAS_NEWS_POLL_INTERVAL_SEC=120
OVERSEAS_NEWS_TIMEOUT_SEC=15
OVERSEAS_NEWS_BENZINGA_API_KEY=
# 可选：使用你配置的 HTTP(S) 代理服务
# OVERSEAS_NEWS_PROXY_URLS=http://127.0.0.1:7890,http://127.0.0.1:7891
```

运行时轮询间隔允许 60–3600 秒，环境间隔低于 60 秒时初始配置按 60 秒使用；单请求超时允许 1–60 秒。来源的限流和错误退避可能使实际间隔更长。海外采集独立于 `ENABLE_SCHEDULER`、`ENABLE_DATA_COLLECTION` 和研究自动运行开关；设置 `OVERSEAS_NEWS_ENABLED=false` 会关闭服务启动，即使页面开关为开启也不会采集，已保存新闻仍可阅读。页面关闭海外开关时手动刷新也返回停用状态。

海外采集读取数据中心的资源设置：默认 `max_concurrent_jobs=1`（1–3）、`max_memory_mb=768` MiB、`min_free_disk_mb=1024` MiB。每轮及每个来源开始前检查当前进程内存和剩余磁盘；高内存或低磁盘时跳过新请求，资源恢复后继续。内存采样是可恢复的软准入，不会强制杀掉当前请求，也不是进程内存硬上限。此并发值分别约束海外和中央采集器，不能相加理解为整个 API 的统一并发上限。海外循环不占用中央队列的 `daily_job_limit`。

Benzinga API 首次连接读取最新 100 条，之后按更新时间增量分页，每轮最多处理 500 条，未完成的分页游标持久保存后续跑。这不是完整历史回填；公开 RSS 则只能累积源当前返回的有限条目。

代理只用于网络连接故障后的线路切换。每轮重新建立 HTTP 连接，使后续请求重新解析域名，不固定上游服务器 IP。程序不能凭空生成代理或自行更换网络运营商分配的公网 IP；真正的出口 IP 由所配置的网络或代理服务决定。拒绝访问、需要授权及限流响应会进入冷却，不通过轮换代理重试绕过。

## 后台、存储和接口

API 生命周期启动独立异步采集循环，SQLite 租约协调同一数据目录的多个 worker。每个来源维护自己的成功时间、条件请求标记和失败退避；单个来源失败不会清空其历史，也不会阻塞其他来源的成功结果。允许的媒体域名内检查重定向和 RSS 发现，验证后的可用地址进入后续采集；不能保证外站改版后始终能自动发现新接口。来源返回旧条目时保留发布时间并标明内容陈旧，不把 HTTP 200 当作实时数据证明。

`overseas_news.db` 保存新闻、来源状态及租约，并将抓取内容同步到已有 `news.db`，供原有个股新闻关联和解读读取。数据库属于长期保留的数据资产，随 `STOCKVIEW_DATA_DIR` 整体备份和搬迁，不进入普通缓存回收。关闭自动运行或修改刷新间隔不会删除数据。

| 接口 | 用途 |
| --- | --- |
| `GET /api/news/overseas` | 读取本地归档；支持 `source`、`query`、`limit`、`offset` |
| `GET /api/news/overseas/status` | 读取服务开关、自动开关、运行状态、资源原因及各来源状态 |
| `POST /api/news/overseas/refresh` | 管理员手动触发采集，仍遵守开关、资源检查、租约和来源限流冷却 |

开启登录后所有接口要求身份验证，手动采集要求管理员。只读接口不会访问外部新闻站点；浏览器刷新频率不会放大来源请求量。API Key 和代理认证信息不返回前端。

状态中的 `service_enabled` 表示环境允许启动，`automatic_enabled` 表示运行配置开关，`collecting` 表示当前持有采集租约，`running` 还包含最近存活的调度循环。循环存活不表示正在请求来源：应同时检查 `reason`（如 `high_memory`、`low_disk_space`、`automatic_disabled`）和 `resources`。来源行保留 `last_success_at`、`next_poll_at`、`newest_published_at` 与 `content_stale`；归档同步失败通过 `archive_pending` 和 `archive_sync_error` 单独说明，已下载新闻先保留并在后续重试写入共享新闻库。

中央采集状态可读 `GET /api/data-center/collection/status`，文件容量汇总按需读 `GET /api/data-center/inventory/summary`；两者要求管理员，不应以重复全量目录扫描替代新闻页面轮询。通用 RSS 的小批归档与下载限制详见 [数据中心](DATA_CENTER.md#新闻采集的内存边界)，该通用抓取路径与此处海外专用采集循环各自执行、各自控制并发。

此页面属于本地／自托管功能；现有公共试用网关不会公开海外新闻接口或已配置的授权数据。
