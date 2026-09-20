import assert from "node:assert/strict";
import test from "node:test";
import { normalizeAsOf, normalizeChartSymbol } from "../src/hooks/urlState.js";
import { qk, watchlistProfileKey } from "../src/hooks/queryKeys.js";
import { safeUrl } from "../src/components/format.js";

test("deep-link symbols retain the exchange and normalize casing", () => {
  assert.equal(normalizeChartSymbol(" 600519.ss "), "600519.SS");
  assert.equal(normalizeChartSymbol("000001.SZ"), "000001.SZ");
  assert.equal(normalizeChartSymbol("920001.BJ"), "920001.BJ");
});

test("invalid deep-link symbols do not become API paths", () => {
  for (const value of [null, "", "600519", "600519.SS/../admin", "<script>", "AAPL"]) {
    assert.equal(normalizeChartSymbol(value), null);
  }
});

test("historical nodes retain valid calendar dates including leap day", () => {
  assert.equal(normalizeAsOf("2024-02-29"), "2024-02-29");
  assert.equal(normalizeAsOf(" 2026-09-16 "), "2026-09-16");
});

test("impossible dates cannot silently roll into a different research date", () => {
  for (const value of ["2026-02-29", "2026-02-31", "2026-04-31", "2026-13-01", "2026-00-01", "2026-1-01", "invalid", null]) {
    assert.equal(normalizeAsOf(value), null);
  }
});

test("list reordering reuses the same profile cache without mutating input", () => {
  const symbols = ["600519.SS", "000001.SZ"];
  assert.equal(watchlistProfileKey(symbols), watchlistProfileKey([...symbols].reverse()));
  assert.deepEqual(symbols, ["600519.SS", "000001.SZ"]);
});

test("user-dependent caches cannot leak between matching stock lists", () => {
  for (const key of [qk.watchlistProfiles, qk.priceContext, qk.stockDetail]) {
    assert.notDeepEqual(key("600519.SS", 1), key("600519.SS", 2));
    assert.notDeepEqual(key("600519.SS", 1), key("600519.SS"));
  }
});

test("research dates and news scopes have independent caches", () => {
  assert.notDeepEqual(qk.transactionAgentViews("600519.SS", "2026-09-15"), qk.transactionAgentViews("600519.SS", "2026-09-16"));
  assert.notDeepEqual(qk.newsArchive("600519.SS", 1, "latest", "analysis"), qk.newsArchive("600519.SS", 1, "latest", "pending"));
});

test("external source links permit web URLs and reject executable schemes", () => {
  assert.equal(safeUrl("https://example.com/news?q=1"), "https://example.com/news?q=1");
  for (const value of ["javascript:alert(1)", "data:text/html,<script>alert(1)</script>", "file:///tmp/private", "/relative", "", null]) {
    assert.equal(safeUrl(value), null);
  }
});
