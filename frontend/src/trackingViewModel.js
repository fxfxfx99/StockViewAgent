import { calendarTime, shanghaiDateKey } from "./calendarViewModel.js";

export const CHANGE_LABELS = {
  news: "新闻变化", calendar: "日历变化", review_due: "到期复盘", data_issue: "数据异常",
  news_new: "新增新闻", news_correction: "新闻修订", source_supplement: "来源补充",
  calendar_changed: "事件修订", calendar_schedule: "相关日程", overview: "变化概览",
};
export const REVIEW_LABELS = { continue: "继续关注", revise: "修订判断", close: "关闭判断" };
export const ASSESSMENT_LABELS = { supports: "支持原判断", contradicts: "与原判断矛盾", uncertain: "尚不确定" };
export const trackingTime = (value) => calendarTime(value, { includeDate: true });

export function nextReviewDate(now = new Date(), days = 30) {
  const date = new Date(`${shanghaiDateKey(now)}T00:00:00.000Z`);
  date.setUTCDate(date.getUTCDate() + days);
  return date.toISOString().slice(0, 10);
}

export function isFutureReviewDate(value, now = new Date()) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value || "")) return false;
  const date = new Date(`${value}T00:00:00.000Z`);
  return Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === value && value > shanghaiDateKey(now);
}

export function normalizeTrackingSymbol(value) {
  const raw = String(value || "").trim().toUpperCase().replace(/\.SH$/, ".SS");
  if (/^\d{6}\.(SS|SZ|BJ)$/.test(raw)) return raw;
  if (!/^\d{6}$/.test(raw)) return "";
  if (/^[69]/.test(raw)) return `${raw}.SS`;
  if (/^[03]/.test(raw)) return `${raw}.SZ`;
  if (/^[48]/.test(raw)) return `${raw}.BJ`;
  return "";
}

export function sortChanges(items = []) {
  return [...items].sort((a, b) => Number(b.priority || 0) - Number(a.priority || 0)
    || new Date(typeof b.created_at === "number" ? b.created_at * 1000 : b.created_at || 0) - new Date(typeof a.created_at === "number" ? a.created_at * 1000 : a.created_at || 0)
    || String(a.id).localeCompare(String(b.id)));
}

export function partitionChanges(items = [], filter = "unread", limit = 5) {
  const included = items.filter((item) => filter === "all" || (!item.is_read && item.feedback !== "irrelevant" && !item.resolved));
  const sorted = sortChanges(included);
  const supplements = sorted.filter((item) => item.change_type === "source_supplement");
  const primary = sorted.filter((item) => item.change_type !== "source_supplement");
  return { first: primary.slice(0, limit), rest: primary.slice(limit), supplements, total: sorted.length };
}

export function appendEvidence(existing = [], evidence) {
  if (!evidence?.id) return [...existing];
  const key = (item) => `${item.kind}:${item.id}:${item.revision_id || ""}`;
  return existing.some((item) => key(item) === key(evidence)) ? [...existing] : [...existing, evidence];
}

/** Archived source bodies are resolved by the server; only strategy opinions carry their saved snapshot. */
export function evidenceInputRef(evidence) {
  if (evidence.kind === "strategy") return evidence;
  return { kind: evidence.kind, id: String(evidence.id), ...(evidence.revision_id ? { revision_id: evidence.revision_id } : {}) };
}

function compactSignalInputs(value) {
  if (!value || typeof value !== "object") return value;
  const omitted = new Set(["financial", "raw_financial", "mda", "basic_info"]);
  return Object.fromEntries(Object.entries(value).filter(([key]) => !omitted.has(key)).map(([key, item]) => [key,
    item && typeof item === "object" && !Array.isArray(item) ? compactSignalInputs(item) : item]));
}

export function newsEvidence(item) {
  if (item?.id === undefined || item?.id === null) return null;
  return { kind: "news", id: String(item.id), ...(item.revision_id ? { revision_id: item.revision_id } : {}), title: item.title || "新闻证据" };
}

export function calendarEvidence(event) {
  if (!event?.id) return null;
  return { kind: "calendar", id: String(event.id), ...(event.revision_id ? { revision_id: event.revision_id } : {}), title: event.title || "经济事件" };
}

export function strategyEvidence(view, data) {
  return {
    kind: "strategy", id: view.strategy_id || view.strategy,
    title: `${view.strategy} · ${view.timeframe || "周期未指定"}`,
    observed_at: new Date().toISOString(), excerpt: view.viewpoint || "",
    snapshot: { symbol: data?.symbol, as_of: data?.as_of || null, source: { mode: data?.source?.mode, kline_source: data?.source?.kline_source, data_as_of: data?.source?.data_as_of },
      strategy_id: view.strategy_id, strategy: view.strategy, timeframe: view.timeframe, direction: view.direction,
      score: view.score, confidence: view.confidence, viewpoint: view.viewpoint,
      reasons: view.reasons || [], risks: view.risks || [], do_not_do: view.do_not_do || [],
      model_version: view.metadata?.model_version, signal_inputs: compactSignalInputs(view.metadata?.signal_inputs) },
  };
}

export function trackingError(error) {
  if (error?.response?.status === 409) return "这条判断已在别处更新。你的输入已保留，请载入最新记录，核对后重新提交。";
  const detail = error?.response?.data?.detail;
  if (Array.isArray(detail)) return detail.slice(0, 3).map((item) => item.msg).join("；");
  return typeof detail === "string" ? detail : error?.response ? "保存未完成，请重试。" : "暂时无法连接服务，你的输入已保留。";
}

export function trackingStatusNotices(status) {
  if (!status) return [];
  const notices = [];
  if (status.automatic_enabled === false) notices.push({ key: "paused", type: "warning", title: "自动跟踪已停止", text: "仍可保存与复盘判断，点击“检查本地变化”可手动更新。" });
  else if (status.worker?.healthy === false) notices.push({ key: "worker", type: "warning", title: "自动跟踪执行器未连接", text: "后台检查暂时无法继续，可手动检查已归档的证据。" });
  for (const [key, label] of [["news", "新闻"], ["calendar", "日历"]]) {
    const source = status[key];
    if (source && ["error", "failed", "partial", "stale", "unavailable", "limited"].includes(source.status)) {
      notices.push({ key, type: "warning", title: `${label}数据覆盖需核查`, text: source.message || "请核对最新采集时间；当前结果基于已归档的数据。" });
    }
    if (source?.pending) notices.push({ key: `${key}-pending`, type: "info", title: `${label}仍有修订待处理`, text: "当前检查尚未覆盖全部归档证据，可继续检查本地变化。" });
  }
  if (status.coverage?.status === "limited" && !notices.some((item) => ["news", "calendar"].includes(item.key))) {
    notices.push({ key: "coverage", type: "info", title: "数据覆盖有限", text: "本页按本地已归档证据检查变化，暂无变化不代表外部信息没有更新。" });
  }
  return notices;
}
