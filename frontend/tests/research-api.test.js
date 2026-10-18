import assert from "node:assert/strict";
import test from "node:test";
import axios from "axios";

let requests = [];
const originalAdapter = axios.defaults.adapter;
axios.defaults.adapter = async (config) => {
  requests.push(config);
  return { data: { id: "fixture-job", status: "queued" }, status: 202, statusText: "Accepted", headers: {}, config };
};
const api = await import("../src/api.js");
axios.defaults.adapter = originalAdapter;

test("research submissions preserve idempotency and send no user impersonation fields", async () => {
  requests = [];
  api.setAuthToken("account-one");
  const result = await api.startResearchRun("evaluate", { strategy_id: "strategy-1" }, "ui-once");
  assert.equal(result.status, "queued");
  assert.equal(requests[0].headers.Authorization, "Bearer account-one");
  assert.deepEqual(JSON.parse(requests[0].data), { kind: "evaluate", payload: { strategy_id: "strategy-1" }, idempotency_key: "ui-once" });
  assert.equal(requests[0].url, "/research/runs");
  api.setAuthToken("");
});
test("research reads propagate cancellation and encode entity IDs", async () => {
  requests = [];
  const signal = new AbortController().signal;
  await api.getResearchOverview({ signal });
  await api.getResearchExperiment("one/two", { signal });
  await api.getResearchStrategy("one/two", { signal });
  await api.getResearchSnapshot("one/two", { signal });
  await api.getResearchJob("one/two", { signal });
  assert.deepEqual(requests.map((r) => r.url), ["/research/overview", "/research/experiments/one%2Ftwo", "/research/strategies/one%2Ftwo", "/research/snapshots/one%2Ftwo", "/research/jobs/one%2Ftwo"]);
  assert.ok(requests.every((r) => r.signal === signal));
});
test("strategy and historical imports use their dedicated contracts", async () => {
  requests = [];
  const spec = { name: "test", entry: { op: "gt" } };
  const snapshot = { bundles: { "600000.SS": { candles: [] } }, metadata: { source: "test" } };
  await api.createResearchStrategy(spec);
  await api.importResearchSnapshot(snapshot);
  await api.updateResearchConfig({ paused: true });
  assert.deepEqual(JSON.parse(requests[0].data), { spec });
  assert.deepEqual(JSON.parse(requests[1].data), snapshot);
  assert.equal(requests[2].method, "patch");
  assert.deepEqual(JSON.parse(requests[2].data), { paused: true });
});
