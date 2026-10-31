import assert from "node:assert/strict";
import test from "node:test";
import { candleSummary, collectionStatus, finiteValue, macroDate, macroDateTime, macroKey, macroMoney, macroPercent, marketAnalysisEvidence, marketGroup, marketGroups, selectMarketInstrument, validOption, validateLearningUpload } from "../src/macroViewModel.js";

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
  const negativeBase = candleSummary([{ t: 1, c: -37 }, { t: 2, c: 10 }]);
  assert.equal(negativeBase.change, null);
  assert.equal(negativeBase.totalChange, null);
  assert.equal(negativeBase.close, 10);
  assert.equal(candleSummary([{ t: 1, c: 10 }, { t: 2, c: -37 }]).change, -470);
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
test("market groups follow catalog metadata and include newly added instruments without hardcoded keys", () => {
  const items = [{ key: "SH_COMP", region: "CN", asset_class: "equity_index" }, { key: "NEW_INDEX", region: "US", asset_class: "equity_index" }, { key: "GOLD", region: "US", asset_class: "futures" }, { key: "EU", region: "EU", asset_class: "equity_index" }];
  const groups = marketGroups(items);
  assert.deepEqual(groups.map((item) => item.key), ["cn", "us", "futures", "other"]);
  assert.equal(groups[2].items[0].key, "GOLD");
  assert.deepEqual(marketGroups([]), []);
  assert.equal(marketGroup({ key: "SH_COMP" }), "cn");
  assert.equal(selectMarketInstrument(items, "GOLD").key, "GOLD");
  assert.equal(selectMarketInstrument(items, "bogus").key, "SH_COMP");
  assert.equal(selectMarketInstrument(items.slice(1), "bogus").key, "NEW_INDEX");
  assert.equal(selectMarketInstrument([], "GOLD"), undefined);
});
test("trading dates use the instrument timezone and date-only fields never shift a day", () => {
  const utc = Date.parse("2026-10-30T23:30:00Z") / 1000;
  assert.equal(macroDate(utc, "America/New_York"), "2026/10/30");
  assert.equal(macroDate(utc, "Asia/Shanghai"), "2026/10/31");
  assert.equal(macroDate("2026-10-30", "America/New_York"), "2026/10/30");
  assert.equal(macroDate(utc, "invalid-zone"), "2026/10/30");
  assert.match(macroDateTime(utc), /10\/31.*07:30/);
  assert.equal(macroDateTime(null), "尚未记录");
});
test("collection status never marks stale, failed or unknown records as successful", () => {
  assert.equal(collectionStatus("success").color, "green");
  assert.equal(collectionStatus("stale").label, "缓存待更新");
  assert.equal(collectionStatus("failed").color, "red");
  assert.equal(collectionStatus("pending").label, "等待采集");
  assert.equal(collectionStatus("new-status").label, "状态未知");
});
test("analysis evidence preserves source and stale trading date independently of generated prose", () => {
  const result = { data_as_of: "2026-10-30", cache_stale: true, kline_source: "yahoo_query2", instrument: { asset_class: "futures", exchange: "NYMEX", unit: "美元/桶", timezone: "America/New_York", description: "近月连续合约会换月" } };
  const evidence = marketAnalysisEvidence(result);
  assert.equal(evidence.asOf, "2026/10/30");
  assert.equal(evidence.stale, true);
  assert.equal(evidence.source, "yahoo_query2");
  assert(evidence.details.includes("报价单位 美元/桶"));
  assert.match(evidence.note, /换月/);
  assert.equal(marketAnalysisEvidence({ ...result, cache_stale: false }).stale, false);
  assert.equal(marketAnalysisEvidence({ interpretation: "旧版个股结果" }), null);
  const unknown = marketAnalysisEvidence({ cache_stale: true, instrument: { providers: [{ id: "yahoo" }] } });
  assert.equal(unknown.source, "来源未标注");
  assert.equal(unknown.asOf, "未提供");
});
