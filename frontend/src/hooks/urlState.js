export function normalizeChartSymbol(value) {
  const symbol = String(value || "").trim().toUpperCase();
  return /^\d{6}\.(SS|SZ|BJ)$/.test(symbol) ? symbol : null;
}

/** 校验真实日历日期，避免 2026-02-31 被日期控件自动滚动到三月。 */
export function normalizeAsOf(value) {
  const day = String(value || "").trim();
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return null;
  const date = new Date(`${day}T00:00:00.000Z`);
  return Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === day ? day : null;
}
