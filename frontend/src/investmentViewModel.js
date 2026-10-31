import { normalizeTrackingSymbol } from "./trackingViewModel.js";
import { parseKeywords, parseStockSymbols, stockSymbolTokens } from "./monitoringViewModel.js";

export const investmentKey = (userId, ...parts) => ["investment-research", userId ?? "guest", ...parts];
export const INVESTMENT_ASSESSMENT = { supported: "证据支持", mixed: "证据有分歧", challenged: "原判断受挑战", insufficient_data: "证据不足" };
export const investmentWorking = (job) => ["queued", "running"].includes(job?.status);
export function dossierValues(dossier = {}, initial = {}) {
  const value = { title: "", target_type: "stock", target_code: "", target_symbols: [], keywords: [], objective: "", thesis: "", status: "active", ...initial, ...dossier };
  return { ...value, target_symbols: stockSymbolTokens(value.target_symbols).join("，"), keywords: parseKeywords(value.keywords).join("，") };
}
export function dossierPayload(value) {
  const target_type = value.target_type || "stock";
  const target_code = target_type === "stock" ? normalizeTrackingSymbol(value.target_code) : "";
  if (target_type === "stock" && !target_code) throw new Error("请填写有效 A 股代码。");
  if (!["stock", "stock_list", "topic"].includes(target_type)) throw new Error("请选择研究对象。");
  const keywords = parseKeywords(value.keywords);
  if (target_type === "topic" && !keywords.length) throw new Error("主题研究至少填写一个关键词。");
  if (keywords.length > 10) throw new Error("关键词最多10个。");
  return { title: String(value.title || "").trim(), target_type, target_code, target_symbols: target_type === "stock_list" ? parseStockSymbols(value.target_symbols) : [], keywords, objective: String(value.objective || "").trim(), thesis: String(value.thesis || "").trim(), status: value.status || "active", ...(value.monitor_run_id ? { monitor_run_id: value.monitor_run_id } : {}) };
}
export function dossierSymbols(dossier) { return dossier?.target_type === "stock_list" ? dossier.target_symbols || [] : dossier?.target_code ? [dossier.target_code] : []; }
export function sameResearchTarget(left, right) {
  const signature = (value) => JSON.stringify([value?.target_type, value?.target_code || "", [...(value?.target_symbols || [])].sort(), value?.target_type === "topic" ? [...(value?.keywords || [])].sort() : []]);
  return Boolean(left && right) && signature(left) === signature(right);
}
export function investmentUrl(dossierId, symbol) { const params = new URLSearchParams(); if (dossierId) params.set("dossier", dossierId); if (symbol) params.set("symbol", symbol); return `/research${params.size ? `?${params}` : ""}`; }
export function observationUrl(reportId, index) { return `/monitor?${new URLSearchParams({ research_report: reportId, observation: String(index) })}`; }
export function stockContextUrl(symbol, date, monitorRun) { const params = new URLSearchParams({ chart: symbol }); if (date) params.set("as_of", date); if (monitorRun) params.set("monitor_run", monitorRun); return `/?${params}`; }
export function monitoringDraft(report, index) {
  const result = report?.result || {};
  const item = result.report?.next_observations?.[index];
  if (!item || !Number.isInteger(index) || index < 0) return null;
  const dossier = result.dossier_snapshot || {};
  return { ...item, target_type: ["stock", "stock_list", "topic"].includes(dossier.target_type) ? dossier.target_type : "topic", target_code: dossier.target_code || "", target_symbols: dossier.target_symbols || [], keywords: dossier.keywords || [], research_link: { dossier_id: report.dossier_id || dossier.id, report_id: report.id, observation_index: index } };
}
export function filteredDossiers(items = [], filter = "active") {
  return items.filter((item) => filter === "all" || (filter === "unread" ? item.unread_count > 0 : filter === "working" ? item.active_jobs > 0 : item.status === filter));
}

export function investmentConflict(error) { return error?.response?.status === 409 && /档案已更新|问题已更新|版本冲突/.test(String(error?.response?.data?.detail || "")); }
