import assert from "node:assert/strict";
import test from "node:test";
import axios from "axios";

// 检查真实 API 包装器生成的请求配置，不连接后端或调用收费模型。
let captured;
const originalAdapter = axios.defaults.adapter;
axios.defaults.adapter = async (config) => {
  captured = config;
  return { data: { ok: true }, status: 200, statusText: "OK", headers: {}, config };
};
const api = await import("../src/api.js");
axios.defaults.adapter = originalAdapter;

test("news synchronization allows slow providers to finish within ten minutes", async () => {
  assert.deepEqual(await api.syncArchiveFeeds(["600519.SS"]), { ok: true });
  assert.equal(captured.url, "/news/sync-archive-feeds");
  assert.equal(captured.timeout, 600_000);
  assert.deepEqual(JSON.parse(captured.data), { symbols: ["600519.SS"], lookback_days: 30 });
});

test("news analysis defaults to at most three items with a ten-minute timeout", async () => {
  await api.analyzeNewsForSymbol("600519.SS");
  assert.equal(api.NEWS_ANALYSIS_BATCH_SIZE, 3);
  assert.equal(captured.url, "/news/analyze-symbol-archive");
  assert.equal(captured.timeout, 600_000);
  assert.deepEqual(JSON.parse(captured.data), {
    symbol: "600519.SS", limit: 3, persist: true, lookback_days: 30,
  });
});

test("explicit analysis limits and persistence flags remain available", async () => {
  await api.analyzeNewsForSymbol("000001.SZ", 20, false);
  assert.deepEqual(JSON.parse(captured.data), {
    symbol: "000001.SZ", limit: 20, persist: false, lookback_days: 30,
  });
});

test("company polling reads server cache and manual refresh explicitly requests an update", async () => {
  await api.getXueqiuCompany("600519.SS");
  assert.equal(captured.url, "/xueqiu/company");
  assert.deepEqual(captured.params, { symbol: "600519.SS" });
  await api.getXueqiuCompany("600519.SS", { force: true });
  assert.deepEqual(captured.params, { symbol: "600519.SS", force: true });
});
