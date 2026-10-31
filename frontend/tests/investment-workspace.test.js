import assert from "node:assert/strict";
import test from "node:test";
import { dossierPayload, dossierValues, filteredDossiers, investmentConflict, investmentKey, monitoringDraft, observationUrl, sameResearchTarget, stockContextUrl } from "../src/investmentViewModel.js";
import { candleFocus, financialSeries, focusCandleOption } from "../src/stockContextViewModel.js";
import { buildCandleVolumeChartOption } from "../src/klineChartOption.js";
import { planPayload, planValues } from "../src/monitoringViewModel.js";

test("question research targets canonicalize without leaking stale list members", () => {
  const base = { title: " 问题 ", objective: " 证据能否支持现金流改善？ ", target_symbols: ["000001.SZ"], target_code: "600519", keywords: "订单，现金流", thesis: "待验证" };
  assert.deepEqual(dossierPayload({ ...base, target_type: "stock" }), { title: "问题", objective: "证据能否支持现金流改善？", target_type: "stock", target_code: "600519.SS", target_symbols: [], keywords: ["订单", "现金流"], thesis: "待验证", status: "active" });
  assert.deepEqual(dossierPayload({ ...base, target_type: "stock_list", target_symbols: "600519 000001 600519.SH" }).target_symbols, ["600519.SS", "000001.SZ"]);
  assert.throws(() => dossierPayload({ ...base, target_type: "topic", keywords: "" }), /关键词/);
  assert.throws(() => dossierPayload({ ...base, target_type: "index" }), /研究对象/);
  assert.equal(dossierValues(null, { target_symbols: ["600519.SS"], keywords: ["现金流"] }).target_symbols, "600519.SS");
});

test("research next observation is a draft with immutable source link, not a saved monitoring plan", () => {
  const report = { id: "r1", dossier_id: "d1", result: { dossier_snapshot: { target_type: "stock_list", target_symbols: ["600519.SS", "000001.SZ"], keywords: ["现金流"] }, report: { next_observations: [{ title: "检查回款", goal: "核对现金流", watch_conditions: "经营现金流改善", invalidation_conditions: "应收款扩大" }] } } };
  const draft = monitoringDraft(report, 0);
  assert.deepEqual(draft.research_link, { dossier_id: "d1", report_id: "r1", observation_index: 0 });
  assert.equal(draft.id, undefined);
  assert.equal(draft.status, undefined);
  assert.equal(planValues(null, draft).keywords, "现金流");
  assert.deepEqual(planPayload({ ...planValues(null, draft), channels: ["forum", "announcements"] }).channels, ["forum", "announcements"]);
  assert.equal(planPayload(planValues(null, draft)).research_link, undefined);
  assert.equal(monitoringDraft(report, -1), null);
  assert.equal(monitoringDraft(report, 4), null);
  assert.throws(() => planPayload({ ...planValues(null, draft), channels: [] }), /监控来源/);
  assert.equal(observationUrl("r/1", 0), "/monitor?research_report=r%2F1&observation=0");
  assert.equal(stockContextUrl("600519.SS", "2026-10-29", "r/1"), "/?chart=600519.SS&as_of=2026-10-29&monitor_run=r%2F1");
});

test("account keys and list scopes keep unread, adoption and jobs independent", () => {
  assert.notDeepEqual(investmentKey(1, "overview"), investmentKey(2, "overview"));
  const rows = [{ id: "a", status: "active", unread_count: 1, active_jobs: 0 }, { id: "b", status: "archived", unread_count: 0, active_jobs: 1 }];
  assert.deepEqual(filteredDossiers(rows, "unread").map(x => x.id), ["a"]);
  assert.deepEqual(filteredDossiers(rows, "working").map(x => x.id), ["b"]);
  assert.equal(investmentConflict({ response: { status: 409, data: { detail: "档案已更新，请刷新" } } }), true);
  assert.equal(investmentConflict({ response: { status: 409, data: { detail: "正在生成研究" } } }), false);
});

test("monitor evidence import only offers dossiers with the exact same target", () => {
  assert(sameResearchTarget({ target_type: "stock_list", target_symbols: ["600519.SS", "000001.SZ"] }, { target_type: "stock_list", target_symbols: ["000001.SZ", "600519.SS"] }));
  assert(!sameResearchTarget({ target_type: "stock", target_code: "600519.SS" }, { target_type: "stock", target_code: "000001.SZ" }));
  assert(!sameResearchTarget({ target_type: "topic", keywords: ["订单"] }, { target_type: "topic", keywords: ["回款"] }));
  assert(sameResearchTarget({ target_type: "stock", target_code: "600519.SS", keywords: ["订单"] }, { target_type: "stock", target_code: "600519.SS", keywords: ["回款"] }));
});

test("trigger date selects an actual candle and a visible zoom window, with previous trading day fallback", () => {
  const candles = Array.from({ length: 100 }, (_, i) => ({ t: Date.UTC(2026, 0, 1 + i) / 1000 }));
  const focus = candleFocus(candles, "2026-02-02");
  assert.deepEqual(focus, { index: 32, date: "2026-02-02", exact: true });
  const option = focusCandleOption({ dataZoom: [{ start: 80, end: 100 }], series: [{ type: "candlestick" }, { type: "bar" }] }, candles, "2026-02-02");
  assert(option.dataZoom[0].start < 32 / 99 * 100 && option.dataZoom[0].end > 32 / 99 * 100);
  assert.equal(option.series[0].markLine.data[0].xAxis, 32);
  assert.equal(option.series[1].markLine, undefined);
  assert.equal(candleFocus(candles, "2026-05-01").exact, false);
  assert.equal(candleFocus(candles, "2025-01-01"), null);
});

test("date focus preserves the latest-price setting and each reference line's appearance", () => {
  const candles = Array.from({ length: 100 }, (_, i) => ({ t: Date.UTC(2026, 0, 1 + i) / 1000, o: 10, h: 12, l: 9, c: 11, v: 100 }));
  for (const showLastPrice of [true, false]) {
    const base = buildCandleVolumeChartOption({ candles, indicatorSettings: { display: { showLastPrice } } });
    const previous = base.series[0].markLine;
    const focused = focusCandleOption(base, candles, "2026-02-02").series[0].markLine;
    assert.equal(focused.data.length, showLastPrice ? 2 : 1);
    assert.equal(focused.data.at(-1).xAxis, 32);
    assert.equal(focused.data.at(-1).label.show, true);
    assert.equal(focused.data.at(-1).label.formatter, "定位 2026-02-02");
    assert.equal(focused.data.at(-1).lineStyle.color, "#d68b25");
    if (showLastPrice) {
      assert.deepEqual(focused.data[0], previous.data[0]);
      assert.deepEqual(focused.lineStyle, previous.lineStyle);
      assert.deepEqual(focused.label, previous.label);
      assert.equal(previous.data.length, 1, "focus must not mutate the base chart");
    } else {
      assert.equal(previous, undefined);
    }
  }
  const custom = { type: "candlestick", markLine: { data: [{ yAxis: 10, label: { show: true, formatter: "custom" }, lineStyle: { color: "blue", width: 3 } }] } };
  const focused = focusCandleOption({ series: [custom] }, candles, "2026-02-02");
  assert.deepEqual(focused.series[0].markLine.data[0], custom.markLine.data[0]);
});

test("financial trend never combines different cumulative periods, entity scopes or units", () => {
  const row = (date, value, more = {}) => ({ report_date: date, report_type: "合并", source_type: "FI_T1", source_name: "本地财报", values: { F1: value }, units: { F1: null }, ...more });
  const result = financialSeries([row("2024-12-31", 100), row("2025-12-31", 0), row("2025-03-31", 30), row("2025-12-31", 77, { report_type: "母公司" }), row("2025-12-31", 5, { units: { F1: "万元" } }), row("2025-12-31", 9, { source_type: "FI_T2" })]);
  assert.equal(result.length, 5);
  assert.deepEqual(result[0].points.map(x => x.value), [100, 0]);
  assert.equal(result[0].unit, null);
  assert.equal(result[0].period, "12-31");
});
