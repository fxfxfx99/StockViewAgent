import assert from "node:assert/strict";
import test from "node:test";
import axios from "axios";
let captured;
const adapter = axios.defaults.adapter;
axios.defaults.adapter = async (config) => { captured = config; return { data: {}, status: 200, statusText: "OK", headers: {}, config }; };
const api = await import("../src/api.js");
axios.defaults.adapter = adapter;

test("earnings reads use cancellable GETs; PDF download uses authenticated blob transport rather than credentials in URLs", async () => {
  const signal = new AbortController().signal;
  await api.getEarningsReports("600519.SS", { signal });
  assert.equal(captured.url, "/news/earnings"); assert.equal(captured.method, "get"); assert.deepEqual(captured.params, { symbol: "600519.SS" }); assert.equal(captured.signal, signal);
  await api.getEarningsReport("a/b", { signal }); assert.equal(captured.url, "/news/earnings/a%2Fb"); assert.equal(captured.signal, signal);
  api.setAuthToken("fixture-token");
  await api.downloadEarningsPdf("a/b"); assert.equal(captured.url, "/news/earnings/a%2Fb/pdf"); assert.equal(captured.responseType, "blob"); assert.equal(captured.headers.Authorization, "Bearer fixture-token");
  api.setAuthToken("");
});

test("discovery, parsing and paid interpretation have distinct explicit POST payloads", async () => {
  await api.refreshEarningsReports("000001.SZ"); assert.equal(captured.method, "post"); assert.equal(captured.url, "/news/earnings/refresh"); assert.deepEqual(JSON.parse(captured.data), { symbol: "000001.SZ" });
  await api.prepareEarningsReport("q3"); assert.equal(captured.url, "/news/earnings/q3/prepare"); assert.deepEqual(JSON.parse(captured.data), {});
  await api.analyzeEarningsReport("q3", "现金流与应收账款"); assert.equal(captured.url, "/news/earnings/q3/analyze"); assert.deepEqual(JSON.parse(captured.data), { focus: "现金流与应收账款" });
});
