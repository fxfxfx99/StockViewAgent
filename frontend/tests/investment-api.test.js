import assert from "node:assert/strict";
import test from "node:test";
import axios from "axios";
let captured;
const adapter = axios.defaults.adapter;
axios.defaults.adapter = async (config) => { captured = config; return { data: {}, status: 200, statusText: "OK", headers: {}, config }; };
const api = await import("../src/api.js");
axios.defaults.adapter = adapter;

test("research overview, monitor context and local finance use GET with cancellation", async () => {
  const signal = new AbortController().signal;
  await api.getInvestmentOverview({ symbol: "600519.SS" }, { signal });
  assert.equal(captured.url, "/investment-research/overview"); assert.equal(captured.method, "get"); assert.equal(captured.signal, signal); assert.equal(captured.params.symbol, "600519.SS");
  await api.getMonitoringOverview({ symbol: "600519.SS", signal }); assert.equal(captured.params.symbol, "600519.SS");
  await api.getMonitoringRunContext("r/1", { signal }); assert.equal(captured.url, "/monitoring/runs/r%2F1/context"); assert.equal(captured.method, "get");
  await api.getStockFinancialHistory("600519.SS", { signal }); assert.equal(captured.url, "/stocks/600519.SS/fundamentals/history"); assert.equal(captured.params.limit, 12);
});
test("explicit generate, read, adoption and monitoring handoff have separate requests", async () => {
  await api.generateInvestmentReport("d1", "现金流"); assert.equal(captured.url, "/investment-research/dossiers/d1/generate"); assert.deepEqual(JSON.parse(captured.data), { focus: "现金流" });
  await api.readInvestmentReport("r1", false); assert.deepEqual(JSON.parse(captured.data), { is_read: false });
  await api.adoptInvestmentReport("r1", 4); assert.equal(captured.url, "/investment-research/reports/r1/adopt"); assert.deepEqual(JSON.parse(captured.data), { revision: 4 });
  await api.importInvestmentMonitoring("d1", "run1"); assert.equal(captured.url, "/investment-research/dossiers/d1/monitoring-updates"); assert.deepEqual(JSON.parse(captured.data), { monitor_run_id: "run1" });
  await api.handleMonitoringNotification("n1", { action: "research", reason: "复核新增证据", dossier_id: "d1", run_id: "run1" }); assert.equal(captured.url, "/monitoring/notifications/n1/handling"); assert.equal(JSON.parse(captured.data).reason, "复核新增证据");
  await api.prepareInvestmentMaterial("m1"); assert.equal(captured.url, "/investment-research/materials/m1/prepare");
});
