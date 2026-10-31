export const OVERSEAS_PAGE_SIZE = 50;
export const OVERSEAS_POLL_MS = 15_000;
export const OVERSEAS_SOURCES = [
  { id: "reuters", label: "Reuters（路透）" },
  { id: "bloomberg", label: "Bloomberg（彭博）" },
  { id: "benzinga", label: "Benzinga" },
  { id: "wsj", label: "WSJ（华尔街日报）" },
  { id: "ft", label: "FT（金融时报）" },
  { id: "cnbc", label: "CNBC" },
];

const DATE_FORMAT = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
});

export function overseasTime(value) {
  if (value == null || value === "") return "暂无时间";
  const parsed = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isNaN(parsed.getTime()) ? "暂无时间" : DATE_FORMAT.format(parsed);
}

export function overseasSourceStatus(status) {
  return ({
    ok: { label: "读取成功", tone: "success" },
    pending: { label: "等待采集", tone: "default" },
    error: { label: "采集异常", tone: "error" },
    rate_limited: { label: "限流等待", tone: "warning" },
    access_denied: { label: "访问受限", tone: "warning" },
    stale: { label: "内容较旧", tone: "warning" },
    disabled: { label: "未启用", tone: "default" },
  })[status] || { label: "状态待确认", tone: "default" };
}

export function overseasTransport(transport) {
  return ({
    google_news_rss: "Google News 索引",
    benzinga_api: "Benzinga 授权 API",
    rss: "公开 RSS 订阅",
  })[transport] || "采集方式待确认";
}

export function overseasEmptyState({ filtered, enabled, hasError }) {
  if (hasError) return { title: "快讯读取失败", description: "请检查后端连接后重试。" };
  if (filtered) return { title: "没有符合条件的消息", description: "更换来源、关键词或清除筛选。" };
  if (enabled === false) return { title: "暂无归档消息", description: "在后台采集设置中启用海外快讯。" };
  return { title: "暂无归档消息", description: "等待首次采集，可在来源状态中查看进度。" };
}
