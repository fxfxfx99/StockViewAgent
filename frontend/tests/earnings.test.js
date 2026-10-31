import assert from "node:assert/strict";
import test from "node:test";
import { analysisGate, earningsKey, extractionSummary, pdfPageUrl, physicalPage, visibleReports, warningText } from "../src/earningsViewModel.js";

test("report filters preserve revisions and prioritize full reports within the same period", () => {
  const reports = [{ id: "summary", report_year: 2025, report_type: "annual", report_period: "2025-12-31", document_kind: "summary", announcement_date: "2026-04-05" }, { id: "original", report_year: 2025, report_type: "annual", report_period: "2025-12-31", document_kind: "full_report", announcement_date: "2026-03-28" }, { id: "revision", report_year: 2025, report_type: "annual", report_period: "2025-12-31", document_kind: "full_report", announcement_date: "2026-04-05", is_revision: true }, { id: "q1", report_year: 2026, report_type: "q1", report_period: "2026-03-31", document_kind: "full_report" }];
  assert.deepEqual(visibleReports(reports, "2025", "annual").map((r) => r.id), ["revision", "original", "summary"]);
  assert.equal(visibleReports(reports)[0].id, "q1");
  assert.equal(reports[0].id, "summary");
});

test("interpretation gates distinguish document language, missing model, disabled worker, work in flight and daily quota", () => {
  const valid = { report: { document_kind: "full_report", language: "zh" }, model: { configured: true }, usage: { remaining: 12 }, workerEnabled: true };
  assert.equal(analysisGate(valid).allowed, true);
  for (const patch of [{ model: { configured: false } }, { workerEnabled: false }, { busy: true }, { usage: { remaining: 0 } }, { report: { document_kind: "summary" } }, { report: { document_kind: "full_report", is_interpretable: false } }, { report: { document_kind: "full_report", language: "en" } }]) assert.equal(analysisGate({ ...valid, ...patch }).allowed, false);
  assert.equal(analysisGate({ ...valid, usage: { remaining: null } }).allowed, true);
});

test("physical PDF references reject invalid page numbers and unsafe links", () => {
  assert.equal(pdfPageUrl("https://example.test/report.pdf#old", 8), "https://example.test/report.pdf#page=8");
  assert.equal(pdfPageUrl("javascript:alert(1)", 1), null);
  assert.equal(pdfPageUrl("https://example.test/report.pdf", 0), null);
  assert.equal(physicalPage(1.5), null);
  assert.equal(physicalPage("3"), 3);
});

test("accounts, symbols and document detail requests cannot share interpretation query keys", () => {
  assert.notDeepEqual(earningsKey(1, "600519.SS", "list"), earningsKey(2, "600519.SS", "list"));
  assert.notDeepEqual(earningsKey(1, "600519.SS", "detail", "a"), earningsKey(1, "000001.SZ", "detail", "a"));
});

test("partial extraction and missing OCR stay explicit", () => {
  assert.equal(extractionSummary(null), "尚未下载解析");
  assert.equal(extractionSummary({ status: "partial", total_pages: 120, text_pages: 85 }), "部分可读 · 85/120页含可提取文字");
  assert.equal(warningText("scanned_pages_no_ocr"), "发现扫描页，未执行OCR文字识别");
  assert.equal(warningText("未来警告"), "未来警告");
});
