import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert, App as AntApp, Badge, Button, Card, Drawer, Empty, Form, Input, InputNumber,
  Layout, Popconfirm, Select, Space, Spin, Switch, Table, Tabs, Tag, Typography,
} from "antd";
import DatabaseOutlined from "@ant-design/icons/es/icons/DatabaseOutlined";
import ReloadOutlined from "@ant-design/icons/es/icons/ReloadOutlined";
import SettingOutlined from "@ant-design/icons/es/icons/SettingOutlined";
import CloudDownloadOutlined from "@ant-design/icons/es/icons/CloudDownloadOutlined";
import DeleteOutlined from "@ant-design/icons/es/icons/DeleteOutlined";
import UndoOutlined from "@ant-design/icons/es/icons/UndoOutlined";
import LinkOutlined from "@ant-design/icons/es/icons/LinkOutlined";
import DisplayRefreshControl, { useDisplayPolling } from "./DisplayRefreshControl.jsx";
import AppToolbar from "./AppToolbar.jsx";
import { ConfigConsoleButton } from "./ConfigConsole.jsx";
import { useAuth } from "./AuthContext.jsx";
import { safeUrl } from "./components/format.js";
import * as api from "./api.js";
import {
  cacheOperationNotice, cachePolicyPayload, categoryOptions, collectionConfigPayload, collectionConfigValues, collectionRuntime, sourceScheduleLabel, collectionServiceNotice, collectionStatus, collectionSubmissionNotice, DATA_LIFECYCLE_OPTIONS,
  dataCenterQueryKey, dataTime, fileQueryParams, fileRoleLabel, formatDataFiles, formatDataSize,
  quarantineSummary, retentionInfo, sourceOptions, sourceMinimumInterval,
} from "./dataCenterViewModel.js";
import "./DataCenter.css";

const { Title, Paragraph, Text } = Typography;
const EMPTY = [];
const numberText = (value) => typeof value === "number" && Number.isFinite(value) ? value.toLocaleString("zh-CN") : "—";

function QueryError({ query, title }) {
  return query.isError ? <Alert className="data-banner" type="warning" showIcon message={title || api.getApiErrorMessage(query.error)} description={title ? api.getApiErrorMessage(query.error) : undefined} action={<Button size="small" loading={query.isFetching} onClick={() => void query.refetch()}>重试</Button>} /> : null;
}

function Status({ item }) {
  const status = collectionStatus(item);
  return <Badge status={status.color} text={status.label} />;
}

function SourceName({ source }) {
  const url = safeUrl(source.url);
  return url ? <a href={url} target="_blank" rel="noopener noreferrer">{source.name || source.id} <LinkOutlined aria-hidden="true" /></a> : <span>{source.name || source.id}</span>;
}

function SourceSchedule({ source, form }) {
  const mode = Form.useWatch(["sources", source.id, "schedule_mode"], form) || "interval";
  return <div className="data-source-config" key={source.id}>
    <div className="data-source-config-name"><SourceName source={source} /><Text type="secondary">{source.description || source.category}</Text></div>
    <Form.Item name={["sources", source.id, "enabled"]} valuePropName="checked" className="data-source-switch"><Switch aria-label={`启用${source.name || source.id}`} size="small" /></Form.Item>
    <div className="data-source-schedule-fields"><Form.Item name={["sources", source.id, "schedule_mode"]}><Select aria-label={`${source.name || source.id}计划方式`} options={[{ value: "daily", label: "每日定时" }, { value: "interval", label: "按间隔" }]} /></Form.Item>
      <Form.Item hidden={mode !== "daily"} name={["sources", source.id, "daily_at"]} rules={[{ required: true, message: "请选择北京时间" }]}><Input type="time" aria-label={`${source.name || source.id}每日时间（北京）`} /></Form.Item><Form.Item hidden={mode === "daily"} name={["sources", source.id, "interval_minutes"]} rules={[{ required: true, message: "请输入间隔" }]}><InputNumber aria-label={`${source.name || source.id}采集间隔（分钟）`} min={sourceMinimumInterval(source)} max={43200} precision={0} /></Form.Item>
    </div>
  </div>;
}

function CollectionForm({ config, sources, onSave, pending }) {
  const [form] = Form.useForm();
  return <Form form={form} layout="vertical" initialValues={collectionConfigValues(config)} onFinish={onSave} className="data-config-form">
    <Alert type="info" showIcon message="本机共享采集计划" description="关闭页面后继续采集，无需配置模型；资源不足时任务顺延。" />
    <div className="data-config-switches">
      <Form.Item name="enabled" label="公共数据自动采集" valuePropName="checked"><Switch checkedChildren="开启" unCheckedChildren="关闭" /></Form.Item>
      <Form.Item name="use_watchlists" label="纳入各账户的关注股票" valuePropName="checked"><Switch /></Form.Item>
      <Form.Item name="rag_enabled" label="允许 Agent 检索已采集资料" valuePropName="checked"><Switch /></Form.Item>
    </div>
    <div className="data-config-numbers">
      <Form.Item name="daily_job_limit" label="每日采集任务上限" extra="不含海外快讯；每个任务可发送多次请求" rules={[{ required: true, message: "请输入每日任务上限" }]}><InputNumber aria-label="每日采集任务上限" min={1} max={1000} precision={0} /></Form.Item>
      <Form.Item name="max_symbols_per_cycle" label="每轮股票数" extra="超出部分顺延至后续轮次" rules={[{ required: true, message: "请输入每轮股票数" }]}><InputNumber aria-label="每轮股票数" min={1} max={30} precision={0} /></Form.Item>
      <Form.Item name="min_free_disk_mb" label="磁盘剩余空间下限（MiB）" extra="低于下限时暂停采集" rules={[{ required: true, message: "请输入磁盘空间下限" }]}><InputNumber aria-label="磁盘剩余空间下限" min={128} max={102400} precision={0} /></Form.Item>
      <Form.Item name="max_concurrent_jobs" label="每类采集并发上限" extra="公共任务与海外来源分别计数" rules={[{ required: true }]}><InputNumber aria-label="每类采集并发上限" min={1} max={3} precision={0} /></Form.Item>
      <Form.Item name="max_memory_mb" label="API 进程内存软阈值（MiB）" extra="达到阈值后暂缓新任务，运行中任务继续" rules={[{ required: true }]}><InputNumber aria-label="进程内存软阈值" min={128} max={8192} precision={0} /></Form.Item>
      <Form.Item name="job_timeout_seconds" label="单任务超时（秒）" extra="超时任务保留此前已入库数据" rules={[{ required: true }]}><InputNumber aria-label="单任务超时" min={30} max={600} precision={0} /></Form.Item>
    </div>
    <Form.Item name="symbols" label="额外关注股票" extra="最多 300 个 A 股代码，以换行或逗号分隔。"><Input.TextArea aria-label="额外关注股票" rows={4} placeholder={"600519.SS\n000001.SZ\n920002.BJ"} /></Form.Item>
    <Title level={5}>公共来源采集计划</Title>
    <Paragraph className="data-caption">每日时间为北京时间，间隔单位为分钟。</Paragraph>
    <div className="data-source-config-list">{sources.map((source) => <SourceSchedule key={source.id} source={source} form={form} />)}</div>
    <Title level={5}>海外快讯后台采集</Title><Paragraph className="data-caption">海外来源共用上述资源限制，服务需在服务器启用。</Paragraph>
    <div className="data-config-numbers"><Form.Item name="overseas_enabled" label="海外快讯自动采集" valuePropName="checked"><Switch /></Form.Item><Form.Item name="overseas_poll_interval_sec" label="海外后台检查间隔（秒）" rules={[{ required: true }]}><InputNumber aria-label="海外后台检查间隔" min={60} max={3600} precision={0} /></Form.Item></div>
    <div className="data-form-actions"><Button type="primary" htmlType="submit" loading={pending}>保存配置</Button></div>
  </Form>;
}

function FileInventory({ userId, categories, sources }) {
  const [filters, setFilters] = useState({ page: 1, pageSize: 20 });
  const params = useMemo(() => fileQueryParams(filters), [filters]);
  const files = useQuery({ queryKey: dataCenterQueryKey(userId, "files", params), queryFn: ({ signal }) => api.getLocalDataFiles(params, { signal }), refetchOnWindowFocus: false });
  const updateFilter = (key, value) => setFilters((current) => ({ ...current, [key]: value, page: 1 }));
  return <>
    <div className="data-file-filters" aria-label="文件筛选">
      <label><span>资产分类</span><Select aria-label="资产分类筛选" allowClear placeholder="全部分类" options={categoryOptions(categories)} value={filters.category} onChange={(value) => updateFilter("category", value)} /></label>
      <label><span>采集来源</span><Select aria-label="文件来源筛选" allowClear showSearch placeholder="全部来源" options={files.data?.sources ? files.data.sources.map((value) => ({ value, label: sources.find((item) => item.id === value)?.name || value })) : sourceOptions(sources)} value={filters.source} onChange={(value) => updateFilter("source", value)} /></label>
      <label><span>保留方式</span><Select aria-label="保留方式筛选" allowClear placeholder="全部方式" options={DATA_LIFECYCLE_OPTIONS} value={filters.lifecycle} onChange={(value) => updateFilter("lifecycle", value)} /></label>
      <label className="data-file-search"><span>搜索文件</span><Input.Search aria-label="搜索本地文件" placeholder="文件路径或名称" maxLength={200} allowClear onSearch={(value) => updateFilter("q", value)} enterButton /></label>
    </div>
    <QueryError query={files} title="文件索引读取失败" />
    <Table className="data-file-table" size="small" loading={files.isLoading} dataSource={files.data?.items || EMPTY} rowKey="path" scroll={{ x: 900 }}
      pagination={{ current: filters.page, pageSize: filters.pageSize, total: files.data?.total || 0, showSizeChanger: true, pageSizeOptions: [20, 50, 100], showTotal: (total) => `共 ${total} 个文件`, onChange: (page, pageSize) => setFilters((current) => ({ ...current, page: pageSize === current.pageSize ? page : 1, pageSize })) }}
      columns={[
        { title: "文件路径", key: "path", width: 340, render: (_, item) => <div className="data-file-identity"><Text className="data-file-path" copyable={{ text: item.path }}>{item.path}</Text><span>{fileRoleLabel(item)}</span></div> },
        { title: "分类", key: "category", width: 130, render: (_, item) => categories.find((entry) => entry.id === item.category)?.label || item.category_label || item.category || "其他" },
        { title: "来源", key: "source", width: 160, render: (_, item) => sources.find((entry) => entry.id === item.source)?.name || item.source || "本地生成" },
        { title: "大小", key: "size_bytes", width: 100, render: (_, item) => formatDataSize(item.size_bytes) },
        { title: "最近更新（北京）", key: "updated_at", width: 145, render: (_, item) => dataTime(item.updated_at) },
        { title: "保留方式", key: "retention", width: 130, render: (_, item) => <Space direction="vertical" size={4}><Tag color={item.protected || item.retention === "durable" ? "blue" : item.expired ? "orange" : "default"}>{item.storage_role === "quarantine" ? "可恢复回收区" : item.rebuildable ? item.expired ? "缓存已到期" : "可再生成" : item.protected ? "长期保护" : retentionInfo(item.retention)}</Tag>{item.expires_at ? <Text type="secondary" className="data-expiry">{dataTime(item.expires_at)} 到期</Text> : null}</Space> },
      ]}
      locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无匹配文件" /> }} />
    <Paragraph className="data-caption">路径相对于数据目录。</Paragraph>
  </>;
}

function CollectedRecords({ userId, sources }) {
  const [source, setSource] = useState();
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);
  const params = useMemo(() => ({ offset: (page - 1) * 20, limit: 20, ...(source ? { source_id: source } : {}), ...(search ? { q: search } : {}) }), [source, search, page]);
  const records = useQuery({ queryKey: dataCenterQueryKey(userId, "records", params), queryFn: ({ signal }) => api.getCollectedDataRecords(params, { signal }), refetchOnWindowFocus: false });
  return <>
    <div className="data-section-intro"><div><Title level={4}>已采集资料</Title><Paragraph className="data-caption">保留资料的历史版本。</Paragraph></div></div>
    <div className="data-record-filters"><label><span>采集来源</span><Select aria-label="资料来源筛选" allowClear placeholder="全部来源" options={sourceOptions(sources)} value={source} onChange={(value) => { setSource(value); setPage(1); }} /></label><label><span>搜索资料</span><Input.Search aria-label="搜索已采集资料" placeholder="资料标题或内容关键词" maxLength={500} allowClear onSearch={(value) => { setSearch(value.trim()); setPage(1); }} enterButton /></label></div>
    <QueryError query={records} title="已采集资料读取失败" />
    <Table size="small" rowKey={(item) => item.revision_id || item.citation} dataSource={records.data?.items || EMPTY} loading={records.isLoading} scroll={{ x: 850 }} pagination={{ current: page, pageSize: 20, total: records.data?.total || 0, showSizeChanger: false, showTotal: (total) => `共 ${total} 条资料`, onChange: setPage }} columns={[
      { title: "资料", key: "title", width: 330, render: (_, item) => <div className="data-record-title"><Text strong>{item.title}</Text><Text type="secondary">{sources.find((entry) => entry.id === item.source_id)?.name || item.source || item.source_id}</Text></div> },
      { title: "数据时期", key: "period", width: 150, render: (_, item) => item.period || "来源未提供" },
      { title: "数值", key: "value", width: 130, render: (_, item) => item.value === null || item.value === undefined || item.value === "" ? "—" : `${item.value}${item.unit ? ` ${item.unit}` : ""}` },
      { title: "发布时间（北京）", key: "published", width: 150, render: (_, item) => dataTime(item.published_at) },
      { title: "采集时间（北京）", key: "observed", width: 150, render: (_, item) => dataTime(item.observed_at) },
    ]} expandable={{ expandedRowRender: (item) => <div className="data-record-detail"><Paragraph>{item.text || "暂无摘要"}</Paragraph><div><Text type="secondary">来源链接：</Text>{safeUrl(item.source_url) ? <a href={safeUrl(item.source_url)} target="_blank" rel="noopener noreferrer">查看原文 <LinkOutlined /></a> : <Text type="secondary">未提供</Text>}</div><div><Text type="secondary">引用：</Text><Text className="data-file-path" copyable={item.citation ? { text: item.citation } : false}>{item.citation || item.revision_id}</Text></div><div><Text type="secondary">采集批次：</Text><Text className="data-file-path">{item.batch_id || "—"}</Text></div></div> }} locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无匹配资料" /> }} />
  </>;
}

function CollectionMonitor({ sources, jobs, onCollect, collecting, config, worker }) {
  const runtime = collectionRuntime(config, worker, jobs.isError);
  const activeJobs = (jobs.data?.items || EMPTY).filter((job) => ["running", "queued"].includes(job.status));
  const serviceNotice = collectionServiceNotice(worker);
  return <>
    <div className="data-section-intro"><Title level={4}>采集来源</Title></div>
    {runtime.description ? <Paragraph className="data-caption">{runtime.description}</Paragraph> : null}
    {serviceNotice ? <Alert className="data-banner" type="info" showIcon message={serviceNotice} /> : null}
    {worker?.active_jobs?.length ? <Paragraph className="data-caption">采集中 {worker.active_jobs.length} / {worker.max_concurrent_jobs || worker.active_jobs.length}：{worker.active_jobs.map((job) => sources.find((source) => source.id === job.source_id)?.name || job.source_id).join("、")}</Paragraph> : null}
    <div className="data-resource-strip"><span>每类并发上限 {worker?.max_concurrent_jobs ?? config?.max_concurrent_jobs ?? "—"}</span><span>单任务超时 {config?.job_timeout_seconds ?? "—"} 秒</span><span>磁盘下限 {config?.min_free_disk_mb ?? "—"} MiB</span><span>{worker?.resources?.supported === false ? "进程内存读取不可用" : `API 内存 / 软阈值 ${worker?.resources?.rss_mb == null ? "—" : Number(worker.resources.rss_mb).toFixed(0)} / ${worker?.resources?.limit_mb ?? config?.max_memory_mb ?? "—"} MiB`}</span></div>
    <div className="data-source-grid">{sources.map((source) => <article className="data-source-card" key={source.id}>
      <div className="data-source-card-heading"><SourceName source={source} /><Status item={source} /></div>
      <Paragraph className="data-caption">{source.description || "公开数据来源"}</Paragraph>
      <dl className="data-source-metadata"><div><dt>自动采集</dt><dd>{sourceScheduleLabel(source, config, worker)}</dd></div><div><dt>最近成功</dt><dd>{dataTime(source.last_success_at)}</dd></div><div><dt>本次接收 / 新增</dt><dd>{numberText(source.records_received)} / {numberText(source.records_added)}</dd></div><div><dt>下次计划</dt><dd>{source.enabled && config?.enabled !== false && worker?.enabled !== false && worker?.automatic_enabled !== false ? dataTime(source.next_run_at) : "—"}</dd></div>{source.total_symbols != null ? <div><dt>股票完成 / 总数</dt><dd>{source.completed_symbols ?? 0} / {source.total_symbols} · 待处理 {source.pending_symbols ?? 0}</dd></div> : null}{source.retry_at ? <div><dt>失败重试</dt><dd>{dataTime(source.retry_at)}</dd></div> : null}{source.last_scheduled_for ? <div><dt>最近计划日</dt><dd>{dataTime(source.last_scheduled_for)}</dd></div> : null}</dl>
      <div className="data-source-card-footer"><span className="data-source-error">{source.error_code || ""}</span><Button size="small" icon={<CloudDownloadOutlined aria-hidden="true" />} disabled={collecting || activeJobs.some((job) => job.source_id === source.id)} onClick={() => onCollect([source.id])}>采集一次</Button></div>
    </article>)}</div>
    <div className="data-section-intro data-jobs-heading"><div><Title level={4}>最近采集任务</Title><Paragraph className="data-caption">时间为北京时间。</Paragraph></div></div>
    <Table size="small" rowKey="id" loading={jobs.isLoading} dataSource={jobs.data?.items || EMPTY} scroll={{ x: 760 }} pagination={{ pageSize: 8, hideOnSinglePage: true }} columns={[
      { title: "来源", key: "source", render: (_, job) => sources.find((source) => source.id === job.source_id)?.name || job.source_id },
      { title: "状态", key: "status", render: (_, job) => <Status item={job} /> },
      { title: "提交 / 完成", key: "time", render: (_, job) => <div className="data-time-pair"><span>{dataTime(job.queued_at)}</span><Text type="secondary">{job.finished_at ? dataTime(job.finished_at) : "尚未完成"}</Text></div> },
      { title: "接收 / 新增", key: "records", render: (_, job) => `${numberText(job.records_received)} / ${numberText(job.records_added)}` },
      { title: "结果说明", key: "error", render: (_, job) => job.error_code || (job.status === "success" ? "已入库" : "—") },
    ]} locale={{ emptyText: "暂无采集任务" }} />
  </>;
}

function CacheManager({ userId, onChanged }) {
  const { message } = AntApp.useApp();
  const [plan, setPlan] = useState(null);
  const [policyOpen, setPolicyOpen] = useState(false);
  const [trashPage, setTrashPage] = useState(1);
  const policy = useQuery({ queryKey: dataCenterQueryKey(userId, "cache-policy"), queryFn: ({ signal }) => api.getLocalCachePolicy({ signal }) });
  const trash = useQuery({ queryKey: dataCenterQueryKey(userId, "trash", { page: trashPage }), queryFn: ({ signal }) => api.getLocalDataTrash({ signal, offset: (trashPage - 1) * 8, limit: 8 }) });
  const preview = useMutation({ mutationFn: api.previewLocalCacheCleanup, onSuccess: setPlan, onError: (error) => message.error(api.getApiErrorMessage(error)) });
  const savePolicy = useMutation({ mutationFn: (values) => api.updateLocalCachePolicy(cachePolicyPayload(values)), onSuccess: () => { setPlan(null); setPolicyOpen(false); message.success("缓存策略已保存。"); onChanged(); void policy.refetch(); }, onError: (error) => message.error(error?.response ? api.getApiErrorMessage(error) : error.message) });
  const apply = useMutation({ mutationFn: () => api.applyLocalCacheCleanup(plan.plan_id), onSuccess: (result) => { setPlan(null); const skipped = (result.items || []).filter((item) => item.status === "skipped").length; message[skipped ? "warning" : "success"](`已回收 ${result.moved_files ?? 0} 个缓存文件${skipped ? `，${skipped} 个文件因状态变化被跳过` : ""}。`); onChanged(); void trash.refetch(); }, onError: (error) => { message.error(api.getApiErrorMessage(error)); setPlan(null); } });
  const restore = useMutation({ mutationFn: api.restoreLocalDataTrash, onSuccess: (result) => { const notice = cacheOperationNotice("restore", result); message[notice.type](notice.text); onChanged(); void trash.refetch(); }, onError: (error) => message.error(api.getApiErrorMessage(error)) });
  const purge = useMutation({ mutationFn: api.deleteLocalDataTrash, onSuccess: (result) => { const notice = cacheOperationNotice("purge", result); message[notice.type](notice.text); onChanged(); void trash.refetch(); }, onError: (error) => message.error(api.getApiErrorMessage(error)) });
  return <>
    <div className="data-section-intro"><div><Title level={4}>缓存管理</Title><Paragraph className="data-caption">清理可再生成缓存，保留长期资料和在用数据库。</Paragraph></div><Space wrap><Button icon={<SettingOutlined aria-hidden="true" />} onClick={() => setPolicyOpen(true)}>缓存策略</Button><Button icon={<DeleteOutlined />} loading={preview.isPending} onClick={() => preview.mutate()}>预览可清理缓存</Button></Space></div>
    <QueryError query={policy} title="缓存策略读取失败" />
    <Alert className="data-banner" type="info" showIcon message="回收后可恢复，永久删除后才释放空间" description={`自动回收${policy.data?.enabled ? "已启用" : "未启用"} · 活跃缓存预算 ${formatDataSize(policy.data?.budget_bytes)}`} />
    {plan ? <Card className="data-cleanup-plan" title="本次清理预览" extra={<Tag>{formatDataSize(plan.total_bytes)}</Tag>}>
      <Paragraph className="data-caption">{plan.total_files ?? plan.items?.length ?? 0} 个候选文件 · 预览有效至 {dataTime(plan.expires_at)}。{plan.over_budget_bytes > 0 ? `活跃缓存超出预算 ${formatDataSize(plan.over_budget_bytes)}。` : ""}</Paragraph>
      <Table size="small" dataSource={plan.items || EMPTY} rowKey="path" pagination={{ pageSize: 6, hideOnSinglePage: true }} scroll={{ x: 540 }} columns={[
        { title: "路径", dataIndex: "path", render: (value) => <Text className="data-file-path">{value}</Text> },
        { title: "大小", dataIndex: "size_bytes", render: formatDataSize },
        { title: "原因", key: "reason", render: (_, item) => item.reason || "符合缓存保留策略" },
      ]} locale={{ emptyText: "当前没有符合条件的缓存" }} />
      {plan.skipped?.length ? <Paragraph className="data-caption">{plan.skipped.length} 项不符合清理条件。</Paragraph> : null}
      <Space className="data-plan-actions"><Button type="primary" loading={apply.isPending} disabled={!plan.items?.length} onClick={() => apply.mutate()}>移入回收区</Button><Button disabled={apply.isPending} onClick={() => setPlan(null)}>关闭预览</Button></Space>
    </Card> : null}
    <Title level={4} className="data-trash-heading">缓存回收区</Title>
    <Paragraph className="data-caption">恢复遇到同名文件时，冲突项保留在回收区。</Paragraph>
    {purge.data?.skipped?.length ? <Alert className="data-banner" type="warning" showIcon message="部分回收文件已保留" description={<div className="data-trash-details">{purge.data.skipped.map((item) => <div key={item.path}><Text className="data-file-path">{item.path}</Text><span>{item.reason}</span></div>)}</div>} /> : null}
    <QueryError query={trash} title="缓存回收区读取失败" />
    <Table rowKey={(batch) => batch.batch_id || batch.id} size="small" loading={trash.isLoading} dataSource={trash.data?.items || EMPTY} scroll={{ x: 680 }} pagination={{ current: trashPage, pageSize: 8, total: trash.data?.total || 0, showSizeChanger: false, hideOnSinglePage: true, onChange: setTrashPage }} expandable={{ expandedRowRender: (batch) => <div className="data-trash-details">{(batch.items || []).map((item) => <div key={item.path}><Text className="data-file-path">{item.path}</Text><Text type={item.restore_error || item.purge_error || item.error ? "warning" : "secondary"}>{item.restore_error || item.purge_error || item.error || ({ restored: "已恢复", quarantined: "在回收区", skipped: "已跳过", pending: "待处理", purged: "已永久删除" }[item.status] || item.status)}</Text></div>)}</div> }} columns={[
      { title: "回收批次", key: "id", render: (_, batch) => <Text code>{batch.batch_id || batch.id}</Text> },
      { title: "回收时间", key: "created", render: (_, batch) => dataTime(batch.created_at) },
      { title: "待恢复 / 大小", key: "size", render: (_, batch) => { const summary = quarantineSummary(batch); return `${summary.remaining} / ${formatDataSize(summary.remainingBytes)}`; } },
      { title: "状态", key: "status", render: (_, batch) => { const summary = quarantineSummary(batch); return summary.conflicts ? <Tag color="warning">{summary.conflicts} 项恢复冲突</Tag> : summary.canRestore ? <Tag>可恢复</Tag> : <Tag color="success">处理完成</Tag>; } },
      { title: "操作", key: "action", width: 200, render: (_, batch) => <Space size={8}><Button size="small" icon={<UndoOutlined />} disabled={!quarantineSummary(batch).canRestore || restore.isPending || purge.isPending} loading={restore.isPending && restore.variables === (batch.batch_id || batch.id)} onClick={() => restore.mutate(batch.batch_id || batch.id)}>恢复</Button><Popconfirm title="永久删除已回收缓存，无法恢复" description="只删除这个批次中仍在回收区的文件。" okText="永久删除" cancelText="取消" okButtonProps={{ danger: true }} onConfirm={() => purge.mutateAsync(batch.batch_id || batch.id)}><Button danger size="small" disabled={!quarantineSummary(batch).canRestore || restore.isPending || purge.isPending} loading={purge.isPending && purge.variables === (batch.batch_id || batch.id)}>永久删除</Button></Popconfirm></Space> },
    ]} locale={{ emptyText: "回收区为空" }} />
    <Drawer title="缓存策略" open={policyOpen} onClose={() => setPolicyOpen(false)} width={600} rootClassName="data-overlay" destroyOnHidden>
      <QueryError query={policy} />
      {policy.isLoading ? <Spin /> : policy.data ? <Form key={policy.dataUpdatedAt} layout="vertical" className="data-config-form" initialValues={{ ...policy.data, budget_mib: policy.data.budget_bytes / 1024 ** 2, max_batch_mib: policy.data.max_batch_bytes / 1024 ** 2 }} onFinish={(values) => savePolicy.mutate(values)}>
        <Alert type="info" showIcon message="到期缓存自动移入回收区。" description="容量预算用于监控活跃缓存。" />
        <Form.Item name="enabled" label="自动移入回收区" valuePropName="checked"><Switch /></Form.Item>
        <div className="data-config-numbers"><Form.Item name="budget_mib" label="活跃缓存预算（MiB）" rules={[{ required: true }]}><InputNumber min={64} max={1048576} precision={0} /></Form.Item><Form.Item name="max_batch_mib" label="每批最大容量（MiB）" rules={[{ required: true }]}><InputNumber min={1} max={2048} precision={0} /></Form.Item><Form.Item name="max_batch_files" label="每批最多文件数" rules={[{ required: true }]}><InputNumber min={1} max={1000} precision={0} /></Form.Item></div>
        <Title level={5}>缓存保留天数</Title>
        <div className="data-config-numbers">{[["market_source_cache", "行情接口快照"], ["kline_bundle_cache", "K 线展示缓存"], ["tushare_daily_cache", "Tushare 接口缓存"]].map(([id, label]) => <Form.Item key={id} name={["ttl_days", id]} label={`${label}（天）`} rules={[{ required: true }]}><InputNumber min={1} max={3650} precision={0} /></Form.Item>)}</div>
        <Button type="primary" htmlType="submit" loading={savePolicy.isPending}>保存缓存策略</Button>
      </Form> : null}
    </Drawer>
  </>;
}

function DataCenterContent() {
  const { user, isAdmin, authRequired, logout } = useAuth();
  const { message } = AntApp.useApp();
  const queryClient = useQueryClient();
  const [configOpen, setConfigOpen] = useState(false);
  const [activeTab, setActiveTab] = useState("collection");
  const polling = useDisplayPolling(user?.id, "data-center", "30");
  const key = (section) => dataCenterQueryKey(user?.id, section);
  const status = useQuery({ queryKey: key("collection-status"), queryFn: ({ signal }) => api.getCollectionStatus({ signal }), enabled: isAdmin, staleTime: 15_000, refetchInterval: activeTab === "collection" ? polling.interval : false, refetchIntervalInBackground: false, refetchOnWindowFocus: false });
  const overview = useQuery({ queryKey: key("overview"), queryFn: ({ signal }) => api.getLocalDataOverview({ signal }), enabled: isAdmin && activeTab === "files", staleTime: 30_000, refetchInterval: false, refetchOnWindowFocus: false });
  const config = useQuery({ queryKey: key("config"), queryFn: ({ signal }) => api.getCollectionConfig({ signal }), enabled: isAdmin && configOpen, staleTime: 0, refetchOnWindowFocus: false });
  const configData = config.data || status.data?.config;
  const sourceItems = status.data?.sources || EMPTY;
  const jobs = { ...status, data: { items: Array.isArray(status.data?.jobs) ? status.data.jobs : status.data?.jobs?.items || EMPTY, usage: status.data?.usage } };
  const worker = status.data?.worker;
  const runtime = collectionRuntime(status.data?.config, worker, status.isError);
  const invalidate = () => void queryClient.invalidateQueries({ queryKey: ["data-center", user?.id ?? "guest"] });
  const refreshIndex = useMutation({ mutationFn: api.refreshDataAssets, onSuccess: () => { invalidate(); message.success("本地文件索引已重建。"); }, onError: (error) => message.error(api.getApiErrorMessage(error)) });
  const collect = useMutation({ mutationFn: api.startDataCollection, onSuccess: (result) => { message.info(collectionSubmissionNotice(result)); setActiveTab("collection"); invalidate(); }, onError: (error) => message.error(api.getApiErrorMessage(error)) });
  const save = useMutation({ mutationFn: (values) => api.updateCollectionConfig(collectionConfigPayload(values, sourceItems)), retry: false, onSuccess: (result) => { queryClient.setQueryData(key("config"), result); setConfigOpen(false); invalidate(); message.success("后台采集配置已保存。"); } });
  const categories = overview.data?.categories || EMPTY;
  const dataset = status.data?.datasets;
  const healthy = sourceItems.filter((source) => collectionStatus(source).color === "success").length;
  function refreshDisplay() { void status.refetch(); if (activeTab === "files") { void overview.refetch(); void queryClient.invalidateQueries({ queryKey: key("files") }); } if (activeTab === "records") void queryClient.invalidateQueries({ queryKey: key("records") }); }
  return <Layout className="sva-layout data-center-layout">
    <AppToolbar active="data" user={user} authRequired={authRequired} onLogout={logout} settingsAction={<ConfigConsoleButton />} />
    <Layout.Content className="data-center-content">
      <div className="data-heading"><div><Title level={1}>本地数据中心</Title><Space wrap size={12}><Badge status={runtime.tone} text={runtime.label} /><Text className="data-caption">本机共享数据 · 关闭页面后继续采集</Text></Space></div>
        {isAdmin ? <div className="data-heading-actions"><Button icon={<SettingOutlined aria-hidden="true" />} onClick={() => { save.reset(); setConfigOpen(true); }}>后台自动采集配置</Button><Button type="primary" icon={<CloudDownloadOutlined aria-hidden="true" />} loading={collect.isPending} onClick={() => collect.mutate()}>立即联网采集</Button></div> : null}
      </div>
      {!isAdmin ? <Alert type="info" showIcon message="请使用管理员账户访问数据中心" /> : <>
        <DisplayRefreshControl polling={polling} onRefresh={refreshDisplay} refreshing={status.isFetching || overview.isFetching} />
        <QueryError query={status} title="后台采集状态读取失败" />
        <div className="data-summary-band"><div><span>今日公共采集任务</span><strong>{status.data?.usage?.jobs_started ?? "—"}<small> / {status.data?.config?.daily_job_limit ?? "—"}</small></strong><small>不含海外快讯</small></div><div><span>公共数据记录</span><strong>{numberText(dataset?.records)}</strong><small>{numberText(dataset?.revisions)} 个历史版本</small></div><div><span>成功来源</span><strong>{healthy}<small> / {sourceItems.length}</small></strong><small>按最近任务统计</small></div><div><span>排队 / 运行中</span><strong>{status.data ? `${status.data.usage?.queued ?? 0} / ${status.data.usage?.running ?? 0}` : "—"}</strong></div></div>
        <Card className="data-workspace-card"><Tabs activeKey={activeTab} onChange={setActiveTab} destroyOnHidden items={[
          { key: "collection", label: "后台采集与任务", children: <CollectionMonitor sources={sourceItems} jobs={jobs} onCollect={(ids) => collect.mutate(ids)} collecting={collect.isPending} config={status.data?.config} worker={worker} /> },
          { key: "files", label: "文件与分类", children: <><QueryError query={overview} title="数据目录读取失败" /><div className="data-section-intro"><div><Title level={4}>本地文件索引</Title><Paragraph className="data-caption">{formatDataSize(overview.data?.directory_totals?.size_bytes)} · {formatDataFiles(overview.data?.directory_totals?.files)} · 索引更新 {dataTime(overview.data?.index?.updated_at)}</Paragraph></div><Button icon={<ReloadOutlined aria-hidden="true" />} loading={refreshIndex.isPending} onClick={() => refreshIndex.mutate()}>重建本地索引</Button></div><Card className="data-directory-card" size="small"><DatabaseOutlined /><div><Text className="data-directory-path" copyable={overview.data?.data_root ? { text: overview.data.data_root } : false}>{overview.data?.data_root || "读取目录信息…"}</Text></div></Card>{categories.length ? <div className="data-category-strip">{categories.map((category) => <div className="data-category" key={category.id}><span>{category.label}</span><strong>{formatDataSize(category.size_bytes)}</strong><small>{formatDataFiles(category.files)}</small></div>)}</div> : null}<FileInventory userId={user?.id} categories={categories} sources={sourceItems} /></> },
          { key: "records", label: "已采集数据", children: <CollectedRecords userId={user?.id} sources={sourceItems} /> },
          { key: "cache", label: "磁盘缓存与回收", children: <CacheManager userId={user?.id} onChanged={invalidate} /> },
        ]} /></Card>
      </>}
    </Layout.Content>
    <Drawer title="后台自动采集配置" open={isAdmin && configOpen} onClose={() => setConfigOpen(false)} width={780} rootClassName="data-overlay" destroyOnHidden>
      <QueryError query={config} />{save.error ? <Alert className="data-banner" type="error" showIcon message={save.error?.response ? api.getApiErrorMessage(save.error) : save.error.message} description="请核对输入后重试。" /> : null}
      {config.isPending || status.isPending ? <Spin /> : configData ? <CollectionForm config={configData} sources={sourceItems} onSave={(values) => save.mutate(values)} pending={save.isPending} /> : null}
    </Drawer>
  </Layout>;
}
export default function DataCenter() { const { user } = useAuth(); return <DataCenterContent key={user?.id ?? "guest"} />; }
