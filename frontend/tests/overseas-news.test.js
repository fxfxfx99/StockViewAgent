import assert from "node:assert/strict";
import test from "node:test";
import { overseasEmptyState, overseasSourceStatus, overseasTime, overseasTransport } from "../src/overseasNewsViewModel.js";

test("news timestamps use Beijing time for epoch seconds and ISO values", () => {
  assert.equal(overseasTime(0), "1970/01/01 08:00:00");
  assert.equal(overseasTime("2026-10-31T00:30:00Z"), "2026/10/31 08:30:00");
  assert.equal(overseasTime(null), "暂无时间");
  assert.equal(overseasTime("invalid"), "暂无时间");
});

test("public indexes and authorized terminal APIs remain distinct", () => {
  assert.equal(overseasTransport("google_news_rss"), "Google News 索引");
  assert.equal(overseasTransport("benzinga_api"), "Benzinga 授权 API");
  assert.equal(overseasTransport("rss"), "公开 RSS 订阅");
  assert.equal(overseasTransport(undefined), "采集方式待确认");
});

test("unknown, limited and unavailable sources are never presented as healthy", () => {
  assert.equal(overseasSourceStatus("ok").tone, "success");
  for (const status of [undefined, "error", "rate_limited", "access_denied", "disabled", "pending", "stale"]) {
    assert.notEqual(overseasSourceStatus(status).tone, "success");
  }
});

test("empty results distinguish failed reads, filtering and disabled collection", () => {
  assert.equal(overseasEmptyState({ hasError: true, filtered: true }).title, "快讯读取失败");
  assert.equal(overseasEmptyState({ filtered: true, enabled: false }).title, "没有符合条件的消息");
  assert.equal(overseasEmptyState({ enabled: false }).title, "暂无归档消息");
  assert.match(overseasEmptyState({ enabled: false }).description, /启用海外快讯/);
  assert.equal(overseasEmptyState({ enabled: true }).title, "暂无归档消息");
});
