import assert from "node:assert/strict";
import test from "node:test";
import { indicatorSettingsKey, normalizeIndicatorSettings, validateIndicatorSettings } from "../src/klineIndicatorSettings.js";
import { indicatorDraftToSettings, indicatorSettingsToDraft, readKlineIndicatorPreferences, writeKlineIndicatorPreferences } from "../src/klineIndicatorPreferences.js";

function memoryStorage(initial = {}) {
  const values = new Map(Object.entries(initial));
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: (key) => values.delete(key),
  };
}

test("indicator preferences remain isolated across accounts and chart pages", () => {
  const storage = memoryStorage();
  const changed = normalizeIndicatorSettings({ main: { boll: true }, subIndicators: ["macd", "kdj"] });
  assert.equal(writeKlineIndicatorPreferences(1, "stock", changed, storage), true);
  assert.deepEqual(readKlineIndicatorPreferences(1, "stock", storage), changed);
  assert.deepEqual(readKlineIndicatorPreferences(2, "stock", storage), normalizeIndicatorSettings());
  assert.deepEqual(readKlineIndicatorPreferences(1, "replay", storage), normalizeIndicatorSettings());
  assert.deepEqual(readKlineIndicatorPreferences(1, "macro", storage), normalizeIndicatorSettings());
});

test("anonymous preferences never read or mutate shared browser storage", () => {
  const unavailable = { getItem() { throw new Error("must not read"); }, setItem() { throw new Error("must not write"); } };
  assert.deepEqual(readKlineIndicatorPreferences(null, "stock", unavailable), normalizeIndicatorSettings());
  assert.equal(writeKlineIndicatorPreferences(null, "stock", { subIndicators: [] }, unavailable), false);
});

test("the old single-subchart preference is consumed by only the first signed-in stock scope", () => {
  const storage = memoryStorage({ sva_kline_sub_indicator: "kdj" });
  assert.deepEqual(readKlineIndicatorPreferences(1, "macro", storage).subIndicators, ["vol"]);
  assert.equal(storage.getItem("sva_kline_sub_indicator"), "kdj");
  assert.deepEqual(readKlineIndicatorPreferences(null, "stock", storage).subIndicators, ["vol"]);
  assert.deepEqual(readKlineIndicatorPreferences(1, "stock", storage).subIndicators, ["kdj"]);
  assert.equal(storage.getItem("sva_kline_sub_indicator"), null);
  assert.deepEqual(readKlineIndicatorPreferences(2, "stock", storage).subIndicators, ["vol"]);
  assert.deepEqual(JSON.parse(storage.getItem(indicatorSettingsKey(1, "stock"))).subIndicators, ["kdj"]);
});

test("a saved empty subchart selection wins over the old preference", () => {
  const storage = memoryStorage({ sva_kline_sub_indicator: "macd" });
  writeKlineIndicatorPreferences(1, "stock", { subIndicators: [] }, storage);
  assert.deepEqual(readKlineIndicatorPreferences(1, "stock", storage).subIndicators, []);
});

test("corrupt, future-version and blocked browser storage recover without throwing", () => {
  const key = indicatorSettingsKey(1, "stock");
  for (const raw of ["not JSON", "null", JSON.stringify({ version: 999, main: { boll: true } })]) {
    assert.deepEqual(readKlineIndicatorPreferences(1, "stock", memoryStorage({ [key]: raw })), normalizeIndicatorSettings());
  }
  const blocked = { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("quota"); } };
  assert.deepEqual(readKlineIndicatorPreferences(1, "stock", blocked), normalizeIndicatorSettings());
  assert.equal(writeKlineIndicatorPreferences(1, "stock", {}, blocked), false);
});

test("parameter drafts round-trip and retain invalid input for validation instead of silently correcting it", () => {
  const settings = normalizeIndicatorSettings({ main: { boll: true }, subIndicators: ["macd"] });
  const draft = indicatorSettingsToDraft(settings);
  assert.deepEqual(indicatorDraftToSettings(draft), settings);
  draft.params.ma.periods = "5，20 60";
  assert.deepEqual(indicatorDraftToSettings(draft).params.ma.periods, [5, 20, 60]);
  draft.params.macd.fast = "30";
  assert.ok(validateIndicatorSettings(indicatorDraftToSettings(draft)).some((message) => message.includes("小于")));
  draft.params.macd.fast = "";
  draft.params.ma.periods = "garbage";
  assert.ok(validateIndicatorSettings(indicatorDraftToSettings(draft)).length >= 2);
  assert.equal(settings.params.macd.fast, 12);
});
