import { SUB_INDICATORS } from "./klineIndicators.js";

// The persisted schema is intentionally small and versioned; future versions
// can be migrated here without pushing chart presentation choices to the API.
export const DEFAULT_INDICATOR_SETTINGS = Object.freeze({
  version: 1,
  main: Object.freeze({ ma: true, boll: false }),
  subIndicators: Object.freeze(["vol"]),
  params: Object.freeze({
    ma: Object.freeze({ periods: Object.freeze([20, 60, 250]) }),
    boll: Object.freeze({ period: 20, multiplier: 2 }),
    macd: Object.freeze({ fast: 12, slow: 26, signal: 9 }),
    kdj: Object.freeze({ period: 9, k: 3, d: 3 }),
    rsi: Object.freeze({ periods: Object.freeze([6, 12, 24]) }),
  }),
});

const SUB_IDS = new Set(SUB_INDICATORS.map(({ id }) => id));
const object = (value) => value != null && typeof value === "object" && !Array.isArray(value) ? value : {};
const isObject = (value) => value != null && typeof value === "object" && !Array.isArray(value);
const owns = (value, key) => Object.prototype.hasOwnProperty.call(value, key);
const inRange = (value, min, max, integer = true) => typeof value === "number" && Number.isFinite(value)
  && (!integer || Number.isInteger(value)) && value >= min && value <= max;
const bounded = (value, fallback, min, max, integer = true) => inRange(value, min, max, integer) ? value : fallback;

function periodsOrDefault(input, defaults) {
  if (!Array.isArray(input)) return [...defaults];
  const valid = [...new Set(input.filter((period) => inRange(period, 2, 500)))].slice(0, 3);
  return valid.length ? valid : [...defaults];
}

/** Recover damaged/untrusted saved preferences, always returning a fresh tree. */
export function normalizeIndicatorSettings(input) {
  const source = object(input);
  // Unknown schemas must not accidentally reinterpret values from a newer app.
  if (owns(source, "version") && source.version !== 1) return normalizeIndicatorSettings(null);
  const main = object(source.main);
  const params = object(source.params);
  const ma = object(params.ma);
  const bands = object(params.boll);
  const convergence = object(params.macd);
  const stochastic = object(params.kdj);
  const strength = object(params.rsi);
  let fast = bounded(convergence.fast, 12, 2, 250);
  let slow = bounded(convergence.slow, 26, 2, 250);
  if (fast >= slow) { fast = 12; slow = 26; }
  return {
    version: 1,
    main: {
      ma: typeof main.ma === "boolean" ? main.ma : true,
      boll: typeof main.boll === "boolean" ? main.boll : false,
    },
    subIndicators: Array.isArray(source.subIndicators)
      ? [...new Set(source.subIndicators.filter((id) => typeof id === "string" && SUB_IDS.has(id)))].slice(0, 3)
      : ["vol"],
    params: {
      ma: { periods: periodsOrDefault(ma.periods, [20, 60, 250]) },
      boll: { period: bounded(bands.period, 20, 2, 250), multiplier: bounded(bands.multiplier, 2, 0.1, 10, false) },
      macd: { fast, slow, signal: bounded(convergence.signal, 9, 1, 120) },
      kdj: { period: bounded(stochastic.period, 9, 2, 250), k: bounded(stochastic.k, 3, 1, 50), d: bounded(stochastic.d, 3, 1, 50) },
      rsi: { periods: periodsOrDefault(strength.periods, [6, 12, 24]) },
    },
  };
}

/** Validate editor input without silently applying normalizer fallbacks. */
export function validateIndicatorSettings(input) {
  const errors = [];
  if (!isObject(input)) return ["指标设置必须是对象"];
  if (owns(input, "version") && input.version !== 1) errors.push("指标设置版本不受支持");
  if (owns(input, "main") && !isObject(input.main)) errors.push("主图设置格式不正确");
  const main = object(input.main);
  for (const [key, label] of [["ma", "MA"], ["boll", "BOLL"]]) {
    if (owns(main, key) && typeof main[key] !== "boolean") errors.push(`${label} 显示选项必须是开关值`);
  }
  if (owns(input, "subIndicators")) {
    if (!Array.isArray(input.subIndicators)) errors.push("副图选择必须是列表");
    else {
      if (input.subIndicators.length > 3) errors.push("最多同时显示 3 个副图");
      if (input.subIndicators.some((id) => !SUB_IDS.has(id))) errors.push("包含不支持的副图指标");
      if (new Set(input.subIndicators).size !== input.subIndicators.length) errors.push("副图指标不能重复");
    }
  }
  if (owns(input, "params") && !isObject(input.params)) errors.push("指标参数格式不正确");
  const params = object(input.params);
  for (const [key, label] of [["ma", "MA"], ["boll", "BOLL"], ["macd", "MACD"], ["kdj", "KDJ"], ["rsi", "RSI"]]) {
    if (owns(params, key) && !isObject(params[key])) errors.push(`${label} 参数格式不正确`);
  }
  for (const [key, label] of [["ma", "MA"], ["rsi", "RSI"]]) {
    const value = object(params[key]);
    if (owns(value, "periods") && (!Array.isArray(value.periods) || value.periods.length < 1 || value.periods.length > 3
      || value.periods.some((period) => !inRange(period, 2, 500)) || new Set(value.periods).size !== value.periods.length)) {
      errors.push(`${label} 周期需为 1 至 3 个不重复的整数，范围 2–500`);
    }
  }
  const rules = [
    ["boll", "period", "BOLL 周期", 2, 250, true],
    ["boll", "multiplier", "BOLL 标准差倍数", 0.1, 10, false],
    ["macd", "fast", "MACD 快线周期", 2, 250, true],
    ["macd", "slow", "MACD 慢线周期", 2, 250, true],
    ["macd", "signal", "MACD 信号周期", 1, 120, true],
    ["kdj", "period", "KDJ 周期", 2, 250, true],
    ["kdj", "k", "KDJ K 平滑周期", 1, 50, true],
    ["kdj", "d", "KDJ D 平滑周期", 1, 50, true],
  ];
  for (const [group, key, label, min, max, integer] of rules) {
    const value = object(params[group]);
    if (owns(value, key) && !inRange(value[key], min, max, integer)) errors.push(`${label}需为 ${min}–${max} ${integer ? "的整数" : "之间的数值"}`);
  }
  const convergence = object(params.macd);
  const fast = owns(convergence, "fast") ? convergence.fast : 12;
  const slow = owns(convergence, "slow") ? convergence.slow : 26;
  if (inRange(fast, 2, 250) && inRange(slow, 2, 250) && fast >= slow) errors.push("MACD 快线周期必须小于慢线周期");
  return errors;
}

/** Account + chart scope isolation; anonymous sessions do not persist choices. */
export function indicatorSettingsKey(userId, scope = "stock") {
  if (userId == null || String(userId).trim() === "") return null;
  return `stockview:kline-indicators:v1:${encodeURIComponent(String(userId))}:${encodeURIComponent(String(scope || "stock"))}`;
}
