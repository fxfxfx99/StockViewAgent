# 本地研究实验室

量化研究实验室位于 `/lab`，从顶部「工具 → 量化实验室」进入。它把历史行情、策略版本、实验结果和模拟账户保存在本机，按预算反复执行“提出假设 → 固定数据验证 → 保存失败原因 → 改进候选”。当前范围是 A 股日线、只做多、持有 5–20 个交易日的研究与模拟，不连接实盘委托。`/research` 现在是独立的[个股与问题研究工作台](INVESTMENT_WORKSPACE.md)。

该功能用于本地或完整 Docker 部署。公开试用部署的接口白名单仍不开放 `/api/research/*`，不能通过公开试用入口提交研究任务。

“越来越强”首先表现为证据更完整、重复实验更少、失败原因可复用；新策略只有通过历史验证和独立前向观察，才会替换模拟账户的现行策略。研究次数增加不等于收益增加。

## 启动与首轮操作

完成 [本机配置](LOCAL_SETUP.md) 后，在项目根目录运行：

```bash
./start.sh
```

脚本检查并准备依赖，启动 API、前端和独立研究 worker，就绪后自动打开首页。首次在 **设置 → 连接与数据** 选择大模型供应商、填写 API Key，点击 **保存并开启自动运行**；自定义接口另填地址和模型。免费公开行情默认启用，付费行情及代理等配置放在高级折叠区。

新账户初始化时提供 5 只示例自选：`600519.SS`、`000001.SZ`、`600036.SS`、`000333.SZ`、`601318.SS`，仅用于开始体验，不是选股建议。已有列表和主动清空的列表不会被覆盖；清空后等待用户添加 A 股标的。

保存配置后，后台自动安排采集、GitHub 来源学习、候选研究和模拟维护。首次按 5 年日线取数，收到完整股票池快照后才开始相应研究。**设置 → 运行日志** 以“待配置 / 正在准备 / 运行中 / 需关注”显示实际状态；配置已保存、任务已入队都不代表采集或研究已经完成。

需要查看证据或手动控制时进入 <http://127.0.0.1:5175/lab>：

1. 查看预算、任务与快照覆盖。普通行情源和旧缓存通常缺少历史时点、交易状态及公司行动证据，只能用于探索；可通过“导入 JSON 快照”补充可信历史资料。
2. 根据需要手动采集、刷新 GitHub 来源、创建策略 JSON 或验证已有策略。研究只允许受限 JSON 表达式，不运行模型生成的 Python。
3. 比较同一快照、同一验证协议的实验，查看逐窗口表现、成本压力、基准和失败理由。没有合格候选时保留现行策略。
4. 通过历史门槛的冻结候选进入模拟观察。持续提供每日完整数据并保持服务运行，等待真实前向证据；“开始模拟观察”并不表示已经晋升。

资料不足、模型未配置、预算耗尽等也是有效任务结果。队列显示“已完成”只表示这次任务处理结束，应同时查看结果中的业务状态。

### 分开运行或 Docker

手动启动 API 和前端时，另开一个终端启动 worker：

```bash
cd backend
./.venv/bin/python -m app.research_worker
```

一次性调度并最多执行一个可运行任务：

```bash
cd backend
./.venv/bin/python -m app.research_worker --once
```

`--once` 不会清空整个队列。API 只负责入队和查询，不会替代 worker 执行研究。不要在 `./start.sh` 已运行时再启动第二个 worker；同一数据目录使用操作系统文件锁，重复启动会报“已有研究 worker 正在运行”。worker 使用 `fcntl`，原生 Windows 请使用 WSL2 或 Docker。

`./start.sh` 的监督器会对意外退出或持续未就绪的服务退避恢复，最多 5 次连续重启；查看数据目录中的 `runtime/logs/research.log` 等服务日志，或运行 `./start.sh --status`。`--no-browser` 关闭自动打开页面，`--reload` 开启后端开发重载。停止启动器或电脑重启后需再次启动；本项目未安装原生操作系统开机服务。完整说明见 [本机启动](LOCAL_SETUP.md)。

```bash
docker compose up -d --build
docker compose logs -f research-worker
```

Docker 入口为 <http://127.0.0.1:8080/lab>。`backend` 与 `research-worker` 共用 `stockviewagent_data` 卷，研究调度独立于 API 的进程数。`ENABLE_RESEARCH_WORKER` 默认 `true`；Docker Compose 中如需全局关闭，可在启动命令前设置 `ENABLE_RESEARCH_WORKER=false`。`ENABLE_SCHEDULER` 控制原有后台任务，不是研究 worker 的开关。

自定义本地数据目录时，在启动进程前设置环境变量，或写入 `backend/.env`；API、worker 与启动监督器必须使用同一路径。相对路径相对 `backend/`，修改后需重启：

```bash
export STOCKVIEW_DATA_DIR="/absolute/path/to/stockview-data"
./start.sh
```

## 调度、暂停与预算

worker 每分钟检查一次计划，保存大模型配置或任务完成后也会协调后续工作。首次初始化后自动采集，无需等到收盘；北京时间每天 16:30 后开启当日收盘轮次，按快照完整性和配置衔接研究、模拟维护。GitHub 来源按周检查，行情与新闻来源维护按日安排。网络失败保留已有成果，失败任务按重试及冷却规则处理。离线数天后按数据覆盖补采，不逐条重放所有错过的定时触发。

上午重新启动也会补采和核算停机前已记录的指令，不消耗当日 16:30 的任务。日预算耗尽后续跑同一批次；周额度耗尽或跨周时先收尾已有候选。租约失效会等待当前后台写入结束再退出 worker，避免遗留线程与下一任务并发写账本。

| 设置 | 默认值 | 行为 |
|---|---:|---|
| `enabled` | `true` | 账户研究开关；`false` 时停止为该账户自动调度，也不领取其已有队列任务 |
| `paused` | `false` | 暂停候选生成、验证及 GitHub 学习；采集、模拟记账、信号和回滚仍可执行 |
| `daily_calls` | 30 | 每账户每日研究模型请求上限 |
| `daily_tokens` | 150,000 | 每账户每日研究模型输入与输出 token 总额度 |
| `weekly_candidates` | 60 | 每账户每周候选尝试额度；手动验证也占用候选额度 |
| `initial_cash` | 100,000 | 模拟初始资金；账户建账后不可通过 API 修改 |
| `max_symbol_weight` | 0.2 | 单股最大目标权重 |
| `max_position_pct` | 1.0 | 总持仓最大目标权重，剩余部分为现金 |

页面可调整启用状态和预算、暂停或恢复候选研究；资金与仓位上限展示为只读。账户设置不删除历史、队列或当前持仓。暂停在下一次领取任务和生成候选前生效，已经发出的模型请求不会被撤销；`enabled=false` 也不是正在运行任务的强制终止按钮。若要停止全部研究执行，可关闭 worker 进程或设置全局 `ENABLE_RESEARCH_WORKER=false` 后重启。

预算周期为上海时区自然日、周一开始的自然周。每次模型请求先原子预留调用数、候选数以及保守估计的输入和输出 token，收到有效 usage 后结算；超时、进程中断或缺少 usage 时保留预留额度。重启不会清零，也不会偷偷重发可能已经计费的同一次请求。相同幂等键的队列任务和预算预留不会重复扣减。

这些额度仅覆盖研究模型请求与候选验证，不包含新闻分析等其它功能的模型调用，也不控制行情源或 GitHub 自身的限流。日额度可能先于每周候选数用尽，后续任务会继续同一研究批次。

### 运行日志

从页面右上角的 **设置 → 运行日志** 查看模型调用、Token、采集量和最近记录。可切换“今日 / 近 7 天 / 累计”，日期按北京时间划分。从功能启用后开始记录，不回填历史用量。

- **LLM 调用**：当前账户实际发出的请求，包括失败及每次真实重试，不是预算预留数。“已报 Token”只累加供应商返回的用量；未报告的请求单独标为未知，不估算为 0，也不据此推算费用。
- **采集批次与记录数**：批次是业务采集单位，不是逐次 HTTP 请求。获取量可含重复内容；“新增 / 修订”只计本次入库变化，公共来源采集明确标为本机共享。
- **覆盖范围**：行情、新闻、公司资料和 GitHub。缓存读取、股票搜索、来源维护探测等不计入，日志不代表全部系统网络流量。

## 数据如何积累

持续积累的数据默认集中在 `backend/data/`，可通过上述环境变量指定独立目录。**设置 → 连接与数据 → 高级设置** 显示实际路径、大小与分类，目录内也有可随文件夹搬迁的索引：

```text
data/
  INDEX.md                     # 分类说明与搬迁指引
  _managed/
    catalog.json               # 相对路径文件索引
    layout.json                # 目录身份与旧版归拢报告
    backups/                   # 部分 JSON 的写入前版本
    legacy/                    # 旧版文件冲突归档
  research.sqlite3              # WAL 模式：账户配置、队列、预算、版本与账本
  research.sqlite3-wal          # 运行期间可能存在，属于数据库的一部分
  research.sqlite3-shm
  research/
    batches/<content-hash>/
      raw.json                 # 原始来源数据及采集信息
      bars.parquet             # 规范化行情，保留历史状态与行动字段
      manifest.json            # 文件哈希、来源、时间、质量与批次关系
    read.duckdb                # 用于只读查询 Parquet 的目录
  users.db                     # 账户身份，恢复时应一并保留
  watchlists/                  # 账户股票池
  llm_runtime.json             # 已保存的大模型配置
  issuer_uploads/               # 公司资料上传原件与索引
  kline_learning/               # 策略学习资料、提示词与索引
  imports/company_fundamentals/ # CSV 导入原件
  runtime/logs/                # 服务日志
  runtime/pids/                # 运行状态，迁移时不携带旧 PID
```

仅默认根目录会在升级初始化时尝试复制旧版上传目录、三份 CSV、旧日志与旧 LLM 配置，保留原件并将冲突归档；结果见 `_managed/layout.json`。自定义新根目录不会自动导入旧实例凭据，延续旧实例应显式迁移。完整规则见 [本地数据目录与搬迁](LOCAL_SETUP.md#本地数据目录与搬迁)。

策略、实验、股票池版本、快照、研究批次、预算事件和晋升证据不可覆盖修改。当前指针、队列租约及模拟进度是可更新状态。账户之间隔离研究记录；相同原始数据批次可以共用，但读取必须有当前账户的批次引用。

文件先完整发布，再写数据库引用。快照固定所引用批次及哈希，读取时检查完整性；文件缺失或被改写会报错，不退回另一份数据冒充原快照。实验保留策略、快照、引擎及代码版本、参数和指标，便于追溯。

首次申请 5 年行情；已有足够覆盖、约 1,000 根以上日线且末日较近时，后续申请近 1 个月并合并。旧记录保留，重叠日期的新修订只影响新快照。来源、复权口径或成交量单位变化会形成独立分支并产生质量问题，不能无标记拼接。当前图表缓存可用于采集兜底；离线或陈旧数据会携带质量阻断项。

读取研究快照和归档日线时，每行包含 `source`、`fetched_at`、`available_at` 和 `batch_id`。其中 `batch_id` 是实际存储批次的内容哈希，可对应到 `research/batches/<batch_id>/manifest.json`，核对原始响应、文件校验值和父批次。这个标识在读取时从实际批次补入，覆盖输入中同名的自报字段，旧 Parquet 无需重写；仅批次或采集时间改变不会被统计为行情修订，也不会消耗新的优化留出期访问。

`ResearchData.read_snapshot(user_id, snapshot_id, as_of=...)` 支持历史时点读取：先筛选 `available_at <= as_of`，再按快照固定的批次顺序选择同一股票、同一时间的最后一个可见修订。比如某日收盘价最初为 12，次年才更正为 10.5，查询原交易日收盘后仍会得到 12；从更正实际可得的时刻起才返回 10.5。不传 `as_of` 则读取该快照固定批次中的最终修订，旧快照不受新批次影响。`as_of` 使用 Unix 秒或带时区的 ISO 字符串，边界包含该时刻。

时点筛选不会改变快照的质量结论。缺少历史可得性证据的缓存仍保留 `availability_inferred=true`，迟到修订、探索数据及其它质量阻断项也不会因筛选而获得严格资格。

进程重启后队列和进度仍在。运行任务使用可续租的租约，过期后允许重新领取，默认最多尝试 3 次；旧租约不能提交新的完成结果。模拟每日事件按账户和日期去重。长期积累会增加磁盘占用，当前没有自动清理旧研究批次的策略。

## 严格数据与可信导入

`quality.eligible=true` / `strict=true` 表示输入通过当前完整性和一致性规则，不能证明提供者的陈述真实，也不等于策略通过验证。自动采集的前复权行情、当前证券资料或只有 OHLCV 的 CSV，不能自行升级为严格时点证据。

导入格式是 `{ "bundles": { "600000.SS": { "candles": [...] } }, "metadata": {...} }`，接口为 `POST /api/research/import`。每次最多 100 万根日线；符号使用六位数字加 `.SS`、`.SZ` 或 `.BJ`。

| 数据 | 必须能说明的事实 |
|---|---|
| 每根日线 | `t,o,h,l,c,v`；时间递增不重复、价格有限且 OHLC 一致；`t` 为 Unix 秒，建议上海时区交易日零点 |
| 价格、成交量 | `price_basis="raw"`；若指标使用复权价格，必须另给原始成交用的 `execution_o`、`execution_c`；`volume_unit="shares"`，不能把“手”当“股” |
| 时间证据 | `source_time` 为来源发布时间，`available_at` 为历史上实际可得时间；`fetched_at` 是本次采集时间。时间戳用 Unix 秒或带时区的 ISO 字符串，不能拿今天抓取时间倒填历史可得时间 |
| 每日交易状态 | 明确的布尔值 `tradable`、`suspended`、`st`；明确的 `limit_up`、`limit_down`，或真实的 `no_price_limit=true`；不能按今天状态反推过去 |
| 公司行动 | 每根日线 `corporate_actions_checked=true`；完整行动清单和覆盖声明；有行动时还须在对应日线给出 `corporate_action` 对象或列表 |
| 交易日历 | `metadata.calendar` 列出覆盖期真实交易日，已上市标的在应有交易日不能缺行；停牌也需有状态记录 |
| 证券历史 | `instrument_history` 每项包含 `symbol,valid_from,valid_to,available_at`；`valid_to` 可为 `null`，覆盖所有相关日线 |
| 覆盖声明 | `coverage.start/end/symbols` 覆盖输入数据，且 `corporate_actions_complete`、`instrument_history_complete` 均为 `true` |

历史日线的最终 OHLCV 必须在当日 15:00 收盘之后、16:30 决策时点之前或当时可得。明确迟到的行及事后修订会被策略解释器屏蔽，并阻断严格质量；不能把修订后的数值倒填成过去的信号。缺失可用时间时保留 `availability_inferred=true`，仅支持探索计算，不构成历史可得性证明。

每项公司行动包含 `symbol,ex_date,available_at,cash_dividend,split_ratio`；`cash_dividend` 是每旧股税前现金，`split_ratio` 是新股数/旧股数，两者至少一项非中性。`available_at` 必须严格早于除权日上海时间 09:30，且元数据与当日日线内的金额、比例、时间逐项一致。没有行动时使用空数组，同时明确完整覆盖；仅省略行动并不表示没有行动。

例如，一项已公告现金分红可表示为：

```json
{
  "symbol": "600000.SS",
  "ex_date": "2025-01-07",
  "available_at": "2025-01-06T18:00:00+08:00",
  "cash_dividend": 0.1,
  "split_ratio": 1.0
}
```

上述数字只是字段说明，不是该证券的真实公司行动。真实导入应从有授权的数据源整理原始证据，并核对单位、历史状态和公告时间；不要为了消除质量问题而虚填 `true` 或修改价格口径。

当前现金分红按税前金额在除权日作探索记账，未模拟投资者差异化股息税、实际派款日差异及零碎股份现金结算，因此带现金分红的数据会产生 `cash_dividend_net_payment_unverified`，阻断严格质量与自动晋升。已完整描述的纯拆股可通过行动校验；更早的费用制度、未支持或无法完整描述的行动仍会阻断严格质量。

### 最小格式样例：只在临时目录验证

[research-import-format.json](examples/research-import-format.json) 含两根明确标记为虚构的日线，仅用于验证导入结构，不是真实行情，不可用于收益结论。解析后会因演示标记被强制列为探索数据，也缺少研究所需历史长度；不要删除演示标记尝试晋升。以下命令只写临时目录，不调用行情或模型，不导入当前账户：

```bash
cd backend
./.venv/bin/python - <<'PY'
import json
import tempfile
from pathlib import Path
from app.services.research_data import ResearchData
from app.services.research_evaluation import build_research_splits

example = json.loads(Path("../docs/examples/research-import-format.json").read_text())
with tempfile.TemporaryDirectory(prefix="research-import-demo-") as directory:
    data = ResearchData(directory)
    snapshot = data.import_snapshot(1, example["bundles"], metadata=example["metadata"])
    rows = data.read_snapshot(1, snapshot["id"])
    print("格式与完整性：", snapshot["payload"]["quality"])
    print("历史长度检查：", build_research_splits({"600000.SS": rows})["issues"])
PY
```

正式导入请替换为自己核实的数据，在页面提交后检查快照质量和覆盖。后续每次追加可信数据也需提供完整覆盖与历史元数据；普通行情采集不会自动补齐这些证据。最新快照可能被采集更新为探索数据，研究 API 可显式指定要使用的 `snapshot_id`。

## 验证、观察、晋升与回滚

候选使用固定股票池和不可变快照。研究批次按周及配置、代码版本固定，同一批次不会因每日行情更新而滑动验证边界。模型只接收允许的开发期上下文、近期验证反馈和来源卡片，不接收保留测试段表现。

历史协议使用 24 个月训练上下文、6 个月验证窗口，每次前移 6 个月，至少 3 个合格窗口；边界按实际交易日清除 20 日持有期可能重叠的样本。首轮最后 6 个月留作独立保留段，至少 100 个交易日。信号在收盘生成、下一交易日开盘执行；撮合处理 T+1、整手、停牌、涨跌停、成交容量、费用和滑点等约束。

历史门槛包括严格数据、完整统计、扣费后平均正收益、至少三分之二窗口同时取得正收益和相对等权的正超额、累计至少 30 笔完整交易、最大回撤不超过 20%，以及双倍成本压力检验。还检查截断数据重算的一致性和指标预热稳定性。候选按验证期 Sharpe 中位数排序，再比较回撤和换手。

每月最多冻结一个候选。首个冻结候选只能消费一次保留段；中断或失败也不能借此反复调参窥视同一保留段。通过历史阶段后，为候选、同期现行策略、同期等权基线建立同起点、同资金和约束的观察账户。

前向晋升要求三者交易日一致、至少 **60 个前向交易日**、候选至少 **20 笔完整交易**、扣费后收益为正且超过同期现行策略及等权基线，最大回撤不超过 20%，且不比现行策略高出 2 个百分点；数据与统计也必须合格。无合格结果就继续观察。页面以实际晋升事件为准，不会把回测排名当成已晋升。

下载五年历史数据不能立即获得 60 天前向证据。观察只计算建账之后的交易日，并且只执行开盘前已保存的决策。电脑离线期间没有产生的决策不会在重启后伪造补单；补采能补行情，不能补出当时不存在的决策。观察期混入不完整快照会保留质量问题，不能靠后来改个标签洗掉。

模拟回撤超过 20% 时停止新开仓，按后续可成交条件退出并进入现金观察；停牌或跌停时不保证立即退出。已有晋升记录后，可在模拟证据区域选择回滚上一版，或入队 `rollback` 任务。回滚保留当前真实模拟现金和持仓，撤销尚未执行的旧决策，从下一个可用收盘点应用上一版规则，不把净值“恢复”为过去的数值。

晋升同样继承现行模拟账户的现金、持仓和累计账本，仅切换后续规则；候选的独立观察账户保留作证据。股票池变化后，遗留持仓仍可退出，新买入仅限新规则池。

当前股票池由自选列表或导入列表决定，仍存在选股、幸存者及多次尝试偏差。严格输入和固定门槛减少部分错误，不能消除这些偏差，也不保证盈利。

## GitHub 学习的范围

当前只读取固定公开来源的文档，记录仓库、文件、commit、内容哈希、来源链接及研究用途；内容变化时保存新知识卡。它不下载执行仓库代码，不自动安装新依赖，不把外部项目的收益当成本地验证结果。

| 来源 | 借鉴用途 |
|---|---|
| [Microsoft Qlib](https://github.com/microsoft/qlib) | 参数、指标、实验产物记录 |
| [Microsoft RD-Agent](https://github.com/microsoft/RD-Agent) | 假设、验证、反馈、修订循环 |
| [Freqtrade](https://github.com/freqtrade/freqtrade) | 前视偏差检查与截断数据重算 |
| [VeighNa / vn.py](https://github.com/vnpy/vnpy) | 数据、策略、研究分层 |
| [CNEquity](https://github.com/rootSunc/CNEquity) | 不可变数据批次、行级来源追溯、历史时点可见性与修订选择 |

每个已成功获取的固定来源最多选取一张最新知识卡作为候选提示材料，避免某个仓库的多次更新挤掉其它来源。知识卡不是经过验证的因子库；文档发生变化只产生待验证假设，具体引用以页面保存的 commit 链接为准。

### CNEquity 的学习与落地

本轮参考 [CNEquity 查询指南](https://github.com/rootSunc/CNEquity/blob/c53c797f0d8c6e58c6748e27b2037e4fa99b9361/docs/zh/datasets/query-guide.md)、[Python API 的时点查询契约](https://github.com/rootSunc/CNEquity/blob/c53c797f0d8c6e58c6748e27b2037e4fa99b9361/docs/zh/reference/python-api.md) 和 [PIT 版本处理](https://github.com/rootSunc/CNEquity/blob/c53c797f0d8c6e58c6748e27b2037e4fa99b9361/src/cnequity/domain/pit.py)，阅读版本为 `c53c797f0d8c6e58c6748e27b2037e4fa99b9361`。

基于本项目已有的不可变批次与快照，补充了逐行批次标识，修正了先覆盖修订再过滤时间导致旧数据消失的问题，并将查询指南纳入固定文档学习来源。这些改动由本项目自行实现，未引入 CNEquity 运行时依赖或复制其源码。现有原始价、复权口径、交易状态和质量检查继续适用；本轮没有接入 CNEquity 的数据湖、全部数据集或 MCP 服务。

感谢 CNEquity 作者及贡献者公开数据工程实践。上游代码使用 [Apache-2.0](https://github.com/rootSunc/CNEquity/blob/c53c797f0d8c6e58c6748e27b2037e4fa99b9361/LICENSE)，其 [NOTICE](https://github.com/rootSunc/CNEquity/blob/c53c797f0d8c6e58c6748e27b2037e4fa99b9361/NOTICE) 保留作者声明；其它已参考项目与使用方式见 [README 开源致谢](../README.md#开源学习与致谢)。

### 行情与新闻来源自动维护

`source_autopilot.py` 根据实际请求与小样本复测维护来源健康。行情状态按账户、日线/分钟能力隔离，只在已配置的内置适配器链中调整顺序；近期验证成功的来源优先，连续 3 次失败后冷却，从 5 分钟开始延长，冷却到期再试。返回 HTTP 200 仍需通过时间顺序、有限数值、OHLC 与成交量检查；来源健康不等于数据符合严格回测要求。

新闻来源在内置信任网站范围内寻找 RSS/Atom 声明，验证链接、条目与近期发布时间后才加入共享来源清单。已有发现源每日复核，连续 3 次检查失败后停用；定义、证据哈希和新增/停用事件保留在研究账本。当前自动维护限于已有适配器的路由配置和经验证的来源定义，不自动编写任意代码、安装新适配器或执行 `git pull` 更新项目。

## API 与排错

接口使用当前登录账户；本地免登录模式使用默认本地账户。启用登录时携带 `Authorization: Bearer <token>`，各账户不能读取其他账户的策略、任务和快照。

| 接口 | 用途 |
|---|---|
| `GET /api/autopilot/status` | 设置中的自动运行阶段、自选初始化、任务、预算、worker 与来源健康摘要 |
| `POST /api/autopilot/reconcile` | 按当前账户配置协调待执行工作，不在 API 内直接运行研究 |
| `GET /api/research/overview` | 配置、预算、worker、数据、近期任务和实验、模拟证据、来源卡 |
| `POST /api/research/runs` | `{kind,payload,idempotency_key?}` 入队；`kind` 为 `collect/research/github/paper/evaluate/rollback` |
| `GET /api/research/jobs/{id}` | 任务进度与结果；POST 返回 202 仅表示接收入队 |
| `POST /api/research/strategies` | `{spec: {...}}` 创建策略版本；相同逻辑去重 |
| `GET /api/research/strategies/{id}` | 策略规则和解释 |
| `GET /api/research/experiments/{id}` | 完整实验、逐窗口表现与门槛 |
| `GET /api/research/snapshots/{id}` | 快照和文件完整性检查 |
| `POST /api/research/import` | `{bundles,metadata}` 导入不可变快照 |
| `PATCH /api/research/config` | 修改账户配置；拒绝未知字段 |

例如，以下命令为本地免登录账户创建一个至多 3 个候选的研究任务；使用自己已有的快照 ID。再次使用同一个幂等键会返回同一个任务：

```bash
curl --fail-with-body http://127.0.0.1:8001/api/research/runs \
  -H 'Content-Type: application/json' \
  -d '{"kind":"research","payload":{"snapshot_id":"YOUR_SNAPSHOT_ID","candidate_count":3},"idempotency_key":"first-review-1"}'
```

| 现象 | 检查 |
|---|---|
| 任务一直排队 | worker 心跳、全局开关、账户 enabled/paused；API 正常不表示 worker 在运行 |
| `configuration_required` | 配置台模型 Key、Base 和模型名 |
| `budget_exhausted` | 已用及已预留额度、上海时区周期；重启不重置预算 |
| `insufficient_history` | 真实覆盖长度、至少三个窗口、标的历史是否对齐；5 年请求不保证源返回五年完整数据 |
| 没有晋升 | 查看质量、历史门槛、保留段或前向观察失败理由，而非只看单次收益 |
| 快照 409 / 校验失败 | 恢复原始整套批次及数据库；不要原地修改 manifest 或删除校验字段 |
| 重复 worker 报锁错误 | 停止多余启动器；锁文件存在本身不等于进程仍存活 |

## 备份与恢复

应备份**整个数据目录**，包括 `research.sqlite3`、可能存在的 WAL/SHM、`research/` 下所有 JSON/Parquet/manifest、上传与 CSV 原件、用户数据库、自选列表和配置。只备份 SQLite 会丢行情，单独复制 Parquet 会丢账户引用与实验；两者必须来自同一个一致时间点。目录会携带账户和已保存的 API 配置；应用代码、外置 `.env`、JWT / API 环境变量与外部服务配置需另行保留。

管理页数据资产目录会枚举 `research.sqlite3` 与 `research/**`。`_managed/backups` 只是部分 JSON 写入前的版本备份，不是研究数据的完整备份，也没有自动建立跨 SQLite 和 Parquet 的一致快照。

需要换目录或搬到另一台机器时，先停止监督器、API、worker、其它写入脚本及相关 Docker 服务，再从项目根运行：

```bash
./backend/.venv/bin/python scripts/storage.py status
./backend/.venv/bin/python scripts/storage.py index
./backend/.venv/bin/python scripts/storage.py verify
./backend/.venv/bin/python scripts/storage.py migrate --to '/absolute/path/to/StockViewAgent-data'
```

目标须尚不存在，且不能与源目录互相嵌套。工具复制并校验，原目录保留；按成功输出设置 `STOCKVIEW_DATA_DIR` 后再启动，新机器另行配置外置环境和服务。迁移后的首次启动前可再执行 `verify`；目录继续写入后，迁移时的校验清单不再代表当前状态。步骤与路径规则见 [本地目录说明](LOCAL_SETUP.md#本地数据目录与搬迁)。

本地最简单的一致备份方式：先 `Ctrl+C` 停止 `start.sh`，确认其它手动 API、worker 和脚本也已停止，再从项目根目录运行：

```bash
mkdir -p backups
tar -C backend -czf "backups/stockview-data-$(date +%Y%m%d-%H%M%S).tar.gz" data
```

自定义 `STOCKVIEW_DATA_DIR` 时请备份实际目录。Docker 可停止写入服务后，使用项目现有镜像读取卷：

```bash
mkdir -p backups
docker compose stop backend research-worker
docker compose run --rm --no-deps --entrypoint tar research-worker \
  -C /app -czf - data > "backups/stockview-data-$(date +%Y%m%d-%H%M%S).tar.gz"
docker compose up -d backend research-worker
```

备份包含账户和可能的凭据，应放在受控位置。恢复时先停所有写入，将完整归档解压到独立目录核对内容，再将该目录作为数据目录启动同版本服务；Docker 则将整套数据恢复到相应卷。恢复后检查用户、worker、快照校验与最近模拟事件。不要用 `docker compose down -v` 保留数据，该命令会删除卷。
