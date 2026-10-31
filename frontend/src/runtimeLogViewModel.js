export const RUNTIME_PERIODS = [
  { label: "今日", value: "today" },
  { label: "近 7 天", value: "7d" },
  { label: "累计", value: "all" },
];

export const runtimeQueryKey = (userId, period) => ["runtime", userId, period];
export const runtimeUnavailable = (error) => [403, 404].includes(error?.response?.status);
export const runtimePollInterval = (query) => runtimeUnavailable(query?.state?.error) ? false : 30_000;

export function runtimeCount(value) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}

export function formatRuntimeCount(value, compact = true) {
  const count = runtimeCount(value);
  if (count === null) return "—";
  if (compact && count >= 100_000_000) return `${Number((count / 100_000_000).toFixed(1))} 亿`;
  if (compact && count >= 10_000) return `${Number((count / 10_000).toFixed(1))} 万`;
  return count.toLocaleString("zh-CN", { maximumFractionDigits: 0 });
}

export function runtimeTime(value, { dateOnly = false, full = false } = {}) {
  if (value === null || value === undefined || value === "") return "—";
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString("zh-CN", {
    timeZone: "Asia/Shanghai", ...(full ? { year: "numeric" } : {}), month: "2-digit", day: "2-digit",
    ...(dateOnly ? {} : { hour: "2-digit", minute: "2-digit", hour12: false }),
  });
}

const OPERATION_LABELS = {
  chat: "模型分析", research_proposal: "策略构思", embedding: "文本向量化",
  kline: "行情采集", news: "新闻采集", company_fundamentals: "财务采集", github: "GitHub 学习",
  market_history: "行情采集", news_archive: "新闻采集", tech_news: "科技新闻采集", company_profile: "公司资料采集", github_source: "GitHub 学习",
};
const STATUS_LABELS = {
  success: ["已完成", "success"], failed: ["失败", "error"], cancelled: ["已取消", "default"],
  pending: ["待完成", "processing"], started: ["待完成", "processing"], running: ["待完成", "processing"],
  unfinished: ["结果待确认", "warning"],
};

function failureLabel(code) {
  if (!code) return null;
  if (/^http_\d{3}$/.test(code)) return `服务返回 ${code.slice(5)}`;
  return {
    timeout: "请求超时", network_error: "网络暂不可用", invalid_response: "返回内容无法解析",
    empty_response: "未收到有效内容", cancelled: "请求已取消",
  }[code] || "请求未完成";
}

export function runtimeRecordView(record, index = 0) {
  const [statusLabel, tone] = STATUS_LABELS[record?.status] || ["待确认", "default"];
  const llm = record?.kind === "llm";
  const label = OPERATION_LABELS[record?.operation] || (llm ? "模型调用" : "数据采集");
  const detail = [];
  if (record?.source) detail.push(record.source);
  if (record?.shared === true) detail.push("共享公共采集");
  if (llm) detail.push(runtimeCount(record?.total_tokens) === null ? "Token 未报告" : `${formatRuntimeCount(record.total_tokens, false)} Token`);
  else {
    if (runtimeCount(record?.records_received) !== null) detail.push(`获取 ${formatRuntimeCount(record.records_received, false)} 条`);
    if (runtimeCount(record?.records_added) !== null) detail.push(`新增 / 修订 ${formatRuntimeCount(record.records_added, false)} 条`);
  }
  if (record?.status === "failed") detail.push(failureLabel(record.error_code) || "请稍后重试");
  if (record?.status === "unfinished") detail.push("超过 15 分钟未返回结果");
  return {
    id: record?.id || `runtime-${index}`, time: record?.started_at,
    title: `${label}${llm && record?.model ? ` · ${record.model}` : ""}`,
    detail: detail.join(" · "), statusLabel, tone,
  };
}

export function runtimeOverviewView(data) {
  const llm = data?.llm;
  const collection = data?.collection;
  const calls = runtimeCount(llm?.calls);
  const unknownTokenCalls = runtimeCount(llm?.unknown_token_calls);
  const tokens = runtimeCount(llm?.total_tokens);
  const records = (Array.isArray(data?.recent) ? data.recent : []).map(runtimeRecordView);
  return {
    calls, tokens, unknownTokenCalls, collected: runtimeCount(collection?.records_received), records,
    latest: records.length ? `${runtimeTime(records[0].time)} · ${records[0].title} · ${records[0].statusLabel}` : null,
    facts: data ? [
      ["调用成功", llm?.success], ["调用失败", llm?.failed], ["调用待完成", llm?.pending],
      ["调用取消", llm?.cancelled],
      ["采集批次", collection?.requests], ["新增 / 修订", collection?.records_added],
      ["采集失败", collection?.failed], ["采集待完成", collection?.pending],
      ["共享采集记录", collection?.shared_records_received],
    ].map(([label, value]) => ({ label, value: formatRuntimeCount(value, false) })) : [],
    notice: `${data?.tracking_started_at ? `自 ${runtimeTime(data.tracking_started_at, { dateOnly: true, full: true })} 开始记录` : "自日志启用后统计"}。统计行情、新闻、公司资料和 GitHub；缓存读取、来源维护探测、股票搜索等未计入。`,
  };
}
