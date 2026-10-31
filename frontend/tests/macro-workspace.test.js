import assert from "node:assert/strict";
import test from "node:test";
import { candleSummary, finiteValue, macroKey, macroMoney, macroPercent, validOption, validateLearningUpload } from "../src/macroViewModel.js";

test("macro quantities distinguish missing values from genuine zero", () => {
  for (const value of [null, undefined, "", false, "bad", Infinity]) assert.equal(finiteValue(value), null);
  assert.equal(macroMoney(null), "—");
  assert.equal(macroMoney(0), "0.00");
  assert.equal(macroMoney(-250_000_000), "-2.50 亿");
  assert.equal(macroPercent(null), "—");
  assert.equal(macroPercent(0), "0.00%");
});
test("macro summary sorts without modifying data and never divides by missing or zero prices", () => {
  const candles = [{ t: 3, c: 110 }, { t: 1, c: 90 }, { t: 2, c: 100 }, { t: 4, c: null }];
  const summary = candleSummary(candles);
  assert.equal(summary.close, 110);
  assert(Math.abs(summary.change - 10) < 1e-9);
  assert.equal(summary.count, 3);
  assert.equal(candles[0].t, 3);
  assert.equal(candleSummary([{ t: 1, c: 0 }, { t: 2, c: 10 }]).change, null);
  assert.equal(candleSummary([]).close, null);
});
test("macro caches and deep links preserve account and supported parameter boundaries", () => {
  assert.notDeepEqual(macroKey(1, "series", "gold_sge"), macroKey(2, "series", "gold_sge"));
  assert.equal(validOption("huge", ["1y", "2y"], "1y"), "1y");
  assert.equal(validOption("2y", ["1y", "2y"], "1y"), "2y");
});
test("reference uploads reject unsupported, empty and oversized files before a request", () => {
  assert.equal(validateLearningUpload({ name: "research.PDF", size: 100 }), "");
  for (const file of [{ name: "research.exe", size: 100 }, { name: "notes.md", size: 0 }, { name: "notes.txt", size: 6 * 1024 * 1024 }]) assert(validateLearningUpload(file));
});
