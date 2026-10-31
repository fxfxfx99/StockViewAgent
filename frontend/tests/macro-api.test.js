import assert from "node:assert/strict";
import test from "node:test";
import axios from "axios";
let captured;
const adapter = axios.defaults.adapter;
axios.defaults.adapter = async (config) => { captured = config; return { data: {}, status: 200, statusText: "OK", headers: {}, config }; };
const api = await import("../src/api.js");
axios.defaults.adapter = adapter;
test("macro chart reads preserve interval, refresh and cancellation without AI mutation", async () => {
  const signal = new AbortController().signal;
  const params = { index_key: "SH_COMP", range: "1y", interval: "1wk", refresh: true };
  await api.getMacroIndexKline(params, { signal });
  assert.equal(captured.url, "/macro/index-kline"); assert.equal(captured.method, "get"); assert.deepEqual(captured.params, params); assert.equal(captured.signal, signal);
  await api.getMacroKline("gold_sge", "2y", { signal });
  assert.deepEqual(captured.params, { series_id: "gold_sge", range: "2y" }); assert.equal(captured.signal, signal);
  await api.getMacroIndustryKline("801010.SI", "1y", { signal });
  assert.deepEqual(captured.params, { ts_code: "801010.SI", range: "1y" });
});
test("AI calls only use explicit POST payloads; material identifiers are escaped", async () => {
  const body = { index_key: "SH_COMP", range: "1y", interval: "1d", candle_rows: 35 };
  await api.analyzeKlineMarket(body); assert.equal(captured.method, "post"); assert.equal(captured.url, "/kline-learning/analyze-market"); assert.deepEqual(JSON.parse(captured.data), body);
  await api.chatKlineMarket({ ...body, question: "当前趋势？" }); assert.equal(captured.url, "/kline-learning/market-chat");
  await api.deleteKlineLearningMaterial("a/b"); assert.equal(captured.url, "/kline-learning/materials/a%2Fb");
});
