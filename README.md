# StockViewAgent

本地运行的 **A 股单股多策略走势观点 Agent**：选一只股票、一个时间节点，在同一页查看 K 线、多策略观点、相关新闻解读和公司信息。

默认 **不必登录**（`AUTH_REQUIRED=false`），适合克隆到本机自托管。所有结论仅供研究参考，**不构成投资建议**。

## 快速开始

需要 **Python 3.11–3.13** 与 **Node.js 20+**（不要用 Python 3.14）。

```bash
git clone https://github.com/fxfxfx99/StockViewAgent.git
cd StockViewAgent
./start.sh
```

打开 <http://127.0.0.1:5175/>，按提示进入 **配置台**（或 `/setup`）：

1. 大模型 API（OpenAI 兼容的 Key / Base / Model）
2. 行情接口：推荐 [KlineShare](https://data.klineshare.cn/docs)，或 Tushare / 仅公开源

更完整的步骤、端口、Docker 与排错见 **[docs/LOCAL_SETUP.md](docs/LOCAL_SETUP.md)**。

```bash
./scripts/check-env.sh   # 检查本机环境；未就绪时返回非零状态
./scripts/setup.sh       # 只安装依赖
./scripts/dev.sh         # 只启动
```

健康检查：<http://127.0.0.1:8001/api/health>  
OpenAPI：<http://127.0.0.1:8001/docs>

默认端口：后端 `8001`，前端 `5175`。占用时可 `BACKEND_PORT=8010 FRONTEND_PORT=5180 ./start.sh`。
`start.sh` 会按依赖文件检查是否需要安装，更新代码后也可直接运行；前后端都通过就绪检查才报告启动成功，按 `Ctrl+C` 同时停止两个服务。

## 界面做什么

单页纵向浏览，URL 深链 `?chart=600519.SS&as_of=2026-09-01`：

- 股票列表（可检索、导入）
- K 线
- 多策略观点（Transaction Agent）
- 相关新闻解读（更新新闻 / 补充分析）
- 公司信息

后端还提供量化、宏观、信号复盘等 API，供脚本与 Agent 调用，见 [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md)。

## 配置与密钥

优先在浏览器 **配置台** 填写（写入 `backend/data/`，已忽略出版本库）。也可以编辑 `backend/.env`（从 `backend/.env.example` 复制）。

公网部署请设置 `AUTH_REQUIRED=true`，并修改 JWT 密钥与初始密码。

浏览器默认只允许 localhost / loopback 来源。局域网或部署域名需设置 `CORS_ALLOWED_ORIGINS`，例如 `FRONTEND_HOST=0.0.0.0 CORS_ALLOWED_ORIGINS=http://192.168.1.20:5175 ./start.sh`（替换为本机实际 IP）；支持逗号分隔的多个完整来源，并将这些主机加入请求 Host 白名单。可信主机上的 CLI / Agent 无 `Origin` 请求保持可用，详见[本机配置](docs/LOCAL_SETUP.md)。股票列表刷新与重建需管理员权限，本地免登录模式仍使用默认管理员。

**不要提交** `backend/.env`、`frontend/.env`、`backend/data/`。`./scripts/setup.sh` 会生成随机 `AUTH_JWT_SECRET`。

## Docker

```bash
cp backend/.env.example backend/.env
docker compose up -d --build
```

入口默认 <http://127.0.0.1:8080/>。详见 [docs/LOCAL_SETUP.md](docs/LOCAL_SETUP.md#docker)。

## 开发

开发约定与贡献流程见 [CONTRIBUTING.md](CONTRIBUTING.md)。

```bash
(cd backend && .venv/bin/python -m pytest tests/ -q)
(cd frontend && npm test && npm run build)
```

## 许可

见 [LICENSE](LICENSE)。
