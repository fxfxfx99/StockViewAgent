import test from "node:test";
import assert from "node:assert/strict";
import { sma, macd, boll, kdj, rsi, buildSubIndicatorSeries, SUB_INDICATORS } from "../src/klineIndicators.js";
import { DEFAULT_INDICATOR_SETTINGS, normalizeIndicatorSettings, validateIndicatorSettings, indicatorSettingsKey } from "../src/klineIndicatorSettings.js";

const near = (actual, expected, message) => assert.ok(Math.abs(actual - expected) < 1e-10, `${message || "value"}: ${actual} != ${expected}`);
function assertSeries(actual, expected) {
  assert.equal(actual.length, expected.length);
  expected.forEach((value, i) => value === null ? assert.equal(actual[i], null, `index ${i}`) : near(actual[i], value, `index ${i}`));
}
const candlesFor = (closes) => closes.map((c, i) => ({ o: c, c, h: c == null ? null : c + 1, l: c == null ? null : c - 1, v: 100 + i }));

test("MA uses trailing values, accepts finite API strings and rewarms after a gap", () => {
  assert.deepEqual(sma([1, 2, 3, null, 4, 5, 6, Infinity, 7, 8, 9, NaN, 10, 11, 12], 3), [null, null, 2, null, null, null, 5, null, null, null, 8, null, null, null, 11]);
  assert.deepEqual(sma(["1", "2", "3", "", false, 2, 4], 2), [null, 1.5, 2.5, null, null, null, 3]);
  assert.deepEqual(sma([0, 0, 0], 2), [null, 0, 0]);
});

test("MACD follows hand-computed SMA seeds, exponential smoothing and double histogram", () => {
  const result = macd([1, 2, 3, 6, 4, 8], 2, 3, 2);
  assertSeries(result.dif, [null, null, 1 / 2, 5 / 6, 5 / 18, 41 / 54]);
  assertSeries(result.dea, [null, null, null, 2 / 3, 11 / 27, 52 / 81]);
  assertSeries(result.hist, [null, null, null, 1 / 3, -7 / 27, 19 / 81]);
});

test("MACD gaps restart both EMAs and signal warm-up instead of carrying old state", () => {
  const tail = [3, 7, 4, 10, 8, 11];
  const result = macd([100, 120, 130, 180, null, ...tail], 2, 3, 2);
  const clean = macd(tail, 2, 3, 2);
  for (const key of ["dif", "dea", "hist"]) {
    assert.equal(result[key][4], null);
    assert.deepEqual(result[key].slice(5), clean[key]);
  }
  for (const missing of [NaN, Infinity, -Infinity, undefined, "", false]) {
    assert.deepEqual(macd([1, 2, 3, missing, ...tail], 2, 3, 2).hist.slice(4), clean.hist);
  }
});

test("MACD flat prices produce zero only after the necessary warm-up", () => {
  const result = macd(Array(40).fill(15));
  assert.deepEqual(result.dif.slice(0, 25), Array(25).fill(null));
  assert.equal(result.dif[25], 0);
  assert.deepEqual(result.hist.slice(0, 33), Array(33).fill(null));
  assert.deepEqual(result.hist.slice(33), Array(7).fill(0));
});

test("BOLL uses population deviation rather than sample deviation", () => {
  const result = boll([1, 2, 3, 4], 3, 2);
  const width = 2 * Math.sqrt(2 / 3);
  assertSeries(result.mid, [null, null, 2, 3]);
  assertSeries(result.upper, [null, null, 2 + width, 3 + width]);
  assertSeries(result.lower, [null, null, 2 - width, 3 - width]);
  assert.deepEqual(boll([8, 8, 8], 2), { mid: [null, 8, 8], upper: [null, 8, 8], lower: [null, 8, 8] });
});

test("BOLL gaps leave a complete warm-up window blank and custom width is applied", () => {
  const result = boll([1, 2, null, 3, 4, 5, Infinity, 7, 8, 9], 3, 1);
  assert.deepEqual(result.mid, [null, null, null, null, null, 4, null, null, null, 8]);
  near(result.upper[5] - result.mid[5], Math.sqrt(2 / 3));
});

test("KDJ uses rolling highs/lows and smoothing from K=D=50", () => {
  const result = kdj([3, 4, 5, 6], [1, 1, 2, 3], [2, 3, 4, 5], 3, 3, 3);
  assertSeries(result.k, [null, null, 175 / 3, 590 / 9]);
  assertSeries(result.d, [null, null, 475 / 9, 1540 / 27]);
  assertSeries(result.j, [null, null, 625 / 9, 2230 / 27]);
});

test("KDJ flat prices remain neutral and a missing H/L/C restarts smoothing", () => {
  assert.deepEqual(kdj([8, 8, 8, 8], [8, 8, 8, 8], [8, 8, 8, 8], 2), { k: [null, 50, 50, 50], d: [null, 50, 50, 50], j: [null, 50, 50, 50] });
  const h = [10, 11, 13, 100, 3, 4, 5, 6];
  const l = [1, 2, 3, 1, 1, 1, 2, 3];
  const c = [9, 10, 12, 90, 2, 3, 4, 5];
  for (const field of [0, 1, 2]) {
    const values = [h.slice(), l.slice(), c.slice()];
    values[field][3] = null;
    const result = kdj(...values, 3, 3, 3);
    const clean = kdj(h.slice(4), l.slice(4), c.slice(4), 3, 3, 3);
    for (const key of ["k", "d", "j"]) {
      assert.equal(result[key][3], null);
      assert.deepEqual(result[key].slice(4), clean[key]);
    }
  }
});

test("RSI is Wilder-smoothed, neutral on a flat run and restarts after gaps", () => {
  assertSeries(rsi([1, 2, 1, 3, 2], 2), [null, null, 50, 250 / 3, 50]);
  assert.deepEqual(rsi([4, 4, 4, 4], 2), [null, null, 50, 50]);
  assert.deepEqual(rsi([8, 7, 6, 5], 2), [null, null, 0, 0]);
  assert.deepEqual(rsi([1, 2, 3, null, 4, 5, 6], 2), [null, null, 100, null, null, null, 100]);
});

test("invalid mathematical periods return unavailable values without unstable loops", () => {
  for (const period of [0, -1, 2.5, NaN, Infinity, null]) {
    assert.deepEqual(sma([1, 2, 3], period), [null, null, null]);
    assert.deepEqual(boll([1, 2, 3], period).mid, [null, null, null]);
    assert.deepEqual(kdj([1, 2, 3], [1, 2, 3], [1, 2, 3], period).k, [null, null, null]);
    assert.deepEqual(rsi([1, 2, 3], period), [null, null, null]);
  }
  assert.deepEqual(macd([1, 2, 3], 3, 2, 1).dif, [null, null, null]);
  assert.deepEqual(boll([1, 2, 3], 2, Infinity).upper, [null, null, null]);
});

test("every replay prefix equals the matching full-series prefix without future leakage", () => {
  const closes = Array.from({ length: 75 }, (_, i) => i === 37 ? null : 20 + Math.sin(i * 1.3) * 5 + i / 9);
  const highs = closes.map((v) => v == null ? null : v + 2);
  const lows = closes.map((v) => v == null ? null : v - 2);
  const full = { ma: sma(closes, 8), ...Object.fromEntries(Object.entries(macd(closes, 5, 11, 4)).map(([k, v]) => [`macd-${k}`, v])), ...Object.fromEntries(Object.entries(boll(closes, 8, 1.5)).map(([k, v]) => [`boll-${k}`, v])), ...Object.fromEntries(Object.entries(kdj(highs, lows, closes, 5, 2, 4)).map(([k, v]) => [`kdj-${k}`, v])), rsi: rsi(closes, 6) };
  for (let length = 0; length <= closes.length; length++) {
    const c = closes.slice(0, length), h = highs.slice(0, length), l = lows.slice(0, length);
    const partial = { ma: sma(c, 8), ...Object.fromEntries(Object.entries(macd(c, 5, 11, 4)).map(([k, v]) => [`macd-${k}`, v])), ...Object.fromEntries(Object.entries(boll(c, 8, 1.5)).map(([k, v]) => [`boll-${k}`, v])), ...Object.fromEntries(Object.entries(kdj(h, l, c, 5, 2, 4)).map(([k, v]) => [`kdj-${k}`, v])), rsi: rsi(c, 6) };
    for (const key of Object.keys(full)) assert.deepEqual(partial[key], full[key].slice(0, length), `${key} prefix ${length}`);
  }
});

test("chart adapters use custom MACD/KDJ/RSI parameters and keep missing values absent", () => {
  const candles = candlesFor([1, 2, 3, 6, 4, 8, null, 3, 4, 5]);
  const params = { macd: { fast: 2, slow: 3, signal: 2 }, kdj: { period: 3, k: 2, d: 4 }, rsi: { periods: [2, 4] } };
  const m = buildSubIndicatorSeries(candles, "macd", { params });
  assert.deepEqual(m.series.find((s) => s.name === "DIF").data, macd(candles.map((c) => c.c), 2, 3, 2).dif);
  assert.deepEqual(m.tooltipLines(6), []);
  const stochastic = buildSubIndicatorSeries(candles, "kdj", { params });
  assert.deepEqual(stochastic.series[0].data, kdj(candles.map((c) => c.h), candles.map((c) => c.l), candles.map((c) => c.c), 3, 2, 4).k);
  const strength = buildSubIndicatorSeries(candles, "rsi", { params });
  assert.deepEqual(strength.legend, ["RSI2", "RSI4"]);
  assert.deepEqual(strength.series[0].data, rsi(candles.map((c) => c.c), 2));
});

test("missing volume and amount are chart gaps, not manufactured zero trades", () => {
  const candles = candlesFor([10, 11]);
  candles[1].v = null;
  const volume = buildSubIndicatorSeries(candles, "vol");
  assert.equal(volume.series[0].data[1], null);
  assert.deepEqual(volume.tooltipLines(1), ["成交量 —"]);
  assert.deepEqual(buildSubIndicatorSeries(candles, "amount").series[0].data, [null, null]);
});

test("default settings are frozen and normalization makes independent valid trees", () => {
  assert.deepEqual(normalizeIndicatorSettings(), DEFAULT_INDICATOR_SETTINGS);
  assert.deepEqual(validateIndicatorSettings(DEFAULT_INDICATOR_SETTINGS), []);
  const first = normalizeIndicatorSettings(), second = normalizeIndicatorSettings();
  first.params.ma.periods[0] = 2;
  first.main.ma = false;
  first.subIndicators.push("macd");
  assert.deepEqual(second, DEFAULT_INDICATOR_SETTINGS);
  assert.equal(DEFAULT_INDICATOR_SETTINGS.params.ma.periods[0], 20);
  assert.throws(() => DEFAULT_INDICATOR_SETTINGS.subIndicators.push("macd"), TypeError);
});

test("normalization repairs corrupt persisted settings without mutating input", () => {
  for (const value of [null, undefined, "broken", [], 5, false, { version: 99 }]) assert.deepEqual(normalizeIndicatorSettings(value), DEFAULT_INDICATOR_SETTINGS);
  const input = { version: 1, main: { ma: "false", boll: true }, subIndicators: ["macd", "macd", "unknown", "kdj", "rsi", "vol"], params: { ma: { periods: [2, 2, -1, 10, 30, 50] }, boll: { period: Infinity, multiplier: null }, macd: { fast: 30, slow: 10, signal: 1 }, kdj: { period: 0, k: 2, d: 4 }, rsi: { periods: [] } } };
  const before = structuredClone(input);
  const result = normalizeIndicatorSettings(input);
  assert.deepEqual(input, before);
  assert.deepEqual(result.main, { ma: true, boll: true });
  assert.deepEqual(result.subIndicators, ["macd", "kdj", "rsi"]);
  assert.deepEqual(result.params.ma.periods, [2, 10, 30]);
  assert.deepEqual(result.params.macd, { fast: 12, slow: 26, signal: 1 });
  assert.deepEqual(result.params.boll, { period: 20, multiplier: 2 });
  assert.deepEqual(result.params.kdj, { period: 9, k: 2, d: 4 });
  assert.deepEqual(validateIndicatorSettings(result), []);
  assert.deepEqual(normalizeIndicatorSettings({ subIndicators: [] }).subIndicators, []);
});

test("settings validation rejects invalid bounds, fractional periods and inverted MACD", () => {
  const cases = [
    { params: { ma: { periods: [] } } }, { params: { ma: { periods: [2, 2] } } },
    { params: { rsi: { periods: [1, 501] } } }, { params: { boll: { period: 2.5 } } },
    { params: { boll: { multiplier: 0 } } }, { params: { boll: { multiplier: Infinity } } },
    { params: { macd: { fast: 26, slow: 26 } } }, { params: { macd: { fast: 250 } } },
    { params: { macd: { signal: 121 } } }, { params: { kdj: { k: 0, d: 51 } } },
    { main: { ma: "false" } }, { subIndicators: ["fake"] }, { subIndicators: ["macd", "macd"] },
    { subIndicators: ["macd", "kdj", "rsi", "vol"] }, { params: null }, { params: { rsi: [] } },
  ];
  for (const value of cases) assert.ok(validateIndicatorSettings(value).length > 0, JSON.stringify(value));
  assert.match(validateIndicatorSettings({ params: { macd: { fast: 30, slow: 10 } } }).join(" "), /快线.*慢线/);
  const boundary = { params: { ma: { periods: [2, 250, 500] }, boll: { period: 250, multiplier: 0.1 }, macd: { fast: 2, slow: 250, signal: 120 }, kdj: { period: 2, k: 1, d: 50 }, rsi: { periods: [500] } }, subIndicators: [] };
  assert.deepEqual(validateIndicatorSettings(boundary), []);
  assert.deepEqual(validateIndicatorSettings({}), []);
});

test("all existing subsidiary indicators remain selectable", () => {
  for (const { id } of SUB_INDICATORS) {
    assert.deepEqual(normalizeIndicatorSettings({ subIndicators: [id] }).subIndicators, [id]);
    assert.deepEqual(validateIndicatorSettings({ subIndicators: [id] }), []);
  }
});

test("preference keys isolate accounts/scopes and decline anonymous persistence", () => {
  assert.equal(indicatorSettingsKey(null), null);
  assert.equal(indicatorSettingsKey(undefined), null);
  assert.equal(indicatorSettingsKey(" "), null);
  assert.notEqual(indicatorSettingsKey("alice", "stock"), indicatorSettingsKey("bob", "stock"));
  assert.notEqual(indicatorSettingsKey("alice", "stock"), indicatorSettingsKey("alice", "replay"));
  assert.notEqual(indicatorSettingsKey("a:b", "c"), indicatorSettingsKey("a", "b:c"));
  assert.equal(typeof indicatorSettingsKey(0), "string");
});
