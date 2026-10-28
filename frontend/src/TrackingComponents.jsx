import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, App as AntApp, Button, Collapse, Form, Input, Modal, Select, Space, Tag, Typography } from "antd";
import { Link } from "react-router-dom";
import { useAuth } from "./AuthContext.jsx";
import { qk } from "./hooks/queryKeys.js";
import { safeUrl } from "./components/format.js";
import { shanghaiDateKey } from "./calendarViewModel.js";
import { appendEvidence, evidenceInputRef, isFutureReviewDate, nextReviewDate, normalizeTrackingSymbol, trackingError, trackingTime } from "./trackingViewModel.js";
import * as api from "./api.js";
import "./TodayPage.css";

const { Text, Paragraph } = Typography;
const EMPTY = [];
export const requiredText = [{ required: true, whitespace: true, message: "请填写，便于之后核对原判断。" }];
export const futureReviewRule = { validator: (_, value) => isFutureReviewDate(value)
  ? Promise.resolve() : Promise.reject(new Error("请选择北京时间今天之后的复盘日期。")) };

export function EvidenceCard({ evidence, label }) {
  if (!evidence) return null;
  const href = safeUrl(evidence.url);
  const snapshot = evidence.snapshot;
  return <div className="tracking-evidence">
    <div className="tracking-evidence-heading"><Tag>{label || { news: "新闻", calendar: "经济事件", strategy: "策略观点" }[evidence.kind] || "证据"}</Tag>
      {href ? <a href={href} target="_blank" rel="noopener noreferrer">{evidence.title || "查看原文"} ↗</a> : <Text strong>{evidence.title || "归档证据"}</Text>}
    </div>
    {evidence.excerpt ? <Paragraph className="tracking-preserve">{evidence.excerpt}</Paragraph> : null}
    {snapshot?.timeframe ? <Text type="secondary">适用周期：{snapshot.timeframe} · 规则评分：{snapshot.score ?? "—"}</Text> : null}
    <div className="tracking-evidence-meta">{snapshot?.source ? <span>来源：{typeof snapshot.source === "string" ? snapshot.source : snapshot.source.kline_source || snapshot.source.mode || "归档记录"}</span> : null}<span>发布：{trackingTime(evidence.published_at)}</span><span>采集：{trackingTime(evidence.observed_at)}</span></div>
    {evidence.revision_id ? <Text type="secondary" className="tracking-reference">证据版本 {evidence.revision_id}</Text> : null}
  </div>;
}

export function EvidenceList({ evidence = EMPTY, label = "已保存的证据" }) {
  if (!evidence.length) return <Text type="secondary">尚未关联证据，可从新闻、策略观点或经济日历补充。</Text>;
  return <Collapse size="small" className="tracking-evidence-collapse" items={[{ key: "sources", label: `${label}（${evidence.length}）`, children: <div className="tracking-evidence-list">{evidence.map((item, index) => <EvidenceCard key={`${item.kind}:${item.id}:${item.revision_id || index}`} evidence={item} />)}</div> }]} />;
}

/** Mounted only while composing: closing and reopening creates a fresh local draft. */
export function ThesisComposer({ symbol = "", initial = {}, evidenceRefs = EMPTY, onClose, onSaved }) {
  const { user } = useAuth();
  const queryClient = useQueryClient();
  const { message } = AntApp.useApp();
  const [form] = Form.useForm();
  const save = useMutation({
    mutationFn: (values) => api.createTrackingThesis({
      symbol: normalizeTrackingSymbol(values.symbol), reason: values.reason.trim(),
      watch_conditions: values.watch_conditions.trim(), invalidation_conditions: values.invalidation_conditions.trim(),
      review_at: values.review_at, evidence_refs: evidenceRefs,
    }),
    onSuccess: (thesis) => {
      void queryClient.invalidateQueries({ queryKey: qk.tracking(user?.id) });
      message.success("关注判断已保存，可在“今日变化”继续跟踪。");
      onSaved?.(thesis);
      onClose();
    },
  });
  return <Modal title="保存关注判断" open onCancel={() => !save.isPending && onClose()} footer={null} width={620} rootClassName="tracking-overlay" maskClosable={!save.isPending} closable={!save.isPending}>
    <Paragraph type="secondary" className="tracking-caption">记录为什么关注、接下来观察什么，以及什么情况会改变判断。无需配置模型。</Paragraph>
    {save.isError ? <Alert type="error" showIcon message={trackingError(save.error)} className="tracking-banner" /> : null}
    <Form form={form} layout="vertical" initialValues={{ symbol, reason: "", watch_conditions: "", invalidation_conditions: "", review_at: nextReviewDate(), ...initial }} onFinish={(values) => save.mutate(values)} disabled={save.isPending}>
      <div className="tracking-form-row">
        <Form.Item name="symbol" label="股票代码" rules={[{ validator: (_, value) => normalizeTrackingSymbol(value) ? Promise.resolve() : Promise.reject(new Error("请输入 A 股代码，例如 600519 或 600519.SS。")) }]}><Input placeholder="例如 600519" maxLength={12} autoComplete="off" /></Form.Item>
        <Form.Item name="review_at" label="复盘日期（北京时间）" rules={[futureReviewRule]}><Input type="date" min={nextReviewDate(new Date(), 1)} /></Form.Item>
      </div>
      <Form.Item name="reason" label="关注理由" rules={requiredText}><Input.TextArea autoSize={{ minRows: 2, maxRows: 5 }} maxLength={4000} placeholder="例如：订单变化可能改善盈利，先验证后续兑现情况" /></Form.Item>
      <Form.Item name="watch_conditions" label="观察条件" rules={requiredText}><Input.TextArea autoSize={{ minRows: 2, maxRows: 4 }} maxLength={4000} placeholder="下一次公告、经营数据或市场表现，需要看到什么？" /></Form.Item>
      <Form.Item name="invalidation_conditions" label="失效条件" rules={requiredText}><Input.TextArea autoSize={{ minRows: 2, maxRows: 4 }} maxLength={4000} placeholder="出现什么证据时，应重新考虑或放弃原判断？" /></Form.Item>
      {evidenceRefs.length ? <div className="tracking-composer-evidence"><EvidenceList evidence={evidenceRefs} label="随判断保存的证据" /></div> : null}
      <div className="tracking-form-actions"><Button onClick={onClose}>取消</Button><Button type="primary" htmlType="submit" loading={save.isPending}>保存判断</Button></div>
    </Form>
  </Modal>;
}

/** Linking is an explicit user action, and reads the latest revision before appending. */
export function LinkEvidenceToThesis({ evidence }) {
  const { user } = useAuth();
  const { message } = AntApp.useApp();
  const queryClient = useQueryClient();
  const [expanded, setExpanded] = useState(false);
  const [selectedId, setSelectedId] = useState();
  const theses = useQuery({ queryKey: qk.trackingTheses(user?.id), queryFn: ({ signal }) => api.getTrackingTheses({ signal }), enabled: expanded && user?.id != null });
  const active = (theses.data?.items || EMPTY).filter((thesis) => thesis.status === "active");
  const link = useMutation({
    mutationFn: async () => {
      const { thesis } = await api.getTrackingThesis(selectedId);
      return api.updateTrackingThesis(selectedId, { revision: thesis.revision, evidence_refs: appendEvidence(thesis.evidence_refs, evidence).map(evidenceInputRef) });
    },
    onSuccess: () => { void queryClient.invalidateQueries({ queryKey: qk.tracking(user?.id) }); message.success("事件已关联到关注判断，后续修订会进入变化清单。"); setExpanded(false); setSelectedId(undefined); },
  });
  if (!evidence) return null;
  return <section className="tracking-link-evidence">
    <Button onClick={() => { setExpanded(!expanded); link.reset(); }} aria-expanded={expanded}>关联到我的关注判断</Button>
    {expanded ? <div className="tracking-link-controls">
      <Paragraph type="secondary" className="tracking-caption">只跟踪你主动关联的事件。选择一条现有判断，将当前事件版本保存为证据。</Paragraph>
      {theses.isError ? <Alert type="warning" message="关注判断读取失败" action={<Button size="small" onClick={() => theses.refetch()}>重试</Button>} /> : null}
      {link.isError ? <Alert type="warning" message={trackingError(link.error)} /> : null}
      <Select aria-label="选择要关联的关注判断" placeholder="选择关注判断" loading={theses.isFetching} value={selectedId} onChange={setSelectedId} disabled={link.isPending} showSearch optionFilterProp="label"
        options={active.map((thesis) => ({ value: thesis.id, label: `${thesis.symbol} · ${thesis.reason}` }))} />
      <Button type="primary" disabled={!selectedId || theses.isError} loading={link.isPending} onClick={() => link.mutate()}>关联此事件</Button>
      {!theses.isPending && !active.length ? <Paragraph className="tracking-caption">还没有正在关注的判断。<Link to="/today">前往今日变化创建</Link>。</Paragraph> : null}
    </div> : null}
  </section>;
}

export function ThesisSummary({ thesis }) {
  return <div className="tracking-thesis-summary">
    <div className="tracking-between"><Tag color={thesis.status === "active" ? "blue" : "default"}>{thesis.status === "active" ? "正在关注" : "已关闭"}</Tag><Text type="secondary">版本 {thesis.revision}</Text></div>
    <h2>{thesis.symbol}</h2>
    <dl className="tracking-conditions">
      <div><dt>关注理由</dt><dd>{thesis.reason}</dd></div>
      <div><dt>观察条件</dt><dd>{thesis.watch_conditions || "尚未填写"}</dd></div>
      <div><dt>失效条件</dt><dd>{thesis.invalidation_conditions || "尚未填写"}</dd></div>
    </dl>
    <div className="tracking-evidence-meta"><span>复盘日期：{thesis.review_at || "—"}{thesis.status === "active" && thesis.review_at <= shanghaiDateKey() ? " · 已到期" : ""}</span><span>更新：{trackingTime(thesis.updated_at)}</span></div>
  </div>;
}
