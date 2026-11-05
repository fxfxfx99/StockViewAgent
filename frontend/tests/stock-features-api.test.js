import assert from "node:assert/strict";
import test from "node:test";
import axios from "axios";

let captured;
const originalAdapter = axios.defaults.adapter;
axios.defaults.adapter = async (config) => { captured = config; return { data: { snapshot: null }, status: 200, statusText: "OK", headers: {}, config }; };
const api = await import("../src/api.js");
axios.defaults.adapter = originalAdapter;

test("feature reads support cancellation and version history has a bounded default", async () => {
  const signal = new AbortController().signal;
  await api.getStockFeatures("600519.SS", { signal });
  assert.equal(captured.method, "get");
  assert.equal(captured.url, "/stocks/600519.SS/features");
  assert.equal(captured.signal, signal);
  await api.getStockFeaturesHistory("600519.SS", { signal });
  assert.equal(captured.url, "/stocks/600519.SS/features/history");
  assert.equal(captured.params.limit, 12);
  assert.equal(captured.signal, signal);
});

test("local feature refresh is a distinct explicit request with an encoded stock path", async () => {
  await api.refreshStockFeatures("test/stock");
  assert.equal(captured.method, "post");
  assert.equal(captured.url, "/stocks/test%2Fstock/features/refresh");
  assert.deepEqual(JSON.parse(captured.data), {});
});
