export const CALENDAR_TIMEZONE = "Asia/Shanghai";
export const REGIONS = [
  { value: "US", label: "美国" }, { value: "EU", label: "欧元区" },
  { value: "CN", label: "中国" }, { value: "JP", label: "日本" },
  { value: "GB", label: "英国" }, { value: "CA", label: "加拿大" },
  { value: "AU", label: "澳大利亚" }, { value: "CH", label: "瑞士" },
];
export const CATEGORIES = [
  { value: "inflation", label: "通胀" }, { value: "employment", label: "就业" },
  { value: "growth", label: "经济增长" }, { value: "central_bank", label: "央行政策" },
  { value: "trade", label: "贸易" }, { value: "other", label: "其他" },
];
export const IMPORTANCE_OPTIONS = [
  { value: 3, label: "仅重大事件" }, { value: 2, label: "重要及以上" }, { value: 1, label: "全部重要性" },
];
export const EVENT_STATUSES = {
  scheduled: { label: "待公布", color: "blue" },
  released: { label: "已公布", color: "green" },
  cancelled: { label: "已取消", color: "default" },
};

const dateFormatter = new Intl.DateTimeFormat("en-CA", {
  timeZone: CALENDAR_TIMEZONE, year: "numeric", month: "2-digit", day: "2-digit",
});
const timeFormatter = new Intl.DateTimeFormat("zh-CN", {
  timeZone: CALENDAR_TIMEZONE, hour: "2-digit", minute: "2-digit", hourCycle: "h23",
});

function validDate(value) {
  if (value === null || value === undefined || value === "") return null;
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isFinite(date.getTime()) ? date : null;
}

export function shanghaiDateKey(value = new Date()) {
  const date = validDate(value);
  if (!date) return "";
  const parts = Object.fromEntries(dateFormatter.formatToParts(date).map(({ type, value: part }) => [type, part]));
  return `${parts.year}-${parts.month}-${parts.day}`;
}

export function calendarTime(value, { allDay = false, includeDate = false } = {}) {
  const date = validDate(value);
  if (!date) return "—";
  const time = allDay ? "全天 / 时间待定" : timeFormatter.format(date);
  return includeDate ? `${shanghaiDateKey(date)} ${time}` : time;
}

/** ISO date-only arithmetic is independent of browser locale and daylight saving. */
export function monthCalendarDays(month) {
  if (!/^\d{4}-\d{2}$/.test(month)) return [];
  const first = new Date(`${month}-01T00:00:00.000Z`);
  if (!Number.isFinite(first.getTime()) || first.toISOString().slice(0, 7) !== month) return [];
  const mondayOffset = (first.getUTCDay() + 6) % 7;
  return Array.from({ length: 42 }, (_, index) => {
    const date = new Date(first);
    date.setUTCDate(1 - mondayOffset + index);
    return date.toISOString().slice(0, 10);
  });
}

export function eventDateKey(event) {
  return event.all_day && /^\d{4}-\d{2}-\d{2}$/.test(event.scheduled_date || "")
    ? event.scheduled_date : shanghaiDateKey(event.scheduled_at);
}

export function groupCalendarEvents(items, status = "all") {
  const result = new Map();
  for (const event of Array.isArray(items) ? items : []) {
    if (status !== "all" && event.status !== status) continue;
    const key = eventDateKey(event);
    if (!key) continue;
    if (!result.has(key)) result.set(key, []);
    result.get(key).push(event);
  }
  for (const events of result.values()) events.sort((a, b) => {
    const time = new Date(a.scheduled_at) - new Date(b.scheduled_at);
    return time || Number(b.importance || 0) - Number(a.importance || 0) || String(a.id).localeCompare(String(b.id));
  });
  return result;
}

export function eventValue(value, unit = "") {
  if (value === null || value === undefined || String(value).trim() === "") return "—";
  const text = String(value).trim();
  return unit && !text.endsWith(unit) ? `${text}${unit === "%" ? "" : " "}${unit}` : text;
}

const sourceDateFormatters = new Map();

export function eventNeedsVerification(event, now = new Date()) {
  if (event?.status !== "scheduled") return false;
  const current = validDate(now);
  if (!current) return false;
  if (!event.all_day) {
    const scheduled = validDate(event.scheduled_at);
    return Boolean(scheduled && scheduled < current);
  }
  // A date-only schedule stays pending throughout its entire source-local day.
  if (!/^\d{4}-\d{2}-\d{2}$/.test(event.scheduled_date || "") || !event.timezone) return false;
  try {
    if (!sourceDateFormatters.has(event.timezone)) sourceDateFormatters.set(event.timezone, new Intl.DateTimeFormat("en-CA", {
      timeZone: event.timezone, year: "numeric", month: "2-digit", day: "2-digit",
    }));
    const parts = Object.fromEntries(sourceDateFormatters.get(event.timezone).formatToParts(current).map(({ type, value }) => [type, value]));
    return event.scheduled_date < `${parts.year}-${parts.month}-${parts.day}`;
  } catch {
    return false;
  }
}

export function eventStatusInfo(eventOrStatus, now = new Date()) {
  const status = typeof eventOrStatus === "string" ? eventOrStatus : eventOrStatus?.status;
  if (eventNeedsVerification(eventOrStatus, now)) return { label: "已到期·待核验", color: "orange" };
  return EVENT_STATUSES[status] || { label: "状态未知", color: "default" };
}

export function sourceStatusInfo(source) {
  if (["error", "failed", "degraded", "partial"].includes(source?.status) || source?.error) return { label: "采集异常", status: "error" };
  if (source?.stale === true || source?.status === "stale") return { label: "待更新", status: "warning" };
  if (["running", "refreshing", "collecting"].includes(source?.status)) return { label: "采集中", status: "processing" };
  if (["ok", "success", "healthy"].includes(source?.status)) return { label: "采集正常", status: "success" };
  if (source?.status === "disabled") return { label: "未启用", status: "default" };
  return { label: "等待采集", status: "default" };
}

export const calendarQueryKey = (userId, section, params) => ["events", userId ?? "guest", section, ...(params === undefined ? [] : [params])];

export function calendarConfigPayload(values) {
  const limits = { lookback_days: [0, 365], lookahead_days: [0, 365], min_importance: [1, 3], top_k: [1, 30] };
  const payload = { enabled: Boolean(values.enabled), agents: {}, regions: values.regions || [], categories: values.categories || [] };
  for (const key of ["news", "research", "strategies", "kline"]) payload.agents[key] = Boolean(values.agents?.[key]);
  for (const [key, [min, max]] of Object.entries(limits)) {
    const value = values[key];
    if (value === null || value === undefined || !Number.isInteger(Number(value)) || Number(value) < min || Number(value) > max) {
      throw new Error(`配置 ${key} 必须为 ${min}–${max} 之间的整数。`);
    }
    payload[key] = Number(value);
  }
  if (payload.regions.some((region) => !REGIONS.some((item) => item.value === region))) throw new Error("请选择有效的事件地区。");
  if (payload.categories.some((category) => !CATEGORIES.some((item) => item.value === category))) throw new Error("请选择有效的事件类别。");
  return payload;
}
