import { formatDataFiles, formatDataSize, RETENTION_LABELS } from "./localDataViewModel.js";

export { formatDataFiles, formatDataSize };
export const dataCenterQueryKey = (userId, section, params) => ["data-center", userId ?? "guest", section, ...(params === undefined ? [] : [params])];
export const DATA_RETENTION_OPTIONS = [
  { value: "rebuildable", label: "可再生成" },
  { value: "durable", label: "长期保护" },
  { value: "runtime", label: "运行状态" },
  { value: "backup", label: "备份" },
  { value: "recoverable", label: "可恢复" },
];
export const DATA_LIFECYCLE_OPTIONS = [
  { value: "rebuildable", label: "可再生成缓存" },
  { value: "protected", label: "长期保护数据" },
  { value: "expired", label: "已到期缓存" },
];

export function dataTime(value) {
  if (value === null || value === undefined || value === "" || typeof value === "boolean") return "—";
  const date = new Date(typeof value === "number" && value < 1e12 ? value * 1000 : value);
  if (!Number.isFinite(date.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(date);
}

export function retentionInfo(value) {
  return DATA_RETENTION_OPTIONS.find((entry) => entry.value === value)?.label || RETENTION_LABELS[value] || "未分类";
}

export function fileRoleLabel(item) {
  const domains = { market: "行情", market_history: "行情历史", news: "新闻", company: "公司资料", research: "量化研究", knowledge: "知识库", accounts: "账户设置", uploads: "上传资料", economic_events: "经济事件", macro: "宏观数据", public_data: "公开资料", other: "本地数据", central_bank: "央行政策", fx: "汇率", commodities: "大宗商品", rates: "利率", financial: "金融数据" };
  return domains[item.data_domain] || "公开资料";
}

export function collectionStatus(item) {
  const status = item?.status || "never";
  if (["running", "queued", "pending"].includes(status)) return { color: "processing", label: status === "running" ? "采集中" : "排队中" };
  if (["failed", "error"].includes(status)) return { color: "error", label: "采集失败" };
  if (status === "partial") return { color: "warning", label: "部分完成" };
  if (["disabled", "paused"].includes(status)) return { color: "default", label: "已暂停" };
  if (status === "skipped") return { color: "default", label: "本次跳过" };
  if (["success", "succeeded", "completed", "ok"].includes(status)) {
    return item?.stale ? { color: "warning", label: "待更新" } : { color: "success", label: "已完成" };
  }
  return { color: "default", label: "尚未采集" };
}

export function collectionSubmissionNotice(result) {
  const jobs = Array.isArray(result?.jobs) ? result.jobs : [];
  const queued = jobs.filter((job) => job.status === "queued" && !job.deduplicated).length;
  const active = jobs.filter((job) => job.deduplicated || job.status === "running").length;
  const cooldown = jobs.filter((job) => job.status === "cooldown").length;
  return [queued ? `已提交 ${queued} 个采集任务` : "本次没有新增采集任务", active ? `${active} 个来源已有任务` : "", cooldown ? `${cooldown} 个来源正在冷却` : ""].filter(Boolean).join("，");
}

export function collectionServiceNotice(worker) {
  if (worker?.enabled === false) return "手动任务会进入队列，启用采集服务后才会执行。";
  if (worker?.enabled === true && worker.automatic_enabled === false) return "自动采集已关闭，仅处理手动提交的采集任务。";
  if (worker?.enabled === true && worker.automatic_enabled == null && worker.scheduler_enabled === false) return "服务器定时调度已关闭，仅处理手动提交的采集任务。";
  return null;
}

export const COLLECTION_REASONS = {
  memory_pressure: "API 内存达到软阈值，暂缓新任务，运行中任务继续。",
  high_memory: "API 内存达到软阈值，暂缓新任务。",
  low_disk_space: "磁盘空间不足，暂缓新任务。",
  low_disk: "磁盘空间不足，暂缓新任务。",
  daily_budget_exhausted: "今日公共采集额度已用完，明日恢复。",
  disabled: "公共自动采集已关闭。",
  service_disabled: "请在服务器启用采集服务。",
  automatic_disabled: "自动采集已暂停。",
  environment_disabled: "请在服务器启用采集服务。",
  timeout: "任务超时，已采集数据保留。",
};
export function collectionRuntime(config, worker, failed = false) {
  if (failed) return { label: "状态暂不可用", tone: "warning", description: "状态读取失败，显示上次记录。" };
  if (!worker) return { label: "正在读取后台状态", tone: "default", description: "" };
  const reason = worker.resources?.pressure ? "high_memory" : worker.reason;
  if (worker.enabled === false) return { label: "服务环境已关闭", tone: "default", description: "" };
  if (reason && COLLECTION_REASONS[reason] && !["disabled"].includes(reason)) return { label: "采集暂缓", tone: "warning", description: COLLECTION_REASONS[reason] };
  if (config?.enabled === false || worker.automatic_enabled === false) return { label: "公共自动采集已暂停", tone: "default", description: "手动任务按资源与每日额度执行。" };
  if (!worker.running) return { label: "等待采集服务", tone: "warning", description: "未检测到运行中的采集服务。" };
  return { label: worker.active_jobs?.length ? "正在后台采集" : "等待下一计划", tone: "processing", description: "" };
}
export function sourceScheduleLabel(source, config, worker) {
  if (source.enabled === false) return "此来源已暂停";
  const schedule = source.schedule_mode === "daily" ? `每日 ${source.daily_at || "未设置"}（北京）` : `每 ${source.interval_minutes ?? "—"} 分钟`;
  return config?.enabled === false || worker?.automatic_enabled === false || worker?.enabled === false ? `${schedule} · 当前未启用` : schedule;
}
export function collectionConfigValues(config = {}) {
  return { max_concurrent_jobs: 1, max_memory_mb: 768, job_timeout_seconds: 240, overseas_enabled: true, overseas_poll_interval_sec: 120, ...config,
    symbols: Array.isArray(config.symbols) ? config.symbols.join("\n") : String(config.symbols || ""), sources: Object.fromEntries(Object.entries(config.sources || {}).map(([id, value]) => [id, { ...value, schedule_mode: value.schedule_mode || "interval", daily_at: value.daily_at || "17:30" }])) };
}

export function fileQueryParams(filters = {}) {
  const limit = Math.min(100, Math.max(10, Number(filters.pageSize) || 20));
  const params = { offset: (Math.max(1, Number(filters.page) || 1) - 1) * limit, limit, sort: filters.sort || "updated_at", descending: filters.descending !== false };
  for (const key of ["category", "source", "lifecycle"]) {
    if (typeof filters[key] === "string" && filters[key]) params[key] = filters[key];
  }
  const query = typeof filters.q === "string" ? filters.q.trim() : "";
  if (query) params.q = query;
  return params;
}

export function cachePolicyPayload(values) {
  const megabyte = 1024 ** 2;
  const budget = Number(values.budget_mib);
  const batch = Number(values.max_batch_mib);
  const files = Number(values.max_batch_files);
  if (!Number.isFinite(budget) || budget < 64 || budget > 1048576) throw new Error("缓存容量预算需为 64–1048576 MiB。");
  if (!Number.isFinite(batch) || batch < 1 || batch > 2048) throw new Error("每批回收容量需为 1–2048 MiB。");
  if (!Number.isInteger(files) || files < 1 || files > 1000) throw new Error("每批文件数需为 1–1000 的整数。");
  const ttl = {};
  for (const key of ["market_source_cache", "kline_bundle_cache", "tushare_daily_cache"]) {
    const value = Number(values.ttl_days?.[key]);
    if (!Number.isInteger(value) || value < 1 || value > 3650) throw new Error("缓存保留天数需为 1–3650 的整数。");
    ttl[key] = value;
  }
  return { enabled: values.enabled === true, budget_bytes: Math.round(budget * megabyte), max_batch_bytes: Math.round(batch * megabyte), max_batch_files: files, ttl_days: ttl };
}

export function quarantineSummary(batch) {
  const items = Array.isArray(batch?.items) ? batch.items : [];
  const remaining = items.filter((item) => item.status === "quarantined" || item.status === "restore_error");
  const restored = items.filter((item) => item.status === "restored").length;
  const conflicts = items.filter((item) => item.status === "restore_error" || item.restore_error).length;
  return { remaining: remaining.length, restored, conflicts, canRestore: remaining.length > 0, remainingBytes: remaining.reduce((total, item) => total + (Number(item.size_bytes) || 0), 0) };
}

export function cacheOperationNotice(action, result) {
  if (action === "purge") {
    const skipped = Array.isArray(result?.skipped) ? result.skipped.length : 0;
    return { type: skipped ? "warning" : "success", text: `已永久删除 ${result?.purged_files ?? 0} 个回收文件，删除文件大小 ${formatDataSize(result?.freed_bytes)}${skipped ? `；${skipped} 项未删除` : ""}。` };
  }
  const { conflicts } = quarantineSummary(result);
  return { type: conflicts ? "warning" : "success", text: `已恢复 ${result?.restored_files ?? 0} 个文件${conflicts ? `，${conflicts} 个冲突项保留在回收区` : ""}。` };
}

export function collectionConfigPayload(values, sources) {
  const symbols = [...new Set((Array.isArray(values.symbols) ? values.symbols : String(values.symbols || "").split(/[\s,，;；]+/)).map((value) => String(value).trim().toUpperCase()).filter(Boolean))];
  if (symbols.length > 300 || symbols.some((symbol) => !/^\d{6}\.(SS|SZ|BJ)$/.test(symbol))) throw new Error("请填写最多 300 个 A 股代码，格式如 600519.SS、000001.SZ。");
  const dailyLimit = Number(values.daily_job_limit);
  const maxSymbols = Number(values.max_symbols_per_cycle);
  const minimumDisk = Number(values.min_free_disk_mb);
  if (!Number.isInteger(dailyLimit) || dailyLimit < 1 || dailyLimit > 1000) throw new Error("每日采集任务上限需为 1–1000 的整数。");
  if (!Number.isInteger(maxSymbols) || maxSymbols < 1 || maxSymbols > 30) throw new Error("每轮股票数需为 1–30 的整数。");
  if (!Number.isInteger(minimumDisk) || minimumDisk < 128 || minimumDisk > 102400) throw new Error("磁盘剩余空间下限需为 128–102400 MiB。");
  const limits = {};
  for (const [key, fallback, min, max, label] of [["max_concurrent_jobs", 1, 1, 3, "并发任务数"], ["max_memory_mb", 768, 128, 8192, "进程内存软阈值"], ["job_timeout_seconds", 240, 30, 600, "单任务超时"], ["overseas_poll_interval_sec", 120, 60, 3600, "海外采集间隔"]]) {
    const value = Number(values[key] ?? fallback);
    if (!Number.isInteger(value) || value < min || value > max) throw new Error(`${label}需为 ${min}–${max} 的整数。`);
    limits[key] = value;
  }
  const payload = { ...limits, overseas_enabled: values.overseas_enabled !== false, enabled: values.enabled === true, rag_enabled: values.rag_enabled === true, use_watchlists: values.use_watchlists === true, daily_job_limit: dailyLimit, max_symbols_per_cycle: maxSymbols, min_free_disk_mb: minimumDisk, symbols, sources: {} };
  for (const source of sources || []) {
    const item = values.sources?.[source.id];
    if (!item) continue;
    const interval = Number(item.interval_minutes);
    if (!Number.isInteger(interval) || interval < 60 || interval > 43200) throw new Error(`${source.name || source.id} 的采集间隔需为 60–43200 分钟。`);
    const mode = item.schedule_mode || "interval";
    const dailyAt = item.daily_at || "17:30";
    if (!["interval", "daily"].includes(mode)) throw new Error("请选择每日或按间隔采集。");
    if (!/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(dailyAt)) throw new Error("每日采集时间须为北京时间 HH:MM。");
    payload.sources[source.id] = { enabled: item.enabled === true, interval_minutes: interval, schedule_mode: mode, daily_at: dailyAt };
  }
  return payload;
}

export function categoryOptions(categories) {
  return (Array.isArray(categories) ? categories : []).filter((item) => typeof item?.id === "string")
    .map((item) => ({ value: item.id, label: item.label || item.id }));
}

export function sourceOptions(sources) {
  return (Array.isArray(sources) ? sources : []).filter((item) => typeof item?.id === "string")
    .map((item) => ({ value: item.id, label: item.name || item.label || item.id }));
}
