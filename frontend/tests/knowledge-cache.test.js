import assert from "node:assert/strict";
import test from "node:test";
import { knowledgeKey, knowledgeParams, knowledgeTask, promptPayload } from "../src/knowledgeCacheViewModel.js";

test("knowledge inventory is paginated on the server and resets invalid page sizes", () => {
  assert.deepEqual(knowledgeParams({ kind: "stock_features", q: "  600519  ", page: 3, pageSize: 20 }), { kind: "stock_features", q: "600519", offset: 40, limit: 20 });
  assert.deepEqual(knowledgeParams({ page: -1, pageSize: 5568 }), { offset: 0, limit: 20 });
  assert.notDeepEqual(knowledgeKey(1, "documents"), knowledgeKey(2, "documents"));
});

test("queued rebuilds are not reported as complete, and job progress remains bounded", () => {
  const queued = knowledgeTask({ job: { id: "job1", status: "queued", total: 5568, completed: 0 } });
  assert.equal(queued.running, true);
  assert.equal(queued.type, "info");
  assert.equal(queued.percent, 0);
  assert.equal(queued.label, "重建任务已排队");
  const running = knowledgeTask({ active_job: { status: "running", total: 5568, completed: 2784 } });
  assert.equal(running.percent, 50);
  const completed = knowledgeTask({ active_job: null, recent_jobs: [{ status: "completed", total: 10, completed: 11 }] });
  assert.equal(completed.percent, 100);
  assert.equal(completed.running, false);
  assert.equal(completed.type, "success");
  assert.equal(knowledgeTask({ active_job: null, recent_jobs: [] }), null);
});

test("partial failures stay visible and internal errors are not used for labels", () => {
  const partial = knowledgeTask({ active_job: null, recent_jobs: [{ status: "partial", failed: 2, completed: 8, total: 10 }] });
  assert.equal(partial.type, "warning");
  assert.equal(partial.failed, 2);
  const failed = knowledgeTask({ job: { status: "failed", error_code: "internal_private_path" } });
  assert.equal(failed.label, "重建未完成");
  assert.equal(failed.percent, null);
});

test("prompt mutations require a reason and optimistic concurrency without losing formatting", () => {
  assert.throws(() => promptPayload("prompt", " ", "revision1"), /原因/);
  assert.throws(() => promptPayload("prompt", "因".repeat(401), "revision1"), /400/);
  assert.throws(() => promptPayload("prompt", "更新", undefined), /版本/);
  assert.throws(() => promptPayload("  ", "更新", "revision1"), /不能为空/);
  assert.throws(() => promptPayload("a".repeat(20001), "更新", "revision1"), /20,000/);
  assert.deepEqual(promptPayload(" 保留\n换行 ", "  补充条件 ", "revision1"), { content: " 保留\n换行 ", reason: "补充条件", expected_revision: "revision1" });
  assert.deepEqual(promptPayload(undefined, "回滚", "revision1"), { reason: "回滚", expected_revision: "revision1" });
});
