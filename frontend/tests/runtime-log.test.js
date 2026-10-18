import assert from "node:assert/strict";
import test from "node:test";
import {
  RUNTIME_PERIODS, formatRuntimeCount, runtimeCount, runtimeOverviewView, runtimePollInterval,
  runtimeQueryKey, runtimeRecordView, runtimeTime, runtimeUnavailable,
} from "../src/runtimeLogViewModel.js";

test("runtime metrics distinguish real zero from unreported or invalid counts", () => {
  for (const input of [undefined, null, "", "0", false, -1, NaN, Infinity]) {
    assert.equal(runtimeCount(input), null);
    assert.equal(formatRuntimeCount(input), "—");
  }
  assert.equal(formatRuntimeCount(0), "0");
  assert.equal(formatRuntimeCount(12500), "1.3 万");
  assert.equal(formatRuntimeCount(12500, false), "12,500");
});

test("runtime query cache separates account and statistics period", () => {
  assert.notDeepEqual(runtimeQueryKey("a", "today"), runtimeQueryKey("b", "today"));
  assert.notDeepEqual(runtimeQueryKey("a", "today"), runtimeQueryKey("a", "all"));
  assert.deepEqual(RUNTIME_PERIODS.map((period) => period.value), ["today", "7d", "all"]);
});

test("unsupported trial routes stop background polling, other failures can recover", () => {
  for (const status of [403, 404]) {
    const error = { response: { status } };
    assert.equal(runtimeUnavailable(error), true);
    assert.equal(runtimePollInterval({ state: { error } }), false);
  }
  assert.equal(runtimePollInterval({ state: { error: { response: { status: 503 } } } }), 30_000);
  assert.equal(runtimePollInterval({ state: {} }), 30_000);
});

test("runtime timestamps use Shanghai time and reject missing timestamps", () => {
  assert.match(runtimeTime("2026-10-17T16:30:00Z"), /10.18.*00:30/);
  assert.equal(runtimeTime(undefined), "—");
  assert.equal(runtimeTime("not-a-date"), "—");
});

test("actual metrics never use budget reservations or infer missing values as zero", () => {
  const pending = runtimeOverviewView(undefined);
  assert.equal(pending.calls, null);
  assert.equal(pending.tokens, null);
  assert.equal(pending.collected, null);
  const budgetOnly = runtimeOverviewView({ budget: { used: { calls: 30, tokens: 150000 } } });
  assert.equal(budgetOnly.calls, null);
  assert.equal(budgetOnly.tokens, null);
  const empty = runtimeOverviewView({ llm: { calls: 0, total_tokens: 0, unknown_token_calls: 0 }, collection: { records_received: 0 }, recent: [] });
  assert.equal(empty.calls, 0);
  assert.equal(empty.tokens, 0);
  assert.equal(empty.collected, 0);
  assert.equal(empty.records.length, 0);
});

test("unknown model usage stays separate from reported tokens, including failed requests", () => {
  const partial = runtimeOverviewView({ llm: { calls: 3, total_tokens: 400, unknown_token_calls: 2, failed: 1, reported_token_calls: 1 } });
  assert.equal(partial.calls, 3);
  assert.equal(partial.tokens, 400);
  assert.equal(partial.unknownTokenCalls, 2);
  const unknown = runtimeOverviewView({ llm: { calls: 2, total_tokens: null, unknown_token_calls: 2 } });
  assert.equal(formatRuntimeCount(unknown.tokens), "—");
  assert.equal(unknown.unknownTokenCalls, 2);
  const record = runtimeRecordView({ kind: "llm", operation: "research_proposal", status: "failed", error_code: "timeout", total_tokens: null });
  assert.equal(record.statusLabel, "失败");
  assert.match(record.detail, /Token 未报告/);
  assert.match(record.detail, /请求超时/);
});

test("shared network collection counts remain distinct from newly stored records", () => {
  const view = runtimeOverviewView({
    collection: { requests: 2, records_received: 300, records_added: 15, shared_records_received: 200 },
    recent: [{ id: "public-fetch", kind: "collection", operation: "news", status: "success", shared: true, records_received: 200, records_added: 10, started_at: "2026-10-18T10:30:00+08:00" }],
  });
  assert.equal(view.collected, 300);
  assert.equal(view.facts.find((fact) => fact.label === "采集批次").value, "2");
  assert.equal(view.facts.find((fact) => fact.label === "新增 / 修订").value, "15");
  assert.equal(view.facts.find((fact) => fact.label === "共享采集记录").value, "200");
  assert.match(view.records[0].detail, /共享公共采集/);
  assert.match(view.records[0].detail, /获取 200 条/);
  assert.match(view.records[0].detail, /新增 \/ 修订 10 条/);
  assert.match(view.notice, /行情、新闻、公司资料和 GitHub/);
  assert.match(view.notice, /来源维护探测、股票搜索等未计入/);
});

test("unfinished requests remain pending and arbitrary raw failures are not rendered", () => {
  const record = runtimeRecordView({ kind: "llm", status: "unfinished" });
  assert.equal(record.statusLabel, "结果待确认");
  assert.match(record.detail, /超过 15 分钟/);
  const failed = runtimeRecordView({ kind: "llm", status: "failed", error_code: "private-key-value" });
  assert.doesNotMatch(failed.detail, /private-key-value/);
  assert.match(failed.detail, /请求未完成/);
});
