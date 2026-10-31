import assert from "node:assert/strict";
import test from "node:test";
import { buildReplayChartOption, getReplayVisibleCandles } from "../src/replayViewModel.js";
import { buildCandleVolumeChartOption } from "../src/klineChartOption.js";

const settings = {
  version: 1,
  main: { ma: true, boll: true },
  subIndicators: ["macd", "kdj", "rsi"],
  params: {
    ma: { periods: [3, 5, 10] },
    boll: { period: 5, multiplier: 2.5 },
    macd: { fast: 3, slow: 6, signal: 4 },
    kdj: { period: 4, k: 2, d: 3 },
    rsi: { periods: [3, 5, 8] },
  },
};

function history(length = 180) {
  return Array.from({ length }, (_, i) => {
    const t = Date.UTC(2026, 0, i + 1, 7) / 1000;
    const o = 50 + i / 10 + Math.sin(i / 3) * 4;
    return { t, date: new Date(t * 1000).toISOString().slice(0, 10), o, c: o + Math.cos(i), h: o + 2, l: o - 2, v: 1000 + i };
  });
}

test("custom BOLL and three replay indicators only use the revealed daily, weekly or monthly prefix", () => {
  const candles = history();
  const cursor = 134;
  const changedFuture = candles.map((bar, i) => i <= cursor ? bar : { ...bar, o: 999999, c: 999999, h: 9999999, l: 0.0001, v: 999999999 });
  for (const period of ["1d", "1wk", "1mo"]) {
    const before = buildReplayChartOption(candles, cursor, period, { indicatorSettings: settings });
    const after = buildReplayChartOption(changedFuture, cursor, period, { indicatorSettings: settings });
    assert.deepEqual(before.series, after.series, `${period} future OHLCV must not leak`);
    assert.deepEqual(before.xAxis, after.xAxis);
    assert.deepEqual(before.dataZoom, after.dataZoom);
    assert.equal(before.xAxis.length, 4, "main chart plus three independently scaled indicators");
    assert.ok(before.series.some((series) => series.name === "DIF"));
    assert.ok(before.series.some((series) => series.name === "K"));
    assert.ok(before.series.some((series) => series.name === "RSI3"));
    assert.ok(before.series.some((series) => /BOLL/.test(series.name)));
    const last = before.xAxis[0].data.length - 1;
    assert.equal(before.tooltip.formatter([{ dataIndex: last }]), after.tooltip.formatter([{ dataIndex: last }]));
  }
});

test("weekly and monthly settings are evaluated on aggregated revealed bars, not full daily history", () => {
  const candles = history();
  for (const period of ["1wk", "1mo"]) {
    const visible = getReplayVisibleCandles(candles, 134, period);
    const actual = buildReplayChartOption(candles, 134, period, { indicatorSettings: settings });
    const expected = buildCandleVolumeChartOption({ candles: visible, indicatorSettings: settings });
    assert.deepEqual(actual.series, expected.series);
    assert.ok(actual.series.every((series) => series.data.length === visible.length));
    if (period === "1mo") {
      assert.equal(visible.length, 5);
      assert.ok(actual.series.find((series) => series.name === "DIF").data.every((value) => value == null), "MACD needs six visible monthly bars for its slow EMA");
    }
  }
});

test("replay indicator parameter changes recalculate existing candles without mutating the source", () => {
  const candles = history(100);
  const original = structuredClone(candles);
  const changed = { ...settings, params: { ...settings.params, macd: { fast: 4, slow: 10, signal: 3 }, kdj: { period: 8, k: 4, d: 4 }, rsi: { periods: [4, 8, 10] } } };
  const before = buildReplayChartOption(candles, 80, "1d", { indicatorSettings: settings });
  const after = buildReplayChartOption(candles, 80, "1d", { indicatorSettings: changed });
  assert.notDeepEqual(before.series.find((series) => series.name === "DIF").data, after.series.find((series) => series.name === "DIF").data);
  assert.notDeepEqual(before.series.find((series) => series.name === "K").data, after.series.find((series) => series.name === "K").data);
  assert.ok(after.series.some((series) => series.name === "RSI4"));
  assert.deepEqual(candles, original);
});

test("replay weekly/monthly aggregation preserves missing prices and never labels a daily turnover as a period value", () => {
  const candles = history(4).map((bar) => ({ ...bar, turnover_rate: 2.5 }));
  for (const period of ["1wk", "1mo"]) {
    const valid = getReplayVisibleCandles(candles, 3, period);
    assert.ok(valid.every((bar) => bar.turnover_rate === null));
    for (const field of ["o", "h", "l", "c"]) {
      const missing = candles.map((bar, index) => index === 1 ? { ...bar, [field]: null } : bar);
      const result = getReplayVisibleCandles(missing, 3, period);
      assert.deepEqual([result[0].o, result[0].h, result[0].l, result[0].c], [null, null, null, null]);
      const chart = buildReplayChartOption(missing, 3, period, { indicatorSettings: { ...settings, subIndicators: ["turnover"] } });
      assert.ok(chart.series.find((series) => series.name === "换手率").data.every((value) => value == null));
    }
  }
  assert.equal(getReplayVisibleCandles(candles, 3, "1d")[3].turnover_rate, 2.5);
});
