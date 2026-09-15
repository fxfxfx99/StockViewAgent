import axios from "axios";

/**
 * 单页 UI：股票列表、K 线、Transaction Agent、相关新闻解读。
 * 其余后端能力（量化、宏观等）仍保留在服务端，见 docs/DATA_SOURCES.md。
 */
function normalizeApiRoot() {
  const raw = (import.meta.env.VITE_API_BASE_URL ?? "").trim().replace(/\/$/, "");
  if (!raw) return "/api";
  if (raw.endsWith("/api")) return raw;
  if (/^https?:\/\/[^/?#]+(:\d+)?$/i.test(raw)) return `${raw}/api`;
  if (/^https?:\/\//i.test(raw)) return raw;
  return raw;
}

const apiRoot = normalizeApiRoot();

const client = axios.create({
  baseURL: apiRoot,
  timeout: 120000,
});

const TOKEN_KEY = "sva_token";

let authToken = typeof localStorage !== "undefined" ? localStorage.getItem(TOKEN_KEY) || "" : "";

export function getAuthToken() {
  return authToken;
}

export function setAuthToken(token) {
  authToken = token ? String(token) : "";
  if (typeof localStorage !== "undefined") {
    if (authToken) localStorage.setItem(TOKEN_KEY, authToken);
    else localStorage.removeItem(TOKEN_KEY);
  }
}

client.interceptors.request.use((config) => {
  if (authToken) {
    config.headers.Authorization = `Bearer ${authToken}`;
  }
  return config;
});

export function getApiErrorMessage(error) {
  if (!error?.response) {
    return error?.code === "ECONNABORTED"
      ? "请求超时，请稍后重试。"
      : "无法连接服务，请确认后端已启动。";
  }
  const data = error.response.data;
  if (typeof data?.detail === "string") return data.detail;
  if (typeof data?.message === "string") return data.message;
  return "请求未完成，请检查配置或稍后重试。";
}

export async function getHealth() {
  const { data } = await client.get("/health", { timeout: 5_000 });
  return data;
}

export async function getMe() {
  const { data } = await client.get("/auth/me", { timeout: 10_000 });
  return data;
}

export async function getWatchlist() {
  const { data } = await client.get("/watchlist");
  return data;
}

export async function saveWatchlist(symbols) {
  const { data } = await client.put("/watchlist", { symbols });
  return data;
}

export async function parseWatchlistImport(text, merge = true) {
  const { data } = await client.post("/watchlist/parse-import", { text, merge });
  return data;
}

export async function parseWatchlistImportXlsx(file, merge = true) {
  const fd = new FormData();
  fd.append("file", file);
  fd.append("merge", merge ? "true" : "false");
  const { data } = await client.post("/watchlist/parse-import-xlsx", fd, {
    timeout: 120000,
  });
  return data;
}

export async function getWatchlistProfiles() {
  const { data } = await client.get("/watchlist/profiles");
  return data;
}

export async function getWatchlistPriceContext() {
  const { data } = await client.get("/watchlist/price-context", { timeout: 180000 });
  return data;
}

export async function quickRefreshWatchlistProfiles(symbols) {
  const { data } = await client.post(
    "/watchlist/profiles/quick-refresh",
    { symbols: symbols ?? null },
    { timeout: 120000 }
  );
  return data;
}

export async function refreshWatchlistProfiles(symbols) {
  const { data } = await client.post(
    "/watchlist/profiles/refresh",
    { symbols: symbols ?? null },
    { timeout: 600_000 }
  );
  return data;
}

export async function patchWatchlistProfileManual(payload) {
  const { data } = await client.patch("/watchlist/profiles/manual", payload);
  return data;
}

export async function getWatchlistStockDetail(symbol) {
  const { data } = await client.get("/watchlist/stock-detail", {
    params: { symbol },
    timeout: 180000,
  });
  return data;
}

export async function getKline(symbol, rangeParam = "1y", interval = "1d") {
  const { data } = await client.get(`/market/kline/${encodeURIComponent(symbol)}`, {
    params: { range: rangeParam, interval },
  });
  return data;
}

export async function getTransactionAgentViews(symbol, strategy, asOf) {
  const params = {};
  if (strategy) params.strategy = strategy;
  if (asOf) params.as_of = asOf;
  const { data } = await client.get(`/transaction-agent/views/${encodeURIComponent(symbol)}`, {
    params,
    timeout: 180000,
  });
  return data;
}

export async function getStocksMeta() {
  const { data } = await client.get("/stocks/meta");
  return data;
}

export async function refreshStocksList() {
  const { data } = await client.post("/stocks/refresh");
  return data;
}

export async function rebuildStocksUniverseFromUpload() {
  const { data } = await client.post("/stocks/universe/rebuild", null, { timeout: 600_000 });
  return data;
}

export async function searchStocks(q, limit = 15) {
  const { data } = await client.get("/stocks/search", {
    params: { q, limit },
    timeout: 600_000,
  });
  return data;
}

export async function getSettingsIntegrations() {
  const { data } = await client.get("/settings/integrations");
  return data;
}

export async function putSettingsIntegrations(body) {
  const { data } = await client.put("/settings/integrations", body);
  return data;
}

export async function getSetupStatus() {
  const { data } = await client.get("/settings/setup-status", { timeout: 15000 });
  return data;
}

export async function testKlineshareIntegration() {
  const { data } = await client.post("/settings/integrations/test-klineshare", {}, { timeout: 30000 });
  return data;
}

export async function testTushareIntegration() {
  const { data } = await client.post("/settings/integrations/test-tushare", {}, { timeout: 30000 });
  return data;
}

export async function getDataCenter() {
  const { data } = await client.get("/settings/data-center", { timeout: 60000 });
  return data;
}

export async function refreshDataCenter() {
  const { data } = await client.post("/settings/data-center/refresh", {}, { timeout: 30000 });
  return data;
}

export async function getXueqiuCompany(symbol) {
  const { data } = await client.get("/xueqiu/company", {
    params: { symbol },
    timeout: 120000,
  });
  return data;
}

export async function getNewsArchive(params) {
  const { data } = await client.get("/news/archive", { params });
  return data;
}

export async function syncArchiveFeeds(symbols = null) {
  const { data } = await client.post(
    "/news/sync-archive-feeds",
    symbols ? { symbols, lookback_days: 30 } : { lookback_days: 30 },
    { timeout: 180000 }
  );
  return data;
}

export async function analyzeNewsForSymbol(symbol, limit = 20, persist = true) {
  const { data } = await client.post(
    "/news/analyze-symbol-archive",
    { symbol, limit, persist, lookback_days: 30 },
    { timeout: 180000 }
  );
  return data;
}
