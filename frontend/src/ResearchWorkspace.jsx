import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { Alert, App as AntApp, Button, Collapse, Empty, Form, Input, Layout, Modal, Popconfirm, Select, Spin, Tabs, Tag } from "antd";
import AppToolbar from "./AppToolbar.jsx";
import { ConfigConsoleButton } from "./ConfigConsole.jsx";
import { useAuth } from "./AuthContext.jsx";
import { EvidenceCard, requiredText } from "./TrackingComponents.jsx";
import { trackingTime } from "./trackingViewModel.js";
import { coverageLines, monitoringError, RUN_STATUS } from "./monitoringViewModel.js";
import { INVESTMENT_ASSESSMENT, dossierPayload, dossierSymbols, dossierValues, filteredDossiers, investmentConflict, investmentKey, investmentUrl, investmentWorking, observationUrl, sameResearchTarget, stockContextUrl } from "./investmentViewModel.js";
import * as api from "./api.js";
import "./ResearchWorkspace.css";

const EMPTY = [];
export function ResearchFailure({ error, retry }) { return error ? <Alert className="ir-alert" type="warning" showIcon message={monitoringError(error)} action={retry ? <Button size="small" onClick={retry}>重新读取</Button> : null} /> : null; }
function TextLines({ items }) { const values = (Array.isArray(items) ? items : items ? [items] : []).filter(Boolean); return values.length ? <ul className="ir-lines">{values.map((item, index) => <li key={index}>{typeof item === "string" ? item : item.text || item.note || JSON.stringify(item)}</li>)}</ul> : <p className="ir-muted">未提供</p>; }
function References({ ids }) { return ids?.length ? <span className="ir-caption">证据 {ids.join("、")}</span> : null; }

function DossierEditor({ dossier, initial, onClose, onSaved, onReload }) {
  const [form] = Form.useForm();
  const [revision, setRevision] = useState(dossier?.revision);
  const [reloadError, setReloadError] = useState(null);
  const type = Form.useWatch("target_type", form) || dossier?.target_type || initial?.target_type || "stock";
  const save = useMutation({ mutationFn: (values) => { const payload = dossierPayload({ ...values, monitor_run_id: initial?.monitor_run_id }); return dossier ? api.updateInvestmentDossier(dossier.id, { ...payload, revision }) : api.createInvestmentDossier(payload); }, retry: false, onSuccess: onSaved });
  return <Modal open title={dossier ? "编辑研究档案" : "新建研究档案"} onCancel={onClose} footer={null} width={650} className="ir-modal" maskClosable={false}><ResearchFailure error={save.error || reloadError} />{investmentConflict(save.error) ? <Button onClick={async () => { try { const data = await onReload(); setRevision(data.dossier.revision); save.reset(); } catch (error) { setReloadError(error); } }}>载入最新版本，保留输入</Button> : null}
    <Form form={form} layout="vertical" initialValues={dossierValues(dossier, initial)} onFinish={(values) => save.mutate(values)} disabled={save.isPending}>
      <Form.Item name="title" label="研究名称" rules={requiredText}><Input maxLength={120} placeholder="例如：订单增长能否改善现金流？" /></Form.Item>
      <div className="ir-form-grid"><Form.Item name="target_type" label="研究对象"><Select options={[{ value: "stock", label: "个股" }, { value: "stock_list", label: "股票列表" }, { value: "topic", label: "主题" }]} /></Form.Item>{type === "stock" ? <Form.Item name="target_code" label="股票代码" rules={requiredText}><Input placeholder="600519.SS" /></Form.Item> : null}</div>
      {type === "stock_list" ? <Form.Item name="target_symbols" label="股票列表成员" rules={requiredText} extra="1—20项，用逗号、空格或换行分隔。"><Input.TextArea autoSize={{ minRows: 2, maxRows: 4 }} /></Form.Item> : null}
      <Form.Item name="objective" label="想解决的问题" rules={requiredText}><Input.TextArea maxLength={4000} autoSize={{ minRows: 3, maxRows: 6 }} /></Form.Item>
      <Form.Item name="thesis" label="当前判断（可选）"><Input.TextArea maxLength={4000} autoSize={{ minRows: 2, maxRows: 5 }} placeholder="写下原判断，便于后续复盘。" /></Form.Item>
      <Form.Item name="keywords" label={type === "topic" ? "主题关键词（必填）" : "关键词（可选）"} rules={type === "topic" ? requiredText : []}><Input maxLength={1000} placeholder="逗号分隔，最多10个" /></Form.Item>
      {dossier ? <Form.Item name="status" label="档案状态"><Select options={[{ value: "active", label: "正在研究" }, { value: "archived", label: "已归档" }]} /></Form.Item> : null}
      <div className="ir-actions"><Button onClick={onClose}>取消</Button><Button type="primary" htmlType="submit" loading={save.isPending} disabled={investmentConflict(save.error)}>保存研究档案</Button></div>
    </Form>
  </Modal>;
}

function QuestionForm({ dossierId, question, onSaved, onClose, onReload }) {
  const [revision, setRevision] = useState(question?.revision);
  const [reloadError, setReloadError] = useState(null);
  const save = useMutation({ mutationFn: (values) => question ? api.updateInvestmentQuestion(question.id, { ...values, revision }) : api.createInvestmentQuestion(dossierId, values), retry: false, onSuccess: onSaved });
  return <Modal open title={question ? "更新研究问题" : "添加研究问题"} footer={null} onCancel={onClose} className="ir-modal"><ResearchFailure error={save.error || reloadError} />{investmentConflict(save.error) ? <Button onClick={async () => { try { const latest = await onReload(); setRevision(latest.revision); save.reset(); } catch (error) { setReloadError(error); } }}>载入最新问题版本，保留输入</Button> : null}<Form layout="vertical" initialValues={question || { text: "", status: "open", answer: "" }} onFinish={(values) => save.mutate(values)} disabled={save.isPending}><Form.Item name="text" label="问题" rules={requiredText}><Input.TextArea maxLength={1000} autoSize={{ minRows: 3, maxRows: 6 }} /></Form.Item><Form.Item name="status" label="处理状态"><Select options={[{ value: "open", label: "未解决" }, { value: "answered", label: "已回答" }, { value: "parked", label: "暂时搁置" }]} /></Form.Item><Form.Item name="answer" label="回答或处理说明"><Input.TextArea maxLength={4000} autoSize={{ minRows: 2, maxRows: 5 }} /></Form.Item><div className="ir-actions"><Button type="primary" htmlType="submit" loading={save.isPending} disabled={investmentConflict(save.error)}>保存问题</Button></div></Form></Modal>;
}

function Materials({ dossierId, materials, userId, onChanged }) {
  const [form] = Form.useForm();
  const [kind, setKind] = useState("note");
  const [file, setFile] = useState(null);
  const [selected, setSelected] = useState(null);
  const save = useMutation({ mutationFn: (values) => kind === "upload" ? api.uploadInvestmentMaterial(dossierId, file, values.title) : api.createInvestmentMaterial(dossierId, { ...values, kind }), retry: false, onSuccess: async () => { form.resetFields(); setFile(null); await onChanged(); } });
  const prepare = useMutation({ mutationFn: api.prepareInvestmentMaterial, retry: false, onSuccess: onChanged });
  const remove = useMutation({ mutationFn: api.deleteInvestmentMaterial, retry: false, onSuccess: onChanged });
  const detail = useQuery({ queryKey: investmentKey(userId, "material", selected), queryFn: ({ signal }) => api.getInvestmentMaterial(selected, { signal }), enabled: Boolean(selected), retry: false });
  return <div><ResearchFailure error={save.error || remove.error || prepare.error} />
    <Form form={form} layout="vertical" onFinish={(values) => save.mutate(values)} disabled={save.isPending}><div className="ir-form-grid"><Form.Item label="材料类型"><Select value={kind} onChange={setKind} options={[{ value: "note", label: "研究笔记" }, { value: "text", label: "原文片段" }, { value: "upload", label: "上传 TXT / MD / PDF" }]} /></Form.Item><Form.Item name="title" label="材料名称" rules={requiredText}><Input maxLength={200} /></Form.Item></div>{kind === "upload" ? <label className="ir-file">选择材料文件<input type="file" accept=".txt,.md,.pdf" aria-label="选择研究材料文件"  onChange={(event) => setFile(event.target.files?.[0] || null)} />{file ? <span className="ir-caption">已选：{file.name}</span> : null}</label> : <><Form.Item name="text" label="材料正文" rules={requiredText}><Input.TextArea autoSize={{ minRows: 4, maxRows: 10 }} maxLength={50000} /></Form.Item><Form.Item name="source_url" label="原始来源地址（可选）"><Input type="url" placeholder="https://…" /></Form.Item></>}<Button htmlType="submit" loading={save.isPending} disabled={kind === "upload" && !file}>保存材料</Button></Form>
    <div className="ir-materials">{materials.map((item) => <article key={item.id}><div><button className="ir-text-button" onClick={() => setSelected(item.id)}>{item.title || "研究材料"}</button><p className="ir-caption">{item.kind === "monitoring" ? "监控证据" : item.kind === "note" ? "研究笔记" : "原文材料"} · {trackingTime(item.created_at)}</p>{item.status ? <Tag>{({ pending: "等待解析", ready: "可用", partial: "部分可读", error: "解析未完成" })[item.status] || item.status}</Tag> : null}{item.error_code ? <p className="ir-caption">{item.error_code}</p> : null}{item.warnings?.length ? <TextLines items={item.warnings} /> : null}{item.kind === "pdf" && item.status === "error" ? <Button size="small" loading={prepare.isPending} onClick={() => prepare.mutate(item.id)}>重试解析</Button> : null}</div><Popconfirm title="删除这份材料？" onConfirm={() => remove.mutate(item.id)} okText="删除" cancelText="取消"><Button size="small" danger>删除材料</Button></Popconfirm></article>)}</div>
    {selected ? <Modal open title={detail.data?.title || "材料原文"} footer={null} onCancel={() => setSelected(null)} width={760} className="ir-modal"><ResearchFailure error={detail.error} retry={() => detail.refetch()} />{detail.isPending ? <Spin /> : <div className="ir-material-text">{detail.data?.monitoring_snapshot ? <><p>{detail.data.monitoring_snapshot.report?.summary || "监控记录"}</p>{(detail.data.monitoring_snapshot.evidence || []).map((item, index) => <EvidenceCard key={item.id || index} evidence={item} />)}</> : detail.data?.text || detail.data?.material?.text || "无可读文本"}{detail.data?.pages?.length ? <p className="ir-caption">包含 {detail.data.pages.length} 页</p> : null}</div>}</Modal> : null}
  </div>;
}

export function InvestmentReport({ report }) {
  const result = report?.result || {};
  const content = result.report || {};
  const evidence = result.evidence || EMPTY;
  return <div className="ir-report"><div className="ir-meta"><Tag>{INVESTMENT_ASSESSMENT[content.assessment] || "研究结果"}</Tag><span>{result.model || "模型未标注"}</span><span>{trackingTime(report.created_at || result.generated_at)}</span>{result.dossier_snapshot?.revision ? <span>档案版本 {result.dossier_snapshot.revision}</span> : null}</div><div className="ir-summary"><h3>研究结论</h3><p>{content.summary || "暂无结论"}</p></div>
    <section><h3>证据与发现</h3>{content.findings?.length ? content.findings.map((item, index) => <article className="ir-finding" key={index}><Tag color={item.kind === "gap" ? "orange" : "default"}>{{ fact: "事实", interpretation: "分析判断", gap: "信息缺口" }[item.kind] || "观察"}</Tag><p>{item.text}</p><References ids={item.evidence_ids} /></article>) : <p className="ir-muted">暂无发现</p>}</section>
    <section><h3>判断变化</h3>{content.judgment_changes?.length ? content.judgment_changes.map((item, index) => <article className="ir-change" key={index}><dl><dt>此前</dt><dd>{item.before || "未记录"}</dd><dt>现在</dt><dd>{item.after}</dd><dt>依据</dt><dd>{item.reason}</dd></dl><References ids={item.evidence_ids} /></article>) : <p className="ir-muted">暂无判断变化</p>}</section>
    <section><h3>未决问题</h3>{content.open_questions?.length ? content.open_questions.map((item, index) => <article className="ir-question-result" key={index}><strong>{item.question}</strong><p>{item.why}</p></article>) : <p className="ir-muted">暂无新增问题</p>}</section>
    <section><h3>下一步观察</h3>{content.next_observations?.length ? content.next_observations.map((item, index) => <article className="ir-next" key={index}><strong>{item.title}</strong><p>{item.goal}</p><p className="ir-caption">观察：{item.watch_conditions}<br />失效：{item.invalidation_conditions}</p><References ids={item.evidence_ids} /><Link to={observationUrl(report.id, index)}>建立监控计划 ↗</Link></article>) : <p className="ir-muted">暂无观察条件</p>}</section>
    <Collapse items={[{ key: "sources", label: `证据与覆盖（${evidence.length}）`, children: <><TextLines items={coverageLines(result.coverage)} />{evidence.map((item, index) => <EvidenceCard key={item.id || index} evidence={{ ...item, title: `${item.id ? `${item.id} · ` : ""}${item.title || "原始证据"}`, snapshot: { source: item.source } }} />)}</> }, { key: "limits", label: "风险与局限", children: <TextLines items={[content.risks, content.limitations].flat()} /> }]} />
  </div>;
}

function DossierDetail({ id, userId, model, usage, worker, onChanged }) {
  const { message } = AntApp.useApp();
  const [tab, setTab] = useState("result");
  const [editing, setEditing] = useState(false);
  const [question, setQuestion] = useState(null);
  const [focus, setFocus] = useState("");
  const [reportId, setReportId] = useState(null);
  const detail = useQuery({ queryKey: investmentKey(userId, "dossier", id), queryFn: ({ signal }) => api.getInvestmentDossier(id, { signal }), retry: false, refetchInterval: (query) => query.state.data?.jobs?.some(investmentWorking) ? 2500 : 15000, refetchIntervalInBackground: false });
  const data = detail.data;
  const dossier = data?.dossier;
  const jobs = data?.jobs || EMPTY;
  const reports = [...(data?.reports || EMPTY)].sort((a, b) => Number(b.created_at) - Number(a.created_at));
  const selected = reports.find((item) => item.id === reportId) || reports[0];
  const active = jobs.some(investmentWorking);
  const pendingMaterials = (data?.materials || EMPTY).some((item) => item.status === "pending");
  async function refresh() { await Promise.all([detail.refetch(), onChanged()]); }
  const generate = useMutation({ mutationFn: () => api.generateInvestmentReport(id, focus.trim()), retry: false, onSuccess: async () => { setReportId(null); await refresh(); message.success("研究任务已提交。"); } });
  const adopt = useMutation({ mutationFn: () => api.adoptInvestmentReport(selected.id, dossier.revision), retry: false, onSuccess: async () => { await refresh(); message.success("已设为当前研究。"); } });
  const read = useMutation({ mutationFn: () => api.readInvestmentReport(selected.id, !selected.is_read), retry: false, onSuccess: refresh });
  const cancel = useMutation({ mutationFn: api.cancelInvestmentJob, retry: false, onSuccess: refresh });
  const deleteQuestion = useMutation({ mutationFn: (item) => api.deleteInvestmentQuestion(item.id, item.revision), retry: false, onSuccess: refresh });
  if (detail.isPending) return <div className="ir-loading"><Spin aria-label="读取研究档案" /></div>;
  if (!dossier) return <ResearchFailure error={detail.error} retry={() => detail.refetch()} />;
  const gated = !model?.configured || worker?.enabled === false || active || pendingMaterials || usage?.remaining === 0 || dossier.status === "archived";
  return <div className="ir-detail"><ResearchFailure error={detail.error} retry={() => detail.refetch()} /><header className="ir-detail-heading"><div><div className="ir-meta"><Tag>{dossier.status === "archived" ? "已归档" : "正在研究"}</Tag>{dossierSymbols(dossier).map((symbol) => <Link key={symbol} to={stockContextUrl(symbol)}>{symbol} ↗</Link>)}</div><h2>{dossier.title}</h2><p>{dossier.objective}</p></div><Button onClick={() => setEditing(true)}>编辑档案</Button></header>{dossier.thesis ? <div className="ir-thesis"><span>你的原判断</span><p>{dossier.thesis}</p></div> : null}
    <Tabs activeKey={tab} onChange={setTab} items={[{ key: "result", label: "研究结果" }, { key: "questions", label: `问题 ${data.questions?.length || 0}` }, { key: "materials", label: `材料 ${data.materials?.length || 0}` }, { key: "history", label: "任务与历史" }]} />
    {tab === "result" ? <><div className="ir-generate"><label htmlFor={`ir-focus-${id}`}>本次重点（可选）</label><Input.TextArea id={`ir-focus-${id}`} value={focus} onChange={(event) => setFocus(event.target.value)} maxLength={2000} autoSize={{ minRows: 2, maxRows: 4 }} placeholder="例如：复核新增订单是否改变此前现金流判断。" /><div className="ir-between"><p className="ir-caption">{!model?.configured ? <Link to="/setup">配置模型 ↗</Link> : worker?.enabled === false ? "研究服务已关闭" : pendingMaterials ? "材料解析中，完成后可生成研究。" : active ? "任务处理中" : `今日研究请求 ${usage?.used ?? "—"} / ${usage?.limit ?? "—"}`}</p><Button type="primary" disabled={gated} loading={generate.isPending} onClick={() => generate.mutate()}>生成研究更新</Button></div><ResearchFailure error={generate.error} /></div>
      {selected ? <><div className="ir-report-tools"><Select aria-label="研究报告历史" value={selected.id} onChange={setReportId} options={reports.map((item) => ({ value: item.id, label: `${trackingTime(item.created_at)}${item.id === dossier.adopted_report_id ? " · 当前研究" : ""}${item.is_read ? "" : " · 未读"}` }))} /><Button disabled={adopt.isPending || selected.id === dossier.adopted_report_id} onClick={() => adopt.mutate()}>{selected.id === dossier.adopted_report_id ? "已采纳为当前研究" : "采纳为当前研究"}</Button><Button onClick={() => read.mutate()} loading={read.isPending}>{selected.is_read ? "标为未读" : "标为已读"}</Button></div><ResearchFailure error={adopt.error || read.error} retry={investmentConflict(adopt.error) ? refresh : undefined} /><InvestmentReport key={selected.id} report={selected} /></> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无结果，补充材料后可生成研究。" />}</> : null}
    {tab === "questions" ? <><div className="ir-between"><Button onClick={() => setQuestion({})}>添加问题</Button></div><ResearchFailure error={deleteQuestion.error} />{(data.questions || EMPTY).map((item) => <article className="ir-question" key={item.id}><Tag>{{ open: "未解决", answered: "已回答", parked: "已搁置" }[item.status] || item.status}</Tag><h3>{item.text}</h3>{item.answer ? <p>{item.answer}</p> : null}<div className="ir-actions"><Button size="small" onClick={() => setQuestion(item)}>更新问题</Button><Popconfirm title="删除这个问题？" onConfirm={() => deleteQuestion.mutate(item)} okText="删除" cancelText="取消"><Button size="small">删除问题</Button></Popconfirm></div></article>)}</> : null}
    {tab === "materials" ? <Materials dossierId={id} userId={userId} materials={data.materials || EMPTY} onChanged={refresh} /> : null}
    {tab === "history" ? <><ResearchFailure error={cancel.error} /><h3>执行任务</h3>{jobs.length ? jobs.map((job) => <article className="ir-job" key={job.id}><div className="ir-between"><Tag>{RUN_STATUS[job.status] || job.status}</Tag><span className="ir-caption">{trackingTime(job.created_at)}</span>{investmentWorking(job) ? <Button size="small" loading={cancel.isPending} onClick={() => cancel.mutate(job.id)}>取消任务</Button> : null}</div>{job.error || job.error_code ? <p>{job.error || job.error_code}</p> : null}</article>) : <p className="ir-muted">暂无任务</p>}<h3>研究报告</h3>{reports.map((item) => <button className="ir-history-row" key={item.id} onClick={() => { setReportId(item.id); setTab("result"); }}><strong>{trackingTime(item.created_at)}</strong><span>{item.id === dossier.adopted_report_id ? "当前研究" : "历史版本"} · {item.is_read ? "已读" : "待看"}</span><p>{item.result?.report?.summary || "查看研究结果"}</p></button>)}<h3>采纳记录</h3>{data.adoptions?.length ? data.adoptions.map((item, index) => <p className="ir-caption" key={item.id || index}>{trackingTime(item.created_at)} · 采纳报告 {String(item.report_id || "").slice(0, 12)}</p>) : <p className="ir-muted">尚未采纳结果</p>}</> : null}
    {editing ? <DossierEditor dossier={dossier} onClose={() => setEditing(false)} onReload={async () => (await detail.refetch({ throwOnError: true })).data} onSaved={async () => { setEditing(false); await refresh(); }} /> : null}
    {question ? <QuestionForm dossierId={id} question={question.id ? question : null} onReload={async () => { const value = (await detail.refetch({ throwOnError: true })).data?.questions?.find((item) => item.id === question.id); if (!value) throw new Error("问题已删除，输入已保留。"); return value; }} onClose={() => setQuestion(null)} onSaved={async () => { setQuestion(null); await refresh(); }} /> : null}
  </div>;
}

function ResearchContent() {
  const { user, authRequired, logout } = useAuth();
  const { message } = AntApp.useApp();
  const qc = useQueryClient();
  const [params, setParams] = useSearchParams();
  const [filter, setFilter] = useState("active");
  const [composer, setComposer] = useState(false);
  const [importId, setImportId] = useState(null);
  const [reason, setReason] = useState("");
  const [handoffError, setHandoffError] = useState(null);
  const symbol = params.get("symbol") || "";
  const monitorRun = params.get("monitor_run");
  const notification = params.get("notification");
  const overview = useQuery({ queryKey: investmentKey(user?.id, "overview", symbol), queryFn: ({ signal }) => api.getInvestmentOverview(symbol ? { symbol } : {}, { signal }), retry: false, refetchInterval: 15000, refetchIntervalInBackground: false });
  const context = useQuery({ queryKey: investmentKey(user?.id, "monitor-context", monitorRun), queryFn: ({ signal }) => api.getMonitoringRunContext(monitorRun, { signal }), enabled: Boolean(monitorRun), retry: false });
  const dossiers = overview.data?.dossiers || EMPTY;
  const shown = filteredDossiers(dossiers, filter);
  const selectedId = params.get("dossier") || shown[0]?.id;
  const refresh = () => qc.invalidateQueries({ queryKey: investmentKey(user?.id) });
  function open(id) { setParams((previous) => { const next = new URLSearchParams(previous); if (id) next.set("dossier", id); else next.delete("dossier"); return next; }, { replace: true }); }
  async function finishImport(dossierId) { setHandoffError(null); if (notification) await api.handleMonitoringNotification(notification, { action: "research", reason: reason.trim(), dossier_id: dossierId, run_id: monitorRun }); await refresh(); open(dossierId); message.success("监控证据已导入"); setParams((previous) => { const next = new URLSearchParams(previous); next.delete("monitor_run"); next.delete("notification"); next.set("dossier", dossierId); return next; }, { replace: true }); }
  const importRun = useMutation({ mutationFn: async () => { await api.importInvestmentMonitoring(importId, monitorRun); await finishImport(importId); }, retry: false });
  const matchingDossiers = dossiers.filter((item) => item.status === "active" && sameResearchTarget(item, context.data?.plan_snapshot));
  const importSupported = !context.data || ["stock", "stock_list", "topic"].includes(context.data.plan_snapshot?.target_type);
  const importBlocked = Boolean(monitorRun) && (!context.data || !importSupported || (notification && !reason.trim()));
  const initial = monitorRun && context.data ? { ...context.data.plan_snapshot, title: `${context.data.plan_snapshot?.title || "监控变化"}复核`, target_type: ["stock", "stock_list"].includes(context.data.plan_snapshot?.target_type) ? context.data.plan_snapshot.target_type : "topic", target_code: context.data.plan_snapshot?.target_type === "stock" ? context.data.plan_snapshot.target_code : "", objective: context.data.plan_snapshot?.goal || "复核监控的新变化", thesis: "", status: "active", monitor_run_id: monitorRun } : { target_type: "stock", target_code: symbol };
  return <Layout className="sva-layout ir-layout"><AppToolbar active="research" user={user} authRequired={authRequired} onLogout={logout} settingsAction={<ConfigConsoleButton isAdmin={user?.role === "admin"} />} /><Layout.Content className="ir-content"><header className="ir-heading"><div><h1>研究</h1><p>整理材料、跟踪判断、安排后续观察。</p></div><div className="ir-actions"><Button onClick={() => overview.refetch()} loading={overview.isFetching}>更新状态</Button><Button type="primary" disabled={importBlocked} onClick={() => setComposer(true)}>新建研究</Button></div></header><ResearchFailure error={overview.error} retry={() => overview.refetch()} />
    {monitorRun ? <section className="ir-import"><h2>导入监控证据</h2>{!importSupported ? <Alert type="info" message="暂不支持导入指数监控。" /> : null}<ResearchFailure error={context.error || importRun.error || handoffError} retry={context.isError ? () => context.refetch() : undefined} /><p>{context.isPending ? "读取监控记录…" : `${context.data?.plan_snapshot?.title || "监控记录"} · ${context.data?.evidence?.length || 0} 条证据`}</p><Input.TextArea aria-label="转研究处理原因" placeholder="转研究的原因（处理提醒时必填）" value={reason} onChange={(event) => setReason(event.target.value)} maxLength={2000} autoSize={{ minRows: 2, maxRows: 4 }} /><div className="ir-actions"><Select aria-label="选择已有研究档案" value={importId} onChange={setImportId} options={matchingDossiers.map((item) => ({ value: item.id, label: item.title }))} placeholder="选择相同对象的已有研究" notFoundContent="暂无匹配研究，可新建" /><Button disabled={!matchingDossiers.some((item) => item.id === importId) || !context.data || !importSupported || (notification && !reason.trim())} loading={importRun.isPending} onClick={() => importRun.mutate()}>导入所选研究</Button><Button disabled={!context.data || !importSupported || (notification && !reason.trim())} onClick={() => setComposer(true)}>新建研究并导入</Button></div></section> : null}
    <div className="ir-workspace"><aside className="ir-sidebar"><div className="ir-list-filter"><Select aria-label="研究列表范围" value={filter} onChange={(value) => { setFilter(value); open(null); }} options={[{ value: "active", label: "正在研究" }, { value: "unread", label: "待看更新" }, { value: "working", label: "进行中任务" }, { value: "archived", label: "已归档" }, { value: "all", label: "全部研究" }]} />{symbol ? <Link to="/research">清除个股筛选</Link> : null}</div>{overview.isPending ? <div className="ir-loading"><Spin /></div> : shown.length ? shown.map((item) => <button className={`ir-dossier-row${item.id === selectedId ? " is-selected" : ""}`} key={item.id} onClick={() => open(item.id)}><div className="ir-meta">{item.unread_count > 0 ? <Tag color="blue">{item.unread_count}条待看</Tag> : null}{item.active_jobs > 0 ? <Tag>正在处理</Tag> : null}<span>{dossierSymbols(item).join("、") || "主题研究"}</span></div><strong>{item.title}</strong><p>{item.adopted_report_summary || item.latest_report_summary || item.objective}</p><span className="ir-caption">{item.adopted_report_id ? "已采纳结论" : "尚未采纳结论"} · {trackingTime(item.updated_at)}</span></button>) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无研究档案" />}</aside><main className="ir-main">{selectedId ? <DossierDetail key={`${user?.id}:${selectedId}`} id={selectedId} userId={user?.id} model={overview.data?.model} usage={overview.data?.usage} worker={overview.data?.worker} onChanged={refresh} /> : <div className="ir-empty"><p>选择已有档案，或点击「新建研究」。</p></div>}</main></div>
    {composer ? <DossierEditor initial={initial} onClose={() => setComposer(false)} onSaved={async (dossier) => { setComposer(false); if (monitorRun && context.data) { try { await finishImport(dossier.id); } catch (error) { setImportId(dossier.id); setHandoffError(new Error(`档案与证据已保存，处理记录保存失败：${monitoringError(error)}。请重试导入。`)); await refresh(); open(dossier.id); } } else { await refresh(); open(dossier.id); } }} /> : null}
  </Layout.Content></Layout>;
}
export default function ResearchWorkspace() { const { user } = useAuth(); return <ResearchContent key={user?.id ?? "guest"} />; }
