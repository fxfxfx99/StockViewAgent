import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { Alert, App as AntApp, Badge, Button, Checkbox, Collapse, Drawer, Empty, Form, Input, InputNumber, Layout, Modal, Popconfirm, Radio, Select, Spin, Switch, Tabs, Tag } from "antd";
import PlusOutlined from "@ant-design/icons/es/icons/PlusOutlined";
import ReloadOutlined from "@ant-design/icons/es/icons/ReloadOutlined";
import SafetyCertificateOutlined from "@ant-design/icons/es/icons/SafetyCertificateOutlined";
import AppToolbar from "./AppToolbar.jsx";
import { ConfigConsoleButton } from "./ConfigConsole.jsx";
import { useAuth } from "./AuthContext.jsx";
import { EvidenceCard, futureReviewRule, requiredText } from "./TrackingComponents.jsx";
import { ASSESSMENT_LABELS, REVIEW_LABELS, nextReviewDate, trackingTime } from "./trackingViewModel.js";
import { inferLlmProvider, llmPatchFromForm, normalizeLlmProviders } from "./autopilotViewModel.js";
import { ASSESSMENT, CONDITION_STATUS, FREQUENCY_OPTIONS, INDEX_OPTIONS, HANDLING_STATUS, MONITOR_CHANNELS, PLAN_STATUS, RUN_STATUS, TARGET_OPTIONS, budgetState, coverageLines, modelGate, monitoringError, monitoringInitial, monitoringKey, parseKeywords, parseStockSymbols, planPayload, planValues, reportLines, revisionConflict, runInProgress, runTime, stockSymbolTokens, targetLabel } from "./monitoringViewModel.js";
import { investmentUrl, monitoringDraft, stockContextUrl } from "./investmentViewModel.js";
import * as api from "./api.js";
import "./MonitoringWorkspace.css";

const EMPTY = [];
const textArea = { autoSize: { minRows: 2, maxRows: 6 }, maxLength: 4000, showCount: true };

function Failure({ error, retry, hasData = false }) {
  return error ? <Alert className="monitor-alert" type="warning" showIcon message={monitoringError(error)} description={hasData ? "显示上次读取的记录。" : undefined} action={retry ? <Button size="small" onClick={retry}>重试</Button> : null} /> : null;
}

function ModelForm({ settings, providers, onSaved, onCancel }) {
  const [form] = Form.useForm();
  const [saved, setSaved] = useState(false);
  const [dirty, setDirty] = useState(false);
  const save = useMutation({ mutationFn: async (values) => {
    await api.saveMonitoringModel(llmPatchFromForm(values, settings, providers));
    setSaved(true);
    setDirty(false);
    await onSaved();
  }, retry: false });
  const verify = useMutation({ mutationFn: api.verifyMonitoringModel, retry: false, onSettled: onSaved });
  const provider = Form.useWatch("llm_provider", form);
  const busy = save.isPending || verify.isPending;
  return <><p className="monitor-muted">此配置用于本账户的所有模型功能，包括自动研究。</p>
    <Failure error={save.error || verify.error} />
    {saved ? <Alert className="monitor-alert" type="success" showIcon message="已保存，请验证连接。" /> : null}
    {verify.isSuccess ? <Alert className="monitor-alert" type={(verify.data?.verified ?? verify.data?.model?.verified) ? "success" : "warning"} showIcon message={(verify.data?.verified ?? verify.data?.model?.verified) ? "连接成功，可创建观察计划。" : (verify.data?.error || verify.data?.model?.error) || "连接未通过，请检查配置。"} /> : null}
    <Form form={form} layout="vertical" disabled={busy} initialValues={{ llm_provider: inferLlmProvider(settings, providers), llm_api_base: settings.api_base, llm_model: settings.model }} onValuesChange={() => { setSaved(false); setDirty(true); verify.reset(); }} onFinish={(values) => save.mutate(values)}>
      <Form.Item name="llm_provider" label="大模型供应商" rules={requiredText}><Select options={providers.map(({ value, label }) => ({ value, label }))} onChange={(value) => { const next = providers.find((item) => item.value === value); if (next?.api_base) form.setFieldsValue({ llm_api_base: next.api_base, llm_model: next.model }); form.setFieldValue("llm_api_key", ""); }} /></Form.Item>
      <Form.Item name="llm_api_key" label="API Key" rules={[{ required: !settings.key_configured && !saved, whitespace: true, message: "请填写 API Key。" }]} extra={settings.key_configured ? "留空沿用已保存密钥；更换地址需填写对应密钥。" : "密钥仅提交至当前后端，不保存在浏览器。"}><Input.Password autoComplete="new-password" visibilityToggle={false} /></Form.Item>
      {provider === "custom" ? <><Form.Item name="llm_api_base" label="API Base" rules={requiredText}><Input placeholder="https://your-provider.example/v1" autoComplete="off" /></Form.Item><Form.Item name="llm_model" label="模型名称" rules={requiredText}><Input placeholder="供应商支持的模型 ID" autoComplete="off" /></Form.Item></> : null}
      <div className="monitor-form-actions"><Button onClick={onCancel}>关闭</Button><Button htmlType="submit" loading={save.isPending}>保存模型配置</Button><Button type="primary" disabled={busy || dirty || (!settings.key_configured && !saved)} loading={verify.isPending} onClick={() => verify.mutate()}>验证已保存连接</Button></div>
    </Form><p className="monitor-caption">验证连接会调用一次模型；修改配置后需重新验证。</p>
  </>;
}

function ModelDialog({ userId, onClose }) {
  const qc = useQueryClient();
  const settings = useQuery({ queryKey: monitoringKey(userId, "model-settings"), queryFn: ({ signal }) => api.getLlmSettings({ signal }), staleTime: 0 });
  const presets = useQuery({ queryKey: monitoringKey(userId, "model-presets"), queryFn: ({ signal }) => api.getLlmProviderPresets({ signal }), staleTime: 300_000 });
  const providers = normalizeLlmProviders(presets.data?.providers);
  async function update() { await Promise.all([qc.invalidateQueries({ queryKey: monitoringKey(userId, "overview") }), qc.invalidateQueries({ queryKey: ["settings", "setup-status"] })]); }
  return <Modal title="配置观察模型" open footer={null} onCancel={onClose} width={560} maskClosable={false} className="monitor-modal">
    {settings.isPending || presets.isPending ? <div className="monitor-loading"><Spin /></div> : settings.isError ? <Failure error={settings.error} retry={() => settings.refetch()} /> : <><Failure error={presets.error} /><ModelForm settings={settings.data || {}} providers={providers} onSaved={update} onCancel={onClose} /></>}
  </Modal>;
}

function StockListField({ form }) {
  const { user } = useAuth();
  const [importOpen, setImportOpen] = useState(false);
  const [selected, setSelected] = useState([]);
  const [importError, setImportError] = useState("");
  const watchlist = useQuery({ queryKey: monitoringKey(user?.id, "watchlist-import"), queryFn: api.getWatchlist, enabled: importOpen && user?.id != null, staleTime: 0, retry: false });
  const available = watchlist.data?.symbols || EMPTY;
  function addSelected() {
    try {
      const current = stockSymbolTokens(form.getFieldValue("target_symbols"));
      const symbols = parseStockSymbols([...new Set([...current, ...selected])]);
      form.setFieldValue("target_symbols", symbols.join("，"));
      void form.validateFields(["target_symbols"]).catch(() => {});
      setImportError(""); setImportOpen(false); setSelected([]);
    } catch (error) { setImportError(monitoringError(error)); }
  }
  return <div className="monitor-stock-list-editor"><Form.Item name="target_symbols" label="列表成员（股票代码）" rules={[{ validator: (_, value) => { try { parseStockSymbols(value); return Promise.resolve(); } catch (error) { return Promise.reject(error); } } }]} extra="1—20 只，用逗号、空格或换行分隔。成员独立保存，不随自选股名单变化。"><Input.TextArea autoSize={{ minRows: 3, maxRows: 6 }} maxLength={1000} placeholder="600519，000001.SZ，300750" /></Form.Item>
    <Button size="small" onClick={() => { setImportOpen(!importOpen); setImportError(""); }}>从自选股导入</Button>
    {importOpen ? <div className="monitor-watchlist-import"><Failure error={watchlist.error} retry={() => watchlist.refetch()} /><label htmlFor="monitor-watchlist-members">选择自选成员</label><Select id="monitor-watchlist-members" mode="multiple" value={selected} onChange={setSelected} loading={watchlist.isFetching} options={available.map((symbol) => ({ value: symbol, label: symbol }))} placeholder="选择要加入本计划的股票" notFoundContent={watchlist.isPending ? "读取自选名单…" : "当前账户暂无自选股"} />{importError ? <Alert className="monitor-alert" type="warning" message={importError} /> : null}<div className="monitor-detail-actions"><Button size="small" disabled={!available.length || available.length > 20} onClick={() => setSelected(available)}>全选</Button><Button size="small" type="primary" disabled={!selected.length || selected.length > 20 || watchlist.isFetching || watchlist.isError} onClick={addSelected}>加入所选股票</Button><span className="monitor-caption">已选 {selected.length} / 20{available.length > 20 ? "，请选取部分成员" : ""}</span></div></div> : null}
    <p className="monitor-caption">每轮最多调用一次模型；行情来自已有采集名单。</p>
  </div>;
}

function PlanFields({ form, editing = false }) {
  const type = Form.useWatch("target_type", form) || "stock";
  return <><Form.Item name="title" label="计划名称" rules={requiredText}><Input maxLength={120} placeholder="例如：我的消费股观察列表" /></Form.Item>
    <Form.Item name="goal" label="观察目标" rules={requiredText}><Input.TextArea {...textArea} placeholder="例如：跟踪订单、现金流和量价变化，月底复盘。" /></Form.Item>
    <div className="monitor-form-grid"><Form.Item name="target_type" label="观察对象"><Select options={TARGET_OPTIONS} onChange={(value) => form.setFieldValue("target_code", value === "index" ? "SH_COMP" : "")} /></Form.Item>
      {type === "stock" ? <Form.Item name="target_code" label="股票代码" rules={requiredText} extra="行情来自自选股或采集名单。"><Input placeholder="例如600519或600519.SS" maxLength={20} /></Form.Item> : type === "index" ? <Form.Item name="target_code" label="大盘指数" rules={requiredText}><Select options={INDEX_OPTIONS} /></Form.Item> : <div className="monitor-field-note">{type === "stock_list" ? "同组股票逐股生成结论。" : "按关键词匹配已归档新闻与事件。"}</div>}</div>
    {type === "stock_list" ? <StockListField form={form} /> : null}
    <Form.Item name="channels" label="监控来源" rules={[{ type: "array", min: 1, required: true, message: "至少选择一种监控来源。" }]}><Checkbox.Group options={Object.entries(MONITOR_CHANNELS).map(([value, label]) => ({ value, label }))} /></Form.Item>
    <Form.Item name="keywords" label="关联关键词" extra={type === "stock_list" ? "最多 10 个，逗号分隔。股票按代码和公司名匹配；关键词用于指定关注重点。" : "最多 10 个，逗号分隔，用于筛选证据。"} rules={[{ validator: (_, value) => parseKeywords(value).length <= 10 ? Promise.resolve() : Promise.reject(new Error("最多填写10个关键词。")) }]}><Input placeholder="例如：订单，现金流，业绩预告" maxLength={1000} /></Form.Item>
    <Form.Item name="watch_conditions" label="重点观察条件" rules={requiredText}><Input.TextArea {...textArea} maxLength={2000} placeholder="例如：放量突破前高，或关键数据偏离预期。" /></Form.Item>
    <Form.Item name="invalidation_conditions" label="原判断的失效条件" rules={requiredText}><Input.TextArea {...textArea} maxLength={2000} placeholder="例如：现金流连续两个季度低于预期。" /></Form.Item>
    <div className="monitor-form-grid"><Form.Item name="interval_minutes" label="检查频率"><Select options={FREQUENCY_OPTIONS} /></Form.Item><Form.Item name="review_at" label="复盘日期（北京时间）" rules={[futureReviewRule]}><Input type="date" min={nextReviewDate(new Date(), 1)} /></Form.Item></div>
    {editing ? <Form.Item name="status" label="计划状态"><Radio.Group options={Object.entries(PLAN_STATUS).map(([value, label]) => ({ value, label }))} /></Form.Item> : null}
  </>;
}

function PlanEditor({ plan, initial, ready, onClose, onSaved, onReload }) {
  const [form] = Form.useForm();
  const [revision, setRevision] = useState(plan?.revision);
  const [reloadError, setReloadError] = useState("");
  const [loading, setLoading] = useState(false);
  const [latestLoaded, setLatestLoaded] = useState(false);
  const status = Form.useWatch("status", form) || plan?.status || "active";
  const save = useMutation({ mutationFn: (values) => {
    const payload = planPayload(values);
    return plan ? api.updateMonitoringPlan(plan.id, { ...payload, revision }) : api.createMonitoringPlan({ ...payload, ...(initial?.research_link ? { research_link: initial.research_link } : {}) });
  }, retry: false, onSuccess: onSaved });
  const conflict = revisionConflict(save.error);
  async function reload() { setLoading(true); setReloadError(""); try { const result = await onReload(); setRevision(result.plan.revision); save.reset(); setLatestLoaded(true); } catch (error) { setReloadError(monitoringError(error)); } finally { setLoading(false); } }
  return <Modal title={plan ? "编辑观察计划" : "新建观察计划"} open footer={null} width={680} onCancel={() => !save.isPending && onClose()} maskClosable={false} className="monitor-modal">
    <Failure error={save.error} />{conflict ? <Button className="monitor-alert" onClick={reload} loading={loading}>载入最新版本，保留输入</Button> : null}
    {reloadError ? <Alert className="monitor-alert" type="warning" message={reloadError} /> : null}{latestLoaded ? <Alert className="monitor-alert" type="info" message="已载入最新版本，请核对输入后提交。" /> : null}
    {!ready ? <Alert className="monitor-alert" type="info" message="模型未就绪，可保存为暂停或关闭状态。" /> : null}
    <Form form={form} layout="vertical" initialValues={planValues(plan, initial)} disabled={save.isPending || loading} onFinish={(values) => save.mutate(values)}><PlanFields form={form} editing={Boolean(plan)} />
      <p className="monitor-caption">按计划检查已有数据，有变化时调用模型并发送站内提醒。</p>
      <div className="monitor-form-actions"><Button onClick={onClose}>取消</Button><Button type="primary" htmlType="submit" loading={save.isPending} disabled={conflict || (!ready && (!plan || status === "active"))}>{plan ? "保存修改" : "保存并启用"}</Button></div>
    </Form>
  </Modal>;
}

function HumanReview({ plan, onSaved, onCancel, onReload }) {
  const [form] = Form.useForm();
  const [revision, setRevision] = useState(plan.revision);
  const [loading, setLoading] = useState(false);
  const [reloadError, setReloadError] = useState("");
  const [loaded, setLoaded] = useState(false);
  const action = Form.useWatch("action", form) || "continue";
  const save = useMutation({ mutationFn: (values) => api.reviewMonitoringPlan(plan.id, {
    revision, action: values.action, reason: values.reason.trim(), evidence_assessment: values.evidence_assessment,
    ...(values.action === "close" ? {} : { next_review_at: values.next_review_at }),
    ...(values.action === "revise" ? { changes: { goal: values.goal.trim(), watch_conditions: values.watch_conditions.trim(), invalidation_conditions: values.invalidation_conditions.trim() } } : {}),
  }), retry: false, onSuccess: onSaved });
  const conflict = revisionConflict(save.error);
  async function reload() { setLoading(true); try { const result = await onReload(); setRevision(result.plan.revision); save.reset(); setLoaded(true); setReloadError(""); } catch (error) { setReloadError(monitoringError(error)); } finally { setLoading(false); } }
  return <section className="monitor-review"><h3>记录人工复盘</h3>
    <Failure error={save.error} />{conflict ? <Button className="monitor-alert" onClick={reload} loading={loading}>载入最新版本，保留复盘</Button> : null}{reloadError ? <Alert className="monitor-alert" type="warning" message={reloadError} /> : null}{loaded ? <Alert className="monitor-alert" type="info" message="最新版本已载入，请核对后提交。" /> : null}
    <Form form={form} layout="vertical" disabled={save.isPending || loading} initialValues={{ action: "continue", evidence_assessment: "uncertain", next_review_at: nextReviewDate(), goal: plan.goal, watch_conditions: plan.watch_conditions, invalidation_conditions: plan.invalidation_conditions }} onFinish={(values) => save.mutate(values)}>
      <Form.Item name="action" label="复盘决定"><Radio.Group options={Object.entries(REVIEW_LABELS).map(([value, label]) => ({ value, label }))} /></Form.Item>
      <Form.Item name="evidence_assessment" label="证据与原判断的关系"><Select options={Object.entries(ASSESSMENT_LABELS).map(([value, label]) => ({ value, label }))} /></Form.Item>
      <Form.Item name="reason" label="复盘理由" rules={requiredText}><Input.TextArea {...textArea} placeholder="填写决定及依据。" /></Form.Item>
      {action === "revise" ? <><Form.Item name="goal" label="修订后的观察目标" rules={requiredText}><Input.TextArea {...textArea} /></Form.Item><Form.Item name="watch_conditions" label="修订后的观察条件" rules={requiredText}><Input.TextArea {...textArea} maxLength={2000} /></Form.Item><Form.Item name="invalidation_conditions" label="修订后的失效条件" rules={requiredText}><Input.TextArea {...textArea} maxLength={2000} /></Form.Item></> : null}
      {action !== "close" ? <Form.Item name="next_review_at" label="下次复盘日期（北京时间）" rules={[futureReviewRule]}><Input type="date" min={nextReviewDate(new Date(), 1)} /></Form.Item> : <Alert className="monitor-alert" type="info" message="关闭后停止检查，历史记录保留。" />}
      <div className="monitor-form-actions"><Button onClick={onCancel}>取消复盘</Button><Button type="primary" htmlType="submit" loading={save.isPending} disabled={conflict}>保存人工复盘</Button></div>
    </Form>
  </section>;
}

function Lines({ value }) { const lines = reportLines(value); return lines.length ? <ul className="monitor-report-lines">{lines.map((line, index) => <li key={index}>{line}</li>)}</ul> : <p className="monitor-caption">未提供内容</p>; }

export function MonitorRunContext({ runId, notificationId }) {
  const { user } = useAuth();
  const [open, setOpen] = useState(false);
  const context = useQuery({ queryKey: monitoringKey(user?.id, "run-context", runId), queryFn: ({ signal }) => api.getMonitoringRunContext(runId, { signal }), enabled: open && Boolean(runId), retry: false });
  const data = context.data;
  const params = new URLSearchParams({ monitor_run: runId || "" });
  if (notificationId) params.set("notification", notificationId);
  if (!runId) return null;
  return <div className="monitor-run-context"><Button size="small" onClick={() => setOpen(!open)} aria-expanded={open}>{open ? "收起证据" : "查看变化证据"}</Button>{open ? <div className="monitor-context-content"><Failure error={context.error} retry={() => context.refetch()} />{context.isPending ? <Spin size="small" /> : data ? <><p className="monitor-caption">运行日期 {data.trigger_date || "未记录"}（北京时间），可能与事件日期不同。</p><div className="monitor-detail-actions">{(data.target_symbols || EMPTY).map((symbol) => <Link key={symbol} to={stockContextUrl(symbol, data.trigger_date, runId)}>定位 {symbol} 行情 ↗</Link>)}<Link to={`/research?${params}`}>转入研究 ↗</Link></div><Collapse size="small" items={[{ key: "evidence", label: `本次变化证据（${data.evidence?.length || 0}）`, children: <><Lines value={coverageLines(data.coverage)} />{(data.evidence || EMPTY).map((item, index) => <EvidenceCard key={item.id || index} evidence={{ ...item, snapshot: { source: item.source } }} />)}</> }]} /></> : null}</div> : null}</div>;
}

function HandlingDialog({ notification, onClose, onSaved }) {
  const save = useMutation({ mutationFn: (values) => api.handleMonitoringNotification(notification.id, { ...values, ...(notification.run_id ? { run_id: notification.run_id } : {}) }), retry: false, onSuccess: onSaved });
  return <Modal open title="记录变化处理" onCancel={onClose} footer={null} className="monitor-modal"><Failure error={save.error} /><Form layout="vertical" initialValues={{ action: "reviewed", reason: "" }} onFinish={(values) => save.mutate(values)} disabled={save.isPending}><Form.Item name="action" label="处理决定"><Radio.Group options={[{ value: "reviewed", label: "已复核" }, { value: "ignored", label: "忽略本次变化" }]} /></Form.Item><Form.Item name="reason" label="处理理由" rules={requiredText}><Input.TextArea maxLength={2000} autoSize={{ minRows: 3, maxRows: 6 }} /></Form.Item><div className="monitor-form-actions"><Button type="primary" htmlType="submit" loading={save.isPending}>保存处理记录</Button></div></Form></Modal>;
}

function RunReport({ run }) {
  const result = run.result || {};
  const report = result.report;
  const evidence = Array.isArray(result.evidence) ? result.evidence : EMPTY;
  return <div className="monitor-report">
    <div className="monitor-meta"><Tag color={run.status === "failed" ? "orange" : runInProgress(run) ? "blue" : "default"}>{RUN_STATUS[run.status] || run.status}</Tag><span>提交 {trackingTime(runTime(run))}</span>{run.finished_at ? <span>完成 {trackingTime(run.finished_at)}</span> : null}{result.model ? <span>模型 {result.model}</span> : null}</div>
    <MonitorRunContext runId={run.id} />
    {run.error_code || run.error ? <Alert className="monitor-alert" type="warning" showIcon message={run.error || `生成未完成（${run.error_code}）`} description="检查模型连接、额度和后台状态后重试。" /> : null}
    {runInProgress(run) ? <div className="monitor-muted"><Spin size="small" /> 处理中，完成后自动显示结果。</div> : null}
    {result.skipped_unchanged ? <Alert className="monitor-alert" type="info" showIcon message="无新证据，沿用已有报告。" description={result.reused_report_generated_at ? `原报告生成于 ${trackingTime(result.reused_report_generated_at)}` : undefined} /> : result.model_called === false ? <p className="monitor-caption">本次未调用模型。</p> : null}
    {report ? <><div className="monitor-report-summary"><Tag color={report.assessment === "attention" || report.assessment === "invalidated" ? "orange" : "blue"}>{ASSESSMENT[report.assessment] || "观察报告"}</Tag><p>{report.summary}</p></div>
      {report.members?.length ? <section className="monitor-member-reports"><h4>逐股观察</h4>{report.members.map((member) => <article key={member.symbol}><div className="monitor-meta"><strong>{member.symbol}</strong><Tag color={member.assessment === "attention" || member.assessment === "invalidated" ? "orange" : "default"}>{ASSESSMENT[member.assessment] || "待确认"}</Tag></div><p>{member.summary}</p>{member.evidence_ids?.length ? <span className="monitor-caption">证据 {member.evidence_ids.join("、")}</span> : <span className="monitor-caption">暂无引用</span>}</article>)}</section> : null}
      {report.conditions?.length ? <section><h4>条件核对</h4><div className="monitor-conditions">{report.conditions.map((condition, index) => <div key={index}><Tag color={condition.status === "met" ? "blue" : "default"}>{CONDITION_STATUS[condition.status] || "待确认"}</Tag><span>{condition.condition}</span>{condition.evidence_ids?.length ? <small>证据 {condition.evidence_ids.join("、")}</small> : null}</div>)}</div></section> : null}
      <div className="monitor-report-grid"><section><h4>本次变化</h4><Lines value={report.changes} /></section><section><h4>风险与不确定性</h4><Lines value={report.risks} /></section><section><h4>下一步观察</h4><Lines value={report.next_steps} /></section>{report.review_summary ? <section><h4>复盘结论</h4><Lines value={report.review_summary} /></section> : null}</div>
    </> : !runInProgress(run) && run.status === "succeeded" ? <p className="monitor-muted">检查完成，无新增报告。</p> : null}
    <Collapse size="small" className="monitor-evidence" items={[{ key: "evidence", label: `证据与覆盖（${evidence.length}）`, children: <><Lines value={coverageLines(result.coverage)} />{evidence.length ? evidence.map((item, index) => <EvidenceCard key={item.id || index} evidence={{ ...item, kind: "monitor", title: `${item.id ? `${item.id} · ` : ""}${item.symbol ? `${item.symbol} · ` : ""}${item.title || "原始证据"}`, snapshot: { source: item.source } }} />) : <p className="monitor-muted">暂无证据</p>}</> }, ...(result.plan_snapshot ? [{ key: "snapshot", label: "本次使用的计划", children: <PlanSummary plan={result.plan_snapshot} /> }] : [])]} />
  </div>;
}

function PlanMembers({ plan, compact = false }) {
  if (plan.target_type !== "stock_list") return null;
  const members = Array.isArray(plan.target_symbols) ? plan.target_symbols : EMPTY;
  return <div className="monitor-members"><div className="monitor-member-tags">{(compact ? members.slice(0, 4) : members).map((symbol) => <Tag key={symbol}>{symbol}</Tag>)}{compact && members.length > 4 ? <span className="monitor-caption">另 {members.length - 4} 只</span> : null}</div>{!compact ? <p className="monitor-caption">成员独立保存；每轮最多调用一次模型。</p> : null}</div>;
}

function PlanSummary({ plan }) {
  return <div className="monitor-plan-summary"><div className="monitor-meta"><Tag>{targetLabel(plan)}</Tag><span>{FREQUENCY_OPTIONS.find((item) => item.value === plan.interval_minutes)?.label || "按计划检查"}</span><span>复盘 {plan.review_at || "未设置"} · 北京时间</span></div>
    <PlanMembers plan={plan} /><div className="monitor-meta">{(plan.channels || ["news", "price"]).map((channel) => <Tag key={channel}>{MONITOR_CHANNELS[channel] || channel}</Tag>)}{plan.research_link?.dossier_id ? <Link to={investmentUrl(plan.research_link.dossier_id)}>查看来源研究 ↗</Link> : null}</div>
    <dl><div><dt>目标</dt><dd>{plan.goal || "—"}</dd></div><div><dt>观察条件</dt><dd>{plan.watch_conditions || "—"}</dd></div><div><dt>失效条件</dt><dd>{plan.invalidation_conditions || "—"}</dd></div></dl>{plan.keywords?.length ? <p className="monitor-caption">关键词：{plan.keywords.join("、")}</p> : null}</div>;
}

function PlanDrawer({ id, userId, ready, canRun, runBlockReason, onClose, onChanged }) {
  const { message } = AntApp.useApp();
  const [editing, setEditing] = useState(false);
  const [reviewing, setReviewing] = useState(false);
  const detail = useQuery({ queryKey: monitoringKey(userId, "plan", id), queryFn: ({ signal }) => api.getMonitoringPlan(id, { signal }), refetchInterval: (query) => query.state.data?.runs?.some(runInProgress) ? 2000 : 15_000, refetchIntervalInBackground: false, retry: false });
  const plan = detail.data?.plan;
  const runs = [...(detail.data?.runs || EMPTY)].sort((a, b) => Number(runTime(b)) - Number(runTime(a)));
  const working = runs.some(runInProgress);
  async function refresh() { await Promise.all([detail.refetch(), onChanged()]); }
  async function reload() { const result = await detail.refetch({ throwOnError: true }); return result.data; }
  const patch = useMutation({ mutationFn: (status) => api.updateMonitoringPlan(id, { revision: plan.revision, status }), retry: false, onSuccess: refresh });
  const run = useMutation({ mutationFn: (kind) => api.runMonitoringPlan(id, kind), retry: false, onSuccess: async () => { message.success("已提交后台任务。"); await refresh(); } });
  const reviews = detail.data?.reviews || EMPTY;
  return <Drawer open title={plan?.title || "观察计划"} onClose={onClose} width="min(900px, 100vw)" rootClassName="monitor-drawer">
    <Failure error={detail.error} retry={() => detail.refetch()} hasData={Boolean(plan)} />
    {detail.isPending ? <div className="monitor-loading"><Spin /></div> : plan ? <>
      <div className="monitor-detail-heading"><Tag color={plan.status === "active" ? "blue" : "default"}>{PLAN_STATUS[plan.status] || plan.status}</Tag><span className="monitor-caption">版本 {plan.revision} · 更新 {trackingTime(plan.updated_at)}</span></div><PlanSummary plan={plan} />
      <div className="monitor-detail-actions"><Button onClick={() => setEditing(true)}>编辑计划</Button>{plan.status === "active" ? <Button loading={patch.isPending} onClick={() => patch.mutate("paused")}>暂停计划</Button> : plan.status === "paused" ? <Button disabled={!ready} loading={patch.isPending} onClick={() => patch.mutate("active")}>恢复计划</Button> : null}{plan.status !== "closed" ? <Popconfirm title="关闭此计划？" description="历史报告与人工复盘仍会保留。" onConfirm={() => patch.mutate("closed")} okText="关闭计划" cancelText="取消"><Button disabled={patch.isPending}>关闭计划</Button></Popconfirm> : null}</div>
      <Failure error={patch.error || run.error} />{revisionConflict(patch.error) ? <Button className="monitor-alert" onClick={async () => { await refresh(); patch.reset(); }}>刷新最新计划</Button> : null}
      {plan.status !== "closed" ? <><div className="monitor-run-actions"><Button type="primary" disabled={!canRun || working || run.isPending || plan.status !== "active"} loading={run.isPending && run.variables === "observe"} onClick={() => run.mutate("observe")}>立即观察</Button><Button disabled={!canRun || working || run.isPending || plan.status !== "active"} loading={run.isPending && run.variables === "review"} onClick={() => run.mutate("review")}>生成复盘</Button><Button onClick={() => setReviewing(!reviewing)} aria-expanded={reviewing}>记录人工复盘</Button></div>{!canRun || plan.status === "paused" ? <p className="monitor-caption">{plan.status === "paused" ? "计划已暂停，恢复后可运行。" : runBlockReason}</p> : null}</> : null}
      {reviewing ? <HumanReview key={id} plan={plan} onSaved={async () => { setReviewing(false); await refresh(); message.success("人工复盘已保存。"); }} onCancel={() => setReviewing(false)} onReload={reload} /> : null}
      <section className="monitor-detail-section"><h3>观察与复盘报告 <span>{runs.length}</span></h3>{runs.length ? <Collapse defaultActiveKey={[String(runs[0].id)]} items={runs.map((item) => ({ key: String(item.id), label: <div className="monitor-run-label"><span>{item.kind === "review" ? "模型复盘" : "观察报告"}</span><span>{trackingTime(runTime(item))}</span><Tag>{RUN_STATUS[item.status] || item.status}</Tag></div>, children: <RunReport run={item} /> }))} /> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无报告，可点击「立即观察」。" />}</section>
      <section className="monitor-detail-section"><h3>变化处理记录</h3>{detail.data?.handling_history?.length ? detail.data.handling_history.map((item, index) => <article className="monitor-handling-row" key={item.id || index}><div className="monitor-meta"><Tag>{HANDLING_STATUS[item.action] || item.action}</Tag><time>{trackingTime(item.created_at)}</time></div><p>{item.reason}</p>{item.dossier_id ? <Link to={investmentUrl(item.dossier_id)}>查看关联研究 ↗</Link> : null}</article>) : <p className="monitor-caption">暂无处理记录</p>}</section>
      <section className="monitor-detail-section"><h3>人工复盘记录</h3>{reviews.length ? <ol className="monitor-review-list">{reviews.map((row) => ({ ...row, ...(row.review || {}) })).map((item, index) => <li key={item.id || index}><div className="monitor-meta"><strong>{REVIEW_LABELS[item.action] || "人工复盘"}</strong><span>{trackingTime(item.created_at)}</span><Tag>{ASSESSMENT_LABELS[item.evidence_assessment] || "待核对"}</Tag></div><p>{item.reason}</p>{item.next_review_at ? <span className="monitor-caption">下次复盘 {item.next_review_at}</span> : null}</li>)}</ol> : <p className="monitor-muted">暂无人工复盘</p>}</section>
      {editing ? <PlanEditor plan={plan} ready={ready} onClose={() => setEditing(false)} onReload={reload} onSaved={async () => { setEditing(false); await refresh(); message.success("计划已更新。"); }} /> : null}
    </> : null}
  </Drawer>;
}

function ScheduleSettings({ config, worker, ready, onChanged }) {
  const [limit, setLimit] = useState(config.daily_call_limit || 12);
  const { message } = AntApp.useApp();
  const patch = useMutation({ mutationFn: api.updateMonitoringConfig, retry: false, onSuccess: async () => { await onChanged(); message.success("观察设置已更新。"); } });
  return <section className="monitor-card monitor-schedule"><div className="monitor-between"><div><h3>后台观察</h3><p>关闭后暂停所有观察任务，包括手动任务。</p></div><Switch aria-label="启用后台观察" checked={config.enabled === true} disabled={!config.enabled && !ready} loading={patch.isPending} onChange={(enabled) => patch.mutate({ enabled })} /></div>
    <div className="monitor-budget-control"><label htmlFor="monitor-budget">每日模型请求上限</label><InputNumber id="monitor-budget" min={1} max={100} precision={0} value={limit} onChange={setLimit} /><Button disabled={!Number.isInteger(limit) || limit < 1 || limit > 100 || patch.isPending} onClick={() => patch.mutate({ daily_call_limit: limit })}>保存额度</Button></div>
    <p className="monitor-caption">每次模型请求计 1 次额度，无新证据时沿用报告。</p>
    {worker?.enabled === false ? <Alert className="monitor-alert" type="warning" showIcon message="观察服务已关闭" /> : null}<Failure error={patch.error} />
  </section>;
}

function Notifications({ items, onOpen, onChanged }) {
  const [showAll, setShowAll] = useState(false);
  const [handling, setHandling] = useState(null);
  const mutation = useMutation({ mutationFn: ({ id, value }) => api.updateMonitoringNotification(id, value), retry: false, onSuccess: onChanged });
  const shown = showAll ? items : items.filter((item) => !HANDLING_STATUS[item.handling_status]);
  return <section className="monitor-card monitor-notifications"><div className="monitor-between"><h2>变化与处理</h2><Button size="small" type="text" onClick={() => setShowAll(!showAll)}>{showAll ? "仅看未处理" : "查看全部"}</Button></div><Failure error={mutation.error} />
    {shown.length ? <div>{shown.map((item) => <article className={`monitor-notification${item.is_read ? " is-read" : ""}`} key={item.id}><div className="monitor-meta"><Badge status={item.is_read ? "default" : "processing"} text={item.is_read ? "已读" : "未读"} /><Tag>{HANDLING_STATUS[item.handling_status] || "待处理"}</Tag><time>{trackingTime(item.created_at)}</time></div><h3>{item.title || "观察计划有更新"}</h3><p>{item.summary || item.message}</p>{item.latest_handling?.reason ? <p className="monitor-caption">最近处理：{item.latest_handling.reason}</p> : null}<div className="monitor-detail-actions">{item.plan_id ? <Button size="small" onClick={() => onOpen(item.plan_id)}>查看计划</Button> : null}<Button size="small" onClick={() => setHandling(item)}>记录处理</Button><Button size="small" type="text" loading={mutation.isPending && mutation.variables?.id === item.id} onClick={() => mutation.mutate({ id: item.id, value: !item.is_read })}>{item.is_read ? "标为未读" : "标为已读"}</Button></div><MonitorRunContext runId={item.run_id} notificationId={item.id} /></article>)}</div> : <p className="monitor-muted">{showAll ? "暂无站内提醒。" : "暂无待处理变化。"}</p>}{handling ? <HandlingDialog notification={handling} onClose={() => setHandling(null)} onSaved={async () => { setHandling(null); await onChanged(); }} /> : null}
  </section>;
}

function MonitoringContent() {
  const { user, authRequired, logout } = useAuth();
  const { message } = AntApp.useApp();
  const qc = useQueryClient();
  const [params, setParams] = useSearchParams();
  const [modelOpen, setModelOpen] = useState(false);
  const [composerOpen, setComposerOpen] = useState(false);
  const [filter, setFilter] = useState("active");
  const [channel, setChannel] = useState("all");
  const sourceReport = params.get("research_report");
  const sourceIndex = Number(params.get("observation"));
  const researchSource = useQuery({ queryKey: monitoringKey(user?.id, "research-source", sourceReport), queryFn: ({ signal }) => api.getInvestmentReport(sourceReport, { signal }), enabled: Boolean(sourceReport), retry: false });
  const researchDraft = monitoringDraft(researchSource.data, sourceIndex);
  const overview = useQuery({ queryKey: monitoringKey(user?.id, "overview", params.get("symbol") || ""), queryFn: ({ signal }) => api.getMonitoringOverview({ signal, symbol: params.get("symbol") || undefined }), enabled: user?.id != null, staleTime: 5000, refetchInterval: 15_000, refetchIntervalInBackground: false, retry: false });
  const data = overview.data;
  const gate = modelGate(data?.model, overview.isError);
  const budget = budgetState(data?.usage, data?.config);
  const canRun = gate.ready && !budget.exhausted && data?.config?.enabled === true && data?.worker?.enabled !== false;
  const runBlockReason = !gate.ready ? "请先配置并验证模型。" : data?.config?.enabled !== true ? "后台观察已暂停，请先开启。" : data?.worker?.enabled === false ? "观察服务已关闭。" : "今日额度已用完，可调整额度或明日再试。";
  const plans = data?.plans || EMPTY;
  const notifications = data?.notifications || EMPTY;
  const shown = plans.filter((plan) => (filter === "all" || plan.status === filter) && (channel === "all" || (plan.channels || ["news", "price"]).includes(channel)));
  const selectedId = params.get("plan");
  function openPlan(id) { setParams((previous) => { const next = new URLSearchParams(previous); if (id) next.set("plan", id); else next.delete("plan"); return next; }, { replace: true }); }
  const refresh = () => qc.invalidateQueries({ queryKey: monitoringKey(user?.id, "overview") });
  const initial = researchDraft || monitoringInitial(params);
  return <Layout className="sva-layout monitor-layout"><AppToolbar active="monitor" user={user} authRequired={authRequired} onLogout={logout} settingsAction={<ConfigConsoleButton isAdmin={user?.role === "admin"} />} />
    <Layout.Content className="monitor-content"><header className="monitor-heading"><div><h1>监控</h1><p>跟踪个股和股票列表，定期检查与复盘。</p></div><div className="monitor-heading-actions"><Button icon={<ReloadOutlined aria-hidden="true" />} loading={overview.isFetching} onClick={() => overview.refetch()}>更新状态</Button><Button type="primary" icon={<PlusOutlined aria-hidden="true" />} disabled={!gate.ready || (Boolean(sourceReport) && !researchDraft)} onClick={() => setComposerOpen(true)}>新建观察计划</Button></div></header>
      <Failure error={overview.error} retry={() => overview.refetch()} hasData={Boolean(data)} />
      {sourceReport ? <section className="monitor-research-source"><h2>研究转入的观察</h2><Failure error={researchSource.error} retry={() => researchSource.refetch()} />{researchSource.isPending ? <Spin /> : researchDraft ? <><p>{researchDraft.title} · {researchDraft.goal}</p><Link to={investmentUrl(researchDraft.research_link.dossier_id)}>返回来源研究 ↗</Link><Button type="primary" disabled={!gate.ready} onClick={() => setComposerOpen(true)}>核对并创建监控</Button><p className="monitor-caption">目标与条件已带入，请设置频率、来源和复盘日期。</p></> : !researchSource.isError ? <p>观察条件已不可用，请返回研究。</p> : null}</section> : null}
      <section className={`monitor-model-strip${gate.ready ? " is-ready" : ""}`} aria-label="大模型配置状态"><div className="monitor-model-status"><SafetyCertificateOutlined aria-hidden="true" /><div><div className="monitor-meta"><strong>{gate.label}</strong>{data?.model?.model ? <span>{data.model.model}</span> : null}</div>{!gate.ready ? <p>{gate.text}</p> : null}{data?.model?.checked_at ? <small>最近验证 {trackingTime(data.model.checked_at)}</small> : null}</div></div><Button onClick={() => setModelOpen(true)}>{gate.ready ? "管理模型连接" : "配置 / 验证模型"}</Button></section>
      <section className="monitor-stats" aria-label="观察概况"><div><span>已启用计划</span><strong>{data ? plans.filter((plan) => plan.status === "active").length : "—"}</strong></div><div><span>今日模型请求</span><strong>{data ? `${budget.used} / ${budget.limit ?? "—"}` : "—"}</strong></div><div><span>待处理变化</span><strong>{data ? notifications.filter((item) => !HANDLING_STATUS[item.handling_status]).length : "—"}</strong></div><div><span>后台状态</span><strong className="monitor-state-text">{!data ? "读取中" : data.config?.enabled === false ? "已暂停" : data.worker?.enabled === false ? "服务已关闭" : data.worker?.running ? "运行中" : "等待检查"}</strong></div></section>
      <div className="monitor-workspace"><section className="monitor-plans"><div className="monitor-section-heading"><h2>我的观察计划</h2><Select aria-label="按监控来源筛选" value={channel} onChange={setChannel} options={[{ value: "all", label: "全部来源" }, ...Object.entries(MONITOR_CHANNELS).map(([value, label]) => ({ value, label }))]} /><Link to="/today">查看人工关注判断 ↗</Link></div><Tabs activeKey={filter} onChange={setFilter} items={[{ key: "active", label: "已启用" }, { key: "paused", label: "已暂停" }, { key: "all", label: "全部计划" }]} />
        {overview.isPending ? <div className="monitor-loading"><Spin aria-label="读取观察计划" /></div> : shown.length ? <div className="monitor-plan-list">{shown.map((plan) => <article className="monitor-card monitor-plan" key={plan.id}><div className="monitor-between"><div className="monitor-meta"><Tag color={plan.status === "active" ? "blue" : "default"}>{PLAN_STATUS[plan.status] || plan.status}</Tag><span>{targetLabel(plan)}</span></div><span className="monitor-caption">{FREQUENCY_OPTIONS.find((item) => item.value === plan.interval_minutes)?.label}</span></div><button className="monitor-plan-title" onClick={() => openPlan(plan.id)}>{plan.title}</button><PlanMembers plan={plan} compact /><div className="monitor-meta">{(plan.channels || ["news", "price"]).map((value) => <Tag key={value}>{MONITOR_CHANNELS[value] || value}</Tag>)}</div><p className="monitor-plan-goal">{plan.goal}</p><div className="monitor-plan-times"><span>上次执行 {trackingTime(plan.last_run_at || plan.last_finished_at)}</span><span>下次检查 {trackingTime(plan.next_run_at)}</span><span>复盘 {plan.review_at || "未设置"}</span></div>{plan.last_error ? <p className="monitor-caption">最近运行：{plan.last_error}</p> : null}<div className="monitor-detail-actions"><Button onClick={() => openPlan(plan.id)}>查看报告与管理</Button></div></article>)}</div> : <div className="monitor-empty"><h3>{filter === "paused" ? "暂无暂停的计划" : "暂无观察计划"}</h3><p>{gate.ready ? "添加个股或股票列表，设置观察条件。" : "配置并验证模型后可创建计划。"}</p><Button type={gate.ready ? "default" : "primary"} disabled={gate.ready && Boolean(sourceReport) && !researchDraft} onClick={() => gate.ready ? setComposerOpen(true) : setModelOpen(true)}>{gate.ready ? "创建观察计划" : "配置大模型"}</Button></div>}
      </section><aside className="monitor-sidebar">{data?.config ? <ScheduleSettings key={`${user?.id}:${data.config.daily_call_limit}`} config={data.config} worker={data.worker} ready={gate.ready} onChanged={refresh} /> : null}<Notifications userId={user?.id} items={notifications} onOpen={openPlan} onChanged={refresh} /><p className="monitor-caption">后台持续检查已采集的数据，变化提醒保存在站内。</p></aside></div>
      {modelOpen ? <ModelDialog userId={user?.id} onClose={() => setModelOpen(false)} /> : null}{composerOpen ? <PlanEditor initial={initial} ready={gate.ready} onClose={() => setComposerOpen(false)} onSaved={async (result) => { setComposerOpen(false); await refresh(); openPlan((result.plan || result).id); message.success("观察计划已保存并启用。"); }} /> : null}
      {selectedId ? <PlanDrawer key={selectedId} id={selectedId} userId={user?.id} ready={gate.ready} canRun={canRun} runBlockReason={runBlockReason} onClose={() => openPlan(null)} onChanged={refresh} /> : null}
    </Layout.Content>
  </Layout>;
}

export default function MonitoringWorkspace() { const { user } = useAuth(); return <MonitoringContent key={user?.id ?? "guest"} />; }
