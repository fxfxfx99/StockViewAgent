import assert from "node:assert/strict";
import test from "node:test";
import axios from "axios";

let captured;
const originalAdapter = axios.defaults.adapter;
axios.defaults.adapter = async (config) => { captured = config; return { data: {}, status: 200, statusText: "OK", headers: {}, config }; };
const api = await import("../src/api.js");
axios.defaults.adapter = originalAdapter;

test("knowledge reads use bounded identifiers and cancellation rather than filesystem paths", async () => {
  const signal = new AbortController().signal;
  await api.getKnowledgeOverview({ signal });
  assert.equal(captured.url, "/data-center/knowledge/overview");
  assert.equal(captured.signal, signal);
  const params = { kind: "stock_features", q: "600519", offset: 20, limit: 20 };
  await api.getKnowledgeDocuments(params, { signal });
  assert.deepEqual(captured.params, params);
  assert.equal(captured.signal, signal);
  await api.getKnowledgeDocument({ kind: "prompts", key: "stock_impact_analysis", revision_id: "rev1" }, { signal });
  assert.equal(captured.url, "/data-center/knowledge/document");
  assert.equal(captured.params.revision_id, "rev1");
  for (const [fn, suffix] of [[api.getKnowledgeHistory, "history"], [api.getKnowledgeEvents, "events"]]) {
    await fn({ kind: "prompts", key: "stock_impact_analysis", limit: 30 }, { signal });
    assert.equal(captured.url, `/data-center/knowledge/${suffix}`);
    assert.equal(captured.signal, signal);
    assert.equal(captured.params.limit, 30);
  }
});

test("rebuild enqueue and status polling are separate operations", async () => {
  await api.rebuildKnowledge({ scope: "stocks" });
  assert.equal(captured.url, "/data-center/knowledge/rebuild");
  assert.equal(captured.method, "post");
  assert.deepEqual(JSON.parse(captured.data), { scope: "stocks" });
  await api.getKnowledgeStatus();
  assert.equal(captured.url, "/data-center/knowledge/status");
  assert.equal(captured.method, "get");
});

test("deep history pagination reads only eight metadata records and opens the selected old revision", async () => {
  const signal = new AbortController().signal;
  await api.getKnowledgeHistory({ kind: "prompts", key: "stock_impact_analysis", offset: 3992, limit: 8 }, { signal });
  assert.equal(captured.url, "/data-center/knowledge/history");
  assert.deepEqual(captured.params, { kind: "prompts", key: "stock_impact_analysis", offset: 3992, limit: 8 });
  assert.equal(captured.signal, signal);
  await api.getKnowledgeDocument({ kind: "prompts", key: "stock_impact_analysis", revision_id: "old-revision-3993" }, { signal });
  assert.equal(captured.params.revision_id, "old-revision-3993");
});

test("prompt saves and activation retain concurrency revision and audit reason", async () => {
  const payload = { content: "条件\n证据", reason: "补充传导条件", expected_revision: "rev1" };
  await api.updateKnowledgePrompt("prompt/test", payload);
  assert.equal(captured.url, "/data-center/knowledge/prompts/prompt%2Ftest");
  assert.equal(captured.method, "put");
  assert.deepEqual(JSON.parse(captured.data), payload);
  await api.resetKnowledgePrompt("stock_impact_analysis", { reason: "还原默认", expected_revision: "rev1" });
  assert.equal(captured.url, "/data-center/knowledge/prompts/stock_impact_analysis/reset");
  const activation = { revision_id: "rev0", reason: "恢复已验证版本", expected_revision: "rev1" };
  await api.activateKnowledgePrompt("stock_impact_analysis", activation);
  assert.equal(captured.url, "/data-center/knowledge/prompts/stock_impact_analysis/activate");
  assert.deepEqual(JSON.parse(captured.data), activation);
  await api.getKnowledgePrompts();
  assert.equal(captured.url, "/data-center/knowledge/prompts");
});
