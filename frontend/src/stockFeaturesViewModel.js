import { calendarTime } from "./calendarViewModel.js";

export const FEATURE_CATEGORIES = {
  business: "业务与收入", products: "产品与服务", geography: "地区与海外敞口",
  customers: "客户与需求", supply_chain: "供应链与成本", financial: "财务特征",
  valuation: "估值", ownership: "股权与治理", market: "市场表现", risks: "风险敞口",
  industry: "行业与竞争", company: "公司信息", other: "其他特征",
  product: "产品与产能", customer: "客户与需求", supplier: "供应链", cost: "成本结构",
};

export const featureKey = (userId, symbol) => ["stock-features", userId ?? "guest", String(symbol || "").trim().toUpperCase()];
export const featureTime = (value) => /^\d{4}-\d{2}-\d{2}$/.test(String(value || "")) ? value : calendarTime(value, { includeDate: true });

export function featureText(value, fallback = "未提供") {
  if (value === null || value === undefined || value === "") return fallback;
  if (Array.isArray(value)) return value.map((item) => featureText(item, "")).filter(Boolean).join("、") || fallback;
  if (typeof value === "object") return Object.entries(value).map(([key, item]) => `${key}：${featureText(item)}`).join("；") || fallback;
  return String(value);
}

export function featureValue(feature) {
  const text = featureText(feature?.value, "未知，待补充");
  return feature?.unit && feature?.value != null && !text.endsWith(feature.unit) ? `${text} ${feature.unit}` : text;
}

export function stringList(value) {
  return (Array.isArray(value) ? value : typeof value === "string" ? [value] : [])
    .filter((item) => typeof item === "string" && item.trim()).map((item) => item.trim());
}

export function featureAssessment(feature, sources = []) {
  if (!feature || feature.value === null || feature.value === undefined || feature.value === "" || feature.status === "missing") {
    return { label: "未知，待补充", color: "default", known: false };
  }
  if (!["reported", "observed"].includes(feature.status)) return { label: "待核实信息", color: "orange", known: false };
  const sourceIds = new Set(sources.map((source) => source.source_id));
  const citedIds = stringList(feature.source_ids);
  if (!citedIds.length || citedIds.some((id) => !sourceIds.has(id))) return { label: "引用待补全", color: "orange", known: false };
  return { label: feature.status === "observed" ? "已观测事实" : "来源记录", color: "blue", known: true };
}

export function groupFeatures(features = []) {
  const groups = new Map();
  for (const feature of features) {
    const category = feature.category || "other";
    if (!groups.has(category)) groups.set(category, []);
    groups.get(category).push(feature);
  }
  return [...groups].map(([key, items]) => ({ key, label: FEATURE_CATEGORIES[key] || key, items }));
}

export function factorExposures(factor, features = [], sources = []) {
  const index = new Map(features.map((feature) => [feature.id, feature]));
  return stringList(factor?.exposure_feature_ids || factor?.feature_ids).map((id) => {
    const feature = index.get(id);
    return { id, label: feature?.label || feature?.name || id, value: feature ? featureValue(feature) : "特征未收录", known: featureAssessment(feature, sources).known };
  });
}

export function directionText(direction) {
  if (direction && typeof direction === "object") return featureText(direction.description, "经营影响依赖事件条件，股价方向尚待验证。");
  return { bullish: "条件满足时可能偏正面", bearish: "条件满足时可能偏负面", positive: "条件满足时可能偏正面", negative: "条件满足时可能偏负面", neutral: "未识别明确方向", conditional: "影响取决于事件条件", unknown: "影响方向尚待验证" }[direction] || featureText(direction, "影响方向尚待验证");
}

export function featurePercent(value) {
  if (value === null || value === undefined || value === "" || !Number.isFinite(Number(value))) return "—";
  return `${Number(value).toFixed(2)}%`;
}

export function coverageGaps(coverage = {}, features = []) {
  const missingCategories = Object.entries(coverage.categories || {}).filter(([, item]) => item?.status === "missing")
    .map(([key, item]) => `${item.label || FEATURE_CATEGORIES[key] || key}：尚无可用资料`);
  return [...new Set([...stringList(coverage.missing), ...missingCategories, ...features.flatMap((feature) => stringList(feature.missing))])];
}

export function featureSnapshot(data, symbol) {
  const snapshot = data?.snapshot;
  if (!snapshot) return null;
  // A server/proxy cache must never show another security's facts in the selected stock.
  return String(snapshot.stock_code || "").toUpperCase() === String(symbol || "").trim().toUpperCase() ? snapshot : null;
}

export function featureRefreshNotice(status) {
  return {
    updated: { type: "success", message: "特征库已更新，已保存新版本。" },
    unchanged: { type: "info", message: "已核对本地资料，当前特征未变化。" },
    cooldown: { type: "info", message: "刚刚检查过本地资料，请稍后再更新。" },
    in_progress: { type: "info", message: "特征库正在更新，请稍后查看。" },
    failed: { type: "warning", message: "本次更新未完成，保留已有版本。" },
  }[status] || null;
}
