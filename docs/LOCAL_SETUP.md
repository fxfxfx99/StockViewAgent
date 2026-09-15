# 本机配置与启动

从 GitHub 克隆后，用这一份即可在本机跑起来。密钥不要写进仓库。

## 需要提前安装

| 依赖 | 版本 | 说明 |
|------|------|------|
| Python | **3.11、3.12 或 3.13** | 不要用 3.14（当前无法安装本项目的 `pydantic` 轮子） |
| Node.js | **20 / 22 / 24**（含 npm） | [nodejs.org](https://nodejs.org/) 或 nvm / fnm |
| Git | 任意近期版本 | 用于克隆 |

macOS：可用 `brew install python@3.12 node`。Windows 建议 [WSL2](https://learn.microsoft.com/zh-cn/windows/wsl/) 或下文的 Docker。

指定解释器：

```bash
PYTHON_BIN=/usr/local/bin/python3.12 ./scripts/setup.sh
```

## 一条命令启动

```bash
git clone https://github.com/fxfxfx99/StockViewAgent.git
cd StockViewAgent
./start.sh
```

脚本会：检测 Python/Node → 创建 `backend/.venv` → 安装依赖 → 从示例生成 `backend/.env` 并写入随机 `AUTH_JWT_SECRET` → 启动后端 **8001** 与前端 **5175**。

浏览器打开 <http://127.0.0.1:5175/>。

### 首次必做：配置台

页面顶部会提示未完成配置。点 **配置台** 或打开 <http://127.0.0.1:5175/setup>，填写：

1. **大模型 API**（OpenAI 兼容）
   - API Key
   - API Base，例如 `https://api.openai.com/v1` 或你的中转地址（需带 `/v1`）
   - Model，例如 `gpt-4o-mini`、`moonshot-v1-32k`
2. **行情信息接口**
   - 推荐 [KlineShare](https://data.klineshare.cn/docs)（填 API Key 并点测试）
   - 或 Tushare Pro Token
   - 或「仅公开源」（东财/腾讯等，无需 Key，稳定性取决于公开接口）

配置写入本机 `backend/data/`（已 gitignore），**不要**把 Key 贴进 Issue 或提交到 Git。

新闻「补充分析」依赖大模型；Key 或模型名错误时卡片会提示 HTTP 4xx，回到配置台核对 Base 与 Model。

## 分步命令

```bash
./scripts/check-env.sh   # 只检查，不安装
./scripts/setup.sh       # 只安装依赖、生成 .env
./scripts/dev.sh         # 只启动（环境已就绪时）
```

换端口（避免本机其它服务占用）：

```bash
BACKEND_PORT=8001 FRONTEND_PORT=5175 ./start.sh
```

让局域网访问前端（默认只绑本机）：

```bash
FRONTEND_HOST=0.0.0.0 ./start.sh
```

健康检查：<http://127.0.0.1:8001/api/health>  
配置状态：<http://127.0.0.1:8001/api/settings/setup-status>  
API 文档：<http://127.0.0.1:8001/docs>

## 环境变量（可选）

浏览器里填的凭据优先于文件。也可以编辑 `backend/.env`（由 `backend/.env.example` 复制，**已 gitignore**）。

常见项：

| 变量 | 作用 |
|------|------|
| `AUTH_REQUIRED` | 默认 `false`，无登录可用。公网部署请改为 `true` 并改掉默认密码 |
| `AUTH_JWT_SECRET` | `setup.sh` 会生成随机值；生产必须自定义 |
| `OPENAI_*` / `KLINE_ASSISTANT_*` / `KIMI_*` | 大模型；与配置台二选一即可 |
| `KLINESHARE_API_KEY` | 行情（也可只在配置台填写） |
| `TUSHARE_TOKEN` | 行情 / 宏观兜底 |
| `ENABLE_SCHEDULER` | 本地默认可开；多副本部署只让一个实例为 `true` |

前端开发：`frontend/.env.example` → `frontend/.env`。`scripts/dev.sh` 会用当前 `BACKEND_PORT` 覆盖 `VITE_DEV_BACKEND_URL`，一般不用手改。

完整列表见 `backend/.env.example`。数据源分层说明见 [DATA_SOURCES.md](./DATA_SOURCES.md)。

## 手动启动（不用脚本时）

两个终端：

```bash
cd backend
python3.12 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # 然后编辑或稍后用配置台
uvicorn app.main:app --host 127.0.0.1 --port 8001 --reload
```

```bash
cd frontend
cp .env.example .env        # 其中 VITE_DEV_BACKEND_URL 指向后端
npm ci
npm run dev -- --host 127.0.0.1 --port 5175
```

## Docker

先准备 `backend/.env`（不要用空文件，至少从 example 复制）：

```bash
cp backend/.env.example backend/.env
docker compose up -d --build
```

默认入口 <http://127.0.0.1:8080/>。数据卷名为 `stockviewagent_data`。

## 测试

```bash
cd backend && .venv/bin/python -m pytest tests/ -q
cd frontend && npm run build
```

## 不要提交

| 路径 | 原因 |
|------|------|
| `backend/.env`、`frontend/.env` | 密钥与本机端口 |
| `backend/data/` | 凭据、SQLite、行情/新闻缓存 |
| `backend/.venv/`、`frontend/node_modules/` | 本机依赖 |
| `kline_learning/`、`issuer_uploads/` | 用户上传 |

克隆后这些目录会在首次运行时自动创建。

## 排错

| 现象 | 处理 |
|------|------|
| `需要 Python 3.11–3.13` | 系统若是 3.14，请另装 3.12 并设 `PYTHON_BIN` |
| `未找到 npm` | 安装完整 Node，新开一个终端使 PATH 生效 |
| 端口被占用 | `BACKEND_PORT=8010 FRONTEND_PORT=5180 ./start.sh` |
| 页面提示无法连接服务 | 确认 `./start.sh` 仍在运行；健康检查是否 200 |
| 新闻补充分析失败 / LLM HTTP 404 | 配置台核对 API Base、模型名与 Key 权限 |
| 「更新新闻」很久或超时 | 多源抓取可能超过 3 分钟；可稍后刷新「待解读」 |
| 首次启动很慢 | 本地已有较大 `news.db` 时会做一次结构迁移 |

所有输出仅供研究参考，不构成投资建议。
