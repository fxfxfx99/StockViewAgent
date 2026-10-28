import assert from "node:assert/strict";
import test from "node:test";
import {
  calendarConfigPayload, calendarQueryKey, calendarTime, eventDateKey, eventStatusInfo,
  eventValue, groupCalendarEvents, monthCalendarDays, shanghaiDateKey, sourceStatusInfo,
} from "../src/calendarViewModel.js";

const config = { enabled: true, agents: { news: true, research: true, strategies: true, kline: true }, lookback_days: 14, lookahead_days: 30, min_importance: 2, regions: [], categories: [], top_k: 8 };

test("Shanghai event days and times are stable across UTC boundaries and preserve invalid dates", () => {
  assert.equal(shanghaiDateKey("2026-10-24T16:30:00Z"), "2026-10-25");
  assert.equal(calendarTime("2026-10-24T16:30:00Z", { includeDate: true }), "2026-10-25 00:30");
  assert.equal(calendarTime(null), "—");
  assert.equal(shanghaiDateKey("bad-date"), "");
  assert.equal(eventDateKey({ all_day: true, scheduled_date: "2026-10-24", scheduled_at: "2026-10-24T18:00:00Z" }), "2026-10-24");
});

test("month grid spans six Monday-first weeks across year and leap-day boundaries", () => {
  const january = monthCalendarDays("2026-01");
  assert.equal(january.length, 42);
  assert.equal(january[0], "2025-12-29");
  assert.equal(january[41], "2026-02-08");
  assert.ok(monthCalendarDays("2024-02").includes("2024-02-29"));
  assert.equal(monthCalendarDays("2026-02").includes("2026-02-29"), false);
  assert.deepEqual(monthCalendarDays("2026-13"), []);
});

test("agenda groups by Shanghai day, filters declared status and sorts without modifying API records", () => {
  const events = [
    { id: "later", scheduled_at: "2026-10-24T20:00:00Z", status: "scheduled", importance: 2 },
    { id: "first", scheduled_at: "2026-10-24T17:00:00Z", status: "released", importance: 3 },
    { id: "cancelled", scheduled_at: "2026-10-24T18:00:00Z", status: "cancelled" },
    { id: "unknown-date", scheduled_at: "invalid", status: "scheduled" },
  ];
  assert.deepEqual(groupCalendarEvents(events).get("2026-10-25").map((event) => event.id), ["first", "cancelled", "later"]);
  assert.deepEqual(groupCalendarEvents(events, "scheduled").get("2026-10-25").map((event) => event.id), ["later"]);
  assert.equal(events[0].id, "later");
  assert.equal(eventStatusInfo("new-unknown").label, "状态未知");
});

test("missing release values stay missing and zero values remain visible", () => {
  assert.equal(eventValue(null, "%"), "—");
  assert.equal(eventValue(" ", "%"), "—");
  assert.equal(eventValue(0, "%"), "0%");
  assert.equal(eventValue("3.2%", "%"), "3.2%");
  assert.equal(eventValue(120, "万人"), "120 万人");
});

test("source failures and unknown collection state cannot appear healthy", () => {
  assert.equal(sourceStatusInfo({ status: "success" }).status, "success");
  assert.equal(sourceStatusInfo({ status: "error", last_success_at: "2026-01-01" }).status, "error");
  assert.equal(sourceStatusInfo({ status: "unknown", error: "Network failure" }).status, "error");
  assert.equal(sourceStatusInfo({ status: "unknown" }).status, "default");
});

test("calendar retrieval configuration is account-scoped and preserves explicitly cleared filters", () => {
  assert.notDeepEqual(calendarQueryKey(1, "config"), calendarQueryKey(2, "config"));
  assert.deepEqual(calendarConfigPayload(config), config);
  const result = calendarConfigPayload({ ...config, enabled: false, regions: [], agents: { news: false } });
  assert.equal(result.enabled, false);
  assert.deepEqual(result.agents, { news: false, research: false, strategies: false, kline: false });
  assert.deepEqual(result.regions, []);
});

test("retrieval configuration rejects empty, fractional and out-of-bound counts or unsupported scopes", () => {
  for (const patch of [{ top_k: null }, { top_k: 0 }, { top_k: 31 }, { lookback_days: -1 }, { lookahead_days: 366 }, { top_k: 1.5 }, { regions: ["bad"] }, { categories: ["bad"] }]) {
    assert.throws(() => calendarConfigPayload({ ...config, ...patch }));
  }
});

test("stale successful sources require an update while failures always take priority", () => {
  assert.deepEqual(sourceStatusInfo({ status: "success", stale: true }), { label: "待更新", status: "warning" });
  assert.deepEqual(sourceStatusInfo({ status: "ok", stale: true, error: "Failed refresh" }), { label: "采集异常", status: "error" });
  assert.deepEqual(sourceStatusInfo({ status: "failed", stale: true }), { label: "采集异常", status: "error" });
  assert.equal(sourceStatusInfo({ status: "success", stale: false }).label, "采集正常");
});

test("elapsed precise schedules require verification without claiming release or creating actual values", () => {
  const now = "2026-10-24T12:00:00Z";
  const event = { id: "past", status: "scheduled", scheduled_at: "2026-10-24T11:59:00Z", all_day: false, actual: null };
  assert.deepEqual(eventStatusInfo(event, now), { label: "日程已过·结果待核验", color: "orange" });
  assert.equal(eventStatusInfo({ ...event, scheduled_at: "2026-10-24T12:01:00Z" }, now).label, "待公布");
  assert.equal(eventStatusInfo({ ...event, status: "released" }, now).label, "已公布");
  assert.equal(eventStatusInfo({ ...event, status: "cancelled" }, now).label, "已取消");
  assert.equal(eventStatusInfo({ ...event, scheduled_at: "invalid" }, now).label, "待公布");
  assert.equal(event.actual, null);
  assert.equal(event.status, "scheduled");
  assert.equal(groupCalendarEvents([event], "scheduled").get("2026-10-24")[0], event);
});

test("date-only schedules wait until the source-local date ends, regardless of UTC or Shanghai day", () => {
  const event = { status: "scheduled", all_day: true, scheduled_date: "2026-10-24", scheduled_at: "2026-10-24T04:00:00Z", timezone: "America/New_York" };
  // Already Oct 25 in UTC and Shanghai, but still Oct 24 in New York.
  assert.equal(eventStatusInfo(event, "2026-10-25T03:59:00Z").label, "待公布");
  assert.equal(eventStatusInfo(event, "2026-10-25T04:00:00Z").label, "日程已过·结果待核验");
  assert.equal(eventStatusInfo({ ...event, timezone: "Asia/Tokyo" }, "2026-10-24T15:00:00Z").label, "日程已过·结果待核验");
  assert.equal(eventStatusInfo({ ...event, timezone: "Invalid/Zone" }, "2026-10-26T00:00:00Z").label, "待公布");
});
