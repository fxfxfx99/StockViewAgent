import assert from "node:assert/strict";
import test from "node:test";
import { knowledgeDocumentPayload, strategyCatalogKey, strategyRagPayload, strategyRetrievalSummary, strategyTab, strategyWorkspaceKey, strategyWorkspacePath } from "../src/strategyWorkspaceViewModel.js";

test("strategy knowledge caches isolate accounts and strategies", () => {
  assert.notDeepEqual(strategyWorkspaceKey(1, "trend"), strategyWorkspaceKey(2, "trend"));
  assert.notDeepEqual(strategyWorkspaceKey(1, "trend"), strategyWorkspaceKey(1, "value"));
  assert.deepEqual(strategyCatalogKey(1).slice(0, 2), strategyWorkspaceKey(1));
  assert.equal(strategyWorkspacePath("trend/value", "knowledge"), "/strategies/trend%2Fvalue?tab=knowledge");
  assert.equal(strategyTab("unexpected"), "overview");
});

test("knowledge submissions preserve content, normalize metadata, and reject unsafe sources", () => {
  const values = { title: "  更新判断  ", content: " 原文\n保留换行 ", tags: "宏观，政策,宏观", source_url: " https://example.com/report ", published_at: "2026-11-01" };
  assert.deepEqual(knowledgeDocumentPayload(values), { title: "更新判断", content: values.content, tags: ["宏观", "政策"], source_url: "https://example.com/report", published_at: "2026-11-01" });
  assert.equal(knowledgeDocumentPayload({ title: "笔记", content: "正文" }).published_at, null);
  for (const source_url of ["javascript:alert(1)", "file:///etc/passwd", "https://", "https://user:password@example.com"]) assert.throws(() => knowledgeDocumentPayload({ ...values, source_url }), /网址/);
  assert.throws(() => knowledgeDocumentPayload({ ...values, content: "  " }), /正文/);
  assert.throws(() => knowledgeDocumentPayload({ ...values, published_at: "2026-02-30" }), /日期/);
  assert.throws(() => knowledgeDocumentPayload({ ...values, published_at: "2026-99-99" }), /日期/);
  assert.throws(() => knowledgeDocumentPayload({ ...values, tags: "长".repeat(41) }), /标签/);
});

test("RAG controls retain disabled states and enforce supported server limits", () => {
  const values = { enabled: false, top_k: 5, max_age_days: null, include_builtin: false, prefer_recent: true };
  assert.deepEqual(strategyRagPayload(values), values);
  assert.equal(strategyRagPayload({ ...values, max_age_days: "30" }).max_age_days, 30);
  for (const top_k of [null, 0, 21, 2.5]) assert.throws(() => strategyRagPayload({ ...values, top_k }), /检索条数/);
  for (const max_age_days of [0, 3651, 1.5]) assert.throws(() => strategyRagPayload({ ...values, max_age_days }), /资料时效/);
});

test("retrieval evidence distinguishes stale exclusions from future evidence", () => {
  assert.equal(strategyRetrievalSummary(null), null);
  assert.deepEqual(strategyRetrievalSummary({ knowledge_version: "kb-2", retrieved_count: 2, candidate_count: 5, stale_count: 1, excluded_after_cutoff_count: 2, as_of: "2026-10-01" }), { version: "kb-2", retrieved: 2, candidates: 5, stale: 1, afterCutoff: 2, asOf: "2026-10-01" });
  assert.equal(strategyRetrievalSummary({ as_of: null, retrieved_at: "2026-11-08T12:00:00Z" }).asOf, "2026-11-08T12:00:00Z");
  assert.equal(strategyRetrievalSummary({ as_of: "2026-10-01", retrieved_at: "2026-11-08T12:00:00Z" }).asOf, "2026-10-01");
});
