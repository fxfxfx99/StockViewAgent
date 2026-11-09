const RECONNECT_DELAY_MS = 1_000;

/** Keep this document attached to the optional local launcher, outside React's lifecycle. */
export function startLocalSession({
  token = "",
  windowObject = globalThis.window,
  WebSocketClass = windowObject?.WebSocket,
  setTimeoutFn = globalThis.setTimeout,
  clearTimeoutFn = globalThis.clearTimeout,
} = {}) {
  if (!token || !windowObject || !WebSocketClass) return () => {};

  const url = new URL("/api/local-session/ws", windowObject.location.href);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.searchParams.set("token", token);

  let socket = null;
  let reconnectTimer = null;
  let suspended = false;
  let disposed = false;

  const cancelReconnect = () => {
    if (reconnectTimer !== null) clearTimeoutFn(reconnectTimer);
    reconnectTimer = null;
  };

  const disconnect = () => {
    if (!socket) return;
    const connection = socket;
    socket = null;
    connection.onclose = null;
    connection.onerror = null;
    connection.close();
  };

  const reconnect = () => {
    if (disposed || suspended || reconnectTimer !== null) return;
    reconnectTimer = setTimeoutFn(() => {
      reconnectTimer = null;
      connect();
    }, RECONNECT_DELAY_MS);
  };

  function connect() {
    if (disposed || suspended || socket) return;
    cancelReconnect();
    try {
      const connection = new WebSocketClass(url.href);
      socket = connection;
      const onDisconnect = () => {
        if (socket !== connection) return;
        disconnect();
        reconnect();
      };
      connection.onclose = onDisconnect;
      connection.onerror = onDisconnect;
    } catch {
      reconnect();
    }
  }

  const onPageHide = () => {
    suspended = true;
    cancelReconnect();
    disconnect();
  };
  const onPageShow = () => {
    suspended = false;
    connect();
  };

  windowObject.addEventListener("pagehide", onPageHide);
  windowObject.addEventListener("pageshow", onPageShow);
  connect();

  return () => {
    disposed = true;
    cancelReconnect();
    disconnect();
    windowObject.removeEventListener("pagehide", onPageHide);
    windowObject.removeEventListener("pageshow", onPageShow);
  };
}
