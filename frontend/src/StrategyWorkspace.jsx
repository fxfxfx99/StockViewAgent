import { useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, App as AntApp, Button, Card, Empty, Form, Input, InputNumber, Layout, Modal, Popconfirm, Space, Spin, Switch, Tabs, Tag, Typography } from "antd";
import ArrowLeftOutlined from "@ant-design/icons/es/icons/ArrowLeftOutlined";
import PlusOutlined from "@ant-design/icons/es/icons/PlusOutlined";
import ReloadOutlined from "@ant-design/icons/es/icons/ReloadOutlined";
import AppToolbar from "./AppToolbar.jsx";
import { ConfigConsoleButton } from "./ConfigConsole.jsx";
import { useAuth } from "./AuthContext.jsx";
import { safeUrl } from "./components/format.js";
import * as api from "./api.js";
import { knowledgeDocumentPayload, strategyCatalogKey, strategyDate, strategyRagPayload, strategyRetrievalSummary, strategyTab, strategyWorkspaceKey, strategyWorkspacePath } from "./strategyWorkspaceViewModel.js";
import "./StrategyWorkspace.css";

const { Paragraph, Text } = Typography;

function QueryError({ query, title }) {
  return query.isError ? <Alert type="error" showIcon message={title} description={api.getApiErrorMessage(query.error)} action={<Button size="small" onClick={() => void query.refetch()}>重试</Button>} /> : null;
}

function PerspectiveList({ title, values }) {
  return <section className="strategy-perspective-section"><h2>{title}</h2>{values?.length ? <ul>{values.map((value, index) => <li key={index}>{value}</li>)}</ul> : <Text type="secondary">暂无说明。</Text>}</section>;
}

function StrategyIntroduction({ workspace }) {
  const { profile } = workspace;
  return <div className="strategy-stack">
    <div className="strategy-perspective-grid">
      <PerspectiveList title="判断原则与前提" values={profile.principles} />
      <PerspectiveList title="重点观察的信号" values={profile.evidence_focus} />
      <PerspectiveList title="适合研究的问题" values={profile.suitable_for} />
      <PerspectiveList title="不适用的条件" values={profile.not_suitable_for} />
    </div>
    <section className="strategy-update-rule"><h2>观点更新</h2><p>{profile.update_policy}</p><p className="strategy-small">新资料和配置在下一次分析时生效，已保存的历史判断保留。</p></section>
  </div>;
}

function KnowledgeDocument({ document, editable, onEdit, onDelete, deleting }) {
  const url = safeUrl(document.source_url);
  return <article className="strategy-document">
    <div className="strategy-document-heading"><div className="strategy-document-title"><h3>{document.title}</h3>{!editable ? <Tag>{document.origin === "methodology" ? "策略方法" : "共享资料"}</Tag> : null}</div>
      <Space size={4} wrap>{url ? <a className="strategy-source-link" href={url} target="_blank" rel="noopener noreferrer">来源 ↗</a> : null}{editable ? <><Button size="small" onClick={() => onEdit(document)} disabled={deleting}>编辑</Button><Popconfirm title="删除这份策略资料？" description="之后的分析将不再检索此资料，已保存的历史判断保留。" okText="删除" cancelText="取消" onConfirm={() => onDelete(document.id)}><Button size="small" danger loading={deleting}>删除</Button></Popconfirm></> : null}</Space>
    </div>
    <div className="strategy-document-meta">{document.publisher ? <span>{document.publisher}</span> : null}<span>发布 {document.published_at ? strategyDate(document.published_at) : "未填写"}</span><span>更新 {strategyDate(document.updated_at || document.created_at)}（北京）</span><span>版本 {document.version ?? "—"}</span></div>
    <Paragraph ellipsis={{ rows: 2, expandable: true, symbol: "展开正文" }} className="strategy-document-content">{document.content || document.summary || "暂无正文"}</Paragraph>
    {document.tags?.length ? <div className="strategy-document-tags">{document.tags.map((tag) => <Tag key={tag}>{tag}</Tag>)}</div> : null}
  </article>;
}

function DocumentEditor({ strategyId, document, onClose, onSaved }) {
  const { message } = AntApp.useApp();
  const [form] = Form.useForm();
  const [validationError, setValidationError] = useState("");
  const save = useMutation({ mutationFn: (body) => api.saveStrategyKnowledge(strategyId, body, document?.id), onSuccess: async (result) => { await onSaved(result); message.success("策略资料已保存，下次分析将重新检索。"); onClose(); } });
  async function submit() {
    try {
      const values = await form.validateFields();
      const body = knowledgeDocumentPayload(values);
      setValidationError("");
      save.mutate(body);
    } catch (error) { if (!error.errorFields) setValidationError(error.message); }
  }
  return <Modal className="strategy-document-editor" title={document?.id ? "编辑策略资料" : "添加策略资料"} open onCancel={() => !save.isPending && onClose()} onOk={() => void submit()} confirmLoading={save.isPending} closable={!save.isPending} maskClosable={!save.isPending} keyboard={!save.isPending} cancelButtonProps={{ disabled: save.isPending }} okText="保存资料" cancelText="取消" width={760}>
    <Paragraph type="secondary">保存到当前账户、当前策略的知识库。填写实际研究内容；来源链接用于溯源，不会自动抓取网页。</Paragraph>
    {validationError || save.isError ? <Alert className="strategy-form-alert" type="error" showIcon message={validationError || api.getApiErrorMessage(save.error)} /> : null}
    <Form form={form} layout="vertical" disabled={save.isPending} initialValues={{ title: document?.title || "", content: document?.content || "", source_url: document?.source_url || "", published_at: document?.published_at?.slice(0, 10) || "", tags: (document?.tags || []).join("，") }}>
      <Form.Item name="title" label="资料标题" rules={[{ required: true, whitespace: true, message: "请填写资料标题" }]}><Input maxLength={160} showCount /></Form.Item>
      <Form.Item name="content" label="研究内容" rules={[{ required: true, whitespace: true, message: "请填写研究内容" }]}><Input.TextArea rows={6} maxLength={20000} showCount placeholder="记录事实、观点、适用条件，以及什么证据会让你修正判断。" /></Form.Item>
      <Form.Item name="source_url" label="来源链接（可选）"><Input placeholder="https://…" maxLength={2048} /></Form.Item>
      <div className="strategy-form-grid"><Form.Item name="published_at" label="资料发布日期（可选）"><Input type="date" /></Form.Item><Form.Item name="tags" label="标签（可选，用逗号分隔）"><Input placeholder="财报，产业变化，反向证据" /></Form.Item></div>
    </Form>
    <Text type="secondary" className="strategy-small">历史分析还会检查资料的保存与修改时间；填写旧的发布日期不会将新资料带入过去。</Text>
  </Modal>;
}

function StrategyKnowledge({ workspace, strategyId, isAdmin, onSaved }) {
  const { message } = AntApp.useApp();
  const [editing, setEditing] = useState(null);
  const remove = useMutation({ mutationFn: (id) => api.deleteStrategyKnowledge(strategyId, id), onSuccess: async (result) => { await onSaved(result); message.success("资料已删除。"); } });
  const refresh = useMutation({ mutationFn: () => api.refreshStrategyPublicKnowledge(strategyId), onSuccess: async (result) => { await onSaved(result); } });
  const refreshResult = refresh.data?.refresh_result;
  return <div className="strategy-stack">
    <div className="strategy-section-heading"><p>个人资料仅用于当前账户、当前策略。</p><Space wrap>{isAdmin ? <Button icon={<ReloadOutlined aria-hidden="true" />} loading={refresh.isPending} onClick={() => refresh.mutate()}>更新公开资料</Button> : null}<Button type="primary" icon={<PlusOutlined aria-hidden="true" />} onClick={() => setEditing({})}>添加资料</Button></Space></div>
    {remove.isError ? <Alert type="error" showIcon message="资料未删除" description={api.getApiErrorMessage(remove.error)} /> : null}
    {refresh.isError ? <Alert type="error" showIcon message="公开资料未更新" description={api.getApiErrorMessage(refresh.error)} /> : null}
    {refreshResult ? <Alert type={refreshResult.errors?.length ? "warning" : "success"} showIcon message={`已检查 ${refreshResult.sources_checked ?? 0} 个来源，写入 ${refreshResult.items_upserted ?? 0} 份资料`} description={refreshResult.errors?.length ? `${refreshResult.errors.length} 个来源未完成更新，已有资料保留。可稍后重试。` : "新的公开资料将在下一次分析时参与检索。"} /> : null}
    <section><div className="strategy-subheading"><h3>我的资料</h3><Tag>{workspace.documents.length} 份</Tag></div>
      {workspace.documents.length ? <div className="strategy-document-list">{workspace.documents.map((document) => <KnowledgeDocument key={document.id} document={document} editable onEdit={setEditing} onDelete={(id) => remove.mutate(id)} deleting={remove.isPending && remove.variables === document.id} />)}</div> : <div className="strategy-empty"><Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无个人资料。添加研究、失效条件或反向证据。" /></div>}
    </section>
    <section><div className="strategy-subheading"><h3>共享资料与策略方法</h3><Tag>{workspace.builtin_documents.length} 份</Tag></div>
      {workspace.builtin_documents.length ? <div className="strategy-document-list">{workspace.builtin_documents.map((document) => <KnowledgeDocument key={`${document.origin}:${document.id}`} document={document} />)}</div> : <div className="strategy-empty"><Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无共享资料" /></div>}
    </section>
    {editing ? <DocumentEditor key={editing.id || "new"} strategyId={strategyId} document={editing} onClose={() => setEditing(null)} onSaved={onSaved} /> : null}
  </div>;
}

function RetrievalPreview({ strategyId, enabled, dirty }) {
  const [question, setQuestion] = useState("");
  const [asOf, setAsOf] = useState("");
  const preview = useMutation({ mutationFn: () => api.retrieveStrategyKnowledge(strategyId, { query: question.trim(), ...(asOf ? { as_of: asOf } : {}) }) });
  const summary = strategyRetrievalSummary(preview.data?.metadata);
  return <Card title="检索试用" className="strategy-preview">
    <Paragraph type="secondary">用研究问题检查资料匹配情况，当前使用关键词检索。</Paragraph>
    <Form layout="vertical" onFinish={() => preview.mutate()}>
      <Form.Item label="研究问题"><Input.TextArea aria-label="检索研究问题" rows={3} maxLength={2000} value={question} onChange={(event) => { setQuestion(event.target.value); preview.reset(); }} placeholder="例如：盈利质量变化会如何影响当前判断？" /></Form.Item>
      <Form.Item label="历史截止日期（可选）" extra="留空使用当前配置；历史日期仅使用当时的配置和可用资料。"><Input type="date" aria-label="检索历史截止日期" value={asOf} onChange={(event) => { setAsOf(event.target.value); preview.reset(); }} /></Form.Item>
      <Button htmlType="submit" loading={preview.isPending} disabled={!enabled || dirty || !question.trim()}>检索资料</Button>
      {dirty ? <Text className="strategy-inline-note" type="secondary">请先保存配置再试用。</Text> : !enabled ? <Text className="strategy-inline-note" type="secondary">启用并保存检索增强后可试用。</Text> : null}
    </Form>
    {preview.isError ? <Alert type="error" showIcon message="检索未完成" description={api.getApiErrorMessage(preview.error)} /> : null}
    {summary ? <div className="strategy-preview-results" aria-live="polite">
      <div className="strategy-status-line"><Tag color="blue">命中 {summary.retrieved} 份</Tag><span>候选 {summary.candidates} 份</span><span>时效排除 {summary.stale} 份</span><span>截止日期后不可用 {summary.afterCutoff} 份</span></div>
      <p className="strategy-small">检索截止 {strategyDate(summary.asOf)}（北京） · 配置版本 {preview.data.config?.version ?? preview.data.metadata?.config_version ?? "—"} · 知识版本 <code>{summary.version}</code></p>
      {preview.data.documents?.length ? preview.data.documents.map((document) => <div className="strategy-preview-result" key={`${document.origin}:${document.id}`}><strong>{document.title}</strong><Space wrap>{typeof document.score === "number" ? <Tag>相关度 {document.score.toFixed(2)}</Tag> : null}{document.matched_terms?.length ? <span className="strategy-small">命中：{document.matched_terms.join("、")}</span> : null}</Space><Paragraph ellipsis={{ rows: 3, expandable: true, symbol: "展开" }}>{document.content || document.summary}</Paragraph>{safeUrl(document.source_url) ? <a href={safeUrl(document.source_url)} target="_blank" rel="noopener noreferrer">查看来源 ↗</a> : null}</div>) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有匹配资料。可调整研究问题，或补充相关资料。" />}
    </div> : null}
  </Card>;
}

function StrategyRag({ workspace, strategyId, onSaved }) {
  const { message } = AntApp.useApp();
  const [form] = Form.useForm();
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState("");
  const config = workspace.rag_config;
  const save = useMutation({ mutationFn: (body) => api.saveStrategyRag(strategyId, body), onSuccess: async (result) => { await onSaved(result); setDirty(false); message.success("检索配置已保存，下次分析生效。"); } });
  function submit(values) { try { const body = strategyRagPayload(values); setError(""); save.mutate(body); } catch (problem) { setError(problem.message); } }
  return <div className="strategy-rag-grid">
    <Card title="检索设置" className="strategy-rag-settings">
      {error || save.isError ? <Alert className="strategy-form-alert" type="error" showIcon message={error || api.getApiErrorMessage(save.error)} /> : null}
      <Form form={form} layout="vertical" initialValues={config} disabled={save.isPending} onValuesChange={() => setDirty(true)} onFinish={submit}>
        <div className="strategy-toggle-row"><div><label htmlFor="strategy-rag-enabled">启用检索增强</label><p>将相关研究材料用于策略解释与风险提示。</p></div><Form.Item name="enabled" valuePropName="checked" noStyle><Switch id="strategy-rag-enabled" /></Form.Item></div>
        <div className="strategy-form-grid"><Form.Item name="top_k" label="每次最多引用条数" rules={[{ required: true, message: "请填写检索条数" }]}><InputNumber min={1} max={20} precision={0} /></Form.Item><Form.Item name="max_age_days" label="资料时效（天）" extra="留空不限；策略方法不受时效限制。"><InputNumber min={1} max={3650} precision={0} placeholder="不限" /></Form.Item></div>
        <div className="strategy-toggle-row"><label htmlFor="strategy-rag-builtin">包含共享资料与策略方法</label><Form.Item name="include_builtin" valuePropName="checked" noStyle><Switch id="strategy-rag-builtin" /></Form.Item></div>
        <div className="strategy-toggle-row"><div><label htmlFor="strategy-rag-recent">优先较新的相关资料</label><p>在相关性匹配基础上考虑更新时间。</p></div><Form.Item name="prefer_recent" valuePropName="checked" noStyle><Switch id="strategy-rag-recent" /></Form.Item></div>
        <Button type="primary" htmlType="submit" loading={save.isPending} disabled={!dirty}>保存配置</Button>
      </Form>
    </Card>
    <RetrievalPreview key={`${workspace.knowledge_version}:${config.version}`} strategyId={strategyId} enabled={config.enabled} dirty={dirty} />
  </div>;
}

function StrategyCatalog({ catalog }) {
  const profiles = catalog.data?.strategies || [];
  return <div className="strategy-catalog">
    <header className="strategy-page-heading"><div className="strategy-title-row"><h1>策略与知识库</h1>{profiles.length ? <span className="strategy-count">{profiles.length} 个研究视角</span> : null}</div><p>选择策略，了解判断原则、维护资料与配置检索。</p></header>
    <QueryError query={catalog} title="策略目录读取失败" />
    {catalog.isPending ? <div className="strategy-loading"><Spin /></div> : null}
    <div className="strategy-catalog-grid">{profiles.map((profile) => <Link className="strategy-catalog-card" to={strategyWorkspacePath(profile.id)} key={profile.id}><div className="strategy-catalog-card-title"><h2>{profile.name}</h2><span aria-hidden="true">↗</span></div><p>{profile.style}</p><span className="strategy-catalog-timeframe">{profile.timeframe}</span></Link>)}</div>
    {!catalog.isPending && !catalog.isError && !profiles.length ? <Empty description="暂无策略" /> : null}
  </div>;
}

function WorkspaceVersions({ workspace }) {
  const { profile, stats, rag_config: config } = workspace;
  return <details className="strategy-workspace-versions"><summary>版本与更新说明</summary><div className="strategy-status-line"><span>方法 v{profile.version}</span><span>RAG 配置 v{config.version ?? "—"} · {config.enabled ? "已启用" : "已关闭"}</span><span>个人资料 {stats.document_count} 份 · 共享与方法 {stats.builtin_count} 份</span></div><p className="strategy-small">知识版本 <code>{workspace.knowledge_version}</code> · 最近更新 {strategyDate(stats.last_updated_at)}（北京）</p><p className="strategy-small">历史分析按资料的保存、修改与发布时间检查可用性，并使用当时的 RAG 配置，避免引入后来获得的信息。配置更新于 {strategyDate(config.updated_at)}（北京）。</p></details>;
}

function StrategyContent({ strategyId }) {
  const { user, authRequired, logout, isAdmin } = useAuth();
  const [searchParams, setSearchParams] = useSearchParams();
  const queryClient = useQueryClient();
  const catalog = useQuery({ queryKey: strategyCatalogKey(user?.id), queryFn: ({ signal }) => api.getStrategyCatalog({ signal }), staleTime: 60_000 });
  const workspace = useQuery({ queryKey: strategyWorkspaceKey(user?.id, strategyId), queryFn: ({ signal }) => api.getStrategyWorkspace(strategyId, { signal }), enabled: Boolean(strategyId), staleTime: 15_000 });
  async function onSaved(result) {
    const key = strategyWorkspaceKey(user?.id, strategyId);
    await queryClient.cancelQueries({ queryKey: key, exact: true });
    queryClient.setQueryData(key, result);
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: key, exact: true }),
      queryClient.invalidateQueries({ queryKey: ["transaction-agent", "views", user?.id ?? "guest"] }),
    ]);
  }
  const data = workspace.data;
  return <Layout className="sva-layout strategy-page">
    <AppToolbar active="strategies" user={user} authRequired={authRequired} onLogout={logout} settingsAction={<ConfigConsoleButton isAdmin={isAdmin} />} />
    <main className="strategy-main">
      {!strategyId ? <StrategyCatalog catalog={catalog} /> : <>
        <div className="strategy-back"><Link to="/strategies"><ArrowLeftOutlined aria-hidden="true" /> 全部策略</Link><Link to="/">返回个股分析</Link></div>
        <QueryError query={workspace} title="策略知识库读取失败" />
        {workspace.isPending ? <div className="strategy-loading"><Spin size="large" aria-label="加载策略知识库" /></div> : null}
        {data ? <>
          <div className="strategy-workspace-layout">
            <aside className="strategy-sidebar"><div className="strategy-sidebar-label">切换研究视角</div><nav aria-label="策略列表">{(catalog.data?.strategies || []).map((profile) => <Link key={profile.id} to={strategyWorkspacePath(profile.id, strategyTab(searchParams.get("tab")))} className={profile.id === strategyId ? "is-active" : undefined} aria-current={profile.id === strategyId ? "page" : undefined}>{profile.name}</Link>)}</nav><QueryError query={catalog} title="策略列表读取失败" /></aside>
            <section className="strategy-workspace-content"><header className="strategy-page-heading strategy-detail-heading"><div><div className="strategy-title-row"><h1>{data.profile.name}</h1><Tag>{data.profile.timeframe}</Tag></div><p>{data.profile.lens}</p></div><Button icon={<ReloadOutlined aria-hidden="true" />} loading={workspace.isFetching} onClick={() => void workspace.refetch()}>刷新</Button></header><Tabs activeKey={strategyTab(searchParams.get("tab"))} onChange={(tab) => setSearchParams((previous) => { const next = new URLSearchParams(previous); if (tab === "overview") next.delete("tab"); else next.set("tab", tab); return next; })} items={[
              { key: "overview", label: "策略介绍", children: <StrategyIntroduction workspace={data} /> },
              { key: "knowledge", label: `知识库 · ${data.stats.document_count + data.stats.builtin_count}`, children: <StrategyKnowledge workspace={data} strategyId={strategyId} isAdmin={isAdmin} onSaved={onSaved} /> },
              { key: "rag", label: "RAG 配置", children: <StrategyRag key={data.rag_config.version} workspace={data} strategyId={strategyId} onSaved={onSaved} /> },
            ]} /><WorkspaceVersions workspace={data} /></section>
          </div>
        </> : null}
      </>}
    </main>
  </Layout>;
}

export default function StrategyWorkspace() {
  const { user } = useAuth();
  const { strategyId } = useParams();
  return <StrategyContent key={`${user?.id ?? "guest"}:${strategyId || "catalog"}`} strategyId={strategyId} />;
}
