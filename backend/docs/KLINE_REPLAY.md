# K 线复盘数据

`GET /api/kline-replay/{symbol}?start_date=2025-01-01&end_date=2025-12-31`

这是登录用户的只读日线接口，用于逐根揭示历史价格的练习页。支持六位代码及 `.SS/.SH/.SZ/.BJ`，返回规范代码；默认近两年，单次最多 10 年。日期按上海时区处理，当天 16:30 前不使用当天未完成日线。`refresh=true` 可重新拉取。

响应包括 `candles`、`source`、`price_basis`、`volume_unit`、实际覆盖起止日期、`warnings` 与 `metric_definitions`。每根包含 OHLC、成交量、成交额、换手率、涨跌幅、振幅、五日量比、总市值、TTM 盈利、PE(TTM)。`metrics` 为指标提供 `value/unit/status/source/note`；状态为 `historical`、`calculated`、`estimated` 或 `missing`。

价格来自已配置的 Tushare daily、东方财富历史日线、已启用的 Baostock，按该顺序回退。全部显式请求**不复权**价格，成交量统一为股，成交额统一为元。没有取数成功时返回清晰错误，不注入演示数据。除权除息可能留下价格跳空。

市值与 PE 使用 Tushare `daily_basic` 对应日期数据；Baostock 还能返回历史 `peTTM`。盈利优先使用 Tushare `income` 的合并报表归母净利润：年报直接作为 TTM；季报 TTM 为本年累计 + 上年全年 − 上年同期累计。仅选择公告日严格早于回放日期的记录；实际公告日 `f_ann_date` 与名义公告日取较晚值，后发修订不反写此前回放日期。缺少报告组成期时不拼接不完整 TTM。

历史市值及 PE 均可用、财报 TTM 不可用时，盈利允许按市值 / PE 估算，并明确标注 `estimated`。无历史事实的指标保持 `null`。当前行情快照、当前股本、本地缺少已验证公告时间的 CSV 财报均不会回填到历史。数据源自身可能修订历史，因此本接口不声称具有经过审计的完整历史版本。

五日量比为当前日成交量 / 前五根完整日线均量，不包含当前日；没有五根数据或分母为零时留空。这不是盘中量比。请求开始日前额外取 60 个自然日用于预热，不作为返回的回放区间。

每个用户、代码、日期区间使用独立缓存，默认 24 小时内复用；在线源失败后可回退到上次同区间数据并提示。最多三个历史价格源各等待 24 秒，财务数据等待 25 秒。付费凭证沿用请求用户上下文，不写入响应。该路由不在公开试用代理 allowlist 内，仅本地/完整部署支持。

单位与字段依据：[Tushare 日线](https://tushare.pro/document/2?doc_id=27)、[每日指标](https://tushare.pro/document/2?doc_id=32)、[利润表](https://tushare.pro/document/2?doc_id=33)。十年单一股票通常少于 3,000 根日线、加 TTM 预热约 50 份合并季度报告；若报告返回达到 100 条会提示可能存在接口覆盖限制，不将缺失期间强行补齐。

验证：`cd backend && .venv/bin/python -m pytest tests/test_kline_replay.py -q`。测试覆盖历史信息边界、TTM 算术、前五日量比、单位、缺失值、无效 K 线、登录及日期校验、用户隔离缓存。
