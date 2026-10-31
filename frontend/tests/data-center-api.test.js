import assert from "node:assert/strict";
import test from "node:test";
import axios from "axios";

let captured;
const originalAdapter = axios.defaults.adapter;
axios.defaults.adapter = async (config) => {
  captured = config;
  return { data: { ok: true }, status: 200, statusText: "OK", headers: {}, config };
};
const api = await import("../src/api.js");
axios.defaults.adapter = originalAdapter;

test("data center overview and inventory read through the admin endpoint with abortable filters", async () => {
  const signal = new AbortController().signal;
  await api.getLocalDataOverview({ signal });
  assert.equal(captured.url, "/data-center/inventory/summary");
  assert.equal(captured.signal, signal);
  const params = { category: "market", lifecycle: "protected", offset: 20, limit: 20, source: "腾讯" };
  await api.getLocalDataFiles(params, { signal });
  assert.equal(captured.url, "/data-center/files");
  assert.deepEqual(captured.params, params);
  assert.equal(captured.signal, signal);
});

test("collection submission only sends selected builtin source IDs and returns queue result", async () => {
  await api.startDataCollection(["public_news"]);
  assert.equal(captured.url, "/data-center/collect");
  assert.equal(captured.method, "post");
  assert.deepEqual(JSON.parse(captured.data), { source_ids: ["public_news"] });
  await api.startDataCollection();
  assert.deepEqual(JSON.parse(captured.data), {});
  await api.getCollectionJobs({ limit: 40 });
  assert.equal(captured.url, "/data-center/jobs");
  assert.deepEqual(captured.params, { limit: 40 });
  await api.getCollectionSources();
  assert.equal(captured.url, "/data-center/sources");
});

test("collection configuration retains explicit false toggles and empty stock lists", async () => {
  await api.getCollectionConfig();
  assert.equal(captured.url, "/data-center/config");
  const patch = { enabled: false, daily_job_limit: 12, symbols: [] };
  await api.updateCollectionConfig(patch);
  assert.equal(captured.method, "patch");
  assert.deepEqual(JSON.parse(captured.data), patch);
});

test("cleanup plans are previewed separately and applied by server-generated ID", async () => {
  await api.previewLocalCacheCleanup();
  assert.equal(captured.url, "/data-center/cleanup/preview");
  assert.deepEqual(JSON.parse(captured.data), {});
  await api.applyLocalCacheCleanup("plan-123");
  assert.equal(captured.url, "/data-center/cleanup/apply");
  assert.deepEqual(JSON.parse(captured.data), { plan_id: "plan-123" });
});

test("recovery URL escapes batch identifiers and destructive deletion sends explicit confirmation", async () => {
  await api.getLocalDataTrash({ offset: 16, limit: 8 });
  assert.equal(captured.url, "/data-center/trash");
  assert.deepEqual(captured.params, { offset: 16, limit: 8 });
  await api.restoreLocalDataTrash("batch/a b");
  assert.equal(captured.url, "/data-center/trash/batch%2Fa%20b/restore");
  assert.equal(captured.method, "post");
  await api.deleteLocalDataTrash("batch/a b");
  assert.equal(captured.url, "/data-center/trash/batch%2Fa%20b");
  assert.equal(captured.method, "delete");
  assert.deepEqual(JSON.parse(captured.data), { confirm: true });
});

test("cache policy persistence preserves configured retention windows", async () => {
  await api.getLocalCachePolicy();
  assert.equal(captured.url, "/data-center/cache/policy");
  const patch = { enabled: false, ttl_days: { market_source_cache: 14 } };
  await api.updateLocalCachePolicy(patch);
  assert.equal(captured.url, "/data-center/cache/policy");
  assert.equal(captured.method, "patch");
  assert.deepEqual(JSON.parse(captured.data), patch);
});

test("collected public records expose source and query filters without reading arbitrary local paths", async () => {
  const params = { source_id: "imf", q: "inflation", offset: 0, limit: 20 };
  const signal = new AbortController().signal;
  await api.getCollectedDataRecords(params, { signal });
  assert.equal(captured.url, "/data-center/records");
  assert.deepEqual(captured.params, params);
  assert.equal(captured.signal, signal);
});

test("collector status is a lightweight cancellable GET, separate from inventory", async () => { const signal=new AbortController().signal; await api.getCollectionStatus({signal}); assert.equal(captured.url,"/data-center/collection/status"); assert.equal(captured.method,"get"); assert.equal(captured.signal,signal); });
