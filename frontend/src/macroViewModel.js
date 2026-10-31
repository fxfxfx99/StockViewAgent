export const MACRO_RANGES = [{ value: "3mo", label: "3个月" }, { value: "6mo", label: "6个月" }, { value: "1y", label: "1年" }, { value: "2y", label: "2年" }, { value: "5y", label: "5年" }];
export const MACRO_VIEWS = [{ value: "1d", label: "日K" }, { value: "1wk", label: "周K" }, { value: "1mo", label: "月K" }];
export const MACRO_TABS = [{ key: "indices", label: "大盘指数" }, { key: "macro", label: "宏观与行业" }, { key: "capital", label: "资金概览" }, { key: "analysis", label: "K线解读" }];
export const macroKey = (userId, ...parts) => ["macro-workspace", userId ?? "guest", ...parts];
export const validOption = (value, values, fallback) => values.includes(value) ? value : fallback;
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
  const change = previous && Number(previous.c) !== 0 ? (Number(last.c) / Number(previous.c) - 1) * 100 : null;
  const totalChange = first && Number(first.c) !== 0 ? (Number(last.c) / Number(first.c) - 1) * 100 : null;
  return { close: last?.c ?? null, change, totalChange, count: valid.length, timestamp: last?.t ?? null };
}
export function macroDate(value) {
  if (value == null || value === "") return "暂无日期";
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleDateString("zh-CN", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit" });
}
export function validateLearningUpload(file) {
  if (!/\.(txt|md|markdown|pdf)$/i.test(file?.name || "")) return "请选择 TXT、Markdown 或 PDF 文件。";
  if (!Number.isFinite(file?.size) || file.size <= 0 || file.size > 5 * 1024 * 1024) return "资料大小须大于 0 且不超过 5 MB。";
  return "";
}
