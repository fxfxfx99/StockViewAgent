# 集成配置优先级与资料缓存

## 雪球 `xueqiu_cookies`

雪球 **`GET /api/xueqiu/bundle`** 为**可选**补充数据（讨论/摘要等），非主流程；详见 [XUEQIU_OPTIONAL.md](./XUEQIU_OPTIONAL.md)。

生效顺序（**前者覆盖后者**）：

1. **`backend/data/integrations.json`** 中的 `xueqiu_cookies`（非空时唯一生效）
2. 环境变量 **`XUEQIU_COOKIES`**（`Settings.xueqiu_cookies`）

代码：`app/services/xueqiu_http.py` → `effective_xueqiu_cookies()`。

若在控制台将 Cookie 清空（空字符串），会**删除**文件中的键并**回退**到环境变量。

## Tushare Token

生效顺序：

1. **`integrations.json`** 中的 `tushare_token`（非空优先）
2. 环境变量 **`TUSHARE_TOKEN`**

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
