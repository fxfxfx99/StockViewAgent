import assert from "node:assert/strict";
import test from "node:test";
import axios from "axios";
let captured;
const adapter = axios.defaults.adapter;
axios.defaults.adapter = async (config) => { captured = config; return { data: {}, status: 200, statusText: "OK", headers: {}, config }; };
const api = await import("../src/api.js");
axios.defaults.adapter = adapter;

test("Merrill reads preserve region, selection, time range and cancellation", async () => {
  const signal = new AbortController().signal;
  await api.getMerrillOverview("CN", { signal });
  assert.equal(captured.url, "/merrill-clock");
  assert.equal(captured.method, "get");
  assert.deepEqual(captured.params, { region: "CN" });
  assert.equal(captured.signal, signal);
  await api.getMerrillSeries(["sp500", "gold"], 50, { signal });
  assert.equal(captured.url, "/merrill-clock/series");
  assert.deepEqual(captured.params, { ids: "sp500,gold", years: 50 });
  assert.equal(captured.signal, signal);
});

test("Merrill model generation and automation use explicit scoped mutations", async () => {
  await api.refreshMerrillData();
  assert.equal(captured.url, "/merrill-clock/refresh");
  assert.equal(captured.method, "post");
  await api.analyzeMerrillClock("CN");
  assert.equal(captured.url, "/merrill-clock/analyze");
  assert.deepEqual(JSON.parse(captured.data), { region: "CN" });
  await api.updateMerrillPreferences(true);
  assert.equal(captured.method, "put");
  assert.equal(captured.url, "/merrill-clock/preferences");
  assert.deepEqual(JSON.parse(captured.data), { auto_analysis: true });
});
