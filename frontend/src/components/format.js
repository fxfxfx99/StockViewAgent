export const sentimentLabels = {
  positive: "正面",
  neutral: "中性",
  negative: "负面",
};
export const sentimentColors = {
  positive: "green",
  neutral: "default",
  negative: "red",
};
export const directionLabels = {
  bullish: "潜在正向",
  neutral: "潜在中性",
  bearish: "潜在负向",
  偏多: "潜在正向",
  偏空: "潜在负向",
  中性: "潜在中性",
  不确定: "方向待评估",
};
export function formatTime(value) {
  if (!value) return "时间未知";
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isNaN(date.getTime())
    ? "时间未知"
    : date.toLocaleString("zh-CN", {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      });
}
export function number(value, digits = 2) {
  return value == null || !Number.isFinite(Number(value))
    ? "—"
    : Number(value).toFixed(digits);
}
export function safeUrl(value) {
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) ? url.href : null;
  } catch {
    return null;
  }
}
export function plainText(value) {
  return String(value || "")
    .replace(/<[^>]+>/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}
export function isAnalyzed(analysis) {
  return Boolean(
    analysis &&
      (!analysis.status || analysis.status === "analyzed") &&
      (analysis.sentiment || analysis.impact_direction || analysis.summary),
  );
}
