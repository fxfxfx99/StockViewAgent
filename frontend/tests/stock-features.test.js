import assert from "node:assert/strict";
import test from "node:test";
import { coverageGaps, directionText, factorExposures, featureAssessment, featureKey, featurePercent, featureRefreshNotice, featureSnapshot, featureTime, featureValue } from "../src/stockFeaturesViewModel.js";

test("stock feature caches and displayed snapshots cannot leak across stocks or accounts", () => {
  assert.notDeepEqual(featureKey(1, "600519.SS"), featureKey(2, "600519.SS"));
  assert.notDeepEqual(featureKey(1, "600519.SS"), featureKey(1, "000001.SZ"));
  assert.deepEqual(featureKey(1, " 600519.ss "), featureKey(1, "600519.SS"));
  const snapshot = { stock_code: "600519.SS", features: [{ id: "one" }] };
  assert.equal(featureSnapshot({ snapshot }, "600519.SS"), snapshot);
  assert.equal(featureSnapshot({ snapshot }, "000001.SZ"), null);
  assert.equal(featureSnapshot({ snapshot: { features: [] } }, "600519.SS"), null);
});

test("only sourced reported or observed values are shown as known facts", () => {
  const sources = [{ source_id: "P1" }];
  const fact = { id: "exposure", value: 0, status: "reported", source_ids: ["P1"] };
  assert.equal(featureAssessment(fact, sources).known, true);
  assert.equal(featureValue({ ...fact, unit: "%" }), "0 %");
  assert.equal(featureAssessment({ ...fact, status: "candidate" }, sources).known, false);
  assert.equal(featureAssessment({ ...fact, status: "new_unknown_status" }, sources).known, false);
  assert.equal(featureAssessment({ ...fact, value: null }, sources).known, false);
  assert.equal(featureAssessment({ ...fact, source_ids: ["P1", "not-saved"] }, sources).known, false);
  assert.equal(featureAssessment({ ...fact, source_ids: [] }, sources).known, false);
  assert.equal(featureValue({ value: null, unit: "亿元" }), "未知，待补充");
});

test("factor paths preserve missing exposures and unknown price direction", () => {
  const factor = { exposure_feature_ids: ["revenue", "unrecorded"] };
  const exposures = factorExposures(factor, [{ id: "revenue", label: "海外收入", value: "30%", status: "candidate" }]);
  assert.equal(exposures.length, 2);
  assert.equal(exposures[0].known, false);
  assert.equal(exposures[1].value, "特征未收录");
  assert.match(directionText({ operating: "conditional", price: "unknown" }), /股价方向尚待验证/);
  assert.equal(directionText({ description: "汇率与套保可能抵消影响" }), "汇率与套保可能抵消影响");
});

test("coverage includes unavailable categories and missing metrics without inventing percentages", () => {
  assert.deepEqual(coverageGaps({ missing: ["缺少海外收入"], categories: { geography: { status: "missing" }, business: { status: "present" } } }, [{ missing: ["缺少海外收入", "套保未知"] }]), ["缺少海外收入", "地区与海外敞口：尚无可用资料", "套保未知"]);
  assert.equal(featurePercent(null), "—");
  assert.equal(featurePercent(""), "—");
  assert.equal(featurePercent("invalid"), "—");
  assert.equal(featurePercent(0), "0.00%");
  assert.equal(featurePercent(-2.345), "-2.35%");
  assert.equal(featureRefreshNotice("failed").type, "warning");
  assert.equal(featureRefreshNotice("cooldown").type, "info");
});

test("report-period dates preserve their granularity while timestamp updates use Shanghai time", () => {
  assert.equal(featureTime("2026-09-30"), "2026-09-30");
  assert.equal(featureTime("2026-11-07T01:00:00Z"), "2026-11-07 09:00");
  assert.equal(featureTime(null), "—");
});
