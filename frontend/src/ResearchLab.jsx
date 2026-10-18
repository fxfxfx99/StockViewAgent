import { useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert, App as AntApp, Badge, Button, Card, Collapse, Descriptions, Divider, Drawer,
  Empty, Form, Input, InputNumber, Layout, Modal, Popconfirm, Progress, Space, Spin,
  Switch, Table, Tabs, Tag, Tooltip, Typography,
} from "antd";
import { useAuth } from "./AuthContext.jsx";
import DownloadOutlined from "@ant-design/icons/es/icons/DownloadOutlined";
import GithubOutlined from "@ant-design/icons/es/icons/GithubOutlined";
import PlusOutlined from "@ant-design/icons/es/icons/PlusOutlined";
import ReloadOutlined from "@ant-design/icons/es/icons/ReloadOutlined";
import AppToolbar from "./AppToolbar.jsx";
import { ConfigConsoleButton } from "./ConfigConsole.jsx";
import * as api from "./api.js";
import {
  ACTIVE_JOB_STATUSES, RUN_LABELS, displayNumber, displayPercent, experimentSummary,
  finiteNumber, gateLabel, parseJsonObject, reasonText, researchQueryKey, safeSourceUrl, statusInfo,
} from "./researchViewModel.js";
import "./ResearchLab.css";

const { Title, Text, Paragraph } = Typography;
const EMPTY = [];
const STRATEGY_TEMPLATE = {
  version: 1, name: "均线趋势候选", hypothesis: "收盘价高于过去 20 日均线时，观察未来 10 个交易日的延续性。",
  applicability: "A 股日线，仅用于研究与模拟", failure_conditions: ["震荡市场频繁反转"],
  entry: { op: "gt", args: [{ op: "field", field: "c" }, { op: "sma", window: 20, args: [{ op: "field", field: "c" }] }] },
  holding_days: 10, rebalance_days: 5, max_positions: 5, max_symbol_weight: 0.2, max_position_pct: 1,
};
const IMPORT_TEMPLATE = { bundles: {}, metadata: { source: "", calendar: [], instrument_history: [], corporate_actions: [], coverage: { start: "", end: "", symbols: [], corporate_actions_complete: false, instrument_history_complete: false } } };
const dateText = (value, short = false) => {
  if (!value) return "—";
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString("zh-CN", short ? { year: "numeric", month: "2-digit", day: "2-digit" } : { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
};
const shortId = (value) => value ? String(value).slice(0, 10) : "—";

function StatusTag({ status }) {
  const [label, color] = statusInfo(status);
  return <Tag color={color}>{label}</Tag>;
}
function Issues({ values = EMPTY, empty = "暂无阻断原因", limit = 6 }) {
  const items = Array.isArray(values) ? values : [values];
  if (!items.length) return <Text type="secondary">{empty}</Text>;
  return <div className="research-issues"><ul>{items.slice(0, limit).map((item, i) => <li key={`${i}-${String(item)}`}>{reasonText(item)}</li>)}</ul>{items.length > limit ? <Text type="secondary">另 {items.length - limit} 项，见完整记录。</Text> : null}</div>;
}
function JsonEvidence({ value, label = "原始证据" }) {
  return <Collapse size="small" className="research-evidence" items={[{ key: "evidence", label, children: <pre className="research-json">{JSON.stringify(value, null, 2)}</pre> }]} />;
}
function Quality({ quality, strict }) {
  if (!quality) return <Tag>尚未检查</Tag>;
  return quality.eligible === true && strict !== false ? <Tag color="green">证据完整</Tag> : <Tag color="gold">探索性数据 · 待补证据</Tag>;
}
function SourceLink({ href, children }) {
  const url = safeSourceUrl(href);
  return url ? <a href={url} target="_blank" rel="noopener noreferrer">{children}</a> : <Text>{children}</Text>;
}
function useModalDetailHandoff(closeModal, openDetail) {
  // Each Modal owns its pending detail; one close callback cannot consume the
  // other Modal's request. Clear before opening so callbacks are safe to repeat.
  const pendingDetail = useRef(null);
  return {
    reset: () => { pendingDetail.current = null; },
    openAfterClose: (detail) => { pendingDetail.current = detail; closeModal(); },
    afterClose: () => {
      const nextDetail = pendingDetail.current;
      pendingDetail.current = null;
      // Modal has restored trigger focus before afterClose. Drawer can now
      // receive focus and handle Escape without a competing focus restoration.
      if (nextDetail) openDetail(nextDetail);
    },
  };
}
function BudgetCard({ budget, onSettings, disabled }) {
  return <Card size="small" title="研究预算" className="research-card research-budget-card" extra={<Button type="text" size="small" onClick={onSettings} disabled={disabled}>调整预算</Button>}><div className="research-budget">
    {[["calls", "今日调用预算", "次"], ["tokens", "今日 Token 预算", ""], ["candidates", "本周候选预算", "个"]].map(([key, label, suffix]) => {
      const used = finiteNumber(budget?.used?.[key]);
      const limit = finiteNumber(budget?.limits?.[key]);
      const percent = used !== null && limit !== null ? (limit > 0 ? Math.min(100, used / limit * 100) : 100) : 0;
      return <div key={key}><div className="research-between"><Text>{label}</Text><Text type="secondary">{displayNumber(used, 0)} / {displayNumber(limit, 0)} {suffix}</Text></div><Progress percent={percent} showInfo={false} size="small" status={limit !== null && used !== null && used >= limit ? "exception" : "normal"} /></div>;
    })}
    <Text type="secondary" className="research-caption">按上海时间重置，含执行中预留。</Text>
  </div></Card>;
}
function MetricGrid({ metrics }) {
  return <div className="research-metric-grid">{[
    ["平均净收益", displayPercent(metrics?.mean_return_pct)], ["平均超额收益", displayPercent(metrics?.mean_excess_return_pct)],
    ["Sharpe 中位数", displayNumber(metrics?.median_sharpe)], ["最大回撤", displayPercent(metrics?.max_drawdown_pct)],
    ["完整交易数", displayNumber(metrics?.completed_trade_count, 0)], ["双倍成本累计收益", displayPercent(metrics?.double_cost_aggregate_return_pct)],
  ].map(([label, value]) => <div key={label}><Text type="secondary">{label}</Text><strong>{value}</strong></div>)}</div>;
}
function GateList({ gates }) {
  if (!gates || !Object.keys(gates).length) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无验证结果" />;
  return <div className="research-gates">{Object.entries(gates).map(([key, passed]) => <div key={key}><Badge status={passed === true ? "success" : "warning"} /><span>{gateLabel(key)}</span><Text type={passed === true ? "success" : "warning"}>{passed === true ? "通过" : "未通过"}</Text></div>)}</div>;
}
function ExperimentDetail({ entity }) {
  const payload = entity?.payload || {};
  const report = payload.report;
  if (!report) return <Space direction="vertical" className="research-full"><Alert type="warning" showIcon message="暂无评估报告" description={payload.error || "暂无评估结果。"} /><JsonEvidence value={entity} label="失败尝试与模型输出" /></Space>;
  const foldColumns = [
    { title: "窗口", key: "window", render: (_, row) => <span>{dateText(row.validation?.start, true)} — {dateText(row.validation?.end, true)}</span>, width: 220 },
    { title: "候选净收益", key: "return", render: (_, row) => displayPercent(row.candidate?.performance?.total_return_pct) },
    { title: "等权基准", key: "baseline", render: (_, row) => displayPercent(row.baselines?.equal_weight?.performance?.total_return_pct) },
    { title: "双倍成本", key: "stress", render: (_, row) => displayPercent(row.cost_stress?.performance?.total_return_pct) },
    { title: "最大回撤", key: "drawdown", render: (_, row) => displayPercent(row.candidate?.performance?.max_drawdown_pct) },
  ];
  return <Space direction="vertical" size={18} className="research-full">
    <Alert type={report.eligible === true ? "info" : "warning"} showIcon message={report.eligible === true ? "历史验证通过" : "历史验证未通过"} />
    <MetricGrid metrics={report.metrics} />
    <Issues values={report.issues} empty="本轮没有阻断项" limit={30} />
    <GateList gates={report.gates} />
    <Table rowKey={(row) => row.index ?? row.validation?.start} columns={foldColumns} dataSource={report.folds || EMPTY} pagination={false} size="small" scroll={{ x: 720 }} locale={{ emptyText: "历史不足，尚无滚动验证窗口" }} />
    <Descriptions size="small" column={2} items={[
      { key: "strategy", label: "策略版本", children: shortId(payload.strategy_id) }, { key: "snapshot", label: "数据快照", children: shortId(payload.snapshot_id) },
      { key: "created", label: "评估时间", children: dateText(entity.created_at) }, { key: "capital", label: "起始资金", children: displayNumber(payload.initial_cash) },
    ]} />
    <JsonEvidence value={entity} label="完整评估记录与数据依据" />
  </Space>;
}
function PaperAccount({ title, account, embedded = false }) {
  return <section className={`research-account${embedded ? " research-account-embedded" : ""}`} aria-label={title}>
    <h3 className="research-account-title">{title}</h3>
    {!account ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="尚未建立模拟记录" /> : <Space direction="vertical" size={12} className="research-full">
      <div className="research-between"><Text strong>{account.name || shortId(account.strategy_id)}</Text><StatusTag status={account.status} /></div>
      <div className="research-metric-grid"><div><Text type="secondary">账户权益</Text><strong>¥ {displayNumber(account.equity)}</strong></div><div><Text type="secondary">现金</Text><strong>¥ {displayNumber(account.cash)}</strong></div><div><Text type="secondary">观察交易日</Text><strong>{displayNumber(account.session_count, 0)}</strong></div><div><Text type="secondary">完整交易</Text><strong>{displayNumber(account.completed_trades, 0)}</strong></div></div>
      <Descriptions size="small" column={2} items={[{ key: "ret", label: "净收益", children: displayPercent(account.performance?.total_return_pct) }, { key: "dd", label: "最大回撤", children: displayPercent(account.performance?.max_drawdown_pct) }]} />
      {account.quality?.issues?.length ? <Issues values={account.quality.issues} /> : null}
      <JsonEvidence value={{ positions: account.positions, updated_at: account.updated_at, quality: account.quality }} label="持仓与观察依据" />
    </Space>}
  </section>;
}

export default function ResearchLab() {
  const { user } = useAuth();
  return <ResearchWorkspace key={user?.id ?? "anonymous"} />;
}

function ResearchWorkspace() {
  const { user, authRequired, logout } = useAuth();
  const { message } = AntApp.useApp();
  const queryClient = useQueryClient();
  const uid = user?.id;
  const [tab, setTab] = useState("progress");
  const [detail, setDetail] = useState(null);
  const [selectedExperiments, setSelectedExperiments] = useState([]);
  const [compareOpen, setCompareOpen] = useState(false);
  const [jsonModal, setJsonModal] = useState(null);
  const [jsonText, setJsonText] = useState("");
  const [jsonError, setJsonError] = useState("");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsForm] = Form.useForm();
  const jsonHandoff = useModalDetailHandoff(() => setJsonModal(null), setDetail);
  const comparisonHandoff = useModalDetailHandoff(() => setCompareOpen(false), setDetail);

  const overview = useQuery({
    queryKey: researchQueryKey(uid), enabled: uid != null,
    queryFn: ({ signal }) => api.getResearchOverview({ signal }), staleTime: 5000,
    refetchInterval: (query) => query.state.data?.jobs?.some((job) => ACTIVE_JOB_STATUSES.has(job.status)) ? 4000 : 20000,
    refetchIntervalInBackground: false,
  });
  const data = overview.data;
  const config = data?.config || {};
  const jobs = data?.jobs || EMPTY;
  const strategies = data?.strategies || EMPTY;
  const experiments = data?.experiments || EMPTY;
  const snapshot = data?.data;
  const snapshotPayload = snapshot?.payload;
  const paused = config.paused === true || config.enabled === false;
  const strategyNames = useMemo(() => new Map(strategies.map((s) => [s.id, s.payload?.spec?.name || shortId(s.id)])), [strategies]);
  const activeJobs = jobs.filter((job) => ACTIVE_JOB_STATUSES.has(job.status));
  const workerAvailable = ["idle", "busy"].includes(data?.worker?.status) && Date.now() / 1000 - Number(data?.worker?.seen_at || 0) < 150;
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["research", uid] });
  const run = useMutation({
    mutationFn: ({ kind, payload, key }) => api.startResearchRun(kind, payload, key),
    onSuccess: (job) => { message.info("已加入任务队列。"); setTab("progress"); void refresh(); if (job?.id) setDetail({ kind: "job", id: job.id }); },
    onError: (error) => message.error(api.getApiErrorMessage(error)),
  });
  const configure = useMutation({ mutationFn: api.updateResearchConfig, onSuccess: () => { setSettingsOpen(false); void refresh(); }, onError: (error) => message.error(api.getApiErrorMessage(error)) });
  const saveJson = useMutation({
    mutationFn: ({ kind, value }) => kind === "strategy" ? api.createResearchStrategy(value) : api.importResearchSnapshot(value),
    onSuccess: (entity, variables) => {
      jsonHandoff.openAfterClose({ kind: variables.kind === "strategy" ? "strategy" : "snapshot", id: entity.id });
      setJsonError("");
      void refresh();
      message.success(variables.kind === "strategy" ? "策略版本已保存，等待验证。" : "数据已归档。");
    },
    onError: (error) => setJsonError(Array.isArray(error.response?.data?.detail) ? error.response.data.detail.slice(0, 5).map((item) => `${(item.loc || []).join(".")}: ${item.msg}`).join("；") : api.getApiErrorMessage(error)),
  });
  const detailQuery = useQuery({
    queryKey: researchQueryKey(uid, detail?.kind || "detail", detail?.id || ""), enabled: uid != null && Boolean(detail?.id),
    queryFn: ({ signal }) => ({ experiment: api.getResearchExperiment, strategy: api.getResearchStrategy, snapshot: api.getResearchSnapshot, job: api.getResearchJob }[detail.kind])(detail.id, { signal }),
    refetchInterval: (query) => detail?.kind === "job" && ACTIVE_JOB_STATUSES.has(query.state.data?.status) ? 3000 : false,
  });
  const enqueue = (kind, payload = {}) => run.mutate({ kind, payload, key: `ui:${crypto.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`}` });
  const openJson = (kind) => { jsonHandoff.reset(); setJsonModal(kind); setJsonError(""); setJsonText(JSON.stringify(kind === "strategy" ? STRATEGY_TEMPLATE : IMPORT_TEMPLATE, null, 2)); };
  const submitJson = () => {
    try {
      const value = parseJsonObject(jsonText);
      if (jsonModal === "import" && (!value.bundles || !Object.keys(value.bundles).length || !value.metadata)) throw new Error("请提供至少一个标的的 bundles 和 metadata。具体字段见数据导入要求。");
      setJsonError(""); saveJson.mutate({ kind: jsonModal, value });
    } catch (error) { setJsonError(error.message); }
  };
  const view = (kind, id) => setDetail({ kind, id });
  const openSettings = () => { settingsForm.setFieldsValue(config); setSettingsOpen(true); };
  const canRun = Boolean(data) && !paused && !run.isPending;
  const errors = Array.isArray(data?.errors) ? data.errors : data?.errors ? Object.values(data.errors) : EMPTY;

  const jobColumns = [
    { title: "任务", dataIndex: "kind", render: (kind, row) => <Button type="link" className="research-table-link" onClick={() => view("job", row.id)}>{RUN_LABELS[kind] || kind}</Button> },
    { title: "状态", dataIndex: "status", render: (status) => <StatusTag status={status} /> },
    { title: "尝试", key: "attempts", render: (_, row) => `${row.attempts ?? 0} / ${row.max_attempts ?? "—"}` },
    { title: "最近更新", dataIndex: "updated_at", render: (value) => dateText(value) },
    { title: "结果 / 原因", key: "result", render: (_, row) => <Text type={row.error ? "warning" : "secondary"}>{row.error || row.result?.reason || row.result?.error || (row.result?.status ? statusInfo(row.result.status)[0] : row.status === "completed" ? "查看执行记录" : "等待结果")}</Text>, ellipsis: true },
  ];
  const strategyColumns = [
    { title: "策略", key: "name", render: (_, row) => <div><Button type="link" className="research-table-link" onClick={() => view("strategy", row.id)}>{row.payload?.spec?.name || shortId(row.id)}</Button><div className="research-caption">{row.payload?.origin === "manual" ? "手动创建" : "研究候选"} · {dateText(row.created_at)}</div></div>, width: 210 },
    { title: "研究假设", key: "hypothesis", render: (_, row) => <Paragraph className="research-no-margin" ellipsis={{ rows: 2, tooltip: true }}>{row.payload?.spec?.hypothesis || "尚未填写"}</Paragraph> },
    { title: "单标的上限", key: "weight", render: (_, row) => displayPercent(finiteNumber(row.payload?.spec?.max_symbol_weight) === null ? null : row.payload.spec.max_symbol_weight * 100), width: 115 },
    { title: "操作", key: "actions", render: (_, row) => <Space><Button size="small" disabled={!canRun || !snapshot} onClick={() => enqueue("evaluate", { strategy_id: row.id })}>验证策略</Button><Button type="text" size="small" onClick={() => view("strategy", row.id)}>详情</Button></Space>, width: 180 },
  ];
  const experimentColumns = [
    { title: "策略 / 实验", key: "strategy", render: (_, row) => <div><Button type="link" className="research-table-link" onClick={() => view("experiment", row.id)}>{strategyNames.get(row.payload?.strategy_id) || shortId(row.payload?.strategy_id)}</Button><div className="research-caption">{dateText(row.created_at)}</div></div>, width: 190 },
    { title: "验证结果", key: "eligible", render: (_, row) => row.payload?.status === "failed" ? <Tag color="error">评估失败</Tag> : <Tag color={experimentSummary(row).eligible === true ? "blue" : "gold"}>{experimentSummary(row).eligible === true ? "历史验证通过" : "待补证据 / 未通过"}</Tag>, width: 155 },
    { title: "平均净收益", key: "return", render: (_, row) => displayPercent(experimentSummary(row).metrics?.mean_return_pct), width: 120 },
    { title: "超额收益", key: "excess", render: (_, row) => displayPercent(experimentSummary(row).metrics?.mean_excess_return_pct), width: 115 },
    { title: "Sharpe 中位数", key: "sharpe", render: (_, row) => displayNumber(experimentSummary(row).metrics?.median_sharpe), width: 130 },
    { title: "最大回撤", key: "drawdown", render: (_, row) => displayPercent(experimentSummary(row).metrics?.max_drawdown_pct), width: 115 },
    { title: "主要原因", key: "issues", render: (_, row) => <Text type="secondary">{row.payload?.error || (experimentSummary(row).issues || []).slice(0, 2).map(reasonText).join("；") || "查看完整验证记录"}</Text>, width: 240 },
  ];
  const comparison = experiments.filter((e) => selectedExperiments.includes(e.id));
  const paper = data?.paper || {};
  const observingCount = (paper.shadows || []).filter((shadow) => ["observing", "shadow"].includes(shadow.status)).length;
  const promotions = data?.promotions || paper.events || EMPTY;

  const progressPanel = <div className="research-dashboard-grid">
    <Card title="任务进度" className="research-card research-table-panel" extra={<span className="research-caption">{activeJobs.length} 项待处理</span>}>
      <Table rowKey="id" dataSource={jobs} columns={jobColumns} pagination={{ pageSize: 8, hideOnSinglePage: true }} size="small" scroll={{ x: 720 }} locale={{ emptyText: <Empty description="暂无任务，先采集自选股历史数据" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }} />
    </Card>
    <aside className="research-stack" aria-label="研究预算与说明">
      <BudgetCard budget={data?.budget} onSettings={openSettings} disabled={!data} />
      <div className="research-note"><Text strong>工作方式</Text><Paragraph>历史验证通过后进入模拟观察。</Paragraph><Paragraph type="secondary">失败实验同样归档。</Paragraph></div>
    </aside>
  </div>;
  const dataPanel = <div className="research-dashboard-grid">
    <Card title="当前数据覆盖" className="research-card" extra={snapshot ? <Button size="small" onClick={() => view("snapshot", snapshot.id)}>查看快照</Button> : null}>
      {!snapshotPayload ? <Empty description="暂无研究数据，点击「立即采集」获取自选股历史。" /> : <Space direction="vertical" size={18} className="research-full">
        <Descriptions column={{ xs: 1, sm: 2 }} items={[
          { key: "range", label: "历史区间", children: `${snapshotPayload.start || "—"} — ${snapshotPayload.end || "—"}` },
          { key: "symbols", label: "覆盖标的", children: `${snapshotPayload.symbols?.length ?? "—"} 个` },
          { key: "rows", label: "归档行情", children: `${displayNumber(snapshotPayload.row_count, 0)} 条` },
          { key: "quality", label: "证据状态", children: <Quality quality={snapshotPayload.quality} strict={snapshotPayload.strict} /> },
        ]} />
        <div className="research-symbols">{snapshotPayload.symbols?.map((symbol) => <Tag key={symbol}>{symbol}</Tag>)}</div>
        <div className="research-data-checks">
          <Alert showIcon type={snapshotPayload.strict ? "info" : "warning"} message={snapshotPayload.strict ? "历史证据完整。" : "可探索回测；晋升需补齐历史证据。"} />
          <Issues values={snapshotPayload.quality?.issues || snapshotPayload.blockers} />
        </div>
        <Text type="secondary" className="research-caption">实验固定使用当时的快照。</Text>
      </Space>}
    </Card>
    <Card title="补充历史证据" className="research-card">
      <Space direction="vertical" size={16} className="research-full">
        <Paragraph className="research-no-margin" type="secondary">导入历史证据，自动检查交易状态、涨跌停、公司行动与可用时间。</Paragraph>
        <Button icon={<DownloadOutlined />} onClick={() => openJson("import")}>导入 JSON 数据</Button>
        <JsonEvidence value={data?.import_requirements || { status: "加载中" }} label="字段与覆盖要求" />
      </Space>
    </Card>
  </div>;
  const strategyPanel = <div className="research-panel-stack">
    <Card title="候选策略" className="research-card research-table-panel" extra={<Button size="small" icon={<PlusOutlined />} onClick={() => openJson("strategy")}>创建策略</Button>}>
      <Table rowKey="id" columns={strategyColumns} dataSource={strategies} pagination={{ pageSize: 6, hideOnSinglePage: true }} scroll={{ x: 780 }} size="small" locale={{ emptyText: "暂无候选。采集后开始研究，或手动创建策略。" }} />
    </Card>
    <Card title="实验比较" className="research-card research-table-panel" extra={<Button size="small" type={comparison.length >= 2 ? "primary" : "default"} disabled={comparison.length < 2} onClick={() => { comparisonHandoff.reset(); setCompareOpen(true); }}>比较所选（{comparison.length}/4）</Button>}>
      <Paragraph type="secondary" className="research-table-description">按同一数据快照比较实验。</Paragraph>
      <Table rowKey="id" dataSource={experiments} columns={experimentColumns} rowSelection={{ selectedRowKeys: selectedExperiments, onChange: setSelectedExperiments, getCheckboxProps: (row) => ({ disabled: selectedExperiments.length >= 4 && !selectedExperiments.includes(row.id) }) }} pagination={{ pageSize: 8, hideOnSinglePage: true }} scroll={{ x: 1100 }} size="small" locale={{ emptyText: "暂无实验结果" }} />
    </Card>
  </div>;
  const paperPanel = <div className="research-panel-stack">
    <div className="research-two-col">
      <PaperAccount title="现行模拟账户" account={paper.active} />
      <Card title="更新与回滚" size="small" className="research-card">
        <Space direction="vertical" size={16} className="research-full">
          <Paragraph className="research-no-margin" type="secondary">候选、现行策略与等权基准同期独立观察；晋升时切换现行策略。</Paragraph>
          <Space wrap>
            <Button disabled={!canRun} onClick={() => enqueue("paper")}>更新模拟记录</Button>
            <Popconfirm title="回滚到上一策略？" description="提交回滚任务，历史记录保留。" onConfirm={() => enqueue("rollback", paper.previous?.strategy_id ? { strategy_id: paper.previous.strategy_id } : {})} okText="提交回滚" cancelText="取消"><Button disabled={!canRun || !paper.previous} danger>回滚上一版本</Button></Popconfirm>
          </Space>
          <Text type="secondary" className="research-caption">{paper.previous ? `上一版本：${paper.previous.name || shortId(paper.previous.strategy_id)}` : "尚无可回滚的历史版本"}</Text>
        </Space>
      </Card>
    </div>
    {(paper.shadows || []).map((shadow) => <Card key={shadow.id} title={shadow.name || strategyNames.get(shadow.strategy_id) || "候选模拟观察"} extra={<StatusTag status={shadow.status} />} className="research-card research-shadow-panel">
      <div className="research-three-col">
        <PaperAccount title="候选策略" account={shadow.candidate} embedded />
        <PaperAccount title="现行策略对照" account={shadow.incumbent} embedded />
        <PaperAccount title="等权基准" account={shadow.baseline} embedded />
      </div>
      <div className="research-observation"><Text strong>前向观察证据</Text>{shadow.evaluation ? <><Issues values={shadow.evaluation.issues} empty="本轮无阻断项" /><GateList gates={shadow.evaluation.gates} /></> : <Paragraph type="secondary">观察记录待积累。</Paragraph>}</div>
    </Card>)}
    {!paper.active && !(paper.shadows || []).length ? <Alert type="info" showIcon message="尚无模拟观察记录" description="候选进入模拟观察后显示账户与对照。" /> : null}
    <Card title="晋升与回滚记录" className="research-card research-table-panel">
      <Table rowKey="id" dataSource={promotions} pagination={{ pageSize: 6, hideOnSinglePage: true }} size="small" columns={[{ title: "时间", dataIndex: "created_at", render: (v) => dateText(v) }, { title: "状态", key: "status", render: (_, row) => <StatusTag status={row.payload?.status || row.payload?.action} /> }, { title: "依据", key: "reason", render: (_, row) => row.payload?.reason || (row.payload?.issues || []).map(reasonText).join("；") || "查看决策证据" }, { title: "记录", key: "evidence", render: (_, row) => <JsonEvidence value={row.payload} label="决策证据" /> }]} scroll={{ x: 700 }} locale={{ emptyText: "尚未发生晋升或回滚" }} />
    </Card>
  </div>;
  const knowledgePanel = <Card title="可追溯的学习来源" className="research-card research-library" extra={<Button size="small" icon={<ReloadOutlined />} disabled={!canRun} onClick={() => enqueue("github")}>刷新 GitHub 来源</Button>}>
    <Paragraph type="secondary" className="research-library-description">来源材料用于形成研究假设。</Paragraph>
    {!(data?.knowledge || []).length ? <Empty description="暂无学习材料，刷新 GitHub 来源后查看。" /> : <div className="research-source-list" role="list">{data.knowledge.map((entity) => {
      const k = entity.payload || {};
      return <article className="research-source-row" role="listitem" key={entity.id}>
        <div className="research-source-identity"><div className="research-source-title"><GithubOutlined /><h3><SourceLink href={k.url}>{k.repository || "研究来源"}</SourceLink></h3></div><Text type="secondary" className="research-caption">归档于 {dateText(k.observed_at || entity.created_at)}</Text><Text type="secondary" className="research-caption">版本 <code>{shortId(k.commit)}</code></Text></div>
        <div className="research-source-content"><Text strong>{k.purpose || k.path}</Text><Paragraph className="research-no-margin" type="secondary">{k.hypothesis || k.applicability}</Paragraph><Space wrap size={16}><SourceLink href={k.url}>阅读原文</SourceLink><SourceLink href={k.license_url}>许可说明</SourceLink><Tag>待本地验证</Tag></Space><JsonEvidence value={{ path: k.path, headings: k.headings, excerpt: k.excerpt, content_hash: k.content_hash }} label="归档摘要与来源依据" /></div>
      </article>;
    })}</div>}
  </Card>;

  return <Layout className="sva-layout research-layout">
    <AppToolbar active="research" settingsAction={<ConfigConsoleButton isAdmin={user?.role === "admin"} />} user={user} authRequired={authRequired} onLogout={logout} />
    <Layout.Content className="research-content">
      <header className="research-heading">
        <div className="research-heading-copy"><Title level={1}>研究实验室</Title><Paragraph type="secondary">采集数据、验证策略、跟踪模拟。</Paragraph><div className="research-heading-status"><Badge status={!data ? "default" : paused ? "warning" : "processing"} text={!data ? "连接中" : paused ? "自动研究已暂停" : "自动研究已启用"} /><span className="research-caption">{overview.dataUpdatedAt ? `最近同步 ${new Date(overview.dataUpdatedAt).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}` : "等待本地服务"}</span><span className="research-caption">研究与模拟</span></div></div>
        <div className="research-heading-actions"><Button icon={<ReloadOutlined />} onClick={() => void overview.refetch()} loading={overview.isFetching}>刷新</Button><Button disabled={!data || configure.isPending} onClick={() => configure.mutate({ paused: !paused, ...(!config.enabled && paused ? { enabled: true } : {}) })}>{paused ? "恢复自动研究" : "暂停自动研究"}</Button><Button icon={<DownloadOutlined />} disabled={!canRun} onClick={() => enqueue("collect")}>立即采集</Button><Tooltip title={!snapshot ? "先采集或导入历史数据" : undefined}><Button type="primary" loading={run.isPending && run.variables?.kind === "research"} disabled={!canRun || !snapshot} onClick={() => enqueue("research")}>开始研究</Button></Tooltip></div>
      </header>
    {overview.isError ? <Alert type="warning" showIcon className="research-banner" message="研究状态更新失败" description={`${api.getApiErrorMessage(overview.error)}${data ? " 显示上次同步记录。" : ""}`} action={<Button size="small" onClick={() => void overview.refetch()}>重新连接</Button>} /> : null}
    {errors.length ? <Alert type="warning" showIcon className="research-banner" message="部分研究数据读取失败" description={<Issues values={errors.map((error) => typeof error === "string" ? error : error.message || error.error || JSON.stringify(error))} />} action={<Button size="small" onClick={() => void overview.refetch()}>重试</Button>} /> : null}
    {data && config.enabled && activeJobs.length > 0 && !workerAvailable ? <Alert type="warning" showIcon className="research-banner" message="研究执行器未连接，任务等待执行。" description="通过本地启动脚本或 Docker Compose 启动研究执行器。" /> : null}
    {paused && data ? <Alert type="info" showIcon className="research-banner" message="自动研究已暂停。" /> : null}
    {overview.isPending ? <div className="research-loading"><Spin size="large" aria-label="读取研究记录" /></div> : <>
      <section className="research-summary-band" aria-label="研究概况">
        <div className="research-summary-item"><span className="research-summary-label">已归档标的</span><strong>{snapshotPayload?.symbols?.length ?? "—"}</strong><span className="research-caption">{snapshotPayload ? `${snapshotPayload.start || "—"} — ${snapshotPayload.end || "—"}` : "等待首次采集"}</span></div>
        <div className="research-summary-item"><span className="research-summary-label">近期候选与实验</span><strong>{data ? strategies.length : "—"} <small>/ {data ? experiments.length : "—"}</small></strong><span className="research-caption">记录结果与原因</span></div>
        <div className="research-summary-item"><span className="research-summary-label">正在模拟观察</span><strong>{data ? observingCount : "—"}</strong><span className="research-caption">{paper.active ? `现行：${paper.active.name || shortId(paper.active.strategy_id)}` : "尚未建立现行模拟账户"}</span></div>
        <div className="research-summary-item"><span className="research-summary-label">当前数据质量</span><div className="research-quality-summary"><Quality quality={snapshotPayload?.quality} strict={snapshotPayload?.strict} /></div><span className="research-caption">{snapshotPayload?.quality?.issues?.length ? `${snapshotPayload.quality.issues.length} 项证据待补充` : snapshotPayload ? "查看快照检查结果" : "等待数据归档"}</span></div>
      </section>
      <Tabs activeKey={tab} onChange={setTab} className="research-tabs" items={[{ key: "progress", label: "研究进展", children: progressPanel }, { key: "data", label: "数据资产", children: dataPanel }, { key: "strategies", label: "策略与实验", children: strategyPanel }, { key: "paper", label: "模拟与晋升", children: paperPanel }, { key: "knowledge", label: "学习来源", children: knowledgePanel }]} />
    </>}

    </Layout.Content>
    <Drawer rootClassName="research-overlay" title={{ experiment: "实验验证详情", strategy: "策略版本", snapshot: "数据快照", job: "任务进度" }[detail?.kind] || "详情"} open={Boolean(detail)} onClose={() => setDetail(null)} width="min(920px, 96vw)" destroyOnHidden>
      {detailQuery.isPending ? <Spin /> : detailQuery.isError ? <Alert type="warning" message={api.getApiErrorMessage(detailQuery.error)} action={<Button onClick={() => void detailQuery.refetch()}>重试</Button>} /> : detailQuery.data ? detail?.kind === "experiment" ? <ExperimentDetail entity={detailQuery.data} /> : detail?.kind === "strategy" ? <Space direction="vertical" size={16} className="research-full"><Title level={4}>{detailQuery.data.payload?.spec?.name}</Title><Paragraph>{detailQuery.data.payload?.spec?.hypothesis}</Paragraph><Descriptions size="small" column={2} items={[{ key: "holding", label: "持有期限", children: `${detailQuery.data.payload?.spec?.holding_days ?? "—"} 个交易日` }, { key: "rebalance", label: "调仓间隔", children: `${detailQuery.data.payload?.spec?.rebalance_days ?? "—"} 个交易日` }, { key: "timing", label: "信号与成交", span: 2, children: "收盘形成信号，下一交易日开盘执行" }]} /><Text strong>失效条件</Text><Issues values={detailQuery.data.payload?.spec?.failure_conditions} empty="尚未填写" /><JsonEvidence value={detailQuery.data.payload?.spec} label="策略规则 JSON" /></Space> : detail?.kind === "job" ? <Space direction="vertical" size={14} className="research-full"><StatusTag status={detailQuery.data.status} /><Descriptions column={1} items={[{ key: "kind", label: "任务", children: RUN_LABELS[detailQuery.data.kind] || detailQuery.data.kind }, { key: "attempts", label: "执行次数", children: `${detailQuery.data.attempts} / ${detailQuery.data.max_attempts}` }, { key: "update", label: "最近更新", children: dateText(detailQuery.data.updated_at) }]} />{detailQuery.data.error ? <Alert type="warning" message={detailQuery.data.error} /> : null}{detailQuery.data.result ? <JsonEvidence value={detailQuery.data.result} label="执行结果" /> : <Text type="secondary">暂无任务结果。</Text>}<JsonEvidence value={detailQuery.data.payload} label="任务输入" /></Space> : <Space direction="vertical" size={16} className="research-full"><Quality quality={detailQuery.data.payload?.quality} strict={detailQuery.data.payload?.strict} /><Issues values={detailQuery.data.payload?.quality?.issues} limit={50} /><JsonEvidence value={detailQuery.data} label="快照覆盖与证据清单" /></Space> : null}
    </Drawer>
    <Modal rootClassName="research-overlay" title="实验对照" open={compareOpen} afterClose={comparisonHandoff.afterClose} onCancel={() => setCompareOpen(false)} footer={null} width="min(1100px, 96vw)"><Alert type="info" showIcon message={new Set(comparison.map((e) => e.payload?.snapshot_id)).size > 1 ? "快照不同，数据范围或修订可能影响比较。" : "使用同一数据快照。"} /><div className="research-comparison">{comparison.map((entity) => <Card size="small" key={entity.id} title={strategyNames.get(entity.payload?.strategy_id) || shortId(entity.id)}><MetricGrid metrics={experimentSummary(entity).metrics} /><Issues values={experimentSummary(entity).issues} /><Button type="link" onClick={() => comparisonHandoff.openAfterClose({ kind: "experiment", id: entity.id })}>完整验证证据</Button></Card>)}</div></Modal>
    <Modal rootClassName="research-overlay" title={jsonModal === "strategy" ? "创建策略版本" : "导入历史数据快照"} open={Boolean(jsonModal)} afterClose={jsonHandoff.afterClose} onCancel={() => !saveJson.isPending && setJsonModal(null)} onOk={submitJson} confirmLoading={saveJson.isPending} okText={jsonModal === "strategy" ? "保存策略" : "导入并检查"} cancelText="取消" width="min(820px, 96vw)" destroyOnHidden>
      <Paragraph type="secondary">{jsonModal === "strategy" ? "保存为独立版本，再用归档数据验证。" : "粘贴 JSON 或选择文件，导入后检查数据质量。"}</Paragraph>
      {jsonError ? <Alert type="error" showIcon message={jsonError} className="research-banner" /> : null}
      {jsonModal === "import" ? <Space direction="vertical" className="research-full"><label className="research-file-label">选择 JSON 文件<input aria-label="选择历史数据 JSON 文件" type="file" accept=".json,application/json" onChange={async (event) => { const file = event.target.files?.[0]; if (!file) return; if (file.size > 20 * 1024 * 1024) { setJsonError("单个文件最多 20 MB，请缩小导入范围或选择较小文件。"); return; } try { setJsonText(await file.text()); setJsonError(""); } catch { setJsonError("无法读取文件，请重新选择。"); } }} /></label><JsonEvidence value={data?.import_requirements} label="导入要求" /></Space> : null}
      <Input.TextArea className="research-editor" aria-label={jsonModal === "strategy" ? "策略规则 JSON" : "历史数据 JSON"} value={jsonText} onChange={(event) => setJsonText(event.target.value)} autoSize={{ minRows: 14, maxRows: 24 }} spellCheck={false} />
    </Modal>
    <Modal rootClassName="research-overlay" title="研究预算" open={settingsOpen} onCancel={() => setSettingsOpen(false)} onOk={() => settingsForm.submit()} confirmLoading={configure.isPending} okText="保存" cancelText="取消"><Form form={settingsForm} layout="vertical" onFinish={(values) => configure.mutate({ enabled: values.enabled, daily_calls: values.daily_calls, daily_tokens: values.daily_tokens, weekly_candidates: values.weekly_candidates })}><Form.Item name="enabled" label="启用自动研究" valuePropName="checked"><Switch /></Form.Item><Form.Item name="daily_calls" label="每日模型调用上限" rules={[{ required: true }]}><InputNumber min={0} precision={0} className="research-full" /></Form.Item><Form.Item name="daily_tokens" label="每日总 Token 上限（输入 + 输出）" rules={[{ required: true }]}><InputNumber min={0} step={10000} precision={0} className="research-full" /></Form.Item><Form.Item name="weekly_candidates" label="每周候选上限" rules={[{ required: true }]}><InputNumber min={0} precision={0} className="research-full" /></Form.Item></Form><Divider /><Descriptions column={1} size="small" items={[{ key: "cash", label: "模拟初始资金", children: `¥ ${displayNumber(config.initial_cash)}` }, { key: "symbol", label: "单标的仓位上限", children: displayPercent(finiteNumber(config.max_symbol_weight) === null ? null : config.max_symbol_weight * 100) }, { key: "total", label: "总仓位上限", children: displayPercent(finiteNumber(config.max_position_pct) === null ? null : config.max_position_pct * 100) }]} /><Text type="secondary">现有模拟账户的初始资金不变。</Text></Modal>
  </Layout>;
}
