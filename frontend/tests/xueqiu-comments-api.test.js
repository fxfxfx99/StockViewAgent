import assert from "node:assert/strict";
import test from "node:test";
import axios from "axios";

const captured = [];
const snapshot = { symbol: "600519.SS", status: "queued", items: [], job: { id: "test-job", status: "queued" } };
const originalAdapter = axios.defaults.adapter;
axios.defaults.adapter = async (config) => {
  captured.push(config);
  return { data: snapshot, status: config.method === "post" ? 202 : 200, statusText: "OK", headers: {}, config };
};
const api = await import("../src/api.js");
axios.defaults.adapter = originalAdapter;

test("reading comments is a cancellable GET without refresh side effects", async () => {
  const controller = new AbortController();
  assert.deepEqual(await api.getXueqiuComments("600519.SS", { signal: controller.signal }), snapshot);
  const config = captured.at(-1);
  assert.equal(config.method, "get");
  assert.equal(config.url, "/xueqiu/comments");
  assert.deepEqual(config.params, { symbol: "600519.SS" });
  assert.equal(config.signal, controller.signal);
  assert.equal(config.timeout, 15_000);
});

test("refresh posts only the selected scope and accepts the queued snapshot without a long request", async () => {
  assert.deepEqual(await api.refreshXueqiuComments("600519.SS"), snapshot);
  const latest = captured.at(-1);
  assert.equal(latest.method, "post");
  assert.equal(latest.url, "/xueqiu/comments/refresh");
  assert.deepEqual(JSON.parse(latest.data), { symbol: "600519.SS", mode: "latest" });
  assert.equal(latest.timeout, 15_000);
  await api.refreshXueqiuComments("000001.SZ", "previous_day");
  assert.deepEqual(JSON.parse(captured.at(-1).data), { symbol: "000001.SZ", mode: "previous_day" });
});
