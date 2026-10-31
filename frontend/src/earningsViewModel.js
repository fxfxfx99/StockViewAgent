import { safeUrl } from "./components/format.js";

export const earningsKey = (userId, symbol, ...parts) => ["earnings", userId ?? "guest", symbol || "", ...parts];
export const REPORT_TYPES = { annual: "年度报告", half_year: "半年度报告", q1: "第一季度报告", q3: "第三季度报告" };
export const DOCUMENT_KINDS = { full_report: "报告全文", summary: "报告摘要", forecast: "业绩预告", express: "业绩快报", correction: "更正公告" };
export const DOCUMENT_STATUS = { not_downloaded: "尚未下载", ready: "解析完成", partial: "部分可读", no_text: "未提取到正文", encrypted: "PDF已加密", invalid_pdf: "文件无法解析", too_large: "文件过大", timeout: "解析超时" };
export const JOB_KINDS = { refresh: "查找财报", prepare: "下载与解析", analyze: "财报解读" };
export const JOB_STATUS = { queued: "排队中", running: "处理中", succeeded: "已完成", failed: "未完成" };
export const PERIOD_TYPES = { single_quarter: "单季度", year_to_date: "年初至报告期末累计", annual: "全年", point_in_time: "期末时点", unknown: "口径未明确" };
export const FINDING_KINDS = { fact: "报告事实", interpretation: "分析推断", gap: "信息缺口" };
export const SECTION_TITLES = { revenue_profit: "收入与利润", cash_flow: "现金流", profit_quality: "盈利质量", balance_sheet: "资产负债", segments: "业务与分部", comparison: "同期与历史比较", audit_risks: "审计与风险", follow_up: "后续观察" };
export const EXTRACTION_WARNINGS = { page_text_limit: "部分页面文字达到提取上限", text_limit: "全文文字达到提取上限", page_limit: "部分页面因页数上限未处理", scanned_pages_no_ocr: "发现扫描页，未执行OCR文字识别", pages_without_text: "部分页面未提取到文字", page_extraction_error: "部分页面提取失败", selected_text_not_full_document: "本次解读使用部分页面" };
export function warningText(value) { return EXTRACTION_WARNINGS[value] || value; }
export function sourceLabel(value) { return ({ cninfo: "巨潮资讯", eastmoney: "东方财富" })[value] || value || "来源未标注"; }

export function activeEarningsJob(job) { return ["queued", "running"].includes(job?.status); }
export function visibleReports(reports = [], year = "all", type = "all") {
  return reports.filter((report) => (year === "all" || String(report.report_year) === String(year)) && (type === "all" || report.report_type === type))
    .slice().sort((a, b) => String(b.report_period || "").localeCompare(String(a.report_period || ""))
      || Number(b.document_kind === "full_report") - Number(a.document_kind === "full_report")
      || String(b.announcement_date || "").localeCompare(String(a.announcement_date || "")));
}

export function earningsDate(value) {
  if (!value) return "暂无日期";
  if (/^\d{4}-\d{2}-\d{2}$/.test(String(value))) return String(value);
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isFinite(date.getTime()) ? date.toLocaleDateString("zh-CN", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit" }) : "暂无日期";
}

export function physicalPage(value) { const page = Number(value); return Number.isInteger(page) && page > 0 ? page : null; }
export function pdfPageUrl(value, page) {
  const url = safeUrl(value); const number = physicalPage(page);
  if (!url || !number) return null;
  return `${url.split("#")[0]}#page=${number}`;
}

export function extractionSummary(document) {
  if (!document) return "尚未下载解析";
  const total = Number(document.total_pages); const text = Number(document.text_pages);
  const count = Number.isFinite(total) && total > 0 && Number.isFinite(text) && text >= 0 ? ` · ${text}/${total}页含可提取文字` : "";
  return `${DOCUMENT_STATUS[document.status] || "解析状态未标注"}${count}`;
}

export function analysisGate({ report, model, usage, workerEnabled, busy = false }) {
  if (!report) return { allowed: false, reason: "先选择一份报告。" };
  if (report.document_kind !== "full_report") return { allowed: false, reason: "请选择正式报告全文。" };
  if (report.is_interpretable === false || report.language === "en") return { allowed: false, reason: "请选择中文报告全文。" };
  if (!model?.configured) return { allowed: false, reason: "请先配置大模型。" };
  if (workerEnabled === false) return { allowed: false, reason: "财报服务已关闭。" };
  if (busy) return { allowed: false, reason: "报告处理中，请稍候。" };
  if (usage?.remaining != null && Number.isFinite(Number(usage.remaining)) && Number(usage.remaining) <= 0) return { allowed: false, reason: "今日财报解读额度已用完，请明日再试。" };
  return { allowed: true, reason: "使用当前模型解读；未解析的 PDF 会先下载解析。" };
}

export function earningsError(error) {
  const detail = error?.response?.data?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.slice(0, 3).map((item) => item.msg).join("；");
  if (detail?.message) return detail.message;
  return "请求失败，请重试。";
}

export function textList(items) { return (Array.isArray(items) ? items : []).filter((item) => typeof item === "string" && item.trim()); }
export function usageLabel(usage) { return usage && Number.isFinite(Number(usage.used)) && Number.isFinite(Number(usage.limit)) ? `${usage.used} / ${usage.limit}` : "—"; }
