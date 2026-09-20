# A 股 K 线数据源备案

本项目 **不保证** 第三方接口 SLA；以下说明仅作合规与排障参考，数据用于学习研究。

## 调用链（由 `kline_pipeline.fetch_a_share_kline_with_fallbacks` 统一调度）

### 日 / 周 / 月（`interval` ∈ `1d`,`5d`,`1wk`,`1mo`）

| 顺序 | `kline_source` 标识 | 类型 | 说明 |
|------|---------------------|------|------|
| 1 | `eastmoney` | HTTP | 东方财富 `push2his` K 线 + `push2` 快照（主源） |
| 2 | `tencent_fallback` | HTTP | 腾讯 `web.ifzq.gtimg.cn` 前复权日/周/月 |
| 3 | `free_stockdb_local` | 本地 HTTP | 可选 free-stockdb 本地服务（需 `FREE_STOCKDB_ENABLED=true` 且服务可达；当前接入日 K） |
| 4 | `baostock_fallback` | SDK | [Baostock](http://www.baostock.com) `query_history_k_data_plus`（需 `pip install baostock`） |
| 5 | `sina_fallback` | HTTP | 新浪 `getKLineData` |
| 6 | `pytdx_fallback` | SDK | [Pytdx](https://pytdx-docs.readthedocs.io/zh-cn/latest/) 通达信行情服务器（需 `pip install pytdx`；**北交所部分代码可能无数据**） |

### 分钟线（`1m` … `1h`）

| 顺序 | 标识 | 说明 |
|------|------|------|
| 1 | `eastmoney` | 东财分钟 K |
| 2 | `tencent_mkline_fallback` | 腾讯 `ifzq` mkline |
| 3 | `baostock_minute_fallback` | Baostock 仅支持 **5 / 15 / 30 / 60** 分钟；`1m`/`2m` 会跳过 |
| 4 | `pytdx_minute_fallback` | Pytdx 分钟类 K 线（单次最多约 800 根） |

## Pytdx 与官方 API 对齐（`kline_pytdx.py`）

- 行情接口说明：<https://pytdx-docs.readthedocs.io/zh-cn/latest/pytdx_hq/>
- **服务器列表**：在少量固定节点之后，按 `from pytdx.config.hosts import hq_hosts` 追加官方维护列表，并对单次请求**最多尝试 18 个** `(ip, port)`，避免全表扫描超时。
- **连接**：`TdxHq_API(auto_retry=True)`，利用库内断线重连；成功连接后用文档推荐的 `with api:`（在 `connect` 成功后）确保 `disconnect`。
- **日 K**：文档示例使用 `category=9`（日 K）；实现上 **优先 9（`KLINE_TYPE_RI_K`），无数据再试 4（`KLINE_TYPE_DAILY`）**；周/月仍为 5、6。
- **1 分钟**：文档区分 7 与 8；**优先 8（`KLINE_TYPE_1MIN`），再试 7（`KLINE_TYPE_EXHQ_1MIN`）**。
- **市场号**：使用 `TDXParams.MARKET_SH` / `MARKET_SZ`，与文档「0 深圳、1 上海」一致。

## 外部参考链接

- Pytdx 文档：<https://pytdx-docs.readthedocs.io/zh-cn/latest/>
- Baostock 数据说明（指数等）：<http://www.baostock.com/mainContent?file=indexData.md>

## 可选 free-stockdb 本地源

`free-stockdb-main` 当前发布物是 Windows `stockdb.exe` / `.pyd`，macOS 无法原生启动。本项目只把它作为可选 HTTP 源接入：在 Windows 机器、Windows 虚拟机或兼容层中启动 `stockdb.exe` 后，配置：

```bash
FREE_STOCKDB_ENABLED=true
FREE_STOCKDB_BASE_URL=http://127.0.0.1:7899
```

如果服务不可达，系统会在 `/api/market/source-health` 标记 `free_stockdb_local` 失败，并自动跳过到后续 Baostock / 新浪 / Pytdx，不会阻断行情功能。

## 与「股票详情」接口的关系

- `GET /api/watchlist/stock-detail` 提供股票详情；列表区迷你 K、长 K 线均通过 **`GET /api/market/kline`**（上表调用链）获取。
- 详情接口使用东财 **push2**（扩展快照）、**slist**（板块）及本地 `watchlist_profiles`，与主 K 线互为补充。

## 依赖

`requirements.txt` 中包含 `baostock`、`pytdx`。若未安装，对应步骤会自动跳过，不影响主源与其它 HTTP 兜底。free-stockdb 不写入 Python 依赖，需外部单独运行本地 HTTP 服务。

## 响应字段

- `kline_source`：实际命中的源（见上表）。
- `metrics.fallback_note`：前置源失败时的简要错误摘要（截断存储）。

## 行情时间口径

A 股来源的无时区日期与分钟时间统一按 `Asia/Shanghai` 转为 Unix 时间戳；已有 Unix 时间戳保持不变。
缓存的行情日期、K 线解读摘要及历史节点均按上海交易日展示/截断，不依赖服务主机的 `TZ`。
