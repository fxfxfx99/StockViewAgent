import assert from "node:assert/strict";
import test from "node:test";
import { applyIndicatorPreset, getIndicatorPreset, normalizeIndicatorSettings, validateIndicatorSettings } from "../src/klineIndicatorSettings.js";
import { indicatorDraftToSettings, indicatorSettingsToDraft, readKlineIndicatorPreferences, resetIndicatorDraftParameters } from "../src/klineIndicatorPreferences.js";

test("existing version-one preferences acquire display defaults without resetting chart selections", () => {
  const saved = { version: 1, main: { ma: false, boll: true }, subIndicators: ["rsi"], params: { rsi: { periods: [7, 14] } } };
  const restored = readKlineIndicatorPreferences(1, "macro", { getItem: () => JSON.stringify(saved) });
  assert.deepEqual(restored.display, { visibleBars: 80, showGrid: true, showLastPrice: true });
  assert.deepEqual(restored.main, saved.main);
  assert.deepEqual(restored.subIndicators, ["rsi"]);
  assert.deepEqual(restored.params.rsi.periods, [7, 14]);
});

test("display settings preserve explicit false and reject unsupported ranges instead of silent editor correction", () => {
  for (const visibleBars of [40, 80, 120, 240]) {
    const settings = normalizeIndicatorSettings({ display: { visibleBars, showGrid: false, showLastPrice: false } });
    assert.deepEqual(settings.display, { visibleBars, showGrid: false, showLastPrice: false });
    assert.deepEqual(validateIndicatorSettings(settings), []);
    assert.deepEqual(indicatorDraftToSettings(indicatorSettingsToDraft(settings)), settings);
  }
  for (const display of [null, [], { visibleBars: 0 }, { visibleBars: 60 }, { visibleBars: "80" }, { showGrid: "false" }, { showLastPrice: 1 }]) {
    assert(validateIndicatorSettings({ display }).length > 0);
    assert.deepEqual(normalizeIndicatorSettings({ display }).display, { visibleBars: 80, showGrid: true, showLastPrice: true });
  }
});

test("presets change only main and subchart selection while retaining custom periods and display", () => {
  const settings = normalizeIndicatorSettings({ main: { boll: true }, subIndicators: ["rsi"], display: { visibleBars: 120, showGrid: false }, params: { ma: { periods: [5, 10, 20] } } });
  const before = structuredClone(settings);
  for (const [id, main, subIndicators] of [["simple", { ma: false, boll: false }, ["vol"]], ["trend", { ma: true, boll: false }, ["vol"]], ["momentum", { ma: true, boll: false }, ["vol", "macd"]]]) {
    const changed = applyIndicatorPreset(settings, id);
    assert.deepEqual(changed.main, main);
    assert.deepEqual(changed.subIndicators, subIndicators);
    assert.deepEqual(changed.params, settings.params);
    assert.deepEqual(changed.display, settings.display);
    assert.equal(getIndicatorPreset(changed), id);
  }
  assert.deepEqual(settings, before);
  assert.equal(getIndicatorPreset(settings), "custom");
  assert.equal(getIndicatorPreset({ subIndicators: ["macd", "vol"] }), "custom");
  assert.deepEqual(applyIndicatorPreset(settings, "unrecognized"), settings);
});

test("restoring parameter drafts preserves pending display and indicator selections", () => {
  const draft = indicatorSettingsToDraft({ main: { ma: false, boll: true }, subIndicators: ["macd", "rsi"], display: { visibleBars: 240, showLastPrice: false }, params: { ma: { periods: [5, 10] } } });
  draft.params.macd.fast = "bad";
  const reset = resetIndicatorDraftParameters(draft);
  assert.deepEqual(reset.main, draft.main);
  assert.deepEqual(reset.subIndicators, draft.subIndicators);
  assert.deepEqual(reset.display, draft.display);
  assert.deepEqual(reset.params, indicatorSettingsToDraft().params);
  assert.equal(draft.params.macd.fast, "bad");
  assert.deepEqual(validateIndicatorSettings(indicatorDraftToSettings(reset)), []);
});
