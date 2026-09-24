import assert from "node:assert/strict";
import test from "node:test";
import axios from "axios";

let handleRequest;
const originalAdapter = axios.defaults.adapter;
axios.defaults.adapter = (config) => handleRequest(config);
const api = await import("../src/api.js");
axios.defaults.adapter = originalAdapter;

function unauthorized(config) {
  return Promise.reject(new axios.AxiosError("Unauthorized", "ERR_BAD_REQUEST", config, null, {
    status: 401, statusText: "Unauthorized", data: { detail: "请重新登录" }, headers: {}, config,
  }));
}

test("public authentication endpoints never send an existing account token", async () => {
  api.setAuthToken("previous-account-token");
  const captured = [];
  handleRequest = async (config) => {
    captured.push(config);
    return { data: {}, status: 200, statusText: "OK", headers: {}, config };
  };
  await api.getAuthConfig();
  await api.login({ username: "tester", password: "long-password" });
  await api.register({ username: "tester", password: "long-password" });
  assert.deepEqual(captured.map((config) => config.url), ["/auth/config", "/auth/login", "/auth/register"]);
  for (const config of captured) assert.equal(config.headers.Authorization, undefined);
  assert.deepEqual(JSON.parse(captured[2].data), { username: "tester", password: "long-password" });
  api.setAuthToken("");
});

test("an authenticated 401 expires the active session and listeners can unsubscribe", async () => {
  api.setAuthToken("active-account-token");
  let expirationCount = 0;
  const unsubscribe = api.onAuthExpired(() => { expirationCount += 1; });
  handleRequest = unauthorized;
  await assert.rejects(api.getWatchlist());
  assert.equal(expirationCount, 1);
  unsubscribe();
  await assert.rejects(api.getWatchlist());
  assert.equal(expirationCount, 1);
  api.setAuthToken("");
});

test("failed login and unauthenticated responses do not expire another session", async () => {
  let expirationCount = 0;
  const unsubscribe = api.onAuthExpired(() => { expirationCount += 1; });
  handleRequest = unauthorized;
  try {
    api.setAuthToken("active-account-token");
    await assert.rejects(api.login({ username: "wrong", password: "wrong" }));
    api.setAuthToken("");
    await assert.rejects(api.getMe());
    assert.equal(expirationCount, 0);
  } finally {
    unsubscribe();
    api.setAuthToken("");
  }
});

test("a delayed 401 from the previous account cannot expire the next account", async () => {
  let expirationCount = 0;
  const unsubscribe = api.onAuthExpired(() => { expirationCount += 1; });
  let deliverFailure;
  let requestStarted;
  const started = new Promise((resolve) => { requestStarted = resolve; });
  handleRequest = (config) => new Promise((resolve, reject) => {
    deliverFailure = () => unauthorized(config).catch(reject);
    requestStarted();
  });
  try {
    api.setAuthToken("old-account-token");
    const oldRequest = api.getWatchlist();
    await started;
    api.setAuthToken("new-account-token");
    deliverFailure();
    await assert.rejects(oldRequest);
    assert.equal(expirationCount, 0);
    assert.equal(api.getAuthToken(), "new-account-token");
  } finally {
    unsubscribe();
    api.setAuthToken("");
  }
});
