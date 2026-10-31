import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Alert, App as AntApp, Button, Collapse, Empty, Input, Select, Spin, Table, Tag } from "antd";
import DownloadOutlined from "@ant-design/icons/es/icons/DownloadOutlined";
import FileSearchOutlined from "@ant-design/icons/es/icons/FileSearchOutlined";
import { useAuth } from "./AuthContext.jsx";
import { safeUrl } from "./components/format.js";
import { trackingTime } from "./trackingViewModel.js";
import { DOCUMENT_KINDS, DOCUMENT_STATUS, FINDING_KINDS, JOB_KINDS, JOB_STATUS, PERIOD_TYPES, REPORT_TYPES, SECTION_TITLES, activeEarningsJob, analysisGate, earningsDate, earningsError, earningsKey, extractionSummary, pdfPageUrl, physicalPage, sourceLabel, textList, usageLabel, visibleReports, warningText } from "./earningsViewModel.js";
import * as api from "./api.js";
import "./EarningsReportsPanel.css";

const EMPTY = [];
function Failure({ error, onRetry, retained = false }) {
  return error ? <Alert className="earnings-alert" showIcon type="warning" message={earningsError(error)} description={retained ? "显示上次成功读取的记录。" : undefined} action={onRetry ? <Button size="small" onClick={onRetry}>重试</Button> : null} /> : null;
}

function JobStatus({ jobs = EMPTY }) {
  const shown = jobs.slice().sort((a, b) => Number(b.created_at) - Number(a.created_at)).slice(0, 4);
  if (!shown.length) return null;
  return <details className="earnings-jobs" open={shown.some(activeEarningsJob)}><summary>最近任务{shown.some(activeEarningsJob) ? " · 正在处理" : ""}</summary><div>{shown.map((job) => <div className="earnings-job" key={job.id}><div><Tag color={job.status === "failed" ? "orange" : activeEarningsJob(job) ? "blue" : "default"}>{JOB_STATUS[job.status] || job.status}</Tag><strong>{JOB_KINDS[job.kind] || "财报任务"}</strong><time>{trackingTime(job.finished_at || job.created_at)}</time></div>{job.error ? <p>{job.error}</p> : null}</div>)}</div></details>;
}

function Citations({ evidence = EMPTY, pdfUrl }) {
  const valid = evidence.filter((item) => physicalPage(item.page));
  if (!valid.length) return <span className="earnings-muted">未提供页码</span>;
  return <div className="earnings-citations">{valid.map((item, index) => <details key={`${item.page}:${index}`}><summary>原文第 {physicalPage(item.page)} 页</summary>{item.quote ? <blockquote>{item.quote}</blockquote> : <p className="earnings-muted">摘录未保存，请查看原文。</p>}{pdfPageUrl(pdfUrl, item.page) ? <a href={pdfPageUrl(pdfUrl, item.page)} target="_blank" rel="noopener noreferrer">打开 PDF 此页 ↗</a> : null}</details>)}</div>;
}

function Coverage({ coverage, document }) {
  const warnings = [...new Set([...textList(document?.warnings), ...textList(coverage?.warnings)])];
  const pages = Array.isArray(coverage?.included_pages) ? coverage.included_pages : EMPTY;
  return <div className="earnings-coverage"><div className="earnings-meta"><span>{extractionSummary(document || (coverage ? { ...coverage, status: coverage.extraction_status } : null))}</span>{coverage ? <><Tag color={coverage.scope === "selected_pages" ? "orange" : "default"}>{coverage.scope === "full_text" ? "全文" : "部分页面"}</Tag><span>输入 {pages.length} 页 · {coverage.included_chars ?? "—"} 字符</span></> : null}</div>
    {warnings.length ? <ul>{warnings.map((warning, index) => <li key={index}>{warningText(warning)}</li>)}</ul> : null}
    {coverage?.scope === "selected_pages" ? <p>解读范围限于下列页面。</p> : null}
    {pages.length ? <details><summary>输入页码</summary><p>{pages.join("、")}</p></details> : null}
  </div>;
}

function Interpretation({ analysis, report, document }) {
  const result = analysis.result || {};
  const content = result.report || {};
  const sections = Array.isArray(content.sections) ? content.sections : EMPTY;
  const metrics = Array.isArray(content.metrics) ? content.metrics : EMPTY;
  const differentDocument = Boolean(document?.sha256 && analysis.document_sha && document.sha256 !== analysis.document_sha);
  const citationPdf = differentDocument ? null : report.pdf_url;
  return <div className="earnings-interpretation"><div className="earnings-meta"><span>{analysis.model || result.model || "模型未标注"}</span><time>解读于 {trackingTime(analysis.created_at || result.generated_at)}</time>{result.model_called === false ? <Tag>已保存结果</Tag> : null}</div>
    {differentDocument ? <Alert className="earnings-alert" type="warning" showIcon message="此解读基于旧版文件。" /> : null}
    {analysis.focus ? <p className="earnings-focus-history">关注点：{analysis.focus}</p> : null}
    <div className="earnings-summary"><h3>财报概览</h3><p>{content.summary || "暂无概览"}</p></div>
    <Coverage coverage={result.coverage} document={differentDocument ? null : document} />
    {metrics.length ? <section className="earnings-metrics"><h3>关键指标与口径</h3><Table size="small" rowKey="_row" dataSource={metrics.map((metric, index) => ({ ...metric, _row: index }))} pagination={false} scroll={{ x: 720 }} columns={[
      { title: "指标", dataIndex: "name", width: 140 },
      { title: "数值", dataIndex: "value", width: 110, render: (value) => value == null || value === "" ? "—" : String(value) },
      { title: "单位", dataIndex: "unit", width: 80, render: (value) => value || "未说明" },
      { title: "统计口径 / 期间", key: "period", width: 200, render: (_, metric) => <div>{PERIOD_TYPES[metric.period_type] || "口径未明确"}<div className="earnings-muted">{metric.period || "期间未说明"}</div></div> },
      { title: "原文依据", key: "evidence", width: 190, render: (_, metric) => <Citations evidence={metric.evidence} pdfUrl={citationPdf} /> },
    ]} /><p className="earnings-note">季度报告含累计数据，比较时请查看统计期间。</p></section> : null}
    {sections.length ? <Collapse className="earnings-sections" defaultActiveKey={[sections[0].id]} items={sections.map((section, index) => ({ key: section.id || String(index), label: section.title || SECTION_TITLES[section.id] || "财报观察", children: <div className="earnings-findings">{(section.findings || EMPTY).map((finding, findingIndex) => <article key={findingIndex}><Tag color={finding.kind === "gap" ? "orange" : finding.kind === "fact" ? "blue" : "default"}>{FINDING_KINDS[finding.kind] || "观察"}</Tag><p>{finding.text}</p><Citations evidence={finding.evidence} pdfUrl={citationPdf} /></article>)}</div> }))} /> : null}
    {textList(content.limitations).length ? <Alert className="earnings-alert" type="info" showIcon message="解读局限" description={<ul className="earnings-limitations">{content.limitations.map((item, index) => <li key={index}>{item}</li>)}</ul>} /> : null}
    <p className="earnings-note">引用按 PDF 页序编号，与正文页码可能不同。</p>
  </div>;
}

function ReportDetail({ reportId, userId, symbol, overview, onChanged }) {
  const { message } = AntApp.useApp();
  const [focus, setFocus] = useState("");
  const [historyId, setHistoryId] = useState(null);
  const [pdfError, setPdfError] = useState(null);
  const [downloading, setDownloading] = useState(false);
  const detail = useQuery({ queryKey: earningsKey(userId, symbol, "detail", reportId), queryFn: ({ signal }) => api.getEarningsReport(reportId, { signal }), retry: false, refetchInterval: (query) => query.state.data?.jobs?.some(activeEarningsJob) ? 2000 : false, refetchIntervalInBackground: false });
  const report = detail.data?.report;
  const document = detail.data?.document;
  const jobs = detail.data?.jobs || EMPTY;
  const active = jobs.some(activeEarningsJob) || (overview.jobs || EMPTY).some((job) => job.report_id === reportId && activeEarningsJob(job));
  const analyses = [...(detail.data?.analyses || EMPTY)].sort((a, b) => Number(b.created_at) - Number(a.created_at));
  const selected = analyses.find((analysis) => analysis.id === historyId) || analyses[0];
  async function refresh() { await Promise.all([detail.refetch(), onChanged()]); }
  const prepare = useMutation({ mutationFn: () => api.prepareEarningsReport(reportId), retry: false, onSuccess: async () => { await refresh(); message.success("下载解析任务已提交。"); } });
  const analyze = useMutation({ mutationFn: () => api.analyzeEarningsReport(reportId, focus.trim()), retry: false, onSuccess: async (job) => { setHistoryId(null); await refresh(); message.success(job.status === "succeeded" ? "已读取已有解读。" : "解读任务已提交。"); } });
  const busy = active || prepare.isPending || analyze.isPending;
  const gate = analysisGate({ report, model: overview.model, usage: overview.usage, workerEnabled: overview.worker_enabled, busy });
  async function download() {
    setDownloading(true); setPdfError(null);
    try {
      const blob = await api.downloadEarningsPdf(reportId);
      const href = URL.createObjectURL(blob);
      const anchor = window.document.createElement("a");
      anchor.href = href; anchor.download = `${symbol}-${report?.report_period || reportId}.pdf`;
      window.document.body.appendChild(anchor); anchor.click(); anchor.remove();
      setTimeout(() => URL.revokeObjectURL(href), 1000);
    } catch (error) { setPdfError(error); } finally { setDownloading(false); }
  }
  if (detail.isPending) return <div className="earnings-loading"><Spin aria-label="读取财报详情" /></div>;
  return <section className="earnings-detail"><Failure error={detail.error} retained={Boolean(report)} onRetry={() => detail.refetch()} />{report ? <>
    <div className="earnings-report-heading"><div className="earnings-meta"><Tag>{REPORT_TYPES[report.report_type] || "财务报告"}</Tag><Tag>{DOCUMENT_KINDS[report.document_kind] || "披露文件"}</Tag>{report.is_revision ? <Tag color="orange">修订版</Tag> : null}</div><h2>{report.title}</h2><div className="earnings-meta"><span>报告期 {report.report_period || "未标注"}</span><span>披露 {earningsDate(report.announcement_date || report.published_at)}</span><span>来源 {sourceLabel(report.source)}</span></div></div>
    <div className="earnings-document-actions">{safeUrl(report.pdf_url) ? <a href={safeUrl(report.pdf_url)} target="_blank" rel="noopener noreferrer">原文PDF ↗</a> : null}{safeUrl(report.announcement_url) ? <a href={safeUrl(report.announcement_url)} target="_blank" rel="noopener noreferrer">披露公告 ↗</a> : null}<Button size="small" icon={<DownloadOutlined aria-hidden="true" />} disabled={busy || overview.worker_enabled === false} loading={prepare.isPending} onClick={() => prepare.mutate()}>{document?.status && !["not_downloaded", "ready"].includes(document.status) ? "重试下载与解析" : document?.status === "ready" ? "重新检查文件" : "下载并解析"}</Button>{document && document.status !== "not_downloaded" ? <Button size="small" disabled={busy} loading={downloading} onClick={() => void download()}>下载已保存PDF</Button> : null}</div>
    <Coverage document={document} />
    <Failure error={prepare.error || analyze.error || pdfError} />
    <JobStatus jobs={jobs} />
    <section className="earnings-analysis-control"><div className="earnings-analysis-heading"><h3>财报解读</h3><span>今日财报请求 {usageLabel(overview.usage)}</span></div><label htmlFor={`earnings-focus-${reportId}`}>关注点（可选）</label><Input.TextArea id={`earnings-focus-${reportId}`} value={focus} onChange={(event) => setFocus(event.target.value)} maxLength={2000} showCount autoSize={{ minRows: 2, maxRows: 6 }} placeholder="例如：收入增长是否转化为经营现金流？关注应收、存货与一次性收益。" disabled={analyze.isPending} />
      <div className="earnings-analysis-actions"><p>{gate.reason}{!overview.model?.configured ? <> <Link to="/setup">打开模型设置 ↗</Link></> : null}</p><Button type="primary" disabled={!gate.allowed || detail.isError} loading={analyze.isPending} onClick={() => analyze.mutate()}>解读财报</Button></div><p className="earnings-note">财报额度独立计算；相同文件和关注点复用已有解读。</p>
    </section>
    <section className="earnings-history"><div className="earnings-analysis-heading"><h3>已保存解读</h3>{analyses.length > 1 ? <Select aria-label="选择历史财报解读" value={selected?.id} onChange={setHistoryId} options={analyses.map((analysis) => ({ value: analysis.id, label: `${trackingTime(analysis.created_at)} · ${analysis.focus ? analysis.focus.slice(0, 25) : "综合解读"}` }))} /> : <span>{analyses.length} 份</span>}</div>
      {selected ? <Interpretation key={selected.id} analysis={selected} report={report} document={document} /> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={busy ? "任务处理中" : "暂无解读"} />}
    </section>
  </> : null}</section>;
}

function EarningsContent({ symbol, displayName }) {
  const { user } = useAuth();
  const { message } = AntApp.useApp();
  const qc = useQueryClient();
  const [year, setYear] = useState("all");
  const [type, setType] = useState("all");
  const [selectedId, setSelectedId] = useState(null);
  const query = useQuery({ queryKey: earningsKey(user?.id, symbol, "list"), queryFn: ({ signal }) => api.getEarningsReports(symbol, { signal }), enabled: Boolean(symbol) && user?.id != null, retry: false, staleTime: 15_000, refetchInterval: (query) => query.state.data?.jobs?.some(activeEarningsJob) ? 2000 : false, refetchIntervalInBackground: false });
  const refreshQueries = () => qc.invalidateQueries({ queryKey: earningsKey(user?.id, symbol) });
  const refresh = useMutation({ mutationFn: () => api.refreshEarningsReports(symbol), retry: false, onSuccess: async () => { await refreshQueries(); message.success("已提交财报查找任务。"); } });
  const reports = query.data?.reports || EMPTY;
  const visible = visibleReports(reports, year, type);
  const selected = visible.find((report) => report.id === selectedId) || visible[0];
  const years = [...new Set(reports.map((report) => report.report_year).filter(Boolean))].sort((a, b) => Number(b) - Number(a));
  const searching = refresh.isPending || (query.data?.jobs || EMPTY).some((job) => job.kind === "refresh" && activeEarningsJob(job));
  const sources = query.data?.source_status?.sources || EMPTY;
  return <div className="earnings-panel"><header className="earnings-heading"><div><h2>财报季 <span>{displayName || symbol}</span></h2><p>下载公开财报，提取指标与解读。</p></div><Button icon={<FileSearchOutlined aria-hidden="true" />} loading={searching} disabled={!symbol || query.data?.worker_enabled === false} onClick={() => refresh.mutate()}>查找财报</Button></header>
    <Failure error={query.error || refresh.error} retained={Boolean(query.data)} onRetry={query.isError ? () => query.refetch() : undefined} />
    <div className="earnings-toolbar"><Select aria-label="报告年份" value={year} onChange={(value) => { setYear(value); setSelectedId(null); }} options={[{ value: "all", label: "全部年份" }, ...years.map((value) => ({ value: String(value), label: `${value}年` }))]} /><Select aria-label="报告类型" value={type} onChange={(value) => { setType(value); setSelectedId(null); }} options={[{ value: "all", label: "全部报告类型" }, ...Object.entries(REPORT_TYPES).map(([value, label]) => ({ value, label }))]} /><span>{visible.length} 份披露文件</span></div>
    {query.data?.worker_enabled === false ? <Alert className="earnings-alert" type="warning" showIcon message="财报服务已关闭" /> : null}
    <JobStatus jobs={(query.data?.jobs || EMPTY).filter((job) => job.kind === "refresh")} />
    {query.isPending ? <div className="earnings-loading"><Spin aria-label="读取财报目录" /></div> : visible.length ? <div className="earnings-workspace"><aside className="earnings-report-list" aria-label="披露文件列表">{visible.map((report) => <button type="button" className={`earnings-report-row${selected?.id === report.id ? " is-selected" : ""}`} aria-pressed={selected?.id === report.id} key={report.id} onClick={() => setSelectedId(report.id)}><div className="earnings-meta"><span>{REPORT_TYPES[report.report_type] || "财务报告"} · {report.report_year}</span>{report.is_revision ? <Tag color="orange">修订</Tag> : null}</div><strong>{report.title}</strong><span>{DOCUMENT_KINDS[report.document_kind] || "披露文件"} · {earningsDate(report.announcement_date || report.published_at)}</span><small>{sourceLabel(report.source)} · {DOCUMENT_STATUS[report.document?.status] || "尚未解析"}</small></button>)}</aside><ReportDetail key={`${user?.id}:${symbol}:${selected.id}`} reportId={selected.id} userId={user?.id} symbol={symbol} overview={query.data || {}} onChanged={refreshQueries} /></div> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={reports.length ? "暂无匹配报告" : "暂无财报，请点击「查找财报」。"} />}
    <details className="earnings-sources"><summary>来源与范围{query.data?.source_status?.partial ? " · 覆盖不完整" : ""}</summary><p>查找近 3 年披露文件，每次最多保存 30 份，包含全文、摘要及更正公告。目录以来源返回为准。</p>{sources.map((source, index) => <p key={`${source.source}:${index}`}><strong>{sourceLabel(source.source)}</strong> · {source.status === "ok" || source.status === "success" ? "读取成功" : source.status === "error" ? "暂不可用" : source.status || "未标注状态"}{source.error ? ` · ${source.error}` : ""}</p>)}<p>最近查找 {trackingTime(query.data?.source_status?.updated_at)}。财报文件共享，解读仅本账户可见。</p></details>
  </div>;
}

export default function EarningsReportsPanel({ symbol, displayName }) { const { user } = useAuth(); return symbol ? <EarningsContent key={`${user?.id}:${symbol}`} symbol={symbol} displayName={displayName} /> : null; }
