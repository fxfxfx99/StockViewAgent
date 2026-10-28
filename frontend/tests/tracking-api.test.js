import assert from "node:assert/strict";
import test from "node:test";
import axios from "axios";

let captured;
const originalAdapter = axios.defaults.adapter;
axios.defaults.adapter = async (config) => {
  captured = config;
  return { data: { ok: true }, status: 200, statusText: "OK", headers: {}, config };
};
const api = await import("../src/api.js");
axios.defaults.adapter = originalAdapter;

test("tracking list and detail reads preserve cancellation and encode record IDs", async () => {
  const signal = new AbortController().signal;
  for (const [request, url] of [
    [() => api.getTrackingTheses({ signal }), "/tracking/theses"],
    [() => api.getTrackingChanges({ signal }), "/tracking/changes"],
    [() => api.getTrackingThesis("record/a b", { signal }), "/tracking/theses/record%2Fa%20b"],
  ]) {
    assert.deepEqual(await request(), { ok: true });
    assert.equal(captured.url, url);
    assert.equal(captured.signal, signal);
    assert.equal(captured.method, "get");
  }
});

test("saving and reviewing judgments send only explicit local tracking operations", async () => {
  const payload = { symbol: "600519.SS", reason: "订单增长", watch_conditions: "下期订单", invalidation_conditions: "订单取消", review_at: "2026-12-01", evidence_refs: [{ kind: "news", id: "7" }] };
  await api.createTrackingThesis(payload);
  assert.equal(captured.url, "/tracking/theses");
  assert.equal(captured.method, "post");
  assert.deepEqual(JSON.parse(captured.data), payload);
  const review = { revision: 3, action: "revise", reason: "新增证据", evidence_assessment: "contradicts", review_at: "2026-12-02", changes: { reason: "继续核查" } };
  await api.reviewTrackingThesis("id/a", review);
  assert.equal(captured.url, "/tracking/theses/id%2Fa/reviews");
  assert.deepEqual(JSON.parse(captured.data), review);
});

test("linking archived evidence retains optimistic revision and read/feedback patches preserve false and null", async () => {
  const patch = { revision: 4, evidence_refs: [{ kind: "calendar", id: "e1", revision_id: "r3" }] };
  await api.updateTrackingThesis("id/a", patch);
  assert.equal(captured.url, "/tracking/theses/id%2Fa");
  assert.equal(captured.method, "patch");
  assert.deepEqual(JSON.parse(captured.data), patch);
  await api.updateTrackingChange("id/a", { is_read: false, feedback: null });
  assert.equal(captured.url, "/tracking/changes/id%2Fa");
  assert.deepEqual(JSON.parse(captured.data), { is_read: false, feedback: null });
});

test("manual checking makes one local-scan request without triggering external refresh or model analysis", async () => {
  await api.refreshTracking();
  assert.equal(captured.url, "/tracking/refresh");
  assert.equal(captured.method, "post");
  assert.equal(captured.timeout, 60_000);
  assert.deepEqual(JSON.parse(captured.data), {});
});
