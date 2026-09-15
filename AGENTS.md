# 开发约定

本文件说明本仓库的默认开发方式。

## 硬规则

- **产品定位**：单股 × 时间节点 × 多策略走势/观点 Agent；前端单页纵向浏览（含相关新闻解读）。
- **产品用语**：界面与对外文案统一为 **股票列表**、**市场研判**；API 路径仍用 `watchlist` 等与数据文件名一致。
- **目录**：后端逻辑在 `backend/app/`（`routers/`、`services/`、`storage/`、`middlewares/`）；前端在 `frontend/src/`。
- **前端**：`@tanstack/react-query` 管服务端状态；`react-router-dom` 的 `BrowserRouter` + 查询参数深链（`hooks/useAppUrlState.js`：`chart` / `as_of`）；新增面板时尽量复用 queryKey（`hooks/queryKeys.js`）避免重复拉数。
- **默认开放本地**：`AUTH_REQUIRED=false` 时无 Token 也可调用需用户上下文的接口；不写死密钥；敏感配置用 `.env` 与 `backend/data/*.json`（已 gitignore 的勿提交）。
- **优先复用**现有模块与配置入口；非任务必需不做大范围重构。后端扩展 API（新闻、量化、宏观等）仍保留供 Agent 与数据管线使用，见 `docs/DATA_SOURCES.md`。
- **新增环境变量**时同步更新 `backend/.env.example`。
- **行为或 API 变更**时更新相关注释/文档；大功能变更可考虑更新 `README.md`。
- **注释与日志**：清晰为准，中英文与文件语境一致即可。

## 常用命令

```bash
./start.sh                 # 缺环境则安装，然后启动
./scripts/check-env.sh     # 检查 Python / Node
./scripts/setup.sh         # 仅初始化 venv / npm / .env
./scripts/dev.sh           # 仅启动

cd backend && .venv/bin/python -m pytest tests/ -q
cd frontend && npm run build
```

本机配置说明见 `docs/LOCAL_SETUP.md`。
