import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, App as AntApp, Button, Collapse, Drawer, Empty, Form, Input, Progress, Select, Space, Spin, Table, Tabs, Tag, Typography } from "antd";
import ReloadOutlined from "@ant-design/icons/es/icons/ReloadOutlined";
import HistoryOutlined from "@ant-design/icons/es/icons/HistoryOutlined";
import { dataTime, formatDataSize } from "./dataCenterViewModel.js";
import { KNOWLEDGE_KINDS, knowledgeContent, knowledgeKey, knowledgeKind, knowledgeParams, knowledgeSource, knowledgeTask, promptPayload, shortRevision } from "./knowledgeCacheViewModel.js";
import * as api from "./api.js";
import "./KnowledgeCachePanel.css";

const { Title, Paragraph, Text } = Typography;
const EMPTY = [];
const scopes = [{ value: "all", label: "全部知识缓存" }, { value: "stocks", label: "个股特征缓存" }, { value: "relevance", label: "新闻相关性知识库" }, { value: "prompts", label: "提示词缓存" }];
const eventNames = { created: "建立版本", updated: "更新版本", unchanged: "检查无变化", activated: "启用历史版本", reset: "恢复默认", repaired: "修复本地副本", projection_repaired: "修复本地副本", integrity_error: "完整性检查异常", prompt_updated: "修改提示词", prompt_reset: "恢复默认", prompt_activated: "启用历史版本" };
const numberText = (value) => Number.isFinite(value) ? value.toLocaleString("zh-CN") : "—";
const listItems = (data) => Array.isArray(data) ? data : data?.items || EMPTY;
const promptText = (document) => typeof document?.content?.text === "string" ? document.content.text : knowledgeContent(document?.content);
const mutationError = (error) => error?.isAxiosError || error?.response ? api.getApiErrorMessage(error) : error?.message || "请检查输入后重试。";

function QueryError({ query, title }) {
  return query.isError ? <Alert className="knowledge-banner" type="warning" showIcon message={title} description={api.getApiErrorMessage(query.error)} action={<Button size="small" loading={query.isFetching} onClick={() => void query.refetch()}>重试</Button>} /> : null;
}

function Metadata({ metadata }) {
  return <dl className="knowledge-metadata"><div><dt>类别</dt><dd>{knowledgeKind(metadata.kind)}</dd></div><div><dt>版本</dt><dd><Text copyable>{metadata.revision_id}</Text></dd></div><div><dt>更新时间（北京）</dt><dd>{dataTime(metadata.updated_at || metadata.created_at)}</dd></div><div><dt>来源</dt><dd>{knowledgeSource(metadata)}</dd></div><div><dt>本地文件</dt><dd><Text copyable={metadata.current_path ? { text: metadata.current_path } : false}>{metadata.current_path || "—"}</Text></dd></div><div><dt>修改原因</dt><dd>{metadata.reason || "未填写"}</dd></div></dl>;
}

function Events({ userId, kind, documentKey, compact = false }) {
  const params = useMemo(() => ({ ...(kind ? { kind } : {}), ...(documentKey ? { key: documentKey } : {}), limit: compact ? 12 : 30 }), [kind, documentKey, compact]);
  const query = useQuery({ queryKey: knowledgeKey(userId, "events", params), queryFn: ({ signal }) => api.getKnowledgeEvents(params, { signal }), staleTime: 15_000, refetchOnWindowFocus: false });
  return <><QueryError query={query} title="更新记录读取失败" /><Table className="knowledge-events" size="small" rowKey="event_id" dataSource={listItems(query.data)} loading={query.isLoading} scroll={{ x: 670 }} pagination={{ pageSize: compact ? 6 : 10, hideOnSinglePage: true, showSizeChanger: false }} locale={{ emptyText: "暂无更新记录" }} columns={[
    { title: "时间（北京）", width: 130, render: (_, item) => dataTime(item.created_at) },
    ...(!documentKey ? [{ title: "文档", width: 180, render: (_, item) => <div>{item.key}<small className="knowledge-subtext">{knowledgeKind(item.kind)}</small></div> }] : []),
    { title: "动作", width: 130, render: (_, item) => eventNames[item.event_type] || item.event_type || "检查" },
    { title: "原因 / 版本", render: (_, item) => <div>{item.reason || "—"}<small className="knowledge-subtext">{shortRevision(item.revision_id)}{item.actor_id ? ` · 操作者 ${item.actor_id}` : ""}</small></div> },
  ]} /></>;
}

function PromptEditor({ document, definition, onChanged }) {
  const [content, setContent] = useState(promptText(document));
  const [reason, setReason] = useState("");
  const { message } = AntApp.useApp();
  const mutation = useMutation({ mutationFn: (action) => {
    const payload = promptPayload(action === "save" ? content : undefined, reason, document.metadata.revision_id);
    return action === "reset" ? api.resetKnowledgePrompt(document.metadata.key, payload) : api.updateKnowledgePrompt(document.metadata.key, payload);
  }, retry: false, onSuccess: async () => { await onChanged(); message.success("提示词已保存并启用，后续分析将使用新版本。"); } });
  const placeholders = definition?.required_placeholders || definition?.required_variables || EMPTY;
  return <div className="knowledge-editor">
    <Paragraph className="data-caption">保存后应用于后续分析；已保存的历史分析保留原版本记录。{definition?.description || ""}</Paragraph>
    {placeholders.length ? <Paragraph className="data-caption">需保留变量：{placeholders.map((item) => <Text code key={item}>{item}</Text>)}</Paragraph> : null}
    <Form layout="vertical" onFinish={() => mutation.mutate("save")}>
      <Form.Item label="提示词内容" extra="最多 20,000 字。"><Input.TextArea aria-label="提示词内容" disabled={mutation.isPending} value={content} onChange={(event) => setContent(event.target.value)} rows={17} maxLength={20000} showCount spellCheck={false} className="knowledge-prompt-input" /></Form.Item>
      <Form.Item label="变更原因" required extra="此原因会写入版本与更新记录。"><Input aria-label="提示词变更原因" disabled={mutation.isPending} value={reason} onChange={(event) => setReason(event.target.value)} maxLength={400} placeholder="例如：补充汇率传导的核对条件" /></Form.Item>
      {mutation.isError ? <Alert className="knowledge-banner" type="error" showIcon message="提示词未保存" description={mutation.error?.response?.status === 409 ? "当前启用版本已被更新。你的输入仍保留，请先复制草稿，再关闭并重新打开文档核对最新内容。" : mutationError(mutation.error)} /> : null}
      <Space wrap><Button type="primary" htmlType="submit" aria-label="保存并启用" loading={mutation.isPending} disabled={content === promptText(document)}>保存并启用</Button><Button disabled={mutation.isPending} onClick={() => mutation.mutate("reset")}>还原内置默认</Button></Space>
    </Form>
  </div>;
}

function History({ userId, item, current, onChanged, editable }) {
  const [revision, setRevision] = useState();
  const [reason, setReason] = useState("");
  const [page, setPage] = useState(1);
  const { message } = AntApp.useApp();
  const params = useMemo(() => ({ kind: item.kind, key: item.key, offset: (page - 1) * 8, limit: 8 }), [item.kind, item.key, page]);
  const query = useQuery({ queryKey: knowledgeKey(userId, "history", params), queryFn: ({ signal }) => api.getKnowledgeHistory(params, { signal }), placeholderData: (previous) => previous, refetchOnWindowFocus: false });
  const selected = useQuery({ queryKey: knowledgeKey(userId, "revision", { kind: item.kind, key: item.key, revision }), queryFn: ({ signal }) => api.getKnowledgeDocument({ kind: item.kind, key: item.key, revision_id: revision }, { signal }), enabled: Boolean(revision), staleTime: Infinity, refetchOnWindowFocus: false });
  const activate = useMutation({ mutationFn: () => api.activateKnowledgePrompt(item.key, { ...promptPayload(undefined, reason, current?.metadata?.revision_id), revision_id: revision }), retry: false, onSuccess: async () => { await onChanged(); setReason(""); message.success("历史提示词已启用，启用操作已记入更新记录。"); } });
  const active = current?.metadata?.revision_id;
  return <><QueryError query={query} title="版本记录读取失败" /><Paragraph className="data-caption">每页读取 8 个历史版本，按需打开内容；差异与该版本的前一个版本比较。</Paragraph><Table className="knowledge-history" size="small" rowKey="revision_id" dataSource={listItems(query.data)} loading={query.isLoading || query.isFetching} scroll={{ x: 600 }} pagination={{ current: page, pageSize: 8, total: query.data?.total || 0, hideOnSinglePage: true, showSizeChanger: false, showQuickJumper: true, showTotal: (total) => `共 ${total.toLocaleString("zh-CN")} 个版本`, onChange: (next) => { setPage(next); setRevision(undefined); setReason(""); activate.reset(); } }} columns={[
    { title: "版本 / 创建时间", width: 165, render: (_, version) => <div><Text code>{shortRevision(version.revision_id)}</Text>{version.revision_id === active ? <Tag color="blue">当前启用</Tag> : null}<small className="knowledge-subtext">{dataTime(version.created_at)}</small></div> },
    { title: "原因", render: (_, version) => version.reason || "—" },
    { title: "操作", width: 120, render: (_, version) => <Button size="small" type={revision === version.revision_id ? "primary" : "default"} onClick={() => { setRevision(version.revision_id); activate.reset(); }}>内容与差异</Button> },
  ]} />{revision ? <section className="knowledge-version-detail"><QueryError query={selected} title="历史版本读取失败" />{selected.isPending ? <Spin /> : selected.data ? <><Title level={5}>历史版本 {shortRevision(revision)}</Title><Metadata metadata={selected.data.metadata} /><Collapse items={[{ key: "content", label: "查看此版本完整内容", children: <pre className="knowledge-content">{knowledgeContent(selected.data.content)}</pre> }]} /><Title level={5}>版本差异</Title>{selected.data.diff?.text ? <><pre className="knowledge-diff">{selected.data.diff.text}</pre>{selected.data.diff.truncated ? <Paragraph className="data-caption">差异过长，仅展示部分内容；可展开完整内容核对。</Paragraph> : null}</> : <Paragraph className="data-caption">{selected.data.diff?.from_revision ? "此版本内容没有变化。" : "此版本没有可比较的前序内容。"}</Paragraph>}{editable && revision !== active ? <div className="knowledge-activate"><label htmlFor="knowledge-activate-reason">启用原因</label><Input id="knowledge-activate-reason" disabled={activate.isPending} value={reason} maxLength={400} onChange={(event) => setReason(event.target.value)} placeholder="说明为什么恢复这一版本" /><Button aria-label="启用此历史版本" icon={<HistoryOutlined aria-hidden="true" />} loading={activate.isPending} onClick={() => activate.mutate()}>启用此历史版本</Button>{activate.error ? <Alert type="error" showIcon message="历史版本未启用" description={mutationError(activate.error)} /> : null}</div> : null}</> : null}</section> : null}</>;
}

function DocumentDrawer({ item, userId, definitions, onClose, onChanged }) {
  const [tab, setTab] = useState("content");
  const params = useMemo(() => ({ kind: item.kind, key: item.key }), [item.kind, item.key]);
  const query = useQuery({ queryKey: knowledgeKey(userId, "document", params), queryFn: ({ signal }) => api.getKnowledgeDocument(params, { signal }), staleTime: 0, refetchOnWindowFocus: false });
  const definition = definitions.find((entry) => (entry.name || entry.key) === item.key);
  const editable = item.kind === "prompts" && Boolean(definition);
  return <Drawer open title={definition?.label || definition?.title || item.key} width={900} onClose={onClose} destroyOnHidden rootClassName="knowledge-drawer"><QueryError query={query} title="知识文档读取失败" />{query.isPending ? <div className="data-loading"><Spin /></div> : query.data ? <><Metadata metadata={query.data.metadata} /><Tabs activeKey={tab} onChange={setTab} destroyOnHidden items={[
    { key: "content", label: editable ? "编辑当前提示词" : "当前内容", children: editable ? <PromptEditor key={query.data.metadata.revision_id} document={query.data} definition={definition} onChanged={onChanged} /> : <><Paragraph className="data-caption">由本地资料生成的只读文档。需要更新时，可在管理页重建对应类别。</Paragraph><pre className="knowledge-content">{knowledgeContent(query.data.content)}</pre></> },
    { key: "history", label: "历史版本与差异", children: <History userId={userId} item={item} current={query.data} onChanged={onChanged} editable={editable} /> },
    { key: "events", label: "更新记录", children: <Events userId={userId} kind={item.kind} documentKey={item.key} /> },
  ]} /></> : null}</Drawer>;
}

export default function KnowledgeCachePanel({ userId }) {
  const queryClient = useQueryClient();
  const { message } = AntApp.useApp();
  const [filters, setFilters] = useState({ page: 1, pageSize: 20 });
  const [scope, setScope] = useState("all");
  const [selected, setSelected] = useState(null);
  const completedTask = useRef("");
  const params = useMemo(() => knowledgeParams(filters), [filters]);
  const overview = useQuery({ queryKey: knowledgeKey(userId, "overview"), queryFn: ({ signal }) => api.getKnowledgeOverview({ signal }), staleTime: 15_000, refetchOnWindowFocus: false });
  const documents = useQuery({ queryKey: knowledgeKey(userId, "documents", params), queryFn: ({ signal }) => api.getKnowledgeDocuments(params, { signal }), staleTime: 15_000, refetchOnWindowFocus: false });
  const prompts = useQuery({ queryKey: knowledgeKey(userId, "prompts"), queryFn: ({ signal }) => api.getKnowledgePrompts({ signal }), staleTime: 30_000, refetchOnWindowFocus: false });
  const status = useQuery({ queryKey: knowledgeKey(userId, "status"), queryFn: ({ signal }) => api.getKnowledgeStatus({ signal }), refetchInterval: (query) => knowledgeTask(query.state.data)?.running ? 2000 : 15000, refetchIntervalInBackground: false, refetchOnWindowFocus: false });
  const task = knowledgeTask(status.data);
  const taskStamp = task && !task.running ? `${task.id}:${task.status}:${task.finished_at}` : "";
  useEffect(() => {
    if (!taskStamp || completedTask.current === taskStamp) return;
    completedTask.current = taskStamp;
    for (const section of ["overview", "documents", "prompts", "document", "history", "events"]) void queryClient.invalidateQueries({ queryKey: knowledgeKey(userId, section) });
  }, [taskStamp, queryClient, userId]);
  const refresh = () => queryClient.invalidateQueries({ queryKey: knowledgeKey(userId) });
  const rebuild = useMutation({ mutationFn: () => api.rebuildKnowledge({ scope }), retry: false, onSuccess: (result) => {
    queryClient.setQueryData(knowledgeKey(userId, "status"), (current) => ({ ...current, worker_enabled: result.worker_enabled, active_job: result.job }));
    message.info(result.deduplicated ? "已有重建任务在处理，可在下方查看进度。" : "重建任务已提交，可在下方查看进度。");
    void status.refetch();
  } });
  const setFilter = (key, value) => setFilters((current) => ({ ...current, [key]: value, page: 1 }));
  const definitions = listItems(prompts.data);
  return <div className="knowledge-cache-panel">
    <div className="data-section-intro"><div><Title level={4}>知识与提示词</Title><Paragraph className="data-caption">集中管理个股特征、新闻相关性依据与提示词。每次变更保留版本、来源和原因。</Paragraph></div><Button icon={<ReloadOutlined aria-hidden="true" />} onClick={() => void refresh()} loading={overview.isFetching || documents.isFetching}>刷新显示</Button></div>
    <QueryError query={overview} title="知识缓存目录读取失败" />
    <div className="knowledge-directory"><span>本地知识缓存目录</span><Text copyable={overview.data?.root_path ? { text: overview.data.root_path } : false}>{overview.data?.root_path || "正在读取…"}</Text><small>{formatDataSize(overview.data?.bytes?.estimated_total)} · 最近更新 {dataTime(overview.data?.last_updated_at)} · 版本与操作记录长期保留</small></div>
    <div className="knowledge-summary">{KNOWLEDGE_KINDS.map((kind) => <button type="button" key={kind.value} className={filters.kind === kind.value ? "is-selected" : ""} onClick={() => setFilter("kind", kind.value)} aria-label={`筛选${kind.label}`}><span>{kind.label}</span><strong>{numberText(overview.data?.kinds?.[kind.value]?.documents)}</strong><small>{numberText(overview.data?.kinds?.[kind.value]?.revisions)} 个版本</small></button>)}</div>
    {definitions.length ? <section className="knowledge-prompt-shortcuts"><Title level={5}>正在使用的提示词</Title><div>{definitions.map((definition) => <article key={definition.name || definition.key}><div><strong>{definition.title || definition.label || definition.name}</strong><small>{definition.overridden ? "已启用自定义版本" : "使用内置默认"}</small></div><Button size="small" onClick={() => setSelected({ kind: "prompts", key: definition.name || definition.key })}>编辑与追踪</Button></article>)}</div></section> : null}
    <div className="knowledge-rebuild"><div><strong>从已保存资料重建</strong><p>更新生成的知识文件并核对完整性。提示词自定义版本会保留。</p></div><Space wrap><Select aria-label="知识重建范围" value={scope} options={scopes} onChange={setScope} /><Button aria-label="重建所选缓存" onClick={() => rebuild.mutate()} loading={rebuild.isPending} disabled={task?.running}>重建所选缓存</Button></Space></div>
    {status.data?.worker_enabled === false ? <Alert className="knowledge-banner" type="info" showIcon message="后台重建服务未启用" description="提交的任务会等待服务启用后处理。" /> : null}
    {status.data ? <Paragraph className="data-caption">{status.data.auto_sync_enabled === false ? "自动同步已暂停" : status.data.next_sync_at ? `下次后台同步：${dataTime(status.data.next_sync_at)}（北京）` : "后台同步计划待确认"}</Paragraph> : null}
    <QueryError query={status} title="重建任务状态读取失败" />{rebuild.isError ? <Alert className="knowledge-banner" type="error" showIcon message="重建任务未提交" description={api.getApiErrorMessage(rebuild.error)} /> : null}
    {task ? <Alert className="knowledge-banner" type={task.type} showIcon message={task.label} description={<div>{task.progressText ? <span>{task.progressText}{task.failed ? ` · ${task.failed} 项失败` : ""}</span> : <span>{task.running ? "任务将在后台处理，关闭页面后仍可继续。" : "可查看下方更新记录。"}</span>}{task.percent !== null ? <Progress percent={task.percent} size="small" status={task.type === "error" ? "exception" : task.running ? "active" : "normal"} /> : null}{task.type === "error" || task.type === "warning" ? <p>部分内容可能未更新，已有版本已保留。可查看更新记录后重试。</p> : null}</div>} /> : null}
    <div className="knowledge-filters"><label><span>知识类别</span><Select aria-label="知识类别筛选" allowClear placeholder="全部类别" value={filters.kind} options={KNOWLEDGE_KINDS} onChange={(value) => setFilter("kind", value)} /></label><label><span>搜索文档</span><Input.Search aria-label="搜索知识文档" placeholder="股票代码、名称或文档标识" maxLength={128} allowClear onSearch={(value) => setFilter("q", value)} enterButton /></label></div>
    <QueryError query={documents} title="知识文档列表读取失败" /><QueryError query={prompts} title="提示词定义读取失败" />
    <Table className="knowledge-documents" size="small" rowKey={(item) => `${item.kind}:${item.key}`} dataSource={listItems(documents.data)} loading={documents.isLoading || documents.isFetching} scroll={{ x: 900 }} pagination={{ current: filters.page, pageSize: filters.pageSize, total: documents.data?.total || 0, showSizeChanger: true, pageSizeOptions: [20, 50, 100], showTotal: (total) => `共 ${total.toLocaleString("zh-CN")} 份文档`, onChange: (page, pageSize) => setFilters((current) => ({ ...current, page: pageSize === current.pageSize ? page : 1, pageSize })) }} locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无匹配文档，可重建对应类别或调整筛选条件。" /> }} columns={[
      { title: "文档", width: 240, render: (_, item) => <div className="knowledge-document-title"><strong>{(definitions.find((entry) => (entry.name || entry.key) === item.key)?.title || definitions.find((entry) => (entry.name || entry.key) === item.key)?.label) || item.provenance?.title || item.provenance?.name || item.key}</strong><small>{item.key}</small></div> },
      { title: "类别 / 来源", width: 195, render: (_, item) => <div>{knowledgeKind(item.kind)}<small className="knowledge-subtext">{knowledgeSource(item)}</small></div> },
      { title: "当前版本", width: 145, render: (_, item) => <div><Text code>{shortRevision(item.revision_id)}</Text><small className="knowledge-subtext">{formatDataSize(item.byte_size)}</small></div> },
      { title: "更新时间（北京）", width: 145, render: (_, item) => dataTime(item.updated_at || item.created_at) },
      { title: "操作", width: 145, fixed: "right", render: (_, item) => <Button size="small" onClick={() => setSelected(item)}>{item.kind === "prompts" && definitions.some((entry) => (entry.name || entry.key) === item.key) ? "编辑与版本管理" : "内容与版本"}</Button> },
    ]} />
    <Collapse className="knowledge-global-events" items={[{ key: "events", label: "最近更新记录", children: <Events userId={userId} compact /> }]} destroyOnHidden />
    {selected ? <DocumentDrawer key={`${selected.kind}:${selected.key}`} item={selected} userId={userId} definitions={definitions} onClose={() => setSelected(null)} onChanged={refresh} /> : null}
  </div>;
}
