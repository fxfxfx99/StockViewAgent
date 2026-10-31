import assert from "node:assert/strict";
import test from "node:test";
import {
  normalizeReplayCode, candleDate, clampReplayCursor, findReplayCursor, initialReplayCursor,
  getReplayVisibleCandles, getReplayCurrentCandle, buildReplayChartOption, formatReplayValue,
  getVisibleReplayNotes, exportReplayNotesCsv, escapeReplayCsvCell,
} from "../src/replayViewModel.js";

const bar = (date, value, overrides = {}) => ({
  date, t: Date.parse(`${date}T07:00:00Z`) / 1000,
  o: value, h: value + 2, l: value - 1, c: value + 1, v: value * 100, amount: value * 1000,
  total_mv: value * 10000, pe_ttm: value / 2, ...overrides,
});

test("replay accepts A-share symbols and rejects unrelated or mismatched exchanges", () => {
  for (const code of ["600519", "SH600519", " sh600519 ", "600519.SH", "600519.SS", "SS600519", "SH600519.SS", "SS600519.SH"]) {
    assert.equal(normalizeReplayCode(code), "600519");
  }
  for (const [code, expected] of [
    ["000001.SZ", "000001"], ["SZ000001", "000001"], ["300750.SZ", "300750"],
    ["688001.SS", "688001"], ["430001.BJ", "430001"], ["830001.BJ", "830001"],
    ["920001.BJ", "920001"], ["BJ920001", "920001"], ["920001", "920001"],
  ]) assert.equal(normalizeReplayCode(code), expected);
  for (const code of [
    null, "", "AAPL", "HK00700", "123456", "600519.SZ", "SH600519.BJ", "SZ000001.SS",
    "SS000001", "688001.BJ", "830001.SS", "920001.SS", "900001", "900001.SS", "900001.BJ", "200001.SZ",
  ]) {
    assert.equal(normalizeReplayCode(code), "");
  }
});

test("trade dates prefer provider date and otherwise resolve at Shanghai midnight", () => {
  assert.equal(candleDate({ date: "20261030", t: 1 }), "2026-10-30");
  assert.equal(candleDate({ date: "2026-10-30", t: 1 }), "2026-10-30");
  assert.equal(candleDate({ t: Date.parse("2026-10-29T16:00:00Z") / 1000 }), "2026-10-30");
  assert.equal(candleDate({ t: Date.parse("2026-10-29T15:59:59Z") / 1000 }), "2026-10-29");
  assert.equal(candleDate({ date: "2026-02-30" }), "");
  assert.equal(candleDate({ t: null }), "");
  assert.equal(candleDate({ t: Infinity }), "");
});

test("cursor bounds, short histories, invalid dates and nontrading date seeks are deterministic", () => {
  const candles = [bar("2026-10-29", 10), bar("2026-10-30", 11), bar("2026-11-02", 12)];
  assert.equal(clampReplayCursor(-8, 3), 0);
  assert.equal(clampReplayCursor(100, 3), 2);
  assert.equal(clampReplayCursor(1.9, 3), 1);
  assert.equal(clampReplayCursor(NaN, 3), 0);
  assert.equal(clampReplayCursor(0, 0), -1);
  assert.equal(initialReplayCursor([]), -1);
  assert.equal(initialReplayCursor(candles), 2);
  assert.equal(initialReplayCursor(candles, "2026-10-29"), 0);
  assert.equal(initialReplayCursor(Array(100).fill(candles[0])), 59);
  assert.equal(findReplayCursor(candles, "2026-10-31"), 1);
  assert.equal(findReplayCursor(candles, "2026-11-01"), 1);
  assert.equal(findReplayCursor(candles, "2020-01-01"), 0);
  assert.equal(findReplayCursor(candles, "2030-01-01"), 2);
  assert.equal(findReplayCursor(candles, "2026-02-30"), 0);
  assert.equal(findReplayCursor(candles, "invalid"), 0);
  assert.equal(findReplayCursor(candles), 2);
  assert.deepEqual(getReplayVisibleCandles([], 0), []);
  assert.equal(buildReplayChartOption([], 0), null);
});

test("incomplete weekly and monthly candles aggregate the revealed prefix only", () => {
  const candles = [
    bar("2026-10-26", 10), bar("2026-10-27", 12),
    bar("2026-10-28", 900, { h: 9999, l: 0.01, v: 9999999, amount: 999999999 }),
    bar("2026-11-02", 20),
  ];
  for (const period of ["weekly", "1wk", "monthly", "1mo"]) {
    const result = getReplayVisibleCandles(candles, 1, period);
    assert.equal(result.length, 1);
    assert.deepEqual(result[0], { ...candles[1], o: 10, h: 14, l: 9, c: 13, v: 2200, amount: 22000, turnover_rate: null });
    assert.equal(result[0].total_mv, 120000);
    assert.equal(result[0].pe_ttm, 6);
  }
  assert.equal(getReplayVisibleCandles(candles, 3, "weekly").length, 2);
  assert.equal(getReplayVisibleCandles(candles, 3, "monthly").length, 2);
  assert.equal(candles[1].o, 12, "aggregation leaves provider rows untouched");
});

test("week and month buckets are stable across local timezone offsets", () => {
  const candles = [
    bar("2026-10-30", 10, { date: undefined, t: Date.parse("2026-10-29T16:00:00Z") / 1000 }),
    bar("2026-11-02", 12, { date: undefined, t: Date.parse("2026-11-01T16:00:00Z") / 1000 }),
  ];
  for (const period of ["weekly", "monthly"]) {
    assert.deepEqual(getReplayVisibleCandles(candles, 1, period).map((c) => c.date), ["2026-10-30", "2026-11-02"]);
  }
});

test("adversarial future OHLCV and metrics never change prior chart, MA or current metrics", () => {
  const candles = Array.from({ length: 90 }, (_, i) => {
    const date = new Date(Date.UTC(2026, 0, i + 1)).toISOString().slice(0, 10);
    return bar(date, 10 + i);
  });
  const changedFuture = candles.map((c, index) => index <= 65 ? c : {
    ...c, o: 100000, c: 0.01, h: 9999999, l: -999, v: 999999999, total_mv: 99, pe_ttm: 9999,
  });
  for (const period of ["daily", "weekly", "monthly"]) {
    const before = buildReplayChartOption(candles, 65, period, { subIndicator: "macd" });
    const after = buildReplayChartOption(changedFuture, 65, period, { subIndicator: "macd" });
    assert.deepEqual(before.series, after.series);
    assert.deepEqual(before.xAxis, after.xAxis);
    assert.deepEqual(before.dataZoom, after.dataZoom);
    const point = [{ dataIndex: before.xAxis[0].data.length - 1 }];
    assert.equal(before.tooltip.formatter(point), after.tooltip.formatter(point));
    assert.equal(before.xAxis[0].data.at(-1), candles[65].date);
    assert.ok(before.series.every((series) => series.data.length === before.xAxis[0].data.length));
  }
  assert.equal(buildReplayChartOption(candles, 65).series.find((series) => series.name === "MA60").data.at(-1), 46.5);
  assert.deepEqual(getReplayCurrentCandle(candles, 65), getReplayCurrentCandle(changedFuture, 65));
  assert.equal(getReplayCurrentCandle([], 0), null);
});

test("missing scalars remain unavailable; a real zero is displayed", () => {
  for (const value of [null, undefined, "", "  ", NaN, Infinity, -Infinity, false]) {
    assert.equal(formatReplayValue(value), "—");
  }
  assert.equal(formatReplayValue(0), "0.00");
  assert.equal(formatReplayValue("12.345", 2), "12.35");
  assert.equal(formatReplayValue(-4, 0), "-4");
  const period = getReplayVisibleCandles([bar("2026-10-26", 10), bar("2026-10-27", 12, { v: null })], 1, "weekly");
  assert.equal(period[0].v, null);
});

test("rewinding hides later notes in the panel and CSV, including malformed dates", () => {
  const notes = [
    { id: 1, symbol: "600519", date: "2026-10-29", stance: "bullish", price: 100, text: "当前判断" },
    { id: 2, symbol: "600519", date: "2026-10-30", stance: "bearish", text: "未来结果，不可提前看到" },
    { id: 3, date: "2026-02-30", text: "坏日期" },
  ];
  assert.deepEqual(getVisibleReplayNotes(notes, "2026-10-29"), [notes[0]]);
  assert.deepEqual(getVisibleReplayNotes(notes, ""), []);
  const csv = exportReplayNotesCsv(notes, "2026-10-29");
  assert.ok(csv.startsWith("\ufeff"));
  assert.match(csv, /600519,2026-10-29,看多,100,当前判断/);
  assert.doesNotMatch(csv, /未来结果|坏日期/);
  assert.equal(getVisibleReplayNotes(notes, "2026-10-30").length, 2);
});

test("CSV notes escape quotes, commas, newlines and spreadsheet formula prefixes", () => {
  assert.equal(escapeReplayCsvCell('记录,"观察"\n继续'), '"记录,""观察""\n继续"');
  for (const text of ["=1+1", "+1+1", "-cmd", "@SUM(A1)", "\t=HYPERLINK(1)", "  =1+1"]) {
    assert.ok(escapeReplayCsvCell(text).startsWith("'"));
  }
  assert.equal(escapeReplayCsvCell(-1.2), "-1.2");
  assert.match(exportReplayNotesCsv([{ date: "2026-10-29", text: "=1+1" }], "2026-10-29"), /'=1\+1/);
});
