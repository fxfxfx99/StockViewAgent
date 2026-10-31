import { normalizeAsOf } from "./hooks/urlState.js";

export function candleFocus(candles, day) {
  if (!normalizeAsOf(day) || !candles?.length) return null;
  let found = null;
  candles.forEach((candle, index) => {
    if (!Number.isFinite(Number(candle.t))) return;
    const date = new Date(Number(candle.t) * 1000).toLocaleDateString("en-CA", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit" });
    if (date <= day && (!found || date > found.date)) found = { index, date, exact: date === day };
  });
  return found;
}
export function focusCandleOption(option, candles, day) {
  const focus = candleFocus(candles, day);
  if (!focus) return option;
  const denominator = Math.max(candles.length - 1, 1);
  const start = Math.max(0, Math.min(focus.index - 20, candles.length - 50));
  const end = Math.min(candles.length - 1, start + 49);
  return { ...option, dataZoom: (option.dataZoom || []).map((zoom) => ({ ...zoom, start: start / denominator * 100, end: end / denominator * 100 })), series: option.series.map((series) => series.type === "candlestick" ? { ...series, markLine: { silent: true, symbol: "none", label: { formatter: `定位 ${focus.date}`, position: "insideEndTop" }, lineStyle: { type: "dashed", color: "#d68b25", width: 2 }, data: [{ xAxis: focus.index }] } } : series) };
}
export function financialSeries(items = []) {
  const groups = new Map();
  for (const item of items) {
    const date = String(item.report_date || "").slice(0, 10);
    if (!normalizeAsOf(date)) continue;
    for (const [field, value] of Object.entries(item.values || {})) {
      if (typeof value !== "number" || !Number.isFinite(value)) continue;
      const unit = item.units?.[field] || null;
      const key = JSON.stringify([field, item.report_type || "", item.source_type || "", date.slice(5), unit]);
      if (!groups.has(key)) groups.set(key, { key, field, reportType: item.report_type || "未标注口径", sourceType: item.source_type || "未标注来源类别", period: date.slice(5), unit, points: [] });
      const group = groups.get(key);
      const point = { date, value, source: item.source_name, observed_at: item.observed_at, row_hash: item.row_hash };
      const index = group.points.findIndex((entry) => entry.date === date);
      if (index < 0) group.points.push(point);
      else if (String(point.observed_at || "") > String(group.points[index].observed_at || "")) group.points[index] = point;
    }
  }
  return [...groups.values()].map((group) => ({ ...group, points: group.points.sort((a, b) => a.date.localeCompare(b.date)) })).sort((a, b) => b.points.length - a.points.length || a.field.localeCompare(b.field));
}
