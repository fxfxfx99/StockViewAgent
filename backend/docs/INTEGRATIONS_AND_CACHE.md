# 集成配置优先级与资料缓存

## 请求账户与本地开放模式

行情、宏观和新闻接口均建立当前账户上下文，确保读取该账户配置的行情与 LLM 凭证。
`AUTH_REQUIRED=true` 时这些接口必须携带 Bearer Token；默认 `false` 时无需登录，自动使用本地默认账户。
新闻抓取、解读和摘要的默认标的范围为当前账户的股票列表；后台归档匹配仍使用所有账户的列表并集。
盘后自动解读逐账户读取凭证，仅处理已配置 LLM 且曾手动解读的标的；未配置的账户单独跳过。

各账户股票列表独立保存，公司资料缓存共享；移除标的只会清理所有账户都不再使用的公司资料。
F10 或 Tushare 资料刷新失败时保留上次成功数据与时间，并记录错误，避免临时断网清空资料。

## 雪球 `xueqiu_cookies`

雪球 **`GET /api/xueqiu/bundle`** 为**可选**补充数据（讨论/摘要等），非主流程；详见 [XUEQIU_OPTIONAL.md](./XUEQIU_OPTIONAL.md)。

默认无需手填 Cookie：实际抓取前，服务自动访问雪球首页建立匿名会话，访客 Cookie 只在进程内存缓存 15 分钟，到期后在下次使用时重新获取。自动获取失败或接口拒绝后冷却 5 分钟，再由后续采集请求触发重试。只读状态接口不触发会话获取，也不返回 Cookie 内容。

生效顺序（**前者覆盖后者**）：

1. **`backend/data/integrations.json`** 中的 `xueqiu_cookies`（非空时唯一生效）
2. 环境变量 **`XUEQIU_COOKIES`**（`Settings.xueqiu_cookies`）
3. 以上均为空时，使用自动获取的匿名会话

代码：`app/services/xueqiu_http.py`；`effective_xueqiu_cookies()` 读取显式配置，实际请求负责获取或复用自动会话。

若在控制台将 Cookie 清空（空字符串），会**删除**文件中的键并**回退**到环境变量；环境变量也为空时恢复自动模式。手动 Cookie 不会被自动匿名会话覆盖。

匿名会话不能代替账号登录，也不能保证访问需要登录的接口。若雪球要求登录或验证码，应人工完成后在管理员设置提供有效 Cookie。手动配置的登录 Cookie 过期后仍需人工更新；自动续取只适用于访客 Cookie。

评论精选只要求当前账户已配置大模型凭据即可入队，不再以是否保存 Cookie 为门槛；会话获取在后台抓取时执行，实际失败状态会反馈到评论面板。

## Tushare Token

生效顺序：

1. **当前账户配置**中的 `tushare_token`（非空优先）
2. **`integrations.json`** 中的 `tushare_token`
3. 环境变量 **`TUSHARE_TOKEN`**

代码：`Settings.effective_tushare_token`。

## 公司基本信息（全市场缓存）

- **变动信息**（股价涨跌摘要、近 20 日 K 等）：`watchlist_price_context`、行情接口，按请求/日度刷新。
- **月度/财务补充**：`profile_store` 下 `tushare` 块，由 Tushare 任务写入。
- **静态基本信息**（全称、主营业务、行业、简介等）：东方财富 F10，缓存在 `backend/data/company_basic_universe.json`（见 `universe_company_store`），并在 `watchlist` 资料接口中作为 **F10 未拉取时的回退**，减少空白与重复请求。

批量填充：管理员 `POST /api/admin/company-universe/refresh`（见 OpenAPI）。

### 推荐流程（先列表、再分批 F10）

1. **生成本地 A 股列表**（无需登录）  
   `POST /api/stocks/refresh`  
   成功后会写入 `backend/data/a_share_stocks.json`，响应里含 `count`。

2. **分批写入全市场 F10 缓存**（需**管理员** JWT）  
   `POST /api/admin/company-universe/refresh`  
   - 通用：`{"offset": 0, "limit": 80, "skip_fresh_days": 60}`，然后 `offset` 每次加 `limit` 直到覆盖全部 `count`。  
   - 少量常看标的：`{"symbols": ["600519.SS", "000001.SZ"], "skip_fresh_days": 60}`  

3. **一键脚本**（在项目 `backend` 下，需已 `pip install httpx` 与管理员 Token）：  
   ```bash
   export STOCKVIEW_ADMIN_TOKEN='你的JWT'
   python scripts/company_universe_refresh_all.py
   ```  
   仅刷新部分代码：  
   `python scripts/company_universe_refresh_all.py --symbols 600519.SS,000001.SZ`  
   已拉过列表、只跑 F10：`python scripts/company_universe_refresh_all.py --skip-stocks-refresh`

查看缓存体量：`GET /api/stocks/company-universe/status`（需登录）。
