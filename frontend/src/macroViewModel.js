export const MACRO_RANGES = [{ value: "3mo", label: "3个月" }, { value: "6mo", label: "6个月" }, { value: "1y", label: "1年" }, { value: "2y", label: "2年" }, { value: "5y", label: "5年" }];
export const MACRO_VIEWS = [{ value: "1d", label: "日K" }, { value: "1wk", label: "周K" }, { value: "1mo", label: "月K" }];
export const MACRO_TABS = [{ key: "indices", label: "全球市场" }, { key: "macro", label: "宏观与行业" }, { key: "capital", label: "资金概览" }, { key: "analysis", label: "K线解读" }];
const MARKET_GROUPS = [{ key: "cn", label: "A股指数" }, { key: "us", label: "美股市场与指数" }, { key: "futures", label: "商品期货" }, { key: "other", label: "其他市场" }];
export const macroKey = (userId, ...parts) => ["macro-workspace", userId ?? "guest", ...parts];
export const validOption = (value, values, fallback) => values.includes(value) ? value : fallback;
export function marketGroup(instrument) {
  if (instrument?.asset_class === "futures") return "futures";
  if (instrument?.region === "US") return "us";
  // Old catalog entries did not include region; they were all A-share indices.
  if (instrument && (!instrument.region || instrument.region === "CN")) return "cn";
  return "other";
}
export function marketGroups(items = []) {
  return MARKET_GROUPS.map((group) => ({ ...group, items: items.filter((item) => marketGroup(item) === group.key) })).filter((group) => group.items.length);
}
export function selectMarketInstrument(items = [], requestedKey) {
  return items.find((item) => item.key === requestedKey) || items.find((item) => item.key === "SH_COMP") || items[0];
}
export function collectionStatus(status) {
  return ({ success: { label: "采集成功", color: "green" }, failed: { label: "采集失败", color: "red" }, stale: { label: "缓存待更新", color: "orange" }, pending: { label: "等待采集", color: "default" }, running: { label: "正在采集", color: "blue" } })[status] || { label: "状态未知", color: "default" };
}
export function finiteValue(value) {
  return value == null || value === "" || typeof value === "boolean" || !Number.isFinite(Number(value)) ? null : Number(value);
}
export function macroNumber(value, digits = 2) {
  const number = finiteValue(value);
  return number === null ? "—" : number.toLocaleString("zh-CN", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}
export function macroMoney(value) {
  const number = finiteValue(value);
  if (number === null) return "—";
  if (Math.abs(number) >= 1e12) return `${macroNumber(number / 1e12)} 万亿`;
  if (Math.abs(number) >= 1e8) return `${macroNumber(number / 1e8)} 亿`;
  if (Math.abs(number) >= 1e4) return `${macroNumber(number / 1e4)} 万`;
  return macroNumber(number);
}
export function macroPercent(value) {
  const number = finiteValue(value);
  return number === null ? "—" : `${number > 0 ? "+" : ""}${number.toFixed(2)}%`;
}
export function candleSummary(candles = []) {
  const valid = candles.filter((row) => finiteValue(row.t) !== null && finiteValue(row.c) !== null).slice().sort((a, b) => a.t - b.t);
  const first = valid[0]; const last = valid.at(-1); const previous = valid.at(-2);
  // Zero/negative futures bases do not have a meaningful conventional percentage change.
  const change = previous && Number(previous.c) > 0 ? (Number(last.c) / Number(previous.c) - 1) * 100 : null;
  const totalChange = first && Number(first.c) > 0 ? (Number(last.c) / Number(first.c) - 1) * 100 : null;
  return { close: last?.c ?? null, change, totalChange, count: valid.length, timestamp: last?.t ?? null };
}
export function macroDate(value, timeZone = "Asia/Shanghai") {
  if (value == null || value === "") return "暂无日期";
  // A provider's trading date is already a calendar date, not a UTC instant.
  if (typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value)) return value.replaceAll("-", "/");
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  if (Number.isNaN(date.getTime())) return String(value);
  try { return date.toLocaleDateString("zh-CN", { timeZone, year: "numeric", month: "2-digit", day: "2-digit" }); }
  catch { return date.toLocaleDateString("zh-CN", { timeZone: "UTC", year: "numeric", month: "2-digit", day: "2-digit" }); }
}
export function macroDateTime(value) {
  if (value == null || value === "") return "尚未记录";
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
}
export function marketAnalysisEvidence(result) {
  const instrument = result?.instrument;
  if (!instrument && !result?.data_as_of && !result?.kline_source && result?.cache_stale !== true) return null;
  return {
    asOf: result.data_as_of ? macroDate(result.data_as_of, instrument?.timezone || "Asia/Shanghai") : "未提供",
    stale: result.cache_stale === true,
    source: result.kline_source || "来源未标注",
    details: [instrument?.exchange ? `交易市场 ${instrument.exchange}` : "", instrument?.unit ? `报价单位 ${instrument.unit}` : "", instrument?.timezone ? `日期时区 ${instrument.timezone}` : ""].filter(Boolean),
    note: instrument?.asset_class === "futures" ? instrument.description || "近月连续合约会换月，区间价格变化不等同于可交易合约收益。" : "",
  };
}
export function validateLearningUpload(file) {
  if (!/\.(txt|md|markdown|pdf)$/i.test(file?.name || "")) return "请选择 TXT、Markdown 或 PDF 文件。";
  if (!Number.isFinite(file?.size) || file.size <= 0 || file.size > 5 * 1024 * 1024) return "资料大小须大于 0 且不超过 5 MB。";
  return "";
}
