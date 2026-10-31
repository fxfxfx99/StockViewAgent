import assert from "node:assert/strict";
import test from "node:test";
import axios from "axios";

let captured;
const adapter = axios.defaults.adapter;
axios.defaults.adapter = async (config) => {
  captured = config;
  return { data: { candles: [] }, status: 200, statusText: "OK", headers: {}, config };
};
const api = await import("../src/api.js");
axios.defaults.adapter = adapter;

test("replay requests dated historical data, with auth and cancellation", async () => {
  api.setAuthToken("replay-test-token");
  const signal = new AbortController().signal;
  const result = await api.getKlineReplay("600519.SS", { start_date: "2024-01-01", end_date: "2024-12-31", signal });
  assert.equal(captured.url, "/kline-replay/600519.SS");
  assert.equal(captured.method, "get");
  assert.deepEqual(captured.params, { start_date: "2024-01-01", end_date: "2024-12-31", refresh: false });
  assert.equal(captured.signal, signal);
  assert.equal(captured.headers.Authorization, "Bearer replay-test-token");
  assert.deepEqual(result, { candles: [] });
  await api.getKlineReplay("600519.SS", { refresh: true });
  assert.equal(captured.params.refresh, true, "explicit reload bypasses cache after source credentials change");
  api.setAuthToken("");
});
