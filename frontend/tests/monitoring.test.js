import assert from "node:assert/strict";
import test from "node:test";
import { budgetState, coverageLines, modelGate, monitoringError, monitoringInitial, monitoringKey, parseKeywords, parseStockSymbols, planPayload, planValues, reportLines, revisionConflict, runInProgress, runTime, targetLabel } from "../src/monitoringViewModel.js";

test("monitoring requires the current configured and verified model; stale successful status cannot bypass read failure", () => {
  assert.equal(modelGate(null).ready, false);
  assert.equal(modelGate({ configured: false, verified: true }).ready, false);
  assert.equal(modelGate({ configured: true, verified: false }).ready, false);
  assert.equal(modelGate({ configured: true, verified: true }).ready, true);
  assert.equal(modelGate({ configured: true, verified: true }, true).ready, false);
  assert.notDeepEqual(monitoringKey(1, "overview"), monitoringKey(2, "overview"));
});

test("plan submission canonicalizes A shares and explicitly drops codes for topic plans", () => {
  const form = { ...planValues(), title: " 观察订单 ", target_code: "600519.SH", keywords: "订单， 消费,订单", interval_minutes: "360", goal: " 需求变化 ", watch_conditions: " 新增订单 ", invalidation_conditions: " 订单取消 " };
  assert.deepEqual(planPayload(form), { title: "观察订单", target_type: "stock", target_code: "600519.SS", target_symbols: [], keywords: ["订单", "消费"], channels: ["news", "price"], goal: "需求变化", watch_conditions: "新增订单", invalidation_conditions: "订单取消", interval_minutes: 360, review_at: form.review_at, status: "active" });
  assert.equal(planPayload({ ...form, target_type: "topic" }).target_code, "");
  assert.equal(planPayload({ ...form, target_type: "index", target_code: "HS300" }).target_code, "HS300");
  assert.throws(() => planPayload({ ...form, target_type: "index", target_code: "600519" }), /请选择大盘指数/);
  assert.throws(() => planPayload({ ...form, target_code: "AAPL" }), /A 股/);
  assert.throws(() => planPayload({ ...form, interval_minutes: 1 }), /频率/);
  assert.throws(() => planPayload({ ...form, target_type: "topic", keywords: "" }), /至少填写一个关键词/);
});

test("stock lists normalize and deduplicate members without accepting invalid or oversized input", () => {
  assert.deepEqual(parseStockSymbols("600519 600519.SH，000001.SZ\n300750;830799"), ["600519.SS", "000001.SZ", "300750.SZ", "830799.BJ"]);
  assert.throws(() => parseStockSymbols("600519,AAPL"), /AAPL.*无效/);
  assert.throws(() => parseStockSymbols([]), /至少1只/);
  assert.throws(() => parseStockSymbols(Array(21).fill("600519")), /最多 20 只/);
  const values = { ...planValues(), target_type: "stock_list", target_code: "old-code", target_symbols: "600519，000001", goal: "保留个人目标" };
  const payload = planPayload(values);
  assert.equal(payload.target_code, "");
  assert.deepEqual(payload.target_symbols, ["600519.SS", "000001.SZ"]);
  assert.equal(payload.goal, "保留个人目标");
  assert.equal(planValues(payload).target_symbols, "600519.SS，000001.SZ");
  assert.deepEqual(planPayload({ ...values, target_type: "stock", target_code: "600519" }).target_symbols, []);
  assert.equal(targetLabel(payload), "股票列表 · 2只");
});

test("individual stocks are the default and URL context only prefills a draft", () => {
  assert.equal(planValues().target_type, "stock");
  assert.equal(planValues().interval_minutes, 360);
  assert.deepEqual(monitoringInitial(new URLSearchParams("symbol=600519.SS")), { target_type: "stock", target_code: "600519.SS" });
  assert.deepEqual(monitoringInitial(new URLSearchParams("target=index&code=HS300")), { target_type: "index", target_code: "HS300" });
});

test("list evidence coverage keeps missing members and distinguishes available from actual model input", () => {
  const lines = coverageLines([{ source: "stock_list", symbol: "600519.SS", status: "available", count: 7, fresh_count: 5, included_count: 2, included_fresh_count: 1, truncated: true, sources: { kline: "available", news: "missing" } }, { source: "stock_list", symbol: "000001.SZ", status: "missing", count: 0, fresh_count: 0, included_count: 0, included_fresh_count: 0 }]);
  assert.match(lines[0], /600519.SS.*7条记录.*实际纳入2条.*1条在有效期内.*截取.*本地行情可用.*个股新闻暂无匹配/);
  assert.match(lines[1], /000001.SZ.*暂无匹配.*实际纳入0条/);
});

test("keywords remain deduplicated and exceeding the server limit is rejected rather than silently truncated", () => {
  assert.deepEqual(parseKeywords("利率，利率;就业\n物价"), ["利率", "就业", "物价"]);
  assert.throws(() => planPayload({ ...planValues(), target_type: "topic", keywords: Array.from({ length: 11 }, (_, i) => `关键词${i}`) }), /最多填写10个/);
  assert.equal(planValues({ keywords: ["利率", "就业"], revision: 4 }).keywords, "利率，就业");
});

test("conflicts instruct preserving drafts and structured backend errors remain useful", () => {
  assert.match(monitoringError({ response: { status: 409 } }), /输入已保留/);
  assert.equal(monitoringError({ response: { data: { detail: { code: "model_required", message: "请验证模型" } } } }), "请验证模型");
  assert.equal(monitoringError(new Error("本地校验失败")), "本地校验失败");
  const verification = { response: { status: 409, data: { detail: "请先测试当前大模型连接，验证通过后再启用" } } };
  assert.equal(revisionConflict(verification), false);
  assert.match(monitoringError(verification), /测试当前大模型连接/);
});

test("budget exhaustion preserves zero and missing limits do not invent an allowance", () => {
  assert.deepEqual(budgetState({ calls_used: 0, daily_call_limit: 12 }), { used: 0, limit: 12, exhausted: false });
  assert.equal(budgetState({ calls_used: 12 }, { daily_call_limit: 12 }).exhausted, true);
  assert.equal(budgetState({}, {}).limit, null);
  assert.equal(runInProgress({ status: "queued" }), true);
  assert.equal(runInProgress({ status: "succeeded" }), false);
  assert.deepEqual(reportLines(["事实", { summary: "变化" }, { message: "受限" }]), ["事实", "变化", "受限"]);
  assert.equal(runTime({ queued_at: 100 }), 100);
  assert.equal(runTime({ created_at: 120, queued_at: 100 }), 120);
  assert.deepEqual(coverageLines([{ source: "kline", status: "stale", count: 3, fresh_count: 0 }]), ["本地行情：仅历史记录，3条记录 / 0条在有效期内"]);
});
