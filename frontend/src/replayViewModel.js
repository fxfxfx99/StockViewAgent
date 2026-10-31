import { buildCandleVolumeChartOption } from "./klineChartOption.js";

const DAY_MS = 86400000;
const SHANGHAI_OFFSET_MS = 8 * 3600000;

/** Canonical six-digit A-share identifier; unrelated ticker symbols are rejected. */
export function normalizeReplayCode(value) {
  const raw = String(value ?? "").trim().toUpperCase();
  const match = raw.match(/^(?:(SH|SS|SZ|BJ))?((?:[03648]\d|92)\d{4})(?:\.(SH|SS|SZ|BJ))?$/);
  if (!match) return "";
  const prefix = match[1] === "SS" ? "SH" : match[1];
  const suffix = match[3] === "SS" ? "SH" : match[3];
  if (prefix && suffix && prefix !== suffix) return "";
  const exchange = prefix || suffix;
  const expected = match[2][0] === "6" ? "SH" : /^[03]/.test(match[2]) ? "SZ" : "BJ";
  return exchange && exchange !== expected ? "" : match[2];
}

function canonicalDate(value) {
  if (typeof value !== "string") return "";
  const match = value.trim().match(/^(\d{4})-?(\d{2})-?(\d{2})$/);
  if (!match) return "";
  const date = `${match[1]}-${match[2]}-${match[3]}`;
  const ms = Date.parse(`${date}T00:00:00Z`);
  return Number.isFinite(ms) && new Date(ms).toISOString().slice(0, 10) === date ? date : "";
}

/** The trading date is a Shanghai date, independent of the browser's timezone. */
export function candleDate(candle) {
  const date = canonicalDate(candle?.date);
  if (date) return date;
  const seconds = finiteNumber(candle?.t);
  if (seconds == null) return "";
  const shifted = new Date(seconds * 1000 + SHANGHAI_OFFSET_MS);
  return Number.isFinite(shifted.getTime()) ? shifted.toISOString().slice(0, 10) : "";
}

export function clampReplayCursor(cursor, length) {
  if (!Number.isFinite(length) || length <= 0) return -1;
  const index = Number(cursor);
  return Math.max(0, Math.min(Math.floor(length) - 1, Number.isNaN(index) ? 0 : Math.floor(index)));
}

/** Candles must be chronological. Holidays/weekends seek to the preceding session. */
export function findReplayCursor(candles = [], date) {
  if (!candles.length) return -1;
  if (date == null || date === "") return clampReplayCursor(59, candles.length);
  const target = canonicalDate(date);
  if (!target) return 0;
  let lo = 0;
  let hi = candles.length - 1;
  let result = 0;
  while (lo <= hi) {
    const mid = Math.floor((lo + hi) / 2);
    if (candleDate(candles[mid]) <= target) {
      result = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  return result;
}

export function initialReplayCursor(candles = [], date, initialBars = 60) {
  return date ? findReplayCursor(candles, date) : clampReplayCursor(initialBars - 1, candles.length);
}

function finiteNumber(value) {
  if (value == null || typeof value === "boolean" || (typeof value === "string" && !value.trim())) return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function sumKnown(candles, key) {
  const values = candles.map((candle) => finiteNumber(candle[key]));
  // Missing or invalid daily volume/amount cannot cancel into a valid total.
  if (values.some((value) => value == null || value < 0)) return null;
  const total = values.reduce((sum, value) => sum + value, 0);
  return Number.isFinite(total) ? total : null;
}

function periodKey(candle, period) {
  const date = candleDate(candle);
  if (period === "monthly" || period === "1mo") return date.slice(0, 7);
  const ms = Date.parse(`${date}T00:00:00Z`);
  const weekday = new Date(ms).getUTCDay();
  const monday = ms - ((weekday + 6) % 7) * DAY_MS;
  return Number.isFinite(monday) ? new Date(monday).toISOString().slice(0, 10) : date;
}

/** Slice before aggregation: the unfinished week/month never sees future bars. */
export function getReplayVisibleCandles(candles = [], cursor = 0, period = "1d") {
  const index = clampReplayCursor(cursor, candles.length);
  if (index < 0) return [];
  const visible = candles.slice(0, index + 1).map((candle) => ({ ...candle, date: candleDate(candle) }));
  if (!["weekly", "monthly", "1wk", "1mo"].includes(period)) return visible;
  const buckets = new Map();
  for (const candle of visible) {
    const key = periodKey(candle, period);
    if (!buckets.has(key)) buckets.set(key, []);
    buckets.get(key).push(candle);
  }
  return Array.from(buckets.values(), (bars) => {
    const last = bars.at(-1);
    const validPrices = bars.every((bar) => [bar.o, bar.h, bar.l, bar.c].every((value) => finiteNumber(value) != null));
    return {
      ...last,
      o: validPrices ? finiteNumber(bars[0].o) : null,
      h: validPrices ? Math.max(...bars.map((bar) => finiteNumber(bar.h))) : null,
      l: validPrices ? Math.min(...bars.map((bar) => finiteNumber(bar.l))) : null,
      c: validPrices ? finiteNumber(last.c) : null,
      v: sumKnown(bars, "v"),
      amount: sumKnown(bars, "amount"),
      // A daily rate cannot be promoted to a weekly/monthly observation.
      turnover_rate: null,
    };
  });
}

/** Current historical row only. In particular, never merge a present-day quote. */
export function getReplayCurrentCandle(candles = [], cursor = 0) {
  const index = clampReplayCursor(cursor, candles.length);
  return index < 0 ? null : { ...candles[index], date: candleDate(candles[index]) };
}

export function buildReplayChartOption(candles, cursor, period = "1d", options = {}) {
  return buildCandleVolumeChartOption({
    ...options,
    formatDate: options.formatDate || candleDate,
    candles: getReplayVisibleCandles(candles, cursor, period),
    // The standard chart's aggregation uses local-time buckets, so bypass it here.
    aggregateFromDaily: false,
  });
}

export function formatReplayValue(value, decimals = 2, fallback = "—") {
  const number = finiteNumber(value);
  const precision = Number.isFinite(decimals) ? Math.max(0, Math.min(10, Math.floor(decimals))) : 2;
  return number == null ? fallback : number.toFixed(precision);
}

export function getVisibleReplayNotes(notes = [], date) {
  const cutoff = canonicalDate(date);
  if (!cutoff) return [];
  return notes.filter((note) => {
    const noteDate = canonicalDate(note?.date);
    return noteDate && noteDate <= cutoff;
  });
}

/** Escape arbitrary note text, including spreadsheet formulas and multiline fields. */
export function escapeReplayCsvCell(value) {
  let text = value == null ? "" : String(value);
  if (typeof value === "string" && /^[\s\u0000-\u001f]*[=+\-@]/.test(text)) text = `'${text}`;
  return /[",\n\r]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

export function exportReplayNotesCsv(notes = [], date) {
  const labels = { bullish: "看多", bearish: "看空", observe: "观察" };
  const rows = [["股票代码", "复盘日期", "方向判断", "当时收盘价", "复盘记录", "记录时间"]];
  for (const note of getVisibleReplayNotes(notes, date)) {
    rows.push([
      note.symbol, note.date, labels[note.stance] || note.stance,
      note.price ?? note.close, note.text ?? note.reason ?? note.reasoning,
      note.createdAt ?? note.created_at,
    ]);
  }
  return `\ufeff${rows.map((row) => row.map(escapeReplayCsvCell).join(",")).join("\r\n")}`;
}
