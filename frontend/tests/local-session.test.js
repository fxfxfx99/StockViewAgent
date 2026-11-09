import assert from "node:assert/strict";
import test from "node:test";
import { startLocalSession } from "../src/localSession.js";

function createClient({ token = "local token", protocol = "http:", failConstruction = false } = {}) {
  const listeners = new Map();
  const sockets = [];
  const timers = new Map();
  let timerId = 0;
  const windowObject = {
    location: { href: `${protocol}//127.0.0.1:5175/today` },
    addEventListener(name, listener) {
      if (!listeners.has(name)) listeners.set(name, new Set());
      listeners.get(name).add(listener);
    },
    removeEventListener(name, listener) {
      listeners.get(name)?.delete(listener);
    },
  };
  class FakeWebSocket {
    constructor(url) {
      if (failConstruction) throw new Error("socket unavailable");
      this.url = url;
      this.closed = false;
      sockets.push(this);
    }
    close() {
      this.closed = true;
      this.onclose?.();
    }
  }
  const dispose = startLocalSession({
    token,
    windowObject,
    WebSocketClass: FakeWebSocket,
    setTimeoutFn(callback, delay) {
      const id = ++timerId;
      timers.set(id, { callback, delay });
      return id;
    },
    clearTimeoutFn(id) { timers.delete(id); },
  });
  return {
    sockets,
    timers,
    listeners,
    dispose,
    emit(name) { listeners.get(name)?.forEach((listener) => listener()); },
    runTimers() {
      const pending = [...timers.values()];
      timers.clear();
      pending.forEach(({ callback }) => callback());
    },
  };
}

test("a document connects once with the launcher token and needs no heartbeat timer", () => {
  const client = createClient();
  const url = new URL(client.sockets[0].url);
  assert.equal(url.protocol, "ws:");
  assert.equal(url.pathname, "/api/local-session/ws");
  assert.equal(url.searchParams.get("token"), "local token");
  assert.equal(client.timers.size, 0);
  client.emit("pageshow");
  client.emit("pageshow");
  assert.equal(client.sockets.length, 1);
  client.dispose();
});

test("a dropped connection retries once after one second, including error/close races", () => {
  const client = createClient();
  const first = client.sockets[0];
  const staleClose = first.onclose;
  first.onerror();
  staleClose();
  assert.equal(first.closed, true);
  assert.equal(client.timers.size, 1);
  assert.equal([...client.timers.values()][0].delay, 1_000);
  client.runTimers();
  assert.equal(client.sockets.length, 2);
  assert.equal(client.timers.size, 0);
  staleClose();
  assert.equal(client.timers.size, 0);
  client.sockets[1].onclose();
  client.runTimers();
  assert.equal(client.sockets.length, 3);
  client.dispose();
});

test("pagehide disconnects and pageshow restores a page from the back-forward cache", () => {
  const client = createClient();
  client.emit("pagehide");
  assert.equal(client.sockets[0].closed, true);
  assert.equal(client.timers.size, 0);
  client.emit("pageshow");
  assert.equal(client.sockets.length, 2);
  client.sockets[1].onclose();
  assert.equal(client.timers.size, 1);
  client.emit("pagehide");
  assert.equal(client.timers.size, 0);
  client.runTimers();
  assert.equal(client.sockets.length, 2);
  client.emit("pageshow");
  assert.equal(client.sockets.length, 3);
  client.dispose();
});

test("separate documents retain independent connections even with the same token", () => {
  const first = createClient();
  const second = createClient();
  assert.notEqual(first.sockets[0], second.sockets[0]);
  first.emit("pagehide");
  assert.equal(first.sockets[0].closed, true);
  assert.equal(second.sockets[0].closed, false);
  first.dispose();
  second.dispose();
});

test("without a launcher token or browser, no connection or listeners are created", () => {
  const client = createClient({ token: "" });
  assert.equal(client.sockets.length, 0);
  assert.equal(client.timers.size, 0);
  assert.equal(client.listeners.size, 0);
  client.dispose();
  assert.doesNotThrow(() => startLocalSession({ token: "token", windowObject: null })());
});

test("dispose cancels reconnects, closes the socket, and removes lifecycle listeners", () => {
  const client = createClient();
  const first = client.sockets[0];
  const staleClose = first.onclose;
  client.dispose();
  client.dispose();
  assert.equal(first.closed, true);
  assert.equal(client.listeners.get("pagehide").size, 0);
  assert.equal(client.listeners.get("pageshow").size, 0);
  client.emit("pageshow");
  staleClose();
  client.runTimers();
  assert.equal(client.sockets.length, 1);

  const retrying = createClient();
  retrying.sockets[0].onclose();
  retrying.dispose();
  assert.equal(retrying.timers.size, 0);
  retrying.runTimers();
  assert.equal(retrying.sockets.length, 1);
});

test("a socket constructor failure retries without interrupting the app", () => {
  const client = createClient({ failConstruction: true });
  assert.equal(client.sockets.length, 0);
  assert.equal(client.timers.size, 1);
  client.runTimers();
  assert.equal(client.timers.size, 1);
  client.dispose();
  assert.equal(client.timers.size, 0);
});

test("secure frontend origins use a secure WebSocket connection", () => {
  const client = createClient({ protocol: "https:" });
  assert.equal(new URL(client.sockets[0].url).protocol, "wss:");
  client.dispose();
});
