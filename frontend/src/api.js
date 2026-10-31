import axios from "axios";

/**
 * 单页 UI：股票列表、K 线、Transaction Agent、相关新闻解读。
 * 其余后端能力（量化、宏观等）仍保留在服务端，见 docs/DATA_SOURCES.md。
 */
function normalizeApiRoot() {
  const raw = (import.meta.env?.VITE_API_BASE_URL ?? "").trim().replace(/\/$/, "");
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
const authExpiredListeners = new Set();

let authToken = "";
try {
  authToken = localStorage.getItem(TOKEN_KEY) || "";
} catch {
  // 浏览器禁用持久存储时仍可使用当前页面内存中的凭据。
}

export function getAuthToken() {
  return authToken;
}

export function setAuthToken(token) {
  authToken = token ? String(token) : "";
  try {
    if (authToken) localStorage.setItem(TOKEN_KEY, authToken);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    // 与初始化保持一致，存储受限不应阻止 API 请求。
  }
}

export function onAuthExpired(listener) {
  authExpiredListeners.add(listener);
  return () => authExpiredListeners.delete(listener);
}

client.interceptors.request.use((config) => {
  if (authToken && !config.skipAuth) {
    config.headers.Authorization = `Bearer ${authToken}`;
  }
  return config;
});

client.interceptors.response.use(
  (response) => response,
  (error) => {
    // 旧账号仍在途的请求不能让后来登录的账号退出。
    const sentToken = error.config?.headers?.Authorization;
    if (error.response?.status === 401 && authToken && sentToken === `Bearer ${authToken}`) {
      for (const listener of authExpiredListeners) listener();
    }
    return Promise.reject(error);
  }
);

export function getApiErrorMessage(error) {
  if (!error?.response) {
    return error?.code === "ECONNABORTED"
      ? "请求超时，请稍后重试。"
      : "无法连接服务，请确认后端已启动。";
  }
  const data = error.response.data;
  if (typeof data?.detail === "string") return data.detail;
  if (typeof data?.message === "string") return data.message;
  return "请求失败，请重试。";
}

export async function getHealth() {
  const { data } = await client.get("/health", { timeout: 5_000 });
  return data;
}

export async function getMe() {
  const { data } = await client.get("/auth/me", { timeout: 10_000 });
  return data;
}

export async function getAuthConfig() {
  const { data } = await client.get("/auth/config", { timeout: 10_000, skipAuth: true });
  return data;
}

export async function login(credentials) {
  const { data } = await client.post("/auth/login", credentials, { skipAuth: true });
  return data;
}

export async function register(credentials) {
  const { data } = await client.post("/auth/register", credentials, { skipAuth: true });
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

/** Historical daily data only; current quote snapshots must never enter replay. */
export async function getKlineReplay(symbol, { start_date, end_date, refresh = false, signal } = {}) {
  const { data } = await client.get(`/kline-replay/${encodeURIComponent(symbol)}`, {
    params: { start_date, end_date, refresh }, signal,
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

export async function getSettingsIntegrations({ signal } = {}) {
  const { data } = await client.get("/settings/integrations", { signal });
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

export async function getEarningsReports(symbol, { signal } = {}) {
  const { data } = await client.get("/news/earnings", { params: { symbol }, signal, timeout: 20_000 });
  return data;
}

export async function refreshEarningsReports(symbol) {
  const { data } = await client.post("/news/earnings/refresh", { symbol }, { timeout: 20_000 });
  return data;
}

export async function getEarningsReport(id, { signal } = {}) {
  const { data } = await client.get(`/news/earnings/${encodeURIComponent(id)}`, { signal, timeout: 20_000 });
  return data;
}

export async function prepareEarningsReport(id) {
  const { data } = await client.post(`/news/earnings/${encodeURIComponent(id)}/prepare`, {}, { timeout: 20_000 });
  return data;
}

export async function analyzeEarningsReport(id, focus = "") {
  const { data } = await client.post(`/news/earnings/${encodeURIComponent(id)}/analyze`, { focus }, { timeout: 20_000 });
  return data;
}

export async function downloadEarningsPdf(id, { signal } = {}) {
  const { data } = await client.get(`/news/earnings/${encodeURIComponent(id)}/pdf`, { signal, responseType: "blob", timeout: 60_000 });
  return data;
}

export async function getMonitoringOverview({ signal, symbol } = {}) {
  const { data } = await client.get("/monitoring/overview", { signal, params: symbol ? { symbol } : undefined, timeout: 15_000 });
  return data;
}

export async function getLlmSettings({ signal } = {}) {
  const { data } = await client.get("/settings/llm", { signal, timeout: 15_000 });
  return data;
}

export async function saveMonitoringModel(body) {
  const { data } = await client.put("/monitoring/model", body, { timeout: 30_000 });
  return data;
}

export async function verifyMonitoringModel() {
  const { data } = await client.post("/monitoring/model/verify", {}, { timeout: 90_000 });
  return data;
}

export async function createMonitoringPlan(body) {
  const { data } = await client.post("/monitoring/plans", body, { timeout: 15_000 });
  return data;
}

export async function getMonitoringPlan(id, { signal } = {}) {
  const { data } = await client.get(`/monitoring/plans/${encodeURIComponent(id)}`, { signal, timeout: 15_000 });
  return data;
}

export async function updateMonitoringPlan(id, body) {
  const { data } = await client.patch(`/monitoring/plans/${encodeURIComponent(id)}`, body, { timeout: 15_000 });
  return data;
}

export async function runMonitoringPlan(id, kind = "observe") {
  const { data } = await client.post(`/monitoring/plans/${encodeURIComponent(id)}/runs`, { kind }, { timeout: 15_000 });
  return data;
}

export async function reviewMonitoringPlan(id, body) {
  const { data } = await client.post(`/monitoring/plans/${encodeURIComponent(id)}/reviews`, body, { timeout: 15_000 });
  return data;
}

export async function updateMonitoringNotification(id, isRead) {
  const { data } = await client.patch(`/monitoring/notifications/${encodeURIComponent(id)}`, { is_read: isRead }, { timeout: 15_000 });
  return data;
}

export async function updateMonitoringConfig(body) {
  const { data } = await client.patch("/monitoring/config", body, { timeout: 15_000 });
  return data;
}

export async function getAutopilotStatus({ signal } = {}) {
  const { data } = await client.get("/autopilot/status", { signal, timeout: 15000 });
  return data;
}

export async function getRuntimeOverview({ period = "today", signal } = {}) {
  const { data } = await client.get("/runtime/overview", {
    params: { period }, signal, timeout: 15_000,
  });
  return data;
}

export async function getLlmProviderPresets({ signal } = {}) {
  const { data } = await client.get("/settings/llm/presets", { signal, timeout: 15000 });
  return data;
}

export async function reconcileAutopilot() {
  const { data } = await client.post("/autopilot/reconcile", {}, { timeout: 30000 });
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

export async function getDataAssets({ signal } = {}) {
  const { data } = await client.get("/admin/data-assets", { signal, timeout: 60_000 });
  return data;
}

export async function refreshDataAssets() {
  const { data } = await client.post("/admin/data-assets/refresh", {}, { timeout: 60_000 });
  return data;
}

export async function getXueqiuCompany(symbol, { force = false } = {}) {
  const { data } = await client.get("/xueqiu/company", {
    params: { symbol, ...(force ? { force: true } : {}) },
    timeout: 120000,
  });
  return data;
}

export async function getXueqiuComments(symbol, { signal } = {}) {
  const { data } = await client.get("/xueqiu/comments", {
    params: { symbol },
    signal,
    timeout: 15_000,
  });
  return data;
}

export async function refreshXueqiuComments(symbol, mode = "latest") {
  const { data } = await client.post("/xueqiu/comments/refresh", { symbol, mode }, {
    timeout: 15_000,
  });
  return data;
}

export async function getNewsArchive(params) {
  const { data } = await client.get("/news/archive", { params });
  return data;
}

/** Read the background collector's archive without triggering collection. */
export async function getOverseasNews(params = {}, { signal } = {}) {
  const { data } = await client.get("/news/overseas", { params, signal, timeout: 15_000 });
  return data;
}

export async function getOverseasNewsStatus({ signal } = {}) {
  const { data } = await client.get("/news/overseas/status", { signal, timeout: 15_000 });
  return data;
}

export const NEWS_ANALYSIS_BATCH_SIZE = 3;
const NEWS_TASK_TIMEOUT_MS = 600_000;

export async function syncArchiveFeeds(symbols = null) {
  const { data } = await client.post(
    "/news/sync-archive-feeds",
    symbols ? { symbols, lookback_days: 30 } : { lookback_days: 30 },
    { timeout: 15_000 }
  );
  return data;
}

/** UI 分批分析；单条可能耗时约两分钟，调用方仍可显式传入后端支持的数量。 */
export async function analyzeNewsForSymbol(symbol, limit = NEWS_ANALYSIS_BATCH_SIZE, persist = true) {
  const { data } = await client.post(
    "/news/analyze-symbol-archive",
    { symbol, limit, persist, lookback_days: 30 },
    { timeout: NEWS_TASK_TIMEOUT_MS }
  );
  return data;
}

/** Local research jobs return immediately; progress is read from the queue. */
export async function getResearchOverview({ signal } = {}) {
  const { data } = await client.get("/research/overview", { signal, timeout: 15_000 });
  return data;
}

export async function startResearchRun(kind, payload = {}, idempotencyKey) {
  const { data } = await client.post("/research/runs", {
    kind, payload, ...(idempotencyKey ? { idempotency_key: idempotencyKey } : {}),
  }, { timeout: 15_000 });
  return data;
}

export async function getResearchJob(id, { signal } = {}) {
  const { data } = await client.get(`/research/jobs/${encodeURIComponent(id)}`, { signal, timeout: 15_000 });
  return data;
}

export async function getResearchExperiment(id, { signal } = {}) {
  const { data } = await client.get(`/research/experiments/${encodeURIComponent(id)}`, { signal, timeout: 30_000 });
  return data;
}

export async function getResearchStrategy(id, { signal } = {}) {
  const { data } = await client.get(`/research/strategies/${encodeURIComponent(id)}`, { signal, timeout: 15_000 });
  return data;
}

export async function getResearchSnapshot(id, { signal } = {}) {
  const { data } = await client.get(`/research/snapshots/${encodeURIComponent(id)}`, { signal, timeout: 30_000 });
  return data;
}

export async function createResearchStrategy(spec) {
  const { data } = await client.post("/research/strategies", { spec }, { timeout: 15_000 });
  return data;
}

export async function importResearchSnapshot(payload) {
  const { data } = await client.post("/research/import", payload, { timeout: 120_000 });
  return data;
}

export async function updateResearchConfig(patch) {
  const { data } = await client.patch("/research/config", patch, { timeout: 15_000 });
  return data;
}

/** Global economic events use Shanghai date boundaries; timestamps remain UTC. */
export async function getEventCalendar(params, { signal } = {}) {
  const { data } = await client.get("/events/calendar", { params, signal, timeout: 30_000 });
  return data;
}

export async function refreshEventCalendar() {
  const { data } = await client.post("/events/refresh", {}, { timeout: 90_000 });
  return data;
}

export async function getEventCalendarConfig({ signal } = {}) {
  const { data } = await client.get("/events/config", { signal, timeout: 15_000 });
  return data;
}

export async function updateEventCalendarConfig(patch) {
  const { data } = await client.patch("/events/config", patch, { timeout: 15_000 });
  return data;
}

export async function getCalendarEvent(id, { signal } = {}) {
  const { data } = await client.get(`/events/${encodeURIComponent(id)}`, { signal, timeout: 15_000 });
  return data;
}

export async function searchCalendarEvents(params, { signal } = {}) {
  const { data } = await client.get("/events/search", { params, signal, timeout: 30_000 });
  return data;
}

/** Administrator-only local data inventory, collection and reversible cache management. */
export async function getLocalDataOverview({ signal } = {}) {
  const { data } = await client.get("/data-center/inventory/summary", { signal, timeout: 60_000 });
  return data;
}

export async function getLocalDataFiles(params, { signal } = {}) {
  const { data } = await client.get("/data-center/files", { params, signal, timeout: 30_000 });
  return data;
}

export async function getCollectedDataRecords(params, { signal } = {}) {
  const { data } = await client.get("/data-center/records", { params, signal, timeout: 30_000 });
  return data;
}

export async function getCollectionConfig({ signal } = {}) {
  const { data } = await client.get("/data-center/config", { signal, timeout: 15_000 });
  return data;
}

export async function updateCollectionConfig(patch) {
  const { data } = await client.patch("/data-center/config", patch, { timeout: 15_000 });
  return data;
}

export async function getCollectionSources({ signal } = {}) {
  const { data } = await client.get("/data-center/sources", { signal, timeout: 15_000 });
  return data;
}

export async function getCollectionJobs(params = {}, { signal } = {}) {
  const { data } = await client.get("/data-center/jobs", { params, signal, timeout: 15_000 });
  return data;
}

export async function startDataCollection(sourceIds) {
  const { data } = await client.post("/data-center/collect", sourceIds?.length ? { source_ids: sourceIds } : {}, { timeout: 15_000 });
  return data;
}

export async function getLocalCachePolicy({ signal } = {}) {
  const { data } = await client.get("/data-center/cache/policy", { signal, timeout: 15_000 });
  return data;
}

export async function updateLocalCachePolicy(patch) {
  const { data } = await client.patch("/data-center/cache/policy", patch, { timeout: 15_000 });
  return data;
}

export async function previewLocalCacheCleanup() {
  const { data } = await client.post("/data-center/cleanup/preview", {}, { timeout: 60_000 });
  return data;
}

export async function applyLocalCacheCleanup(planId) {
  const { data } = await client.post("/data-center/cleanup/apply", { plan_id: planId }, { timeout: 60_000 });
  return data;
}

export async function getLocalDataTrash({ signal, offset = 0, limit = 20 } = {}) {
  const { data } = await client.get("/data-center/trash", { params: { offset, limit }, signal, timeout: 30_000 });
  return data;
}

export async function restoreLocalDataTrash(batchId) {
  const { data } = await client.post(`/data-center/trash/${encodeURIComponent(batchId)}/restore`, {}, { timeout: 60_000 });
  return data;
}

export async function deleteLocalDataTrash(batchId) {
  const { data } = await client.delete(`/data-center/trash/${encodeURIComponent(batchId)}`, { data: { confirm: true }, timeout: 60_000 });
  return data;
}

/** Personal research judgments and changes use archived evidence, without a model call. */
export async function getTrackingTheses({ signal } = {}) {
  const { data } = await client.get("/tracking/theses", { signal, timeout: 15_000 });
  return data;
}

export async function createTrackingThesis(payload) {
  const { data } = await client.post("/tracking/theses", payload, { timeout: 15_000 });
  return data;
}

export async function getTrackingThesis(id, { signal } = {}) {
  const { data } = await client.get(`/tracking/theses/${encodeURIComponent(id)}`, { signal, timeout: 15_000 });
  return data;
}

export async function updateTrackingThesis(id, payload) {
  const { data } = await client.patch(`/tracking/theses/${encodeURIComponent(id)}`, payload, { timeout: 15_000 });
  return data;
}

export async function reviewTrackingThesis(id, payload) {
  const { data } = await client.post(`/tracking/theses/${encodeURIComponent(id)}/reviews`, payload, { timeout: 15_000 });
  return data;
}

export async function getTrackingChanges({ signal } = {}) {
  const { data } = await client.get("/tracking/changes", { signal, timeout: 15_000 });
  return data;
}

export async function updateTrackingChange(id, patch) {
  const { data } = await client.patch(`/tracking/changes/${encodeURIComponent(id)}`, patch, { timeout: 15_000 });
  return data;
}

export async function refreshTracking() {
  const { data } = await client.post("/tracking/refresh", {}, { timeout: 60_000 });
  return data;
}

/** Market judgement reads remain independent from explicit, paid AI analysis. */
export async function getMacroIndices({ signal } = {}) {
  const { data } = await client.get("/macro/indices", { signal, timeout: 15_000 });
  return data;
}
export async function getMacroIndexKline(params, { signal } = {}) {
  const { data } = await client.get("/macro/index-kline", { params, signal, timeout: 120_000 });
  return data;
}
export async function getMacroCatalog({ signal } = {}) {
  const { data } = await client.get("/macro/catalog", { signal, timeout: 15_000 });
  return data;
}
export async function getMacroKline(seriesId, range = "1y", { signal } = {}) {
  const { data } = await client.get("/macro/kline", { params: { series_id: seriesId, range }, signal, timeout: 120_000 });
  return data;
}
export async function getMacroIndustryIndices({ signal } = {}) {
  const { data } = await client.get("/macro/sw-daily/indices", { signal, timeout: 120_000 });
  return data;
}
export async function getMacroIndustryKline(tsCode, range = "1y", { signal } = {}) {
  const { data } = await client.get("/macro/sw-daily/kline", { params: { ts_code: tsCode, range }, signal, timeout: 120_000 });
  return data;
}
export async function getMarketFundFlowDashboard({ signal } = {}) {
  const { data } = await client.get("/market/fund-flow/dashboard", { params: { limit: 40 }, signal, timeout: 120_000 });
  return data;
}
export async function getMarketNorthFlow({ signal } = {}) {
  const { data } = await client.get("/market/sentiment/north-flow", { params: { mode: "history" }, signal, timeout: 120_000 });
  return data;
}
export async function getMarketMargin({ signal } = {}) {
  const { data } = await client.get("/market/sentiment/margin", { signal, timeout: 120_000 });
  return data;
}
export async function getMarketHot({ signal } = {}) {
  const { data } = await client.get("/market/sentiment/hot", { signal, timeout: 120_000 });
  return data;
}
export async function getEtfCurrent(code, { signal } = {}) {
  const { data } = await client.get(`/market/etf/${encodeURIComponent(code)}/adata/current`, { signal, timeout: 60_000 });
  return data;
}
export async function getStockCapitalFlow(symbol, { signal } = {}) {
  const { data } = await client.get(`/market/stock/${encodeURIComponent(symbol)}/adata/capital-flow`, { signal, timeout: 120_000 });
  return data;
}
export async function getKlineLearningPrompt({ signal } = {}) {
  const { data } = await client.get("/kline-learning/prompt", { signal, timeout: 15_000 });
  return data;
}
export async function getKlineLearningMaterials({ signal } = {}) {
  const { data } = await client.get("/kline-learning/materials", { signal, timeout: 15_000 });
  return data;
}
export async function uploadKlineLearningMaterial(file) {
  const body = new FormData(); body.append("file", file); body.append("summary", "");
  const { data } = await client.post("/kline-learning/materials", body, { timeout: 60_000 });
  return data;
}
export async function deleteKlineLearningMaterial(id) {
  const { data } = await client.delete(`/kline-learning/materials/${encodeURIComponent(id)}`, { timeout: 15_000 });
  return data;
}
export async function analyzeKlineMarket(body) {
  const { data } = await client.post("/kline-learning/analyze-market", body, { timeout: 600_000 });
  return data;
}
export async function chatKlineMarket(body) {
  const { data } = await client.post("/kline-learning/market-chat", body, { timeout: 600_000 });
  return data;
}
export async function analyzeKlineStock(body) {
  const { data } = await client.post("/kline-learning/analyze", body, { timeout: 600_000 });
  return data;
}

// Account-owned question research, separate from the quantitative research API.
export async function getInvestmentOverview(params = {}, { signal } = {}) { const { data } = await client.get("/investment-research/overview", { params, signal }); return data; }
export async function getInvestmentDossier(id, { signal } = {}) { const { data } = await client.get(`/investment-research/dossiers/${encodeURIComponent(id)}`, { signal }); return data; }
export async function createInvestmentDossier(body) { const { data } = await client.post("/investment-research/dossiers", body); return data; }
export async function updateInvestmentDossier(id, body) { const { data } = await client.patch(`/investment-research/dossiers/${encodeURIComponent(id)}`, body); return data; }
export async function createInvestmentQuestion(id, body) { const { data } = await client.post(`/investment-research/dossiers/${encodeURIComponent(id)}/questions`, body); return data; }
export async function updateInvestmentQuestion(id, body) { const { data } = await client.patch(`/investment-research/questions/${encodeURIComponent(id)}`, body); return data; }
export async function deleteInvestmentQuestion(id, revision) { const { data } = await client.delete(`/investment-research/questions/${encodeURIComponent(id)}`, { params: { revision } }); return data; }
export async function createInvestmentMaterial(id, body) { const { data } = await client.post(`/investment-research/dossiers/${encodeURIComponent(id)}/materials`, body); return data; }
export async function uploadInvestmentMaterial(id, file, title = "") { const form = new FormData(); form.append("file", file); if (title) form.append("title", title); const { data } = await client.post(`/investment-research/dossiers/${encodeURIComponent(id)}/materials/upload`, form, { timeout: 120_000 }); return data; }
export async function getInvestmentMaterial(id, { signal } = {}) { const { data } = await client.get(`/investment-research/materials/${encodeURIComponent(id)}`, { signal }); return data; }
export async function deleteInvestmentMaterial(id) { const { data } = await client.delete(`/investment-research/materials/${encodeURIComponent(id)}`); return data; }
export async function importInvestmentMonitoring(id, monitor_run_id) { const { data } = await client.post(`/investment-research/dossiers/${encodeURIComponent(id)}/monitoring-updates`, { monitor_run_id }); return data; }
export async function generateInvestmentReport(id, focus) { const { data } = await client.post(`/investment-research/dossiers/${encodeURIComponent(id)}/generate`, { focus }); return data; }
export async function cancelInvestmentJob(id) { const { data } = await client.post(`/investment-research/jobs/${encodeURIComponent(id)}/cancel`, {}); return data; }
export async function getInvestmentReport(id, { signal } = {}) { const { data } = await client.get(`/investment-research/reports/${encodeURIComponent(id)}`, { signal }); return data; }
export async function adoptInvestmentReport(id, revision) { const { data } = await client.post(`/investment-research/reports/${encodeURIComponent(id)}/adopt`, { revision }); return data; }
export async function readInvestmentReport(id, is_read = true) { const { data } = await client.post(`/investment-research/reports/${encodeURIComponent(id)}/read`, { is_read }); return data; }
export async function getMonitoringRunContext(id, { signal } = {}) { const { data } = await client.get(`/monitoring/runs/${encodeURIComponent(id)}/context`, { signal }); return data; }
export async function handleMonitoringNotification(id, body) { const { data } = await client.post(`/monitoring/notifications/${encodeURIComponent(id)}/handling`, body); return data; }
export async function getStockFinancialHistory(symbol, { signal } = {}) { const { data } = await client.get(`/stocks/${encodeURIComponent(symbol)}/fundamentals/history`, { params: { limit: 12 }, signal }); return data; }
export async function prepareInvestmentMaterial(id) { const { data } = await client.post(`/investment-research/materials/${encodeURIComponent(id)}/prepare`, {}); return data; }

// Merrill clock: shared observations; account-owned analysis and preferences.
export async function getMerrillOverview(region = "US", { signal } = {}) {
  const { data } = await client.get("/merrill-clock", { params: { region }, signal });
  return data;
}
export async function getMerrillSeries(ids, years = 10, { signal } = {}) {
  const { data } = await client.get("/merrill-clock/series", { params: { ids: ids.join(","), years }, signal });
  return data;
}
export async function refreshMerrillData() {
  const { data } = await client.post("/merrill-clock/refresh", {});
  return data;
}
export async function analyzeMerrillClock(region = "US") {
  const { data } = await client.post("/merrill-clock/analyze", { region });
  return data;
}
export async function updateMerrillPreferences(autoAnalysis) {
  const { data } = await client.put("/merrill-clock/preferences", { auto_analysis: autoAnalysis });
  return data;
}

export async function getCollectionStatus({ signal } = {}) { const { data } = await client.get("/data-center/collection/status", { signal, timeout: 15_000 }); return data; }
