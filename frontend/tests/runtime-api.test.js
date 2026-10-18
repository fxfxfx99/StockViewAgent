import assert from "node:assert/strict";
import test from "node:test";
import axios from "axios";

let requests = [];
const originalAdapter = axios.defaults.adapter;
axios.defaults.adapter = async (config) => {
  requests.push(config);
  return { data: { period: config.params.period }, status: 200, statusText: "OK", headers: {}, config };
};
const api = await import("../src/api.js");
axios.defaults.adapter = originalAdapter;

test("runtime overview uses authenticated, cancellable period queries", async () => {
  requests = [];
  api.setAuthToken("runtime-account-fixture");
  const signal = new AbortController().signal;
  for (const period of ["today", "7d", "all"]) {
    assert.deepEqual(await api.getRuntimeOverview({ period, signal }), { period });
  }
  assert.deepEqual(requests.map((request) => request.params.period), ["today", "7d", "all"]);
  assert.ok(requests.every((request) => request.method === "get" && request.url === "/runtime/overview"
    && request.signal === signal && request.headers.Authorization === "Bearer runtime-account-fixture"));
  api.setAuthToken("");
});

test("runtime overview defaults to today without leaking a previous account token", async () => {
  requests = [];
  await api.getRuntimeOverview();
  assert.equal(requests[0].params.period, "today");
  assert.equal(requests[0].headers.Authorization, undefined);
});
