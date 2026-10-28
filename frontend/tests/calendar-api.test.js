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

test("calendar requests preserve date filters and cancellation signals", async () => {
  const params = { start: "2026-10-01", end: "2026-10-31", region: "US", min_importance: 3, category: "central_bank", q: "Federal" };
  const signal = new AbortController().signal;
  assert.deepEqual(await api.getEventCalendar(params, { signal }), { ok: true });
  assert.equal(captured.url, "/events/calendar");
  assert.deepEqual(captured.params, params);
  assert.equal(captured.signal, signal);
});

test("manual refresh allows source collection to complete with one explicit POST", async () => {
  await api.refreshEventCalendar();
  assert.equal(captured.url, "/events/refresh");
  assert.equal(captured.method, "post");
  assert.equal(captured.timeout, 90_000);
  assert.deepEqual(JSON.parse(captured.data), {});
});

test("agent event configuration is read and saved through the dedicated endpoint", async () => {
  await api.getEventCalendarConfig();
  assert.equal(captured.url, "/events/config");
  assert.equal(captured.method, "get");
  const patch = { enabled: false, agents: { kline: false }, regions: [], top_k: 4 };
  await api.updateEventCalendarConfig(patch);
  assert.equal(captured.url, "/events/config");
  assert.equal(captured.method, "patch");
  assert.deepEqual(JSON.parse(captured.data), patch);
});

test("event detail escapes external IDs and retrieval preserves historical as-of", async () => {
  await api.getCalendarEvent("source/id with spaces");
  assert.equal(captured.url, "/events/source%2Fid%20with%20spaces");
  const params = { q: "inflation", as_of: "2026-10-24T08:00:00Z", agent: "research" };
  await api.searchCalendarEvents(params);
  assert.equal(captured.url, "/events/search");
  assert.deepEqual(captured.params, params);
});
