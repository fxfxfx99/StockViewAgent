import assert from "node:assert/strict";
import test from "node:test";
import { buildMerrillChartOption, defaultMerrillSelection, merrillClockPosition, merrillDate, merrillKey, merrillNumber, merrillSeriesColor, transformMerrillSeries } from "../src/merrillViewModel.js";

test("Merrill numeric parsing preserves real zero and rejects missing or nonfinite observations", () => {
  for (const value of [null, undefined, "", false, true, "oops", Infinity, NaN]) assert.equal(merrillNumber(value), null);
  assert.equal(merrillNumber(0), 0);
  assert.equal(merrillNumber("2.5"), 2.5);
  assert.equal(merrillDate("bad"), "—");
  assert.notDeepEqual(merrillKey(1, "overview", "US"), merrillKey(2, "overview", "US"));
  assert.notDeepEqual(merrillKey(1, "overview", "US"), merrillKey(1, "overview", "CN"));
});

test("database epoch seconds and milliseconds show the same Beijing date", () => {
  const millis = Date.parse("2026-10-30T20:00:00Z");
  assert.equal(merrillDate(millis / 1000, true), merrillDate(millis, true));
  assert.equal(merrillDate(String(millis / 1000), true), merrillDate(millis, true));
  assert.match(merrillDate(millis / 1000, true), /2026/);
  assert.match(merrillDate(millis / 1000, true), /10\/31/);
});

test("default four-asset palette stays distinct when catalog order changes", () => {
  assert.equal(new Set(["sp500", "gold", "treasury_long", "usd"].map((id) => merrillSeriesColor(id))).size, 4);
  assert.equal(merrillSeriesColor("gold", 1), merrillSeriesColor("gold", 15));
});

test("Merrill default assets come from the live catalog and prefer bond returns over yields", () => {
  const catalog = [{ id: "treasury10y", name: "美国10年国债收益率" }, { id: "usd", name: "美元指数" }, { id: "treasury_long", name: "美国长期国债ETF" }, { id: "gold", name: "黄金" }, { id: "sp500", name: "标普500" }];
  assert.deepEqual(defaultMerrillSelection(catalog), ["sp500", "gold", "treasury_long", "usd"]);
  assert.deepEqual(defaultMerrillSelection([]), []);
  assert.deepEqual(defaultMerrillSelection([{ id: "one", name: "仅有序列" }]), ["one"]);
});

test("negative prices never invert normalized returns or become percentages", () => {
  const series = [{ id: "oil", points: [-10, 0, 10, -2].map((close, index) => ({ date: `2024-01-${String(5 + 7 * index).padStart(2, "0")}`, close })) }];
  const normalized = transformMerrillSeries(series, "normalized")[0];
  assert.equal(normalized.baseline_date, "2024-01-19");
  assert.deepEqual(normalized.points.map((point) => point.value), [null, null, 100, null]);
  assert.deepEqual(transformMerrillSeries(series, "raw")[0].points.map((point) => point.value), [-10, 0, 10, -2]);
  assert.deepEqual(transformMerrillSeries(series, "drawdown")[0].points.map((point) => point.value), [null, null, 0, null]);
});

test("normalization uses the first real observation on or after the requested date without filling gaps", () => {
  const series = [{ id: "a", points: [{ date: "2024-01-19", close: 30 }, { date: "2024-01-05", close: 10 }, { date: "2024-01-12", close: null }, { date: "2024-01-26", close: 0 }] }];
  const result = transformMerrillSeries(series, "normalized", "2024-01-06")[0];
  assert.equal(result.baseline_date, "2024-01-19");
  assert.deepEqual(result.points.map((point) => point.value), [null, null, 100, 0]);
  assert.equal(series[0].points[0].date, "2024-01-19");
  assert(transformMerrillSeries(series, "normalized", "2030-01-01")[0].points.every((point) => point.value === null));
});

test("drawdown tracks visible running peaks, preserves missing weeks, and a real zero is a total loss", () => {
  const series = [{ id: "a", points: [100, 90, null, 120, 0].map((close, index) => ({ date: `2024-02-${String(2 + 7 * index).padStart(2, "0")}`, close })) }];
  // Use five valid weekly dates spanning the month boundary.
  series[0].points[4].date = "2024-03-01";
  const values = transformMerrillSeries(series, "drawdown")[0].points.map((point) => point.value);
  assert.equal(values[0], 0);
  assert(Math.abs(values[1] + 10) < 1e-8);
  assert.equal(values[2], null);
  assert.equal(values[3], 0);
  assert.equal(values[4], -100);
});

test("yield series always retain original percentage units on an independent axis", () => {
  const series = [{ id: "yield", name: "国债收益率", kind: "yield", unit: "%", points: [{ date: "2024-01-05", close: 4 }, { date: "2024-01-12", close: 2 }] }];
  for (const mode of ["normalized", "drawdown", "raw"]) {
    assert.deepEqual(transformMerrillSeries(series, mode)[0].points.map((point) => point.value), [4, 2]);
    const option = buildMerrillChartOption({ series, mode });
    assert.equal(option.series[0].yAxisIndex, 1);
    assert.equal(option.yAxis[1].name, "收益率 %");
  }
});

test("chart inserts absent weeks and disables null connection rather than fabricating observations", () => {
  const option = buildMerrillChartOption({ mode: "raw", series: [{ id: "a", points: [{ date: "2024-01-05", close: 10 }, { date: "2024-01-19", close: 20 }] }] });
  assert.deepEqual(option.xAxis.data, ["2024-01-05", "2024-01-12", "2024-01-19"]);
  assert.deepEqual(option.series[0].data, [10, null, 20]);
  assert.equal(option.series[0].connectNulls, false);
  assert.equal(buildMerrillChartOption({ series: [] }), null);
  assert.equal(buildMerrillChartOption({ series: [{ id: "a", points: [{ date: "2024-01-05", close: null }] }] }), null);
});

test("clock uses growth-up/inflation-right and hides stale, ambiguous or incomplete positions", () => {
  const recovery = { status: "ready", phase: "recovery", position: { x: -.5, y: .5 } };
  const point = merrillClockPosition(recovery);
  assert(point.x < 210 && point.y < 210);
  const stagflation = merrillClockPosition({ ...recovery, phase: "stagflation", position: { x: .5, y: -.5 } });
  assert(stagflation.x > 210 && stagflation.y > 210);
  for (const status of ["stale", "insufficient", "transition"]) assert.equal(merrillClockPosition({ ...recovery, status }), null);
  assert.equal(merrillClockPosition({ ...recovery, position: { x: null, y: 0 } }), null);
  assert.equal(merrillClockPosition({ ...recovery, phase: null }), null);
  const edge = merrillClockPosition({ ...recovery, position: { x: 1, y: 1 } });
  assert(Math.hypot(edge.x - 210, edge.y - 210) <= 132.000001);
});
