import assert from "node:assert/strict";
import test from "node:test";
import axios from "axios";

let requests = [];
const originalAdapter = axios.defaults.adapter;
axios.defaults.adapter = async (config) => {
  requests.push(config);
  return { data: { phase: "collecting", queued_jobs: [] }, status: 200, statusText: "OK", headers: {}, config };
};
const api = await import("../src/api.js");
axios.defaults.adapter = originalAdapter;

test("autopilot reads and presets share authentication and support cancellation", async () => {
  requests = [];
  api.setAuthToken("fixture-account");
  const signal = new AbortController().signal;
  await api.getAutopilotStatus({ signal });
  await api.getLlmProviderPresets({ signal });
  await api.getSettingsIntegrations({ signal });
  assert.deepEqual(requests.map((request) => request.url), ["/autopilot/status", "/settings/llm/presets", "/settings/integrations"]);
  assert.ok(requests.every((request) => request.signal === signal && request.headers.Authorization === "Bearer fixture-account"));
  api.setAuthToken("");
});

test("reconcile is a scoped empty-body POST, not a forced research run", async () => {
  requests = [];
  await api.reconcileAutopilot();
  assert.equal(requests[0].method, "post");
  assert.equal(requests[0].url, "/autopilot/reconcile");
  assert.deepEqual(JSON.parse(requests[0].data), {});
});
