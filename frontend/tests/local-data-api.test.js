import assert from "node:assert/strict";
import test from "node:test";
import axios from "axios";

let requests = [];
const originalAdapter = axios.defaults.adapter;
axios.defaults.adapter = async (config) => {
  requests.push(config);
  return { data: { totals: { files: 0, size_bytes: 0 } }, status: 200, statusText: "OK", headers: {}, config };
};
const api = await import("../src/api.js");
axios.defaults.adapter = originalAdapter;

test("local data inventory uses the authenticated admin endpoint and cancellation", async () => {
  requests = [];
  api.setAuthToken("local-data-account");
  const signal = new AbortController().signal;
  await api.getDataAssets({ signal });
  assert.equal(requests[0].url, "/admin/data-assets");
  assert.equal(requests[0].method, "get");
  assert.equal(requests[0].signal, signal);
  assert.equal(requests[0].headers.Authorization, "Bearer local-data-account");
  api.setAuthToken("");
});

test("refreshing the index never invokes remote collection or sends a destination path", async () => {
  requests = [];
  await api.refreshDataAssets();
  assert.equal(requests[0].url, "/admin/data-assets/refresh");
  assert.equal(requests[0].method, "post");
  assert.deepEqual(JSON.parse(requests[0].data), {});
  assert.equal(requests[0].headers.Authorization, undefined);
});
