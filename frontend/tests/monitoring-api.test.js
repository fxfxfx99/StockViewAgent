import assert from "node:assert/strict";
import test from "node:test";
import axios from "axios";
let captured;
const adapter = axios.defaults.adapter;
axios.defaults.adapter = async (config) => { captured = config; return { data: {}, status: 200, statusText: "OK", headers: {}, config }; };
const api = await import("../src/api.js");
axios.defaults.adapter = adapter;

test("monitoring reads use account-authenticated GETs with cancellation", async () => {
  const signal = new AbortController().signal;
  await api.getMonitoringOverview({ signal });
  assert.equal(captured.url, "/monitoring/overview"); assert.equal(captured.method, "get"); assert.equal(captured.signal, signal);
  await api.getMonitoringPlan("a/b", { signal }); assert.equal(captured.url, "/monitoring/plans/a%2Fb"); assert.equal(captured.signal, signal);
});

test("model saving is isolated from legacy autopilot; verification and runs are explicit mutations", async () => {
  await api.saveMonitoringModel({ api_base: "https://example.test/v1", model: "example", api_key: "test-placeholder" });
  assert.equal(captured.url, "/monitoring/model"); assert.equal(captured.method, "put");
  await api.verifyMonitoringModel(); assert.equal(captured.url, "/monitoring/model/verify"); assert.equal(captured.method, "post");
  await api.runMonitoringPlan("a/b", "review"); assert.equal(captured.url, "/monitoring/plans/a%2Fb/runs"); assert.deepEqual(JSON.parse(captured.data), { kind: "review" });
});

test("plan edits and human decisions retain optimistic revision; notification false and config zero-like booleans are preserved", async () => {
  const patch = { revision: 4, status: "paused" };
  await api.updateMonitoringPlan("id", patch); assert.equal(captured.method, "patch"); assert.deepEqual(JSON.parse(captured.data), patch);
  const review = { revision: 5, action: "close", reason: "观察完成", evidence_assessment: "uncertain" };
  await api.reviewMonitoringPlan("id", review); assert.deepEqual(JSON.parse(captured.data), review);
  await api.updateMonitoringNotification("n/1", false); assert.equal(captured.url, "/monitoring/notifications/n%2F1"); assert.deepEqual(JSON.parse(captured.data), { is_read: false });
  await api.updateMonitoringConfig({ enabled: false, daily_call_limit: 1 }); assert.deepEqual(JSON.parse(captured.data), { enabled: false, daily_call_limit: 1 });
});
