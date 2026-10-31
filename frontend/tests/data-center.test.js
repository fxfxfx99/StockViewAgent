import assert from "node:assert/strict";
import test from "node:test";
import {
  cacheOperationNotice, cachePolicyPayload, collectionConfigPayload, collectionConfigValues, collectionRuntime, sourceScheduleLabel, collectionServiceNotice, collectionStatus, collectionSubmissionNotice, dataCenterQueryKey,
  dataTime, fileQueryParams, quarantineSummary, sourceMinimumInterval,
} from "../src/dataCenterViewModel.js";

const config = {
  enabled: true, rag_enabled: true, use_watchlists: false, daily_job_limit: 120, max_symbols_per_cycle: 10,
  min_free_disk_mb: 1024, symbols: "600519.ss, 000001.SZ\n600519.SS",
  sources: { news: { enabled: false, interval_minutes: 120 } },
};
const sources = [{ id: "news", name: "财经新闻" }];

test("data center queries remain isolated between authenticated administrators", () => {
  assert.notDeepEqual(dataCenterQueryKey(1, "files"), dataCenterQueryKey(2, "files"));
  assert.notDeepEqual(dataCenterQueryKey(1, "files", { offset: 0 }), dataCenterQueryKey(1, "files", { offset: 20 }));
});

test("file filters use server pagination and preserve protected versus rebuildable lifecycle", () => {
  assert.deepEqual(fileQueryParams({ page: 3, pageSize: 20, category: "market", source: "腾讯", lifecycle: "protected", q: " 日线 ", descending: false }), {
    offset: 40, limit: 20, sort: "updated_at", descending: false, category: "market", source: "腾讯", lifecycle: "protected", q: "日线",
  });
  assert.deepEqual(fileQueryParams(), { offset: 0, limit: 20, sort: "updated_at", descending: true });
});

test("collection config deduplicates symbols, keeps switches and emits task limits", () => {
  assert.deepEqual(collectionConfigPayload(config, sources), {
    enabled: true, rag_enabled: true, use_watchlists: false, daily_job_limit: 120, max_symbols_per_cycle: 10, min_free_disk_mb: 1024, max_concurrent_jobs: 1, max_memory_mb: 768, job_timeout_seconds: 240, overseas_enabled: true, overseas_poll_interval_sec: 120,
    symbols: ["600519.SS", "000001.SZ"], sources: { news: { enabled: false, interval_minutes: 120, schedule_mode: "interval", daily_at: "17:30" } },
  });
  assert.deepEqual(collectionConfigPayload({ ...config, symbols: "" }, sources).symbols, []);
});

test("collection config prevents unsupported stocks and out-of-bound jobs or source frequency", () => {
  for (const symbols of ["AAPL", "600519.SH", "http://example.com", "000001"]) assert.throws(() => collectionConfigPayload({ ...config, symbols }, sources), /A 股/);
  for (const daily_job_limit of [0, 1001, 1.5, NaN]) assert.throws(() => collectionConfigPayload({ ...config, daily_job_limit }, sources), /任务上限/);
  for (const max_symbols_per_cycle of [0, 31]) assert.throws(() => collectionConfigPayload({ ...config, max_symbols_per_cycle }, sources), /每轮/);
  assert.throws(() => collectionConfigPayload({ ...config, min_free_disk_mb: 127 }, sources), /磁盘/);
  assert.throws(() => collectionConfigPayload({ ...config, sources: { news: { enabled: true, interval_minutes: 59 } } }, sources), /采集间隔/);
});
test("market schedules accept the server minimum without relaxing other collection limits", () => {
  const market = { id: "public_market", name: "全球行情", min_interval_minutes: 5 };
  const values = { ...config, sources: { ...config.sources, public_market: { enabled: true, interval_minutes: 5 } } };
  assert.equal(collectionConfigPayload(values, [...sources, market]).sources.public_market.interval_minutes, 5);
  assert.equal(sourceMinimumInterval(market), 5);
  assert.equal(sourceMinimumInterval({ id: "news" }), 60);
  for (const min_interval_minutes of [null, -1, "bad", 43201]) assert.equal(sourceMinimumInterval({ min_interval_minutes }), 60);
  assert.throws(() => collectionConfigPayload({ ...values, sources: { public_market: { enabled: true, interval_minutes: 4 } } }, [market]), /5–43200/);
  assert.throws(() => collectionConfigPayload({ ...values, sources: { news: { enabled: true, interval_minutes: 5 } } }, sources), /60–43200/);
});

test("queued requests, errors, partial jobs and stale success have distinct visible states", () => {
  assert.equal(collectionStatus({ status: "queued" }).label, "排队中");
  assert.equal(collectionStatus({ status: "running" }).label, "采集中");
  assert.equal(collectionStatus({ status: "failed", last_success_at: "2026-10-01" }).color, "error");
  assert.equal(collectionStatus({ status: "partial" }).color, "warning");
  assert.equal(collectionStatus({ status: "success", stale: true }).label, "待更新");
  assert.equal(collectionStatus({ status: "success" }).color, "success");
  assert.equal(collectionStatus({}).label, "尚未采集");
});

test("manual collection response counts only newly queued jobs and distinguishes cooldown or duplicates", () => {
  const notice = collectionSubmissionNotice({ jobs: [{ status: "queued" }, { status: "queued", deduplicated: true }, { status: "cooldown" }] });
  assert.match(notice, /已提交 1 个/);
  assert.match(notice, /1 个来源已有任务/);
  assert.match(notice, /1 个来源正在冷却/);
  assert.match(collectionSubmissionNotice({ jobs: [{ status: "cooldown" }] }), /没有新增采集任务/);
  assert.doesNotMatch(notice, /采集完成/);
});

test("disabled collection service only queues manual jobs while disabled scheduling still permits an enabled worker", () => {
  const disabled = collectionServiceNotice({ enabled: false, scheduler_enabled: false });
  assert.match(disabled, /进入队列，启用采集服务后才会执行/);
  assert.doesNotMatch(disabled, /仅处理手动/);
  assert.equal(collectionServiceNotice({ enabled: true, scheduler_enabled: false }), "服务器定时调度已关闭，仅处理手动提交的采集任务。");
  assert.equal(collectionServiceNotice({ enabled: true, scheduler_enabled: true }), null);
  assert.equal(collectionServiceNotice({ enabled: true, scheduler_enabled: false, automatic_enabled: true }), null);
  assert.equal(collectionServiceNotice({ enabled: true, scheduler_enabled: true, automatic_enabled: false }), "自动采集已关闭，仅处理手动提交的采集任务。");
  assert.equal(collectionServiceNotice(undefined), null);
});

test("timestamps use Beijing time and distinguish missing metadata", () => {
  assert.match(dataTime("2026-10-24T20:20:00Z"), /10\/25.*04:20/);
  assert.equal(dataTime(Date.parse("2026-10-24T20:20:00Z") / 1000), dataTime("2026-10-24T20:20:00Z"));
  for (const input of [null, undefined, "", false, "invalid"]) assert.equal(dataTime(input), "—");
});

test("cache policy converts visible MiB to bytes and validates safe server limits", () => {
  const values = { enabled: false, budget_mib: 2048, max_batch_mib: 256, max_batch_files: 500, ttl_days: { market_source_cache: 7, kline_bundle_cache: 90, tushare_daily_cache: 180 } };
  const payload = cachePolicyPayload(values);
  assert.equal(payload.budget_bytes, 2 * 1024 ** 3);
  assert.equal(payload.max_batch_bytes, 256 * 1024 ** 2);
  assert.equal(payload.enabled, false);
  assert.throws(() => cachePolicyPayload({ ...values, budget_mib: 63 }), /容量预算/);
  assert.throws(() => cachePolicyPayload({ ...values, max_batch_files: 1001 }), /文件数/);
  assert.throws(() => cachePolicyPayload({ ...values, ttl_days: { ...values.ttl_days, market_source_cache: 0 } }), /保留天数/);
});

test("recovery summaries preserve conflict items and never offer restored or skipped files as recoverable", () => {
  const result = quarantineSummary({ items: [
    { path: "cache/a", status: "quarantined", size_bytes: 10 },
    { path: "cache/b", status: "quarantined", size_bytes: 20, restore_error: "已有新缓存" },
    { path: "cache/c", status: "restored", size_bytes: 30 },
    { path: "cache/d", status: "skipped", size_bytes: 40 },
  ] });
  assert.deepEqual(result, { remaining: 2, restored: 1, conflicts: 1, canRestore: true, remainingBytes: 30 });
  assert.equal(quarantineSummary({ items: [{ status: "restored" }, { status: "skipped" }] }).canRestore, false);
  assert.equal(quarantineSummary({}).canRestore, false);
});

test("permanent deletion notices use canonical purge counts without claiming actual filesystem free space", () => {
  const notice = cacheOperationNotice("purge", { batch_id: "batch-1", purged_files: 3, freed_bytes: 2048, skipped: [] });
  assert.equal(notice.type, "success");
  assert.match(notice.text, /已永久删除 3 个/);
  assert.match(notice.text, /删除文件大小 2 KiB/);
  assert.doesNotMatch(notice.text, /可用磁盘|释放空间/);
  const partial = cacheOperationNotice("purge", { purged_files: 0, freed_bytes: 0, skipped: [{ path: "cache/a", reason: "文件变化" }] });
  assert.equal(partial.type, "warning");
  assert.match(partial.text, /0 B；1 项未删除/);
});

test("restore notices read restored_files plus returned batch item errors", () => {
  const result = { batch_id: "batch-1", restored_files: 1, items: [
    { path: "cache/a", status: "restored" },
    { path: "cache/b", status: "quarantined", restore_error: "原路径已有文件，保留当前文件与回收副本" },
  ] };
  assert.deepEqual(cacheOperationNotice("restore", result), { type: "warning", text: "已恢复 1 个文件，1 个冲突项保留在回收区。" });
});


test("daily schedules preserve legacy intervals and validate resource boundaries", () => {
  const values = collectionConfigValues(config);
  assert.equal(values.sources.news.schedule_mode, "interval");
  const daily = collectionConfigPayload({ ...values, sources: { news: { ...values.sources.news, schedule_mode: "daily", daily_at: "17:45" } }, overseas_enabled: false, max_concurrent_jobs: 2 }, sources);
  assert.equal(daily.sources.news.interval_minutes, 120);
  assert.equal(daily.sources.news.daily_at, "17:45");
  assert.equal(daily.overseas_enabled, false);
  for (const value of ["24:00", "9:30", "17:99"]) assert.throws(() => collectionConfigPayload({ ...values, sources: { news: { ...values.sources.news, daily_at: value } } }, sources), /HH:MM/);
  for (const [key, value] of [["max_concurrent_jobs",4],["max_memory_mb",127],["job_timeout_seconds",601],["overseas_poll_interval_sec",59]]) assert.throws(() => collectionConfigPayload({ ...values, [key]:value }, sources), /整数/);
});

test("effective service status never mistakes saved enablement for an active unblocked worker", () => {
  assert.equal(collectionRuntime({ enabled:true }, { enabled:false }).label, "服务环境已关闭");
  assert.equal(collectionRuntime({ enabled:true }, { enabled:true, running:false }).label, "等待采集服务");
  assert.equal(collectionRuntime({ enabled:true }, { enabled:true, running:true, resources:{pressure:true} }).label, "采集暂缓");
  assert.match(collectionRuntime({ enabled:true }, { enabled:true, reason:"daily_budget_exhausted" }).description, /额度/);
  assert.match(sourceScheduleLabel({enabled:true,schedule_mode:"daily",daily_at:"17:30"},{enabled:false},{}), /当前未启用/);
});
