# 本机配置与启动

本文说明本地依赖、配置与启动流程。密钥保存在本机，不提交版本库。

## 需要提前安装

| 依赖 | 版本 | 说明 |
|------|------|------|
| Python | **3.11、3.12 或 3.13** | 不要用 3.14（当前无法安装本项目的 `pydantic` 轮子） |
| Node.js | **20 / 22 / 24**（含 npm） | [nodejs.org](https://nodejs.org/) 或 nvm / fnm |
| Git | 任意近期版本 | 用于克隆 |

macOS：可用 `brew install python@3.12 node`。Windows 建议 [WSL2](https://learn.microsoft.com/zh-cn/windows/wsl/) 或下文的 Docker。

已安装 Node 但提示未找到时，先确认当前终端 `node -v` 与 `npm -v` 都可执行；使用 nvm / fnm 的用户需加载对应终端初始化配置，也可将安装目录的 `bin` 加入 `PATH` 后重试。桌面应用内终端与系统终端的 `PATH` 可能不同。

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

脚本会：检测 Python/Node → 创建 `backend/.venv` → 按依赖文件哈希安装、检查或修复依赖 → 从示例生成 `backend/.env` 并写入随机 `AUTH_JWT_SECRET` → 启动后端 **8001**、前端 **5175** 和独立研究 worker → 就绪后自动打开浏览器。它只按仓库的依赖文件准备环境；Python、Node 和 Git 本身仍需预先安装。更新代码后再次运行即可同步变更的依赖。

本地监督器检查前后端就绪及研究 worker 进程存活。运行中单个服务退出或就绪检查持续失败时，只重启该服务，按退避间隔最多尝试 5 次；稳定运行约两分钟后清零连续失败计数。达到上限或依赖准备失败才退出，并清理本次启动的服务。端口冲突会直接提示，不会结束占用端口的其它进程。重复执行 `./start.sh` 会识别已有实例，避免重复启动。

浏览器未自动打开时访问 <http://127.0.0.1:5175/>。按 `Ctrl+C` 停止本次所有服务，收尾超时后只清理本次进程组；研究队列、预算和数据保留。监督器不是操作系统开机服务：关闭后或电脑重启后，需再次执行 `./start.sh`。Docker 部署可使用其 `restart: unless-stopped` 策略，仍依赖 Docker 服务已启动。

新账户首次初始化时准备 5 只示例自选；已有自选列表，包括主动清空的列表，保持不变。**设置 → 运行日志** 显示“待配置 / 正在准备 / 运行中 / 需关注”，详细任务在 <http://127.0.0.1:5175/research>。默认每日研究预算为 30 次模型请求、15 万总 token，每周最多 60 个候选尝试；研究实验室可暂停候选研究、关闭账户调度或调整预算。自动采集行情不保证具备严格回测证据，可信导入及模拟晋升流程见 [RESEARCH_LAB.md](RESEARCH_LAB.md)。

通过页面右上角的 **设置 → 运行日志** 查看调用量、Token、采集量和最近记录，可切换“今日 / 近 7 天 / 累计”。它统计实际模型请求和采集量，区别于研究预算预留；从功能启用后开始记录，不回填旧用量。Token 未知、共享采集及获取量与新增量的口径见 [运行日志](RESEARCH_LAB.md#运行日志)。

### 连接大模型

点 **设置 → 连接与数据** 或打开 <http://127.0.0.1:5175/setup>：

1. 选择大模型供应商，填写 **API Key**。供应商列表复用后端预设，包括 OpenAI、Kimi、DeepSeek、阿里云 DashScope 等。
2. 只有选择“自定义 / OpenAI 兼容接口”时，才需填写 **API Base** 和 **模型名称**。
3. 点击 **保存并开启自动运行**，按页面实际状态等待准备或处理提示。保存成功不代表后台已执行；worker 未启动时会提示需关注。

默认使用无需 Key 的免费公开行情。已有 [KlineShare](https://data.klineshare.cn/docs) 或 [Tushare](https://tushare.pro/document/2) 账户时，可展开 **高级设置（可选）** 添加；它们不是首次启动条件。数据中心、代理与管理员雪球设置也保留在高级区域，可单独保存。

配置写入本机数据目录，默认 `backend/data/`（已 gitignore），**不要**把 Key 贴进 Issue 或提交到 Git。

新闻「补充分析」依赖大模型；Key 或模型名错误时卡片会提示 HTTP 4xx，回到配置台核对 Base 与 Model。

## 分步命令

```bash
./scripts/check-env.sh   # 只检查，不安装；依赖或配置未就绪时返回非零状态
./scripts/setup.sh       # 只安装依赖、生成 .env
./scripts/dev.sh         # 与 start.sh 共用监督器和依赖检查
./start.sh --status      # 只查看是否运行，不启动服务
./start.sh --no-browser  # 正常启动，不自动打开浏览器
./start.sh --reload      # 开发模式：开启后端代码重载
```

默认关闭后端代码重载，适合持续运行。服务日志位于数据目录的 `runtime/logs/backend.log`、`runtime/logs/frontend.log`、`runtime/logs/research.log`，按大小轮转；运行快照位于 `runtime/pids/local-runtime.json`。默认完整路径以 `backend/data/` 开头。恢复失败时先查看相应日志，修复原因后重新启动。`--status` 在未运行时返回非零状态，不修改业务数据。

换端口（避免本机其它服务占用）：

```bash
BACKEND_PORT=8001 FRONTEND_PORT=5175 ./start.sh
```

让局域网访问前端（默认只绑本机；将示例 IP 换成这台电脑的实际局域网地址）：

```bash
FRONTEND_HOST=0.0.0.0 CORS_ALLOWED_ORIGINS=http://192.168.1.20:5175 ./start.sh
```

浏览器默认仅允许 `http(s)://localhost`、`127.0.0.1`、`[::1]` 的任意端口。局域网或部署域名需要在 `CORS_ALLOWED_ORIGINS` 登记完整来源（协议、主机、非默认端口），多个来源用逗号分隔；例如 `https://stocks.example.com,http://192.168.1.20:5175`，不要包含路径、末尾 `/` 或 `*`。即使前端与 API 经代理部署于同一域名，也需要登记浏览器地址。

请求 `Host` 仅允许 loopback 或上述明确登记的主机，不信任 `X-Forwarded-Host`；自定义反向代理应保留已登记的外部 `Host`，或使用 loopback 后端地址。项目的 Vite 代理与 Docker Nginx 配置支持此校验。未登记的来源或请求主机均返回 403，包括预检与无 `Origin` 请求；可信主机上的 CLI / Agent 无 `Origin` 请求保持可用。`AUTH_REQUIRED=false` 时本地免登录。

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
| `CORS_ALLOWED_ORIGINS` | 额外允许的浏览器来源，逗号分隔；同时登记可信请求主机，局域网或部署域名必须显式填写 |
| `OPENAI_*` / `KLINE_ASSISTANT_*` / `KIMI_*` | 大模型；与配置台二选一即可 |
| `KLINESHARE_API_KEY` | 行情（也可只在配置台填写） |
| `TUSHARE_TOKEN` | 行情 / 宏观兜底 |
| `ENABLE_SCHEDULER` | 本地默认可开；多副本部署只让一个实例为 `true` |
| `ENABLE_DATA_COLLECTION` | 默认 `true`，在 API 内运行中央公共数据采集队列；自动排队另受 `ENABLE_SCHEDULER` 和数据中心页面开关控制 |
| `ENABLE_RESEARCH_WORKER` | 默认 `true`，控制独立研究 worker 的调度和执行；与 `ENABLE_SCHEDULER` 分开 |
| `STOCKVIEW_DATA_DIR` | 数据根目录，可写入 `backend/.env` 或通过 shell `export` 指定；相对路径以 `backend/` 为基准，修改后重启全部服务 |
| `SVA_OPEN_BROWSER` | 启动器环境变量，设为 `false` 可禁止自动打开浏览器，与 `--no-browser` 等效 |
| `SVA_DEV_RELOAD` | 启动器环境变量，设为 `true` 开启后端重载，与 `--reload` 等效 |

前端开发：`frontend/.env.example` → `frontend/.env`。`scripts/dev.sh` 会用当前 `BACKEND_PORT` 覆盖 `VITE_DEV_BACKEND_URL`，一般不用手改。

完整列表见 `backend/.env.example`。数据源分层说明见 [DATA_SOURCES.md](./DATA_SOURCES.md)。

### 数据中心与自动采集

管理员访问 `/data` 可浏览分类文件、来源状态和任务队列，调整自动采集、股票轮询、每日额度、磁盘下限与公共资料 RAG。默认开启中央采集，每天最多启动 120 个逻辑任务，每个股票类任务最多 10 个标的，剩余空间低于 1 GiB 时暂停领取任务；一个逻辑任务可能请求多项上游数据。采集本身不调用模型。

`ENABLE_DATA_COLLECTION=true`（默认）在 API 内启动中央采集 worker；`ENABLE_SCHEDULER` 控制自动排队。页面关闭自动采集后，手动任务仍可处理；设 `ENABLE_DATA_COLLECTION=false` 并重启会停止中央 worker，日历在 `ENABLE_SCHEDULER=true` 时回退独立监控。缓存自动回收另有开关，默认关闭，回收区仍占用磁盘。完整来源、开关含义、PIT 限制与清理流程见 [数据中心说明](DATA_CENTER.md)。

## 本地数据目录与搬迁

默认数据根目录是 `backend/data/`。API、研究 worker 与启动监督器使用同一 `STOCKVIEW_DATA_DIR`；shell 环境变量优先于 `backend/.env`，相对路径始终相对项目的 `backend/`，与执行命令时所在目录无关。配置在进程启动时确定，修改后需重启。

| 根目录内的位置 | 内容 |
|---|---|
| `research.sqlite3`、`research/`、其它业务数据库 | 策略、实验、队列、模拟账户、运行用量、新闻及财务修订 |
| `tracking.sqlite3` | 关注判断、历史修订、冻结证据、人工复盘、变化游标与阅读反馈 |
| `data_center.sqlite3`、`datasets/` | 采集任务、预算、来源状态、公开数据不可变批次与检索修订 |
| `issuer_uploads/`、`kline_learning/` | 上传原件、资料清单、提示词与资料索引 |
| `imports/company_fundamentals/` | 财务、管理层讨论和公司基本信息 CSV 原件 |
| `users.db`、`watchlists/`、配置 JSON | 账户、自选股、已保存的 LLM 和行情 API 配置 |
| `runtime/logs/`、`runtime/pids/` | 本地服务日志、监督器状态和 PID |
| `INDEX.md`、`_managed/catalog.json` | 分类说明与相对路径文件索引 |
| `_managed/backups/`、`_managed/legacy/` | 部分 JSON 历史版本与旧目录冲突归档 |
| `_managed/cache/`、`_managed/quarantine/` | 缓存策略、清理预览、可恢复回收文件与操作记录 |

设置 → 连接与数据 → 高级设置 → **本地数据目录** 可查看实际位置、总大小、文件数和分类，并刷新索引。索引刷新只扫描已有文件；分类中的“可重建”不表示系统会自动删除缓存。

从项目根目录运行：

```bash
./backend/.venv/bin/python scripts/storage.py status  # 查看实际目录与分类
./backend/.venv/bin/python scripts/storage.py index   # 刷新文件索引
# 先停止启动监督器、API、worker、其它写入脚本，以及使用此目录的 Docker 服务：
./backend/.venv/bin/python scripts/storage.py verify  # 离线校验
./backend/.venv/bin/python scripts/storage.py migrate --to '/absolute/path/to/StockViewAgent-data'
```

目标必须是**尚不存在**且与源目录互不包含的新目录。迁移执行复制和校验，保留源目录；运行中的 PID 与锁不会带到新位置。成功后按输出的 `shell_command` 设置 `STOCKVIEW_DATA_DIR`，在首次启动前可再运行 `verify`，随后执行 `./start.sh`；长期使用可将同一路径写入 `backend/.env`。`--to` 的相对路径以当前命令目录为准，建议使用绝对路径。迁移校验清单保存在 `_managed/migration-manifest.json`；新目录投入运行后的正常写入会改变文件，旧清单不代表实时内容。

数据目录携带已有账户及已保存的 API 配置；搬到另一台电脑时也需保留应用代码版本。外置 `.env`、JWT 密钥、通过环境变量提供的 API Key、外部服务及未导入的外部文件需另行配置。Docker 数据卷同样先停写入服务，再按实际卷位置备份或迁移，不能直接把主机上的默认目录当作卷内容。

### 旧版默认目录升级

仅当使用默认 `backend/data/` 时，启动初始化会尝试无覆盖复制旧项目根目录的 `issuer_uploads/`、`kline_learning/`、`财务指标 2025.csv`、`管理层经营讨论与分析 2025.csv`、`上市公司基本信息 2025.csv`，以及旧 `logs/` 和 `backend/app/data/llm_runtime.json`。CSV 归入 `imports/company_fundamentals/`，旧日志归入 `runtime/logs/legacy/`。原件保留；目标已有内容时归档到 `_managed/legacy/`，复制、冲突或待处理原因记录在 `_managed/layout.json`，请以实际报告为准。

自定义新数据根目录不会隐式导入旧实例的资料或凭据。需要延续旧实例时，应使用上述迁移命令，而不是仅指定一个空目录。

## 手动启动（不用脚本时）

API 和前端各一个终端：

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

研究实验室另需第三个终端（macOS / Linux / WSL2）：

```bash
cd backend
./.venv/bin/python -m app.research_worker
```

`--once` 参数会调度并最多执行一个任务后退出。只启动 API 时可以浏览或提交任务，队列需 worker 执行。同一数据目录只运行一个 worker；使用 `start.sh` 时无需再手动启动。

## Docker

先准备 `backend/.env`（不要用空文件，至少从 example 复制）：

```bash
cp backend/.env.example backend/.env
docker compose up -d --build
```

默认入口 <http://127.0.0.1:8080/>，研究实验室为 `/research`。Compose 启动独立 `research-worker` 服务，与 API 共用 `stockviewagent_data` 数据卷（实际 Docker 卷名通常有项目名前缀）。可用 `docker compose logs -f research-worker` 检查任务运行情况。

研究记录同时依赖 SQLite 与不可变 JSON/Parquet，备份需保留整套数据目录或数据卷；管理页的 JSON 版本备份不能替代完整备份。步骤见 [研究数据备份与恢复](RESEARCH_LAB.md#备份与恢复)。

## 测试

```bash
(cd backend && .venv/bin/python -m pytest tests/ -q)
(cd frontend && npm test && npm run build)
```

前端依赖审计使用 `cd frontend && npm audit`。依赖版本以 `frontend/package.json` 和 `package-lock.json` 为准。升级前查阅 [Vite 发行说明](https://vite.dev/releases)和 [ECharts 升级指南](https://echarts.apache.org/handbook/zh/basics/release-note/v6-upgrade-guide/)，升级后检查 K 线、指标切换、缩放与小图布局，并提交 `package-lock.json`。

## 不要提交

| 路径 | 原因 |
|------|------|
| `backend/.env`、`frontend/.env` | 密钥与本机端口 |
| `backend/data/` 及自定义数据目录 | 凭据、业务数据库、上传资料、缓存和运行日志 |
| `backend/.venv/`、`frontend/node_modules/` | 本机依赖 |
| 旧版根目录的 `kline_learning/`、`issuer_uploads/` | 升级复制后仍保留的上传原件 |

当前数据目录及其运行子目录会在首次启动时创建；旧版目录仅作为升级来源保留。

## 排错

| 现象 | 处理 |
|------|------|
| `需要 Python 3.11–3.13` | 系统若是 3.14，请另装 3.12 并设 `PYTHON_BIN` |
| `未找到 npm` | 安装完整 Node，新开一个终端使 PATH 生效 |
| 端口被占用 | `BACKEND_PORT=8010 FRONTEND_PORT=5180 ./start.sh` |
| `npm ci` 失败 | 检查网络及 Node 版本；若提示 lockfile 与 package.json 不一致，修复依赖锁文件后再运行，不会自动修改锁文件 |
| 页面提示无法连接服务 | 确认 `./start.sh` 仍在运行；健康检查是否 200 |
| 服务反复恢复后退出 | `./start.sh --status` 查看状态，检查实际数据目录 `runtime/logs/` 内的服务日志；修复后重新运行，最多 5 次连续恢复不会无限重试 |
| 浏览器未自动打开 | 手动访问输出地址，或检查是否设置了 `--no-browser` / `SVA_OPEN_BROWSER=false` |
| 配置已保存但自动运行“需关注” | 查看设置中的运行日志及研究实验室；worker 离线、列表已清空或任务失败都需按实际原因处理，付费行情 Key 不是必填项 |
| 局域网/部署域名请求返回 403「不允许的浏览器来源 / 请求主机」 | 将浏览器实际协议、主机与端口加入 `CORS_ALLOWED_ORIGINS`；自定义反代保留该 Host，重启后端 |
| 新闻补充分析失败 / LLM HTTP 404 | 配置台核对 API Base、模型名与 Key 权限 |
| 「更新新闻」很久或超时 | 多源抓取可能超过 3 分钟；可稍后刷新「待解读」 |
| 首次启动很慢 | 启动时会检查并更新数据库结构，较大的 `news.db` 可能需要更长时间 |

所有输出仅供研究参考，不构成投资建议。
