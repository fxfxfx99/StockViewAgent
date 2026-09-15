# 雪球接口（可选补充数据源）

## 定位

- **不是**主行情源：A 股 K 线、指标区以东财链路 + 本地缓存为主（见 `GET /api/market/kline`）。
- **可选**：在社区讨论、第二路行情摘要、人工交叉验证时使用。
- **主界面**：默认**不展示**雪球区块；接口仍保留，供脚本、Agent、自建工具调用。

## 接口

- `GET /api/xueqiu/company?symbol=600519.SS` — 公司信息栏：F10 简介、最近大事件、个股新闻。
- `GET /api/xueqiu/bundle?symbol=600519.SS` — 行情片段、讨论节选等（非主 K 线）。

实现见 `app/services/xueqiu_pipeline.py`（`run_company_bundle` / `run_bundle`）。

## 配置与优先级

见 [INTEGRATIONS_AND_CACHE.md](./INTEGRATIONS_AND_CACHE.md) 中「雪球 `xueqiu_cookies`」：`integrations.json` 非空优先于 `XUEQIU_COOKIES`。

## 使用注意

- 未配置 Cookie 时常见 **400016**，属预期；非系统故障。
- 请遵守雪球服务条款，控制频率（后端 `xueqiu_http` 已限频与重试）。
- Cookie 会过期，需不定期从浏览器更新。
