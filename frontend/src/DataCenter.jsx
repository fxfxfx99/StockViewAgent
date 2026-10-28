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
import AppToolbar from "./AppToolbar.jsx";
import { ConfigConsoleButton } from "./ConfigConsole.jsx";
import { useAuth } from "./AuthContext.jsx";
import { safeUrl } from "./components/format.js";
import * as api from "./api.js";
import {
  cacheOperationNotice, cachePolicyPayload, categoryOptions, collectionConfigPayload, collectionServiceNotice, collectionStatus, collectionSubmissionNotice, DATA_LIFECYCLE_OPTIONS,
  dataCenterQueryKey, dataTime, fileQueryParams, fileRoleLabel, formatDataFiles, formatDataSize,
  quarantineSummary, retentionInfo, sourceOptions,
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

function CollectionForm({ config, sources, onSave, pending }) {
  return <Form layout="vertical" initialValues={{ ...config, symbols: (config.symbols || []).join("\n") }} onFinish={onSave} className="data-config-form">
    <Alert type="info" showIcon message="自动采集公共数据，持续积累本地资料" description="关注股票按轮次分批采集，来源失败会保留已有数据。任务只采集数据，不启动模型解读。" />
    <div className="data-config-switches">
      <Form.Item name="enabled" label="自动采集" valuePropName="checked"><Switch checkedChildren="开启" unCheckedChildren="关闭" /></Form.Item>
      <Form.Item name="use_watchlists" label="纳入各账户的关注股票" valuePropName="checked"><Switch /></Form.Item>
      <Form.Item name="rag_enabled" label="将公共资料用于 Agent 检索" valuePropName="checked" extra="只检索当时已采集的版本，可独立于采集开关设置"><Switch /></Form.Item>
    </div>
    <div className="data-config-numbers">
      <Form.Item name="daily_job_limit" label="每日采集任务上限" extra="一个任务可能请求多项上游数据" rules={[{ required: true, message: "请输入每日任务上限" }]}><InputNumber aria-label="每日采集任务上限" min={1} max={1000} precision={0} /></Form.Item>
      <Form.Item name="max_symbols_per_cycle" label="每轮股票数" extra="超出部分在后续轮次继续采集" rules={[{ required: true, message: "请输入每轮股票数" }]}><InputNumber aria-label="每轮股票数" min={1} max={30} precision={0} /></Form.Item>
      <Form.Item name="min_free_disk_mb" label="磁盘剩余空间下限（MiB）" extra="空间不足时暂停采集，保留历史资料" rules={[{ required: true, message: "请输入磁盘空间下限" }]}><InputNumber aria-label="磁盘剩余空间下限" min={128} max={102400} precision={0} /></Form.Item>
    </div>
    <Form.Item name="symbols" label="额外关注股票" extra="目前支持 A 股，最多 300 个；每行一个代码或用逗号分隔。留空时仍可使用账户股票列表。"><Input.TextArea aria-label="额外关注股票" rows={4} placeholder={"600519.SS\n000001.SZ\n920002.BJ"} /></Form.Item>
    <Title level={5}>采集来源与频率</Title>
    <Paragraph className="data-caption">仅使用内置来源。间隔以分钟计，实际运行取决于服务器调度、每日限额和来源可用性。</Paragraph>
    <div className="data-source-config-list">{sources.map((source) => <div className="data-source-config" key={source.id}>
      <div className="data-source-config-name"><SourceName source={source} /><Text type="secondary">{source.description || source.category}</Text></div>
      <Form.Item name={["sources", source.id, "enabled"]} valuePropName="checked" className="data-source-switch"><Switch aria-label={`启用${source.name || source.id}`} size="small" /></Form.Item>
      <Form.Item name={["sources", source.id, "interval_minutes"]} rules={[{ required: true, message: "请输入间隔" }]} className="data-source-interval"><InputNumber aria-label={`${source.name || source.id}采集间隔（分钟）`} min={60} max={43200} precision={0} /></Form.Item>
    </div>)}</div>
    <div className="data-form-actions"><Button type="primary" htmlType="submit" loading={pending}>保存自动采集配置</Button></div>
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
        { title: "文件与存储位置", key: "path", width: 340, render: (_, item) => <div className="data-file-identity"><Text className="data-file-path" copyable={{ text: item.path }}>{item.path}</Text><span>{fileRoleLabel(item)}</span></div> },
        { title: "分类", key: "category", width: 130, render: (_, item) => categories.find((entry) => entry.id === item.category)?.label || item.category_label || item.category || "其他" },
        { title: "来源", key: "source", width: 160, render: (_, item) => sources.find((entry) => entry.id === item.source)?.name || item.source || "本地生成" },
        { title: "大小", key: "size_bytes", width: 100, render: (_, item) => formatDataSize(item.size_bytes) },
        { title: "最近更新（北京）", key: "updated_at", width: 145, render: (_, item) => dataTime(item.updated_at) },
        { title: "保留方式", key: "retention", width: 130, render: (_, item) => <Space direction="vertical" size={3}><Tag color={item.protected || item.retention === "durable" ? "blue" : item.expired ? "orange" : "default"}>{item.storage_role === "quarantine" ? "可恢复回收区" : item.rebuildable ? item.expired ? "缓存已到期" : "可再生成" : item.protected ? "长期保护" : retentionInfo(item.retention)}</Tag>{item.expires_at ? <Text type="secondary" className="data-expiry">{dataTime(item.expires_at)} 到期</Text> : null}</Space> },
      ]}
      locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无匹配文件；可调整筛选或启动采集" /> }} />
    <Paragraph className="data-caption">路径相对于数据目录。长期保护的历史资料、账户配置与数据库不会进入普通缓存清理。</Paragraph>
  </>;
}

function CollectedRecords({ userId, sources }) {
  const [source, setSource] = useState();
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);
  const params = useMemo(() => ({ offset: (page - 1) * 20, limit: 20, ...(source ? { source_id: source } : {}), ...(search ? { q: search } : {}) }), [source, search, page]);
  const records = useQuery({ queryKey: dataCenterQueryKey(userId, "records", params), queryFn: ({ signal }) => api.getCollectedDataRecords(params, { signal }), refetchOnWindowFocus: false });
  return <>
    <div className="data-section-intro"><div><Title level={4}>已采集的公开资料</Title><Paragraph className="data-caption">查看真实入库内容、数据所属时期和本地采集时间。相同资料的变动版本会保留独立证据引用。</Paragraph></div></div>
    <div className="data-record-filters"><label><span>采集来源</span><Select aria-label="资料来源筛选" allowClear placeholder="全部来源" options={sourceOptions(sources)} value={source} onChange={(value) => { setSource(value); setPage(1); }} /></label><label><span>搜索资料</span><Input.Search aria-label="搜索已采集资料" placeholder="资料标题或内容关键词" maxLength={500} allowClear onSearch={(value) => { setSearch(value.trim()); setPage(1); }} enterButton /></label></div>
    <QueryError query={records} title="已采集资料读取失败" />
    <Table size="small" rowKey={(item) => item.revision_id || item.citation} dataSource={records.data?.items || EMPTY} loading={records.isLoading} scroll={{ x: 850 }} pagination={{ current: page, pageSize: 20, total: records.data?.total || 0, showSizeChanger: false, showTotal: (total) => `共 ${total} 条资料`, onChange: setPage }} columns={[
      { title: "资料", key: "title", width: 330, render: (_, item) => <div className="data-record-title"><Text strong>{item.title}</Text><Text type="secondary">{sources.find((entry) => entry.id === item.source_id)?.name || item.source || item.source_id}</Text></div> },
      { title: "数据时期", key: "period", width: 150, render: (_, item) => item.period || "来源未提供" },
      { title: "数值", key: "value", width: 130, render: (_, item) => item.value === null || item.value === undefined || item.value === "" ? "—" : `${item.value}${item.unit ? ` ${item.unit}` : ""}` },
      { title: "发布时间（北京）", key: "published", width: 150, render: (_, item) => dataTime(item.published_at) },
      { title: "采集时间（北京）", key: "observed", width: 150, render: (_, item) => dataTime(item.observed_at) },
    ]} expandable={{ expandedRowRender: (item) => <div className="data-record-detail"><Paragraph>{item.text || "该记录未提供文本摘要。"}</Paragraph><div><Text type="secondary">来源链接：</Text>{safeUrl(item.source_url) ? <a href={safeUrl(item.source_url)} target="_blank" rel="noopener noreferrer">查看原始来源 <LinkOutlined /></a> : <Text type="secondary">未提供</Text>}</div><div><Text type="secondary">证据引用：</Text><Text className="data-file-path" copyable={item.citation ? { text: item.citation } : false}>{item.citation || item.revision_id}</Text></div><div><Text type="secondary">采集批次：</Text><Text className="data-file-path">{item.batch_id || "—"}</Text></div></div> }} locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无匹配资料；采集任务完成后会显示入库记录" /> }} />
  </>;
}

function CollectionMonitor({ sources, jobs, onCollect, collecting, config, worker }) {
  const activeJobs = (jobs.data?.items || EMPTY).filter((job) => ["running", "queued"].includes(job.status));
  const serviceNotice = collectionServiceNotice(worker);
  return <>
    <div className="data-section-intro"><div><Title level={4}>自动采集与来源健康</Title><Paragraph className="data-caption">按配置持续补充行情、新闻、企业资料与宏观公开数据。失败来源保留最近成功版本。</Paragraph></div><Badge status={worker?.running ? "processing" : "default"} text={worker?.running ? "采集服务运行中" : worker?.enabled ? "采集服务等待启动" : "采集服务未开启"} /></div>
    {config?.enabled === false ? <Alert className="data-banner" type="info" showIcon message="自动采集已暂停，可手动提交采集任务。" /> : null}
    {serviceNotice ? <Alert className="data-banner" type="info" showIcon message={serviceNotice} /> : null}
    {worker?.reason === "low_disk" || worker?.reason === "low_disk_space" ? <Alert className="data-banner" type="warning" showIcon message="磁盘剩余空间低于配置下限，采集暂时暂停。" /> : null}
    {jobs.data?.usage ? <Paragraph className="data-caption">今日已开始 {jobs.data.usage.jobs_started ?? 0} / {config?.daily_job_limit ?? "—"} 个采集任务 · 排队 {jobs.data.usage.queued ?? 0} · 运行 {jobs.data.usage.running ?? 0}</Paragraph> : null}
    <div className="data-source-grid">{sources.map((source) => <article className="data-source-card" key={source.id}>
      <div className="data-source-card-heading"><SourceName source={source} /><Status item={source} /></div>
      <Paragraph className="data-caption">{source.description || "公开数据来源"}</Paragraph>
      <dl className="data-source-metadata"><div><dt>自动采集</dt><dd>{source.enabled ? `每 ${source.interval_minutes} 分钟` : "已暂停"}</dd></div><div><dt>最近成功</dt><dd>{dataTime(source.last_success_at)}</dd></div><div><dt>本次接收 / 新增</dt><dd>{numberText(source.records_received)} / {numberText(source.records_added)}</dd></div><div><dt>下次计划</dt><dd>{source.enabled ? dataTime(source.next_run_at) : "—"}</dd></div></dl>
      <div className="data-source-card-footer"><span className="data-source-error">{source.error_code || ""}</span><Button size="small" icon={<CloudDownloadOutlined />} disabled={collecting || activeJobs.some((job) => job.source_id === source.id)} onClick={() => onCollect([source.id])}>采集此来源</Button></div>
    </article>)}</div>
    <div className="data-section-intro data-jobs-heading"><div><Title level={4}>最近采集任务</Title><Paragraph className="data-caption">提交后先进入队列，任务完成后才会更新接收与新增数量。北京时间。</Paragraph></div></div>
    <QueryError query={jobs} title="采集任务读取失败" />
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
  const apply = useMutation({ mutationFn: () => api.applyLocalCacheCleanup(plan.plan_id), onSuccess: (result) => { setPlan(null); const skipped = (result.items || []).filter((item) => item.status === "skipped").length; message[skipped ? "warning" : "success"](`已回收 ${result.moved_files ?? 0} 个缓存文件${skipped ? `，${skipped} 个文件因状态变化被跳过` : ""}。回收区仍占用磁盘空间。`); onChanged(); void trash.refetch(); }, onError: (error) => { message.error(api.getApiErrorMessage(error)); setPlan(null); } });
  const restore = useMutation({ mutationFn: api.restoreLocalDataTrash, onSuccess: (result) => { const notice = cacheOperationNotice("restore", result); message[notice.type](notice.text); onChanged(); void trash.refetch(); }, onError: (error) => message.error(api.getApiErrorMessage(error)) });
  const purge = useMutation({ mutationFn: api.deleteLocalDataTrash, onSuccess: (result) => { const notice = cacheOperationNotice("purge", result); message[notice.type](notice.text); onChanged(); void trash.refetch(); }, onError: (error) => message.error(api.getApiErrorMessage(error)) });
  return <>
    <div className="data-section-intro"><div><Title level={4}>缓存生命周期</Title><Paragraph className="data-caption">先预览符合策略的可再生成缓存，再移入回收区。长期资料和正在使用的数据库保持保护。</Paragraph></div><Space wrap><Button icon={<SettingOutlined />} onClick={() => setPolicyOpen(true)}>缓存策略</Button><Button icon={<DeleteOutlined />} loading={preview.isPending} onClick={() => preview.mutate()}>预览可清理缓存</Button></Space></div>
    <QueryError query={policy} title="缓存策略读取失败" />
    <Alert className="data-banner" type="info" showIcon message="回收可恢复，文件仍占用磁盘空间" description={`自动回收${policy.data?.enabled ? "已启用" : "未启用"} · 活跃缓存预算 ${formatDataSize(policy.data?.budget_bytes)}。回收区不会自动永久删除，移入回收区释放的磁盘空间为 0。`} />
    {plan ? <Card className="data-cleanup-plan" title="本次清理预览" extra={<Tag>{formatDataSize(plan.total_bytes)}</Tag>}>
      <Paragraph className="data-caption">{plan.total_files ?? plan.items?.length ?? 0} 个候选文件 · 预览有效至 {dataTime(plan.expires_at)}。执行时会再次校验文件状态。{plan.over_budget_bytes > 0 ? `活跃缓存超出预算 ${formatDataSize(plan.over_budget_bytes)}。` : ""}</Paragraph>
      <Table size="small" dataSource={plan.items || EMPTY} rowKey="path" pagination={{ pageSize: 6, hideOnSinglePage: true }} scroll={{ x: 540 }} columns={[
        { title: "路径", dataIndex: "path", render: (value) => <Text className="data-file-path">{value}</Text> },
        { title: "大小", dataIndex: "size_bytes", render: formatDataSize },
        { title: "原因", key: "reason", render: (_, item) => item.reason || "符合缓存保留策略" },
      ]} locale={{ emptyText: "当前没有符合条件的缓存" }} />
      {plan.skipped?.length ? <Paragraph className="data-caption">{plan.skipped.length} 项因受保护或不符合策略未列入清理。</Paragraph> : null}
      <Space className="data-plan-actions"><Button type="primary" loading={apply.isPending} disabled={!plan.items?.length} onClick={() => apply.mutate()}>将这些缓存移入回收区</Button><Button disabled={apply.isPending} onClick={() => setPlan(null)}>关闭预览</Button></Space>
    </Card> : null}
    <Title level={4} className="data-trash-heading">缓存回收区</Title>
    <Paragraph className="data-caption">恢复会校验原路径，已有新文件时保留新文件并记录冲突。确认无需恢复后，可手动永久删除批次以释放磁盘空间。</Paragraph>
    {purge.data?.skipped?.length ? <Alert className="data-banner" type="warning" showIcon message="部分回收文件已保留" description={<div className="data-trash-details">{purge.data.skipped.map((item) => <div key={item.path}><Text className="data-file-path">{item.path}</Text><span>{item.reason}</span></div>)}</div>} /> : null}
    <QueryError query={trash} title="缓存回收区读取失败" />
    <Table rowKey={(batch) => batch.batch_id || batch.id} size="small" loading={trash.isLoading} dataSource={trash.data?.items || EMPTY} scroll={{ x: 680 }} pagination={{ current: trashPage, pageSize: 8, total: trash.data?.total || 0, showSizeChanger: false, hideOnSinglePage: true, onChange: setTrashPage }} expandable={{ expandedRowRender: (batch) => <div className="data-trash-details">{(batch.items || []).map((item) => <div key={item.path}><Text className="data-file-path">{item.path}</Text><Text type={item.restore_error || item.purge_error || item.error ? "warning" : "secondary"}>{item.restore_error || item.purge_error || item.error || ({ restored: "已恢复", quarantined: "在回收区", skipped: "已跳过", pending: "待处理", purged: "已永久删除" }[item.status] || item.status)}</Text></div>)}</div> }} columns={[
      { title: "回收批次", key: "id", render: (_, batch) => <Text code>{batch.batch_id || batch.id}</Text> },
      { title: "回收时间", key: "created", render: (_, batch) => dataTime(batch.created_at) },
      { title: "待恢复 / 大小", key: "size", render: (_, batch) => { const summary = quarantineSummary(batch); return `${summary.remaining} / ${formatDataSize(summary.remainingBytes)}`; } },
      { title: "状态", key: "status", render: (_, batch) => { const summary = quarantineSummary(batch); return summary.conflicts ? <Tag color="warning">{summary.conflicts} 项恢复冲突</Tag> : summary.canRestore ? <Tag>可恢复</Tag> : <Tag color="success">处理完成</Tag>; } },
      { title: "操作", key: "action", width: 200, render: (_, batch) => <Space size={6}><Button size="small" icon={<UndoOutlined />} disabled={!quarantineSummary(batch).canRestore || restore.isPending || purge.isPending} loading={restore.isPending && restore.variables === (batch.batch_id || batch.id)} onClick={() => restore.mutate(batch.batch_id || batch.id)}>恢复</Button><Popconfirm title="永久删除已回收缓存，无法恢复" description="只删除这个批次中仍在回收区的文件。" okText="永久删除" cancelText="取消" okButtonProps={{ danger: true }} onConfirm={() => purge.mutateAsync(batch.batch_id || batch.id)}><Button danger size="small" disabled={!quarantineSummary(batch).canRestore || restore.isPending || purge.isPending} loading={purge.isPending && purge.variables === (batch.batch_id || batch.id)}>永久删除</Button></Popconfirm></Space> },
    ]} locale={{ emptyText: "回收区为空" }} />
    <Drawer title="缓存保留与自动回收策略" open={policyOpen} onClose={() => setPolicyOpen(false)} width={600} rootClassName="data-overlay" destroyOnHidden>
      <QueryError query={policy} />
      {policy.isLoading ? <Spin /> : policy.data ? <Form key={policy.dataUpdatedAt} layout="vertical" className="data-config-form" initialValues={{ ...policy.data, budget_mib: policy.data.budget_bytes / 1024 ** 2, max_batch_mib: policy.data.max_batch_bytes / 1024 ** 2 }} onFinish={(values) => savePolicy.mutate(values)}>
        <Alert type="info" showIcon message="自动回收仅处理到期的可再生成缓存" description="容量预算用于监控活跃缓存，回收后仍可恢复，不会自动永久删除。" />
        <Form.Item name="enabled" label="自动移入回收区" valuePropName="checked"><Switch /></Form.Item>
        <div className="data-config-numbers"><Form.Item name="budget_mib" label="活跃缓存预算（MiB）" rules={[{ required: true }]}><InputNumber min={64} max={1048576} precision={0} /></Form.Item><Form.Item name="max_batch_mib" label="每批最大容量（MiB）" rules={[{ required: true }]}><InputNumber min={1} max={2048} precision={0} /></Form.Item><Form.Item name="max_batch_files" label="每批最多文件数" rules={[{ required: true }]}><InputNumber min={1} max={1000} precision={0} /></Form.Item></div>
        <Title level={5}>缓存保留天数</Title>
        <div className="data-config-numbers">{[["market_source_cache", "行情接口快照"], ["kline_bundle_cache", "K 线展示缓存"], ["tushare_daily_cache", "Tushare 接口缓存"]].map(([id, label]) => <Form.Item key={id} name={["ttl_days", id]} label={`${label}（天）`} rules={[{ required: true }]}><InputNumber min={1} max={3650} precision={0} /></Form.Item>)}</div>
        <Button type="primary" htmlType="submit" loading={savePolicy.isPending}>保存缓存策略</Button>
      </Form> : null}
    </Drawer>
  </>;
}

export default function DataCenter() {
  const { user, isAdmin, authRequired, logout } = useAuth();
  const { message } = AntApp.useApp();
  const queryClient = useQueryClient();
  const [configOpen, setConfigOpen] = useState(false);
  const [activeTab, setActiveTab] = useState("files");
  const key = (section) => dataCenterQueryKey(user?.id, section);
  const overview = useQuery({ queryKey: key("overview"), queryFn: ({ signal }) => api.getLocalDataOverview({ signal }), enabled: isAdmin, staleTime: 30_000, refetchInterval: 30_000, refetchOnWindowFocus: false });
  const config = useQuery({ queryKey: key("config"), queryFn: ({ signal }) => api.getCollectionConfig({ signal }), enabled: isAdmin });
  const sources = useQuery({ queryKey: key("sources"), queryFn: ({ signal }) => api.getCollectionSources({ signal }), enabled: isAdmin, refetchInterval: 15_000 });
  const jobs = useQuery({ queryKey: key("jobs"), queryFn: ({ signal }) => api.getCollectionJobs({ limit: 40 }, { signal }), enabled: isAdmin, refetchInterval: (query) => (query.state.data?.items || EMPTY).some((job) => ["running", "queued"].includes(job.status)) ? 3000 : 15_000 });
  const invalidate = () => void queryClient.invalidateQueries({ queryKey: ["data-center", user?.id ?? "guest"] });
  const refreshIndex = useMutation({ mutationFn: api.refreshDataAssets, onSuccess: () => { invalidate(); message.success("本地文件索引已刷新。"); }, onError: (error) => message.error(api.getApiErrorMessage(error)) });
  const collect = useMutation({ mutationFn: api.startDataCollection, onSuccess: (result) => { message.info(collectionSubmissionNotice(result)); setActiveTab("collection"); invalidate(); }, onError: (error) => message.error(api.getApiErrorMessage(error)) });
  const save = useMutation({ mutationFn: (values) => api.updateCollectionConfig(collectionConfigPayload(values, sources.data?.items || EMPTY)), onSuccess: (result) => { queryClient.setQueryData(key("config"), result); setConfigOpen(false); invalidate(); message.success("自动采集配置已保存。"); }, onError: (error) => message.error(error?.response ? api.getApiErrorMessage(error) : error.message || "保存失败") });
  const categories = overview.data?.categories || EMPTY;
  const sourceItems = sources.data?.items || EMPTY;
  const totals = overview.data?.directory_totals;
  const dataset = overview.data?.collection?.datasets;
  const healthy = sourceItems.filter((source) => collectionStatus(source).color === "success").length;

  return <Layout className="sva-layout data-center-layout">
    <AppToolbar active="data" user={user} authRequired={authRequired} onLogout={logout} settingsAction={<ConfigConsoleButton />} />
    <Layout.Content className="data-center-content">
      <div className="data-heading"><div><Title level={1}>本地数据中心</Title><Paragraph type="secondary">持续采集、分类存储，让历史资料与可再生成缓存各有归处。</Paragraph><Space wrap size={12}><Badge status={config.data?.enabled ? "processing" : "default"} text={config.data ? `自动采集${config.data.enabled ? "已启用" : "已暂停"}` : "读取采集配置"} /><Text className="data-caption">管理员管理 · 本机共享数据</Text></Space></div>
        {isAdmin ? <div className="data-heading-actions"><Button icon={<ReloadOutlined />} loading={refreshIndex.isPending} onClick={() => refreshIndex.mutate()}>刷新索引</Button><Button icon={<SettingOutlined />} onClick={() => setConfigOpen(true)}>自动采集配置</Button><Button type="primary" icon={<CloudDownloadOutlined />} loading={collect.isPending} onClick={() => collect.mutate()}>立即采集</Button></div> : null}
      </div>
      {!isAdmin ? <Alert type="info" showIcon message="数据中心需要管理员权限" description="本机目录、自动采集和缓存回收由管理员统一维护。请使用管理员账户访问。" /> : <>
        <QueryError query={overview} title="数据目录读取失败" /><QueryError query={sources} title="采集来源读取失败" /><QueryError query={config} title="自动采集配置读取失败" />
        <div className="data-summary-band">
          <div><span>本地数据总量</span><strong>{formatDataSize(totals?.size_bytes)}</strong><small>{formatDataFiles(totals?.files)}</small></div>
          <div><span>公共数据记录</span><strong>{numberText(dataset?.records)}</strong><small>{numberText(dataset?.revisions)} 个历史版本</small></div>
          <div><span>来源健康</span><strong>{healthy}<small> / {sourceItems.length}</small></strong><small>最近任务成功的来源</small></div>
          <div><span>正在处理</span><strong>{jobs.data?.usage ? (jobs.data.usage.queued || 0) + (jobs.data.usage.running || 0) : (jobs.data?.items || EMPTY).filter((job) => ["queued", "running"].includes(job.status)).length}</strong><small>排队与运行中的任务</small></div>
        </div>
        <Card className="data-directory-card" size="small"><DatabaseOutlined /><div><Text className="data-directory-path" copyable={overview.data?.data_root ? { text: overview.data.data_root } : false}>{overview.data?.data_root || "正在读取本地数据目录…"}</Text><span className="data-caption">索引更新 {dataTime(overview.data?.index?.updated_at)} · 数据目录可整体迁移</span></div></Card>
        {categories.length ? <div className="data-category-strip" aria-label="数据分类容量">{categories.map((category) => <div className="data-category" key={category.id}><span>{category.label}</span><strong>{formatDataSize(category.size_bytes)}</strong><small>{formatDataFiles(category.files)}</small></div>)}</div> : overview.isLoading ? <div className="data-loading"><Spin /></div> : null}
        <Card className="data-workspace-card"><Tabs activeKey={activeTab} onChange={setActiveTab} destroyOnHidden items={[
          { key: "files", label: "文件与分类", children: <FileInventory userId={user?.id} categories={categories} sources={sourceItems} /> },
          { key: "records", label: "已采集数据", children: <CollectedRecords userId={user?.id} sources={sourceItems} /> },
          { key: "collection", label: "采集与任务", children: <CollectionMonitor sources={sourceItems} jobs={jobs} onCollect={(ids) => collect.mutate(ids)} collecting={collect.isPending} config={config.data} worker={overview.data?.collection?.worker} /> },
          { key: "cache", label: "缓存与回收区", children: <CacheManager userId={user?.id} onChanged={invalidate} /> },
        ]} /></Card>
      </>}
    </Layout.Content>
    <Drawer title="自动采集配置" open={isAdmin && configOpen} onClose={() => setConfigOpen(false)} width={720} rootClassName="data-overlay" destroyOnHidden>
      <QueryError query={config} /><QueryError query={sources} />
      {config.isLoading || sources.isLoading ? <Spin /> : config.data && sources.data ? <CollectionForm key={config.dataUpdatedAt} config={config.data} sources={sourceItems} onSave={(values) => save.mutate(values)} pending={save.isPending} /> : null}
    </Drawer>
  </Layout>;
}
