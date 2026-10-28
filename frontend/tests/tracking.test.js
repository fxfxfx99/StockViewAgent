import assert from "node:assert/strict";
import test from "node:test";
import { qk } from "../src/hooks/queryKeys.js";
import {
  appendEvidence, calendarEvidence, evidenceInputRef, isFutureReviewDate, newsEvidence, nextReviewDate,
  normalizeTrackingSymbol, partitionChanges, sortChanges, strategyEvidence, trackingError, trackingStatusNotices,
} from "../src/trackingViewModel.js";

test("all personal tracking reads and strategy views isolate users, symbols and history", () => {
  for (const key of [qk.tracking, qk.trackingTheses, qk.trackingChanges]) {
    assert.notDeepEqual(key(1), key(2));
    assert.notDeepEqual(key(0), key(undefined));
  }
  assert.notDeepEqual(qk.trackingThesis(1, "a"), qk.trackingThesis(2, "a"));
  assert.notDeepEqual(qk.trackingThesis(1, "a"), qk.trackingThesis(1, "b"));
  assert.notDeepEqual(qk.transactionAgentViews("600519.SS", null, 1), qk.transactionAgentViews("600519.SS", null, 2));
  assert.notDeepEqual(qk.transactionAgentViews("600519.SS", "2026-10-25", 1), qk.transactionAgentViews("600519.SS", null, 1));
});

test("first five changes prioritize due reviews and corrections, with source supplements kept separate", () => {
  const items = [
    ...Array.from({ length: 8 }, (_, index) => ({ id: `news${index}`, kind: "news", priority: 60, created_at: 100 + index })),
    { id: "review", kind: "review_due", priority: 100, created_at: 1 },
    { id: "correction", kind: "news", change_type: "news_correction", priority: 80, created_at: 2 },
    { id: "supplement", kind: "news", change_type: "source_supplement", priority: 10, created_at: 500 },
  ];
  const result = partitionChanges(items);
  assert.deepEqual(result.first.map((item) => item.id), ["review", "correction", "news7", "news6", "news5"]);
  assert.equal(result.rest.length, 5);
  assert.deepEqual(result.supplements.map((item) => item.id), ["supplement"]);
  assert.equal(result.total, 11);
  assert.equal(items[0].id, "news0");
  assert.equal(items.some((item) => item.is_read), false);
});

test("read, irrelevant and resolved changes leave pending view but remain available in all", () => {
  const items = [{ id: "read", is_read: true }, { id: "irrelevant", feedback: "irrelevant" }, { id: "resolved", resolved: true }, { id: "useful", feedback: "useful" }];
  assert.deepEqual(partitionChanges(items).first.map((item) => item.id), ["useful"]);
  assert.equal(partitionChanges(items, "all").total, 4);
  assert.deepEqual(sortChanges([{ id: "b", created_at: 4 }, { id: "a", created_at: 4 }]).map((item) => item.id), ["a", "b"]);
});

test("review defaults and validation follow Shanghai date, leap years and real calendar days", () => {
  const boundary = "2026-10-28T16:05:00Z";
  assert.equal(nextReviewDate(boundary), "2026-11-28");
  assert.equal(nextReviewDate("2024-02-28T08:00:00Z", 1), "2024-02-29");
  assert.equal(isFutureReviewDate("2026-10-29", boundary), false);
  assert.equal(isFutureReviewDate("2026-10-30", boundary), true);
  assert.equal(isFutureReviewDate("2026-02-30", "2026-01-01"), false);
  assert.equal(isFutureReviewDate("2026-13-01", boundary), false);
  assert.equal(isFutureReviewDate("", boundary), false);
});

test("manual stock entry normalizes canonical A-share codes without guessing unknown exchanges", () => {
  assert.equal(normalizeTrackingSymbol(" 600519.sh "), "600519.SS");
  assert.equal(normalizeTrackingSymbol("000001"), "000001.SZ");
  assert.equal(normalizeTrackingSymbol("830799"), "830799.BJ");
  assert.equal(normalizeTrackingSymbol("600519"), "600519.SS");
  for (const value of ["股票", "AAPL", "123456", "600519.US", "javascript:600519.SS"]) assert.equal(normalizeTrackingSymbol(value), "");
});

test("source references preserve versions, deduplicate exact links and avoid resending full archived bodies", () => {
  const source = { kind: "news", id: "7", revision_id: "r1", title: "title", snapshot: { text: "x".repeat(40_000) } };
  assert.deepEqual(evidenceInputRef(source), { kind: "news", id: "7", revision_id: "r1" });
  assert.deepEqual(newsEvidence({ id: 0, title: "zero-id" }), { kind: "news", id: "0", title: "zero-id" });
  assert.equal(newsEvidence({}), null);
  const event = calendarEvidence({ id: "event/id", revision_id: "r2", title: "发布安排" });
  assert.equal(event.revision_id, "r2");
  assert.equal(appendEvidence([source], source).length, 1);
  assert.equal(appendEvidence([source], { ...source, revision_id: "r2" }).length, 2);
  assert.equal(appendEvidence([source], event).length, 2);
  assert.equal(source.snapshot.text.length, 40_000);
});

test("strategy opinions retain their own horizon and inputs, exclude oversized financial documents", () => {
  const view = { strategy_id: "value", strategy: "价值", timeframe: "6–24个月", score: 70, confidence: .6, viewpoint: "等待业绩兑现", reasons: ["估值"], metadata: { model_version: "rules1", signal_inputs: { pe_ttm: 22, fundamentals: { financial: { giant: "x".repeat(50_000) }, mda_preview: "业务摘要", pe_ttm: 22 } } } };
  const evidence = strategyEvidence(view, { symbol: "600519.SS", as_of: "2026-10-24", source: { data_as_of: "2026-10-24", resources: { private_path: "/path" } } });
  assert.equal(evidence.kind, "strategy");
  assert.equal(evidence.snapshot.timeframe, "6–24个月");
  assert.equal(evidence.snapshot.as_of, "2026-10-24");
  assert.equal(evidence.snapshot.signal_inputs.fundamentals.pe_ttm, 22);
  assert.equal(evidence.snapshot.signal_inputs.fundamentals.financial, undefined);
  assert.equal(evidence.snapshot.source.resources, undefined);
  assert.equal(evidenceInputRef(evidence), evidence);
  assert.ok(new TextEncoder().encode(JSON.stringify(evidence.snapshot)).length < 32768);
  assert.equal(view.metadata.signal_inputs.fundamentals.financial.giant.length, 50_000);
});

test("automation stop, worker loss, source failure and pending coverage remain distinct", () => {
  assert.equal(trackingStatusNotices({ paused: true, automatic_enabled: true, worker: { healthy: true } }).length, 0);
  assert.equal(trackingStatusNotices({ calendar: { status: "not_linked", sources: [] } }).length, 0);
  assert.equal(trackingStatusNotices({ automatic_enabled: false })[0].title, "自动跟踪已停止");
  assert.equal(trackingStatusNotices({ worker: { healthy: false } })[0].key, "worker");
  const partial = trackingStatusNotices({ news: { status: "stale", pending: true, message: "来源过期" }, coverage: { status: "limited" } });
  assert.equal(partial.find((notice) => notice.key === "news").text, "来源过期");
  assert.ok(partial.some((notice) => notice.key === "news-pending"));
  assert.equal(partial.some((notice) => notice.key === "coverage"), false);
  assert.equal(trackingStatusNotices(undefined).length, 0);
});

test("concurrency and validation errors retain actionable recovery text", () => {
  assert.match(trackingError({ response: { status: 409 } }), /输入已保留/);
  assert.match(trackingError(new Error("offline")), /输入已保留/);
  assert.equal(trackingError({ response: { status: 422, data: { detail: [{ msg: "日期无效" }] } } }), "日期无效");
});
