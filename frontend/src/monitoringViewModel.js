import { normalizeTrackingSymbol, nextReviewDate } from "./trackingViewModel.js";

export const monitoringKey = (userId, ...parts) => ["monitoring", userId ?? "guest", ...parts];
export const TARGET_OPTIONS = [{ value: "stock", label: "个股" }, { value: "stock_list", label: "股票列表" }, { value: "index", label: "大盘指数" }, { value: "topic", label: "主题 / 事件" }];
export const INDEX_OPTIONS = [{ value: "SH_COMP", label: "上证指数" }, { value: "SZ_COMP", label: "深证成指" }, { value: "CYB", label: "创业板指" }, { value: "HS300", label: "沪深300" }, { value: "SSE50", label: "上证50" }];
export const FREQUENCY_OPTIONS = [{ value: 60, label: "每小时" }, { value: 360, label: "每6小时" }, { value: 1440, label: "每天" }];
export const PLAN_STATUS = { active: "已启用", paused: "已暂停", closed: "已关闭" };
export const RUN_STATUS = { queued: "排队中", running: "正在生成", succeeded: "已完成", failed: "生成失败", cancelled: "已取消", skipped: "无新变化" };
export const ASSESSMENT = { watch: "继续观察", attention: "需要关注", invalidated: "触及失效条件", insufficient_data: "证据不足" };
export const CONDITION_STATUS = { met: "已满足", not_met: "未满足", uncertain: "待确认" };
export const MONITOR_CHANNELS = { news: "新闻", price: "行情", indicators: "技术指标", strategy: "策略观点", announcements: "公司公告", forum: "论坛讨论" };
export const HANDLING_STATUS = { reviewed: "已复核", ignored: "已忽略", research: "已转研究" };

export function modelGate(model, unavailable = false) {
  if (unavailable) return { ready: false, label: "状态暂不可用", tone: "warning", text: "模型状态读取失败，请重试。" };
  if (!model) return { ready: false, label: "正在读取", tone: "default", text: "读取模型配置…" };
  if (!model.configured) return { ready: false, label: "待配置模型", tone: "warning", text: "配置并验证模型后可创建计划。" };
  if (!model.verified) return { ready: false, label: model.error ? "连接需检查" : "待验证连接", tone: "warning", text: model.error || "请验证已保存的模型连接。" };
  return { ready: true, label: "模型已验证", tone: "success", text: "可创建和启用观察计划。" };
}

export function parseKeywords(value) {
  const items = Array.isArray(value) ? value : String(value || "").split(/[,，;；\n]/);
  return [...new Set(items.map((item) => String(item).trim()).filter(Boolean))];
}

export function stockSymbolTokens(value) {
  return (Array.isArray(value) ? value : String(value || "").split(/[,，;；\s]+/)).map((item) => String(item).trim()).filter(Boolean);
}

export function parseStockSymbols(value) {
  const items = stockSymbolTokens(value);
  if (!items.length) throw new Error("请填写至少1只股票。");
  if (items.length > 20) throw new Error("股票列表最多 20 只。");
  const symbols = items.map((item) => {
    const symbol = normalizeTrackingSymbol(item);
    if (!symbol) throw new Error(`股票代码「${item}」无效，请使用 A 股代码。`);
    return symbol;
  });
  return [...new Set(symbols)];
}

export function monitoringInitial(params) {
  return { target_type: params.get("target") === "index" ? "index" : "stock", target_code: params.get("symbol") || params.get("code") || "" };
}

export function planValues(plan, initial = {}) {
  return { title: "", target_type: "stock", target_code: "", target_symbols: "", keywords: "", channels: ["news", "price"], goal: "", watch_conditions: "", invalidation_conditions: "", interval_minutes: 360, review_at: nextReviewDate(), status: "active", ...initial, ...(plan || {}), target_symbols: Array.isArray(plan?.target_symbols) ? plan.target_symbols.join("，") : (Array.isArray(initial.target_symbols) ? initial.target_symbols.join("，") : initial.target_symbols || ""), keywords: Array.isArray(plan?.keywords) ? plan.keywords.join("，") : (Array.isArray(initial.keywords) ? initial.keywords.join("，") : initial.keywords || "") };
}

export function planPayload(values) {
  const target_type = values.target_type;
  const keywords = parseKeywords(values.keywords);
  if (keywords.length > 10) throw new Error("关键词最多填写10个。");
  if (keywords.some((item) => item.length > 100)) throw new Error("每个关键词最多100个字符。");
  if (target_type === "topic" && !keywords.length) throw new Error("主题观察至少填写一个关键词。");
  const target_code = target_type === "stock" ? normalizeTrackingSymbol(values.target_code) : target_type === "index" ? values.target_code : "";
  const target_symbols = target_type === "stock_list" ? parseStockSymbols(values.target_symbols) : [];
  if (target_type === "stock" && !target_code) throw new Error("请输入有效 A 股代码，例如600519。");
  if (target_type === "index" && !INDEX_OPTIONS.some((item) => item.value === target_code)) throw new Error("请选择大盘指数。");
  if (!TARGET_OPTIONS.some((item) => item.value === target_type)) throw new Error("请选择观察对象。");
  if (!FREQUENCY_OPTIONS.some((item) => item.value === Number(values.interval_minutes))) throw new Error("请选择检查频率。");
  const channels = Array.isArray(values.channels) ? [...new Set(values.channels)] : ["news", "price"];
  if (!channels.length || channels.some((channel) => !MONITOR_CHANNELS[channel])) throw new Error("请选择至少一种有效监控来源。");
  return { title: String(values.title || "").trim(), target_type, target_code, target_symbols, keywords, channels, goal: String(values.goal || "").trim(), watch_conditions: String(values.watch_conditions || "").trim(), invalidation_conditions: String(values.invalidation_conditions || "").trim(), interval_minutes: Number(values.interval_minutes), review_at: values.review_at, status: values.status || "active" };
}

export function targetLabel(plan) {
  if (plan?.target_type === "stock_list") return `股票列表 · ${plan.target_symbols?.length || 0}只`;
  if (plan?.target_type === "index") return INDEX_OPTIONS.find((item) => item.value === plan.target_code)?.label || plan.target_code;
  return plan?.target_type === "topic" ? "主题 / 事件" : plan?.target_code || "个股";
}

export function revisionConflict(error) {
  const detail = error?.response?.data?.detail;
  return error?.response?.status === 409 && (!detail || typeof detail === "string" && /计划已更新|计划已有新版本|版本冲突/.test(detail));
}

export function monitoringError(error) {
  if (revisionConflict(error)) return "计划已更新，输入已保留。请载入最新版本。";
  const detail = error?.response?.data?.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && !Array.isArray(detail)) return detail.message || detail.error || "操作未完成，请检查模型与计划状态。";
  if (Array.isArray(detail)) return detail.slice(0, 3).map((item) => item.msg).join("；");
  return error?.message && !error?.isAxiosError ? error.message : "操作未完成，请重试。";
}

export function runInProgress(run) { return ["queued", "running"].includes(run?.status); }
export function runTime(run) { return run?.created_at ?? run?.queued_at; }
export function coverageLines(value) {
  const names = { stock_list: "股票列表", kline: "本地行情", news: "个股新闻", overseas_news: "海外新闻", public_datasets: "公共数据归档", economic_calendar: "经济日历", input_limit: "输入范围" };
  const states = { available: "可用", stale: "仅历史记录", missing: "暂无匹配", unavailable: "暂不可读", disabled: "已关闭", truncated: "部分截取" };
  return (Array.isArray(value) ? value : []).map((item) => typeof item === "string" ? item : `${item.symbol ? `${item.symbol} · ` : ""}${names[item.source] || item.source || "来源"}：${states[item.status] || item.status || "未标注"}${item.count != null ? `，${item.count}条记录 / ${item.fresh_count ?? "—"}条在有效期内` : ""}${item.included_count != null ? `；实际纳入${item.included_count}条 / ${item.included_fresh_count ?? "—"}条在有效期内` : ""}${item.truncated ? "；已按输入上限截取" : ""}${item.sources ? `；${Object.entries(item.sources).map(([source, status]) => `${names[source] || source}${states[status] || status}`).join("、")}` : ""}${item.note ? `。${item.note}` : ""}`);
}
export function reportLines(value) {
  if (value == null) return [];
  return (Array.isArray(value) ? value : [value]).map((item) => typeof item === "string" ? item : item?.text || item?.summary || item?.message || item?.description || JSON.stringify(item)).filter(Boolean);
}

export function budgetState(usage, config) {
  const used = Math.max(0, Number(usage?.calls_used ?? usage?.used) || 0);
  const limit = Number(usage?.daily_call_limit ?? usage?.limit ?? config?.daily_call_limit);
  return { used, limit: Number.isFinite(limit) && limit > 0 ? limit : null, exhausted: Number.isFinite(limit) && limit > 0 && used >= limit };
}
