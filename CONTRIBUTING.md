# 参与贡献

感谢你对 StockViewAgent 的兴趣。

## 本地开发

按 [docs/LOCAL_SETUP.md](docs/LOCAL_SETUP.md) 安装依赖并运行 `./start.sh`。

```bash
(cd backend && .venv/bin/python -m pytest tests/ -q)
(cd frontend && npm test && npm run build)
```

## 开发约定

- 后端位于 `backend/app/`，按 `routers/`、`services/`、`storage/`、`middlewares/` 分层；前端位于 `frontend/src/`。
- 前端使用 `@tanstack/react-query` 管理服务端状态，复用 `hooks/queryKeys.js` 中的查询键，避免重复拉取数据。
- 路由使用 `react-router-dom` 的 `BrowserRouter`，通过 `hooks/useAppUrlState.js` 维护 `chart` / `as_of` 查询参数深链。
- 复用项目已有模块与配置入口；行为或 API 变更同步更新相关注释、文档，新增环境变量同步更新 `backend/.env.example`。
- 密钥与本机配置写入 `.env` 或 `backend/data/`，不写入代码；已被 `.gitignore` 排除的敏感文件不提交。

## 提交前请确认

- 没有把 `backend/.env`、`frontend/.env`、`backend/data/` 或密钥加进提交
- 单页能按同一只股票查看 K 线、多策略观点、新闻解读
- 对外文案使用「股票列表」「市场研判」

欢迎通过 Issue / Pull Request 讨论功能与缺陷。分析结果不构成投资建议。
