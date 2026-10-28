import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { Alert, App as AntApp, Badge, Button, Card, Collapse, Drawer, Empty, Form, Input, Layout, Radio, Segmented, Space, Spin, Tag, Typography } from "antd";
import CheckOutlined from "@ant-design/icons/es/icons/CheckOutlined";
import PlusOutlined from "@ant-design/icons/es/icons/PlusOutlined";
import ReloadOutlined from "@ant-design/icons/es/icons/ReloadOutlined";
import AppToolbar from "./AppToolbar.jsx";
import { ConfigConsoleButton } from "./ConfigConsole.jsx";
import { useAuth } from "./AuthContext.jsx";
import { qk } from "./hooks/queryKeys.js";
import { shanghaiDateKey } from "./calendarViewModel.js";
import { EvidenceCard, EvidenceList, ThesisComposer, ThesisSummary, futureReviewRule, requiredText } from "./TrackingComponents.jsx";
import { ASSESSMENT_LABELS, CHANGE_LABELS, REVIEW_LABELS, nextReviewDate, partitionChanges, trackingError, trackingStatusNotices, trackingTime } from "./trackingViewModel.js";
import * as api from "./api.js";
import "./TodayPage.css";

const { Title, Text, Paragraph } = Typography;
const EMPTY = [];

function ExistingAnalysis({ analysis }) {
  if (!analysis) return null;
  const groups = [["已知事实", analysis.facts], ["分析推断", analysis.inferences], ["反向证据", analysis.counter_evidence || analysis.counter_arguments], ["后续观察", analysis.watch_points], ["失效条件", analysis.invalidation_conditions]]
    .map(([label, values]) => [label, (Array.isArray(values) ? values : []).map((item) => typeof item === "string" ? item : item?.text || item?.reason || "").filter(Boolean)])
    .filter(([, values]) => values.length);
  return <Collapse size="small" className="tracking-change-evidence" items={[{ key: "analysis", label: "既有解读 · 事实与反向证据", children: <div>
    {analysis.content_changed ? <Alert className="tracking-banner" type="warning" showIcon message="正文已有修订，这份解读基于此前版本，请先核对本次证据。" /> : null}
    <Paragraph type="secondary" className="tracking-caption">{analysis.usage || "以下是已归档解读；是否支持原判断，需要结合证据核对。"}</Paragraph>
    {analysis.reasoning ? <Paragraph className="tracking-preserve">{analysis.reasoning}</Paragraph> : null}
    {groups.map(([label, values]) => <div className="tracking-analysis-group" key={label}><Text strong>{label}</Text><ul>{values.map((text, index) => <li key={index}>{text}</li>)}</ul></div>)}
    {!analysis.reasoning && !groups.length ? <Text type="secondary">暂无可核对的结构化解读，请查看原文证据。</Text> : null}
  </div> }]} />;
}

function ChangeCard({ change, thesisNames, onOpenThesis, onFeedback, pending }) {
  const kind = change.change_type || change.kind;
  const linkedIds = change.thesis_ids || EMPTY;
  return <article className={`tracking-change${change.is_read ? " is-read" : ""}${change.resolved ? " is-resolved" : ""}`}>
    <div className="tracking-change-meta"><Space wrap size={[5, 4]}><Tag color={change.kind === "review_due" ? "orange" : change.kind === "data_issue" ? "gold" : "default"}>{CHANGE_LABELS[kind] || CHANGE_LABELS[change.kind] || "证据变化"}</Tag>{change.symbol ? <Link to={`/?chart=${encodeURIComponent(change.symbol)}`}>{change.symbol}</Link> : null}{change.resolved ? <Tag>已处理</Tag> : null}{change.is_read ? <span>已读</span> : <span className="tracking-unread-dot">未读</span>}</Space><time>{trackingTime(change.created_at)}</time></div>
    <h3>{change.title}</h3>
    <Paragraph className="tracking-change-summary">{change.summary}</Paragraph>
    {linkedIds.length ? <div className="tracking-related"><Text type="secondary">对应判断</Text>{linkedIds.map((id) => <Button key={id} type="link" size="small" onClick={() => onOpenThesis(id)} title={thesisNames.get(id) || "查看原判断"}>{thesisNames.get(id) || "查看原判断"}</Button>)}</div> : null}
    {change.evidence_ref || change.previous_evidence_ref ? <Collapse size="small" className="tracking-change-evidence" items={[{ key: "evidence", label: change.previous_evidence_ref ? "对照本次与此前证据" : "查看变化证据", children: <div className="tracking-evidence-list">{change.previous_evidence_ref ? <EvidenceCard evidence={change.previous_evidence_ref} label="此前证据" /> : null}<EvidenceCard evidence={change.evidence_ref} label={change.previous_evidence_ref ? "本次证据" : undefined} /></div> }]} /> : null}
    <ExistingAnalysis analysis={change.analysis} />
    <div className="tracking-change-actions">
      <Button type="text" size="small" icon={change.is_read ? <CheckOutlined /> : undefined} disabled={pending} onClick={() => onFeedback(change.id, { is_read: !change.is_read })}>{change.is_read ? "标为未读" : "标为已读"}</Button>
      <div className="tracking-feedback"><span>这条变化</span><Button size="small" type={change.feedback === "useful" ? "primary" : "text"} aria-pressed={change.feedback === "useful"} disabled={pending} onClick={() => onFeedback(change.id, { feedback: change.feedback === "useful" ? null : "useful" })}>有用</Button><Button size="small" type={change.feedback === "irrelevant" ? "primary" : "text"} aria-pressed={change.feedback === "irrelevant"} disabled={pending} onClick={() => onFeedback(change.id, { feedback: change.feedback === "irrelevant" ? null : "irrelevant" })}>无关</Button></div>
    </div>
  </article>;
}

function ScanStatus({ status }) {
  const notices = trackingStatusNotices(status);
  return <>
    {notices.length ? <Alert type={notices.some((notice) => notice.type === "warning") ? "warning" : "info"} showIcon className="tracking-status-banner" message="更新与覆盖状态" description={<ul className="tracking-status-lines">{notices.map((notice) => <li key={notice.key}>{notice.title}</li>)}</ul>} /> : null}
    <details className="tracking-scan-details">
      <summary>数据截止与跟踪状态<span>{status?.last_scanned_at ? `最近检查 ${trackingTime(status.last_scanned_at)}` : "尚未完成检查"}</span></summary>
      <div className="tracking-source-status">{[["news", "新闻证据"], ["calendar", "日历证据"]].map(([key, name]) => {
        const source = status?.[key];
        return <div key={key}><Text strong>{name}</Text><span>扫描截止：{trackingTime(source?.cutoff_at)}</span><span>最近采集：{trackingTime(source?.last_ingested_at)}</span>{source?.message ? <Paragraph className="tracking-caption">{source.message}</Paragraph> : null}{source?.pending ? <span>仍有归档证据等待检查</span> : null}</div>;
      })}</div>
      {notices.length ? <ul className="tracking-status-explanations">{notices.map((notice) => <li key={notice.key}><strong>{notice.title}：</strong>{notice.text}</li>)}</ul> : null}
      <Paragraph className="tracking-caption">本页检查已归档的新闻和主动关联的经济事件。未采集到的信息不在覆盖范围内；时间均为北京时间。</Paragraph>
    </details>
  </>;
}

function ReviewForm({ thesis, onSaved, onCancel, onReload }) {
  const [form] = Form.useForm();
  const [revision, setRevision] = useState(thesis.revision);
  const [reloading, setReloading] = useState(false);
  const [reloadError, setReloadError] = useState("");
  const [latestLoaded, setLatestLoaded] = useState(false);
  const action = Form.useWatch("action", form) || "continue";
  const save = useMutation({
    mutationFn: (values) => api.reviewTrackingThesis(thesis.id, {
      revision, action: values.action, reason: values.review_reason.trim(), evidence_assessment: values.evidence_assessment,
      ...(values.action !== "close" ? { review_at: values.review_at } : {}),
      ...(values.action === "revise" ? { changes: { reason: values.reason.trim(), watch_conditions: values.watch_conditions.trim(), invalidation_conditions: values.invalidation_conditions.trim() } } : {}),
    }),
    onSuccess: onSaved,
  });
  const reload = async () => {
    setReloading(true); setReloadError("");
    try {
      const latest = await onReload();
      if (!latest?.thesis) throw new Error("记录未加载");
      setRevision(latest.thesis.revision); setLatestLoaded(true); save.reset();
    } catch { setReloadError("最新记录读取失败，输入已保留，请重试。"); }
    finally { setReloading(false); }
  };
  const conflict = save.error?.response?.status === 409;
  return <section className="tracking-review-form" aria-label="复盘关注判断">
    <Title level={4}>记录这次复盘</Title>
    <Paragraph type="secondary" className="tracking-caption">对照原判断与新证据，写下你的结论。复盘记录会保留当时的判断版本。</Paragraph>
    {save.isError ? <Alert className="tracking-banner" type="warning" showIcon message={trackingError(save.error)} action={conflict ? <Button size="small" loading={reloading} onClick={reload}>载入最新记录</Button> : null} /> : null}
    {reloadError ? <Alert className="tracking-banner" type="error" message={reloadError} /> : null}
    {latestLoaded ? <Alert className="tracking-banner" type="info" showIcon message="上方已显示最新判断；本表单保留你的输入，请对照后提交。" /> : null}
    <Form form={form} layout="vertical" initialValues={{ action: "continue", evidence_assessment: "uncertain", review_at: nextReviewDate(), reason: thesis.reason, watch_conditions: thesis.watch_conditions, invalidation_conditions: thesis.invalidation_conditions }} disabled={save.isPending || reloading} onFinish={(values) => save.mutate(values)}>
      <Form.Item name="action" label="复盘决定"><Radio.Group optionType="button" buttonStyle="solid" options={Object.entries(REVIEW_LABELS).map(([value, label]) => ({ value, label }))} /></Form.Item>
      <Form.Item name="evidence_assessment" label="新证据与原判断的关系"><Radio.Group options={Object.entries(ASSESSMENT_LABELS).map(([value, label]) => ({ value, label }))} /></Form.Item>
      <Form.Item name="review_reason" label="复盘理由" rules={requiredText}><Input.TextArea autoSize={{ minRows: 3, maxRows: 6 }} maxLength={4000} placeholder="哪些证据支持或挑战了原判断？为什么继续、修订或关闭？" /></Form.Item>
      {action === "revise" ? <div className="tracking-revised-fields"><Form.Item name="reason" label="修订后的关注理由" rules={requiredText}><Input.TextArea autoSize={{ minRows: 2, maxRows: 5 }} maxLength={4000} /></Form.Item><Form.Item name="watch_conditions" label="修订后的观察条件" rules={requiredText}><Input.TextArea autoSize={{ minRows: 2, maxRows: 4 }} maxLength={4000} /></Form.Item><Form.Item name="invalidation_conditions" label="修订后的失效条件" rules={requiredText}><Input.TextArea autoSize={{ minRows: 2, maxRows: 4 }} maxLength={4000} /></Form.Item></div> : null}
      {action !== "close" ? <Form.Item name="review_at" label="下次复盘日期（北京时间）" rules={[futureReviewRule]}><Input type="date" min={nextReviewDate(new Date(), 1)} /></Form.Item> : <Alert className="tracking-banner" type="info" message="关闭后停止此判断的后续跟踪，原判断、证据和复盘记录继续保留。" />}
      <div className="tracking-form-actions"><Button onClick={onCancel}>取消</Button><Button type="primary" htmlType="submit" loading={save.isPending} disabled={conflict || thesis.status !== "active"}>{action === "close" ? "确认关闭并保存复盘" : "保存复盘"}</Button></div>
    </Form>
  </section>;
}

function ThesisTimeline({ items }) {
  if (!items.length) return <Text type="secondary">暂无时间线记录。</Text>;
  return <ol className="tracking-timeline">{[...items].reverse().map((item) => {
    const review = item.payload?.review;
    const snapshot = item.payload?.thesis;
    return <li key={item.id}><div className="tracking-between"><Text strong>{review ? REVIEW_LABELS[review.action] || "完成复盘" : item.kind === "created" ? "保存原始判断" : "修订判断 / 关联证据"}</Text><time>{trackingTime(item.created_at)}</time></div>{review ? <><Tag>{ASSESSMENT_LABELS[review.evidence_assessment] || "待核对"}</Tag><Paragraph className="tracking-preserve">{review.reason}</Paragraph></> : null}{snapshot ? <Collapse size="small" items={[{ key: "version", label: `查看当时的判断与证据 · 版本 ${item.revision ?? snapshot.revision ?? "—"}`, children: <><ThesisSummary thesis={snapshot} /><EvidenceList evidence={snapshot.evidence_refs} /></> }]} /> : null}</li>;
  })}</ol>;
}

function ThesisDetail({ id, userId, onClose, onOpenThesis, onFeedback, feedbackPendingId }) {
  const queryClient = useQueryClient();
  const { message } = AntApp.useApp();
  const [reviewing, setReviewing] = useState(false);
  const query = useQuery({ queryKey: qk.trackingThesis(userId, id), queryFn: ({ signal }) => api.getTrackingThesis(id, { signal }), enabled: Boolean(id) && userId != null });
  const thesis = query.data?.thesis;
  const changes = query.data?.changes || EMPTY;
  const reload = async () => {
    const result = await query.refetch();
    if (result.isError) throw result.error;
    return result.data;
  };
  const reviewed = () => {
    setReviewing(false); message.success("复盘记录已保存。");
    void queryClient.invalidateQueries({ queryKey: qk.tracking(userId) });
  };
  return <Drawer title="关注判断与证据时间线" open onClose={onClose} width="min(760px, 100vw)" rootClassName="tracking-overlay">
    {query.isPending ? <div className="tracking-loading"><Spin /></div> : query.isError && !thesis ? <Alert type="error" showIcon message={api.getApiErrorMessage(query.error)} action={<Button onClick={() => query.refetch()}>重试</Button>} /> : thesis ? <>
      {query.isError ? <Alert type="warning" showIcon className="tracking-banner" message="更新失败，显示上次读取的判断。" action={<Button onClick={() => query.refetch()}>重试</Button>} /> : null}
      <ThesisSummary thesis={thesis} />
      <div className="tracking-detail-actions"><Link to={`/?chart=${encodeURIComponent(thesis.symbol)}`}>查看个股研究 ↗</Link>{thesis.status === "active" && !reviewing ? <Button type="primary" onClick={() => setReviewing(true)}>复盘这条判断</Button> : null}</div>
      <EvidenceList evidence={thesis.evidence_refs} />
      {reviewing ? <ReviewForm key={id} thesis={thesis} onSaved={reviewed} onCancel={() => setReviewing(false)} onReload={reload} /> : null}
      {changes.length ? <section className="tracking-detail-section"><Title level={4}>与这条判断相关的变化</Title>{changes.slice(0, 5).map((change) => <ChangeCard key={change.id} change={change} thesisNames={new Map([[id, thesis.reason]])} onOpenThesis={onOpenThesis} onFeedback={onFeedback} pending={feedbackPendingId === change.id} />)}{changes.length > 5 ? <Text type="secondary">另 {changes.length - 5} 条变化可在今日变化列表中查看。</Text> : null}</section> : null}
      <section className="tracking-detail-section"><Title level={4}>判断时间线</Title><ThesisTimeline items={query.data?.timeline || EMPTY} /></section>
    </> : null}
  </Drawer>;
}

export default function TodayPage() {
  const { user } = useAuth();
  return <TodayWorkspace key={user?.id ?? "guest"} />;
}

function TodayWorkspace() {
  const { user, authRequired, logout } = useAuth();
  const { message } = AntApp.useApp();
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const selectedId = searchParams.get("thesis");
  const [filter, setFilter] = useState("unread");
  const [thesisFilter, setThesisFilter] = useState("active");
  const [composerOpen, setComposerOpen] = useState(false);
  const uid = user?.id;
  // Independent requests run together and use account-scoped cache keys.
  const changes = useQuery({ queryKey: qk.trackingChanges(uid), queryFn: ({ signal }) => api.getTrackingChanges({ signal }), enabled: uid != null, staleTime: 10_000, refetchInterval: 60_000, refetchIntervalInBackground: false });
  const theses = useQuery({ queryKey: qk.trackingTheses(uid), queryFn: ({ signal }) => api.getTrackingTheses({ signal }), enabled: uid != null, staleTime: 10_000 });
  const refresh = () => queryClient.invalidateQueries({ queryKey: qk.tracking(uid) });
  const scan = useMutation({ mutationFn: api.refreshTracking, onSuccess: (result) => { void refresh(); if (result.status === "partial" || result.news?.pending || result.calendar?.pending) message.info("已完成本轮检查，仍有证据待处理，可继续检查。"); else message.success("本地证据检查完成。"); }, onError: (error) => message.error(api.getApiErrorMessage(error)) });
  const feedback = useMutation({
    mutationFn: ({ id, patch }) => api.updateTrackingChange(id, patch),
    onSuccess: () => { void refresh(); },
    onError: (error) => message.error(api.getApiErrorMessage(error)),
  });
  const allChanges = changes.data?.items || EMPTY;
  const allTheses = theses.data?.items || EMPTY;
  const names = new Map(allTheses.map((thesis) => [thesis.id, thesis.reason]));
  const activeTheses = allTheses.filter((thesis) => thesis.status === "active");
  const shownTheses = allTheses.filter((thesis) => thesisFilter === "all" || thesis.status === "active");
  const parts = partitionChanges(allChanges, filter);
  const unread = allChanges.filter((change) => !change.is_read && !change.resolved && change.feedback !== "irrelevant").length;
  const due = activeTheses.filter((thesis) => thesis.review_at && thesis.review_at <= shanghaiDateKey()).length;
  const openThesis = (id) => setSearchParams((previous) => { const next = new URLSearchParams(previous); if (id) next.set("thesis", id); else next.delete("thesis"); return next; });
  const recordFeedback = (id, patch) => feedback.mutate({ id, patch });
  const pendingId = feedback.isPending ? feedback.variables?.id : null;
  const renderChange = (change) => <ChangeCard key={change.id} change={change} thesisNames={names} onOpenThesis={openThesis} onFeedback={recordFeedback} pending={pendingId === change.id} />;

  return <Layout className="sva-layout tracking-layout">
    <AppToolbar active="today" user={user} authRequired={authRequired} onLogout={logout} settingsAction={<ConfigConsoleButton isAdmin={user?.role === "admin"} />} />
    <Layout.Content className="tracking-content">
      <header className="tracking-heading"><div><span className="tracking-date">{shanghaiDateKey().replaceAll("-", ".")} · 北京时间</span><Title level={1}>今日变化</Title><Paragraph type="secondary">把新证据放回原来的判断，关注真正需要重新思考的变化。</Paragraph></div><div className="tracking-heading-actions"><Button icon={<ReloadOutlined />} loading={scan.isPending} onClick={() => scan.mutate()}>检查本地变化</Button><Button type="primary" icon={<PlusOutlined />} onClick={() => setComposerOpen(true)}>新建关注判断</Button></div></header>
      <section className="tracking-summary-band" aria-label="跟踪概况"><div><span>待处理变化</span><strong>{changes.isPending ? "—" : unread}</strong><small>重要变化优先</small></div><div><span>正在关注</span><strong>{theses.isPending ? "—" : activeTheses.length}</strong><small>保留最初的理由与证据</small></div><div><span>到期复盘</span><strong>{theses.isPending ? "—" : due}</strong><small>决定继续、修订或结束</small></div></section>
      <ScanStatus status={typeof changes.data?.status === "object" ? changes.data.status : changes.data} />
      {changes.isError ? <Alert className="tracking-banner" type="warning" showIcon message="变化列表暂时无法更新" description={changes.data ? "显示上次读取的记录。" : api.getApiErrorMessage(changes.error)} action={<Button onClick={() => changes.refetch()}>重试</Button>} /> : null}
      <div className="tracking-main-grid">
        <section className="tracking-feed" aria-label="变化清单">
          <div className="tracking-section-heading"><Title level={2}>需要关注的变化</Title><Segmented aria-label="变化范围" value={filter} onChange={setFilter} options={[{ label: "待处理", value: "unread" }, { label: "全部", value: "all" }]} /></div>
          {changes.isPending ? <div className="tracking-loading"><Spin aria-label="读取变化清单" /></div> : parts.total ? <>
            {parts.first.map(renderChange)}
            {parts.rest.length ? <details className="tracking-more"><summary>展开其余 {parts.rest.length} 条变化</summary><div>{parts.rest.map(renderChange)}</div></details> : null}
            {parts.supplements.length ? <details className="tracking-more tracking-supplements"><summary>来源补充 · {parts.supplements.length} 条<span>同一事件的补充材料</span></summary><div>{parts.supplements.map(renderChange)}</div></details> : null}
          </> : !changes.isError ? <div className="tracking-empty"><div className="tracking-empty-icon"><CheckOutlined /></div><h3>{filter === "unread" && allChanges.length ? "目前没有待处理变化" : "尚未发现需要关注的变化"}</h3><p>{activeTheses.length ? "检查本地已归档证据，相关变化会出现在这里。也可以打开一条判断，随时记录复盘。" : "保存一条关注判断，记录观察与失效条件。之后的新证据与到期提醒会汇集在这里。"}</p>{!activeTheses.length ? <Button onClick={() => setComposerOpen(true)}>写下第一条判断</Button> : <Button onClick={() => scan.mutate()} loading={scan.isPending}>检查本地变化</Button>}</div> : null}
        </section>
        <aside className="tracking-judgments" aria-label="我的关注判断"><Card title="我的关注判断" className="tracking-card" extra={<Button type="text" size="small" onClick={() => setThesisFilter(thesisFilter === "all" ? "active" : "all")}>{thesisFilter === "all" ? "仅看关注中" : "包含已关闭"}</Button>}>
          {theses.isError ? <Alert type="warning" message="判断读取失败" action={<Button size="small" onClick={() => theses.refetch()}>重试</Button>} /> : null}
          {theses.isPending ? <div className="tracking-loading"><Spin /></div> : shownTheses.length ? <div className="tracking-thesis-list">{shownTheses.map((thesis) => <button type="button" className="tracking-thesis-row" key={thesis.id} onClick={() => openThesis(thesis.id)}><div className="tracking-between"><strong>{thesis.symbol}</strong>{thesis.status === "closed" ? <Tag>已关闭</Tag> : thesis.review_at <= shanghaiDateKey() ? <Tag color="orange">待复盘</Tag> : <Badge status="processing" text="关注中" />}</div><p>{thesis.reason}</p><span>复盘 {thesis.review_at || "—"} · 版本 {thesis.revision}</span></button>)}</div> : !theses.isError ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={thesisFilter === "all" ? "尚未保存判断" : "暂无正在关注的判断"} /> : null}
          <div className="tracking-sidebar-note">也可以从个股策略观点或新闻保存判断，在经济日历中关联需要跟踪的事件。</div>
        </Card><p className="tracking-caption tracking-local-note">判断与复盘保存在本地，无需配置模型。</p></aside>
      </div>
    </Layout.Content>
    {composerOpen ? <ThesisComposer onClose={() => setComposerOpen(false)} /> : null}
    {selectedId ? <ThesisDetail key={selectedId} id={selectedId} userId={uid} onClose={() => openThesis(null)} onOpenThesis={openThesis} onFeedback={recordFeedback} feedbackPendingId={pendingId} /> : null}
  </Layout>;
}
