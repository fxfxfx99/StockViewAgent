# 雪球接口（可选补充数据源）

## 定位

- **不是**主行情源：A 股 K 线、指标区以东财链路 + 本地缓存为主（见 `GET /api/market/kline`）。
- **可选**：在社区讨论、第二路行情摘要、人工交叉验证时使用。
- **主界面**：公司资料使用雪球与公开来源补充；讨论、第二路行情摘要接口供脚本、Agent、自建工具调用。

## 接口

- `GET /api/xueqiu/company?symbol=600519.SS` — 公司信息栏：F10 简介、最近大事件、个股新闻；雪球不可用时补充公开公司资料与本地新闻归档，并标明实际来源。
- `GET /api/xueqiu/company?symbol=600519.SS&force=true` — 手动刷新；同一标的最短刷新间隔为 30 秒，修改 Cookie 后立即使缓存失效。
- `GET /api/xueqiu/bundle?symbol=600519.SS` — 行情片段、讨论节选等（非主 K 线）。

实现见 `app/services/xueqiu_pipeline.py`（`run_company_bundle` / `run_bundle`）与 `app/services/company_updates.py`。

## 自动更新与缓存

- 服务运行期间，默认每 15 分钟更新所有账户股票列表的标的并集；后台串行获取，同标的并发请求合并。页面可见时每 60 秒检查更新，已有结果继续展示。
- `COMPANY_AUTO_REFRESH_ENABLED=true` 开启后台更新，`COMPANY_REFRESH_INTERVAL_SEC=900` 设置刷新间隔（秒，最短 60 秒）。关闭后台更新后，页面请求仍按缓存到期时间获取数据。
- 缓存保存在 `backend/data/company_updates/`，重启后继续使用；来源暂不可用时保留各部分最后成功的数据，成功返回空列表则清除旧列表。
- `sources` 标明各部分来源，`section_fetched_at` / `fetched_at` 为真实采集时间（Unix 秒；未知为 `null`），`stale_fields` 标明沿用旧资料的部分。`last_attempt_at` 表示本次尝试时间，`next_refresh_at` 表示下次到期时间，重试不会把旧资料标成刚更新。
- 缓存仅存 Cookie 的哈希指纹以识别配置变更，不保存 Cookie 内容；此后台更新不会调用大模型。

## 配置与优先级

见 [INTEGRATIONS_AND_CACHE.md](./INTEGRATIONS_AND_CACHE.md) 中「雪球 `xueqiu_cookies`」：`integrations.json` 非空优先于 `XUEQIU_COOKIES`。

## 使用注意

- 雪球登录态缺失或过期时可能返回 **400016**；公司栏仍可展示可用的公开来源与最近成功结果，雪球专属讨论及事件可能暂不可用。
- 请遵守雪球服务条款，控制频率（后端 `xueqiu_http` 已限频与重试）。
- 自动更新指数据刷新；Cookie 过期后仍需在浏览器登录雪球，再到管理员设置手动更新 Cookie，服务不会代替用户登录或自动续期账号凭证。
