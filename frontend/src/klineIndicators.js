/**
 * 副图技术指标（前端本地计算）。
 * 蜡烛升序：{ o,h,l,c,v,amount?,turnover_rate? }
 */

// Missing observations are gaps, never zero-price observations. Numeric API strings
// are accepted, while null, blanks, booleans and non-finite numbers are not.
function num(v, fallback = 0) {
  if (typeof v !== "number" && (typeof v !== "string" || !v.trim())) return fallback;
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}

function fills(n, val = null) {
  return Array.from({ length: n }, () => val);
}

function validPeriod(period) {
  return Number.isInteger(period) && period > 0;
}

/** A trailing mean requires `period` consecutive valid observations. */
export function sma(arr, period) {
  const out = fills(arr.length);
  if (!validPeriod(period)) return out;
  let sum = 0;
  let count = 0;
  for (let i = 0; i < arr.length; i++) {
    const value = num(arr[i], null);
    if (value == null) { sum = 0; count = 0; continue; }
    sum += value;
    count += 1;
    if (count > period) sum -= num(arr[i - period], null);
    if (count >= period) out[i] = Number.isFinite(sum) ? sum / period : null;
  }
  return out;
}

/**
 * EMA uses the first complete period's SMA as its seed, then alpha=2/(period+1).
 * A missing observation resets the seed and warm-up. This deliberately preserves
 * the chart's existing convention, which differs from MyTT/pandas ewm's first
 * observation seed used by some backend indicators. No future bars are read.
 */
function emaClean(src, period) {
  const out = fills(src.length);
  if (!validPeriod(period)) return out;
  const alpha = 2 / (period + 1);
  let count = 0;
  let mean = 0;
  let prev = null;
  for (let i = 0; i < src.length; i++) {
    const value = num(src[i], null);
    if (value == null) { count = 0; mean = 0; prev = null; continue; }
    if (prev == null) {
      count += 1;
      mean += (value - mean) / count;
      if (count === period) { prev = mean; out[i] = mean; }
    } else {
      prev += (value - prev) * alpha;
      out[i] = Number.isFinite(prev) ? prev : null;
    }
  }
  return out;
}

/** MACD histogram uses the customary A-share scale: 2 * (DIF - DEA). */
export function macd(closes, fast = 12, slow = 26, signal = 9) {
  const dif = fills(closes.length);
  const dea = fills(closes.length);
  const hist = fills(closes.length);
  if (![fast, slow, signal].every(validPeriod) || fast >= slow) return { dif, dea, hist };
  const emaFast = emaClean(closes, fast);
  const emaSlow = emaClean(closes, slow);
  for (let i = 0; i < closes.length; i++) {
    dif[i] = emaFast[i] != null && emaSlow[i] != null ? emaFast[i] - emaSlow[i] : null;
  }
  const signalLine = emaClean(dif, signal);
  for (let i = 0; i < closes.length; i++) {
    dea[i] = signalLine[i];
    hist[i] = dif[i] != null && dea[i] != null ? (dif[i] - dea[i]) * 2 : null;
  }
  return { dif, dea, hist };
}

/** Bollinger bands: trailing SMA ± multiplier * population standard deviation. */
export function boll(closes, period = 20, multiplier = 2) {
  const empty = () => ({ mid: fills(closes.length), upper: fills(closes.length), lower: fills(closes.length) });
  if (!validPeriod(period) || !Number.isFinite(multiplier) || multiplier <= 0) return empty();
  const mid = sma(closes, period);
  const upper = fills(closes.length);
  const lower = fills(closes.length);
  for (let i = period - 1; i < closes.length; i++) {
    if (mid[i] == null) continue;
    let squared = 0;
    for (let j = i - period + 1; j <= i; j++) squared += (num(closes[j], null) - mid[i]) ** 2;
    const width = multiplier * Math.sqrt(squared / period);
    upper[i] = Number.isFinite(mid[i] + width) ? mid[i] + width : null;
    lower[i] = Number.isFinite(mid[i] - width) ? mid[i] - width : null;
  }
  return { mid, upper, lower };
}

/** K/D start at 50 after each complete N-bar window; a flat range has RSV=50. */
export function kdj(highs, lows, closes, n = 9, m1 = 3, m2 = 3) {
  const len = closes.length;
  const kArr = fills(len);
  const dArr = fills(len);
  const jArr = fills(len);
  if (![n, m1, m2].every(validPeriod)) return { k: kArr, d: dArr, j: jArr };
  let k = 50;
  let d = 50;
  let validCount = 0;
  for (let i = 0; i < len; i++) {
    const high = num(highs[i], null);
    const low = num(lows[i], null);
    const close = num(closes[i], null);
    if (high == null || low == null || close == null || high < low) {
      validCount = 0; k = 50; d = 50; continue;
    }
    validCount += 1;
    if (validCount < n) continue;
    let hh = -Infinity;
    let ll = Infinity;
    for (let j = i - n + 1; j <= i; j++) {
      hh = Math.max(hh, num(highs[j], null));
      ll = Math.min(ll, num(lows[j], null));
    }
    const rsv = hh === ll ? 50 : ((close - ll) / (hh - ll)) * 100;
    k = (rsv + (m1 - 1) * k) / m1;
    d = (k + (m2 - 1) * d) / m2;
    kArr[i] = k;
    dArr[i] = d;
    jArr[i] = 3 * k - 2 * d;
  }
  return { k: kArr, d: dArr, j: jArr };
}

/** Wilder smoothing, requiring period valid changes after each data gap. */
export function rsi(closes, period = 14) {
  const out = fills(closes.length);
  if (!validPeriod(period)) return out;
  let previous = null;
  let count = 0;
  let gain = 0;
  let loss = 0;
  for (let i = 0; i < closes.length; i++) {
    const value = num(closes[i], null);
    if (value == null) { previous = null; count = 0; gain = 0; loss = 0; continue; }
    if (previous == null) { previous = value; continue; }
    const change = value - previous;
    previous = value;
    count += 1;
    const up = Math.max(change, 0);
    const down = Math.max(-change, 0);
    if (count <= period) { gain += up; loss += down; }
    if (count < period) continue;
    if (count === period) { gain /= period; loss /= period; }
    else {
      gain = (gain * (period - 1) + up) / period;
      loss = (loss * (period - 1) + down) / period;
    }
    // A completely flat window is neutral; rises with no losses are 100.
    out[i] = loss === 0 ? (gain === 0 ? 50 : 100) : 100 - 100 / (1 + gain / loss);
  }
  return out;
}

export function wr(highs, lows, closes, period = 14) {
  const n = closes.length;
  const out = fills(n);
  for (let i = period - 1; i < n; i++) {
    let hh = -Infinity, ll = Infinity;
    for (let j = i - period + 1; j <= i; j++) {
      hh = Math.max(hh, highs[j]);
      ll = Math.min(ll, lows[j]);
    }
    out[i] = hh === ll ? 0 : ((hh - closes[i]) / (hh - ll)) * -100;
  }
  return out;
}

export function cci(highs, lows, closes, period = 14) {
  const n = closes.length;
  const tp = closes.map((c, i) => (highs[i] + lows[i] + c) / 3);
  const out = fills(n);
  for (let i = period - 1; i < n; i++) {
    let sum = 0;
    for (let j = 0; j < period; j++) sum += tp[i - j];
    const ma = sum / period;
    let md = 0;
    for (let j = 0; j < period; j++) md += Math.abs(tp[i - j] - ma);
    md /= period;
    out[i] = md === 0 ? 0 : (tp[i] - ma) / (0.015 * md);
  }
  return out;
}

export function bias(closes, period = 6) {
  const ma = sma(closes, period);
  return closes.map((c, i) => (ma[i] == null || ma[i] === 0 ? null : ((c - ma[i]) / ma[i]) * 100));
}

export function obv(closes, volumes) {
  const n = closes.length;
  const out = fills(n, 0);
  for (let i = 1; i < n; i++) {
    const prev = out[i - 1] ?? 0;
    if (closes[i] > closes[i - 1]) out[i] = prev + volumes[i];
    else if (closes[i] < closes[i - 1]) out[i] = prev - volumes[i];
    else out[i] = prev;
  }
  return out;
}

export function atr(highs, lows, closes, period = 14) {
  const n = closes.length;
  const tr = fills(n);
  for (let i = 0; i < n; i++) {
    if (i === 0) tr[i] = highs[i] - lows[i];
    else {
      tr[i] = Math.max(highs[i] - lows[i], Math.abs(highs[i] - closes[i - 1]), Math.abs(lows[i] - closes[i - 1]));
    }
  }
  return sma(tr, period);
}

export function psy(closes, period = 12) {
  const n = closes.length;
  const out = fills(n);
  for (let i = period; i < n; i++) {
    let up = 0;
    for (let j = i - period + 1; j <= i; j++) if (closes[j] > closes[j - 1]) up += 1;
    out[i] = (up / period) * 100;
  }
  return out;
}

export function roc(closes, period = 12) {
  const n = closes.length;
  const out = fills(n);
  for (let i = period; i < n; i++) {
    const base = closes[i - period];
    out[i] = !base ? null : ((closes[i] - base) / base) * 100;
  }
  return out;
}

export function mtm(closes, period = 12) {
  const n = closes.length;
  const out = fills(n);
  for (let i = period; i < n; i++) out[i] = closes[i] - closes[i - period];
  return out;
}

export function dma(closes, shortP = 10, longP = 50, m = 10) {
  const s = sma(closes, shortP);
  const l = sma(closes, longP);
  const n = closes.length;
  const dif = fills(n);
  for (let i = 0; i < n; i++) dif[i] = s[i] != null && l[i] != null ? s[i] - l[i] : null;
  const ama = sma(dif.map((x) => (x == null ? NaN : x)), m).map((x) => (x != null && !Number.isNaN(x) ? x : null));
  return { dif, ama };
}

export function trix(closes, period = 12) {
  const ee1 = emaClean(closes, period);
  const ee2 = emaClean(ee1, period);
  const ee3 = emaClean(ee2, period);
  const n = closes.length;
  const trx = fills(n);
  for (let i = 1; i < n; i++) {
    if (ee3[i] == null || ee3[i - 1] == null || ee3[i - 1] === 0) trx[i] = null;
    else trx[i] = ((ee3[i] - ee3[i - 1]) / ee3[i - 1]) * 100;
  }
  return { trix: trx, matrix: emaClean(trx, 9) };
}

export function vr(closes, volumes, period = 26) {
  const n = closes.length;
  const out = fills(n);
  for (let i = period; i < n; i++) {
    let avs = 0, bvs = 0, cvs = 0;
    for (let j = i - period + 1; j <= i; j++) {
      if (closes[j] > closes[j - 1]) avs += volumes[j];
      else if (closes[j] < closes[j - 1]) bvs += volumes[j];
      else cvs += volumes[j];
    }
    const den = bvs + cvs / 2;
    out[i] = den === 0 ? null : ((avs + cvs / 2) / den) * 100;
  }
  return out;
}

export function emv(highs, lows, volumes, period = 14) {
  const n = highs.length;
  const raw = fills(n);
  for (let i = 1; i < n; i++) {
    const midMove = (highs[i] + lows[i]) / 2 - (highs[i - 1] + lows[i - 1]) / 2;
    const boxRatio = volumes[i] === 0 ? null : (volumes[i] / 1e8) / Math.max(highs[i] - lows[i], 1e-8);
    raw[i] = boxRatio == null ? null : midMove / boxRatio;
  }
  return sma(raw.map((x) => (x == null ? NaN : x)), period).map((x) => (x != null && !Number.isNaN(x) ? x : null));
}

export const SUB_INDICATORS = [
  { id: "vol", label: "成交量" },
  { id: "amount", label: "成交额" },
  { id: "turnover", label: "换手率" },
  { id: "macd", label: "MACD" },
  { id: "kdj", label: "KDJ" },
  { id: "rsi", label: "RSI" },
  { id: "bias", label: "BIAS" },
  { id: "wr", label: "WR" },
  { id: "cci", label: "CCI" },
  { id: "obv", label: "OBV" },
  { id: "atr", label: "ATR" },
  { id: "psy", label: "PSY" },
  { id: "roc", label: "ROC" },
  { id: "mtm", label: "MTM" },
  { id: "dma", label: "DMA" },
  { id: "trix", label: "TRIX" },
  { id: "vr", label: "VR" },
  { id: "emv", label: "EMV" },
];

function line(name, data, color, width = 1.2) {
  return { name, type: "line", xAxisIndex: 1, yAxisIndex: 1, data, showSymbol: false, lineStyle: { width, color }, emphasis: { disabled: true } };
}

export function buildSubIndicatorSeries(candles, indicatorId, { volumeAxisFormatter, params = {} } = {}) {
  const closes = candles.map((c) => num(c.c, null));
  const highs = candles.map((c) => num(c.h, null));
  const lows = candles.map((c) => num(c.l, null));
  const volumes = candles.map((c) => num(c.v, null));
  const amounts = candles.map((c) => num(c.amount, null));
  const turns = candles.map((c) => (c.turnover_rate != null ? num(c.turnover_rate, null) : null));
  const id = indicatorId || "vol";
  const barTone = (i) => ({ color: closes[i] >= num(candles[i].o) ? "#f87171" : "#4ade80", opacity: 0.85 });

  if (id === "vol") {
    const ma5 = sma(volumes, 5);
    const ma10 = sma(volumes, 10);
    return {
      legend: ["成交量", "VOL-MA5", "VOL-MA10"],
      yAxisName: "成交量",
      yAxisFormatter: volumeAxisFormatter,
      series: [
        { name: "成交量", type: "bar", xAxisIndex: 1, yAxisIndex: 1, data: volumes.map((v, i) => (v == null ? null : { value: v, itemStyle: barTone(i) })) },
        line("VOL-MA5", ma5, "#eab308", 1),
        line("VOL-MA10", ma10, "#60a5fa", 1),
      ],
      tooltipLines: (i) => [`成交量 ${volumes[i] ?? "—"}`, ma5[i] != null ? `MA5 ${ma5[i].toFixed(0)}` : null, ma10[i] != null ? `MA10 ${ma10[i].toFixed(0)}` : null].filter(Boolean),
    };
  }
  if (id === "amount") {
    return {
      legend: ["成交额"], yAxisName: "成交额",
      series: [{ name: "成交额", type: "bar", xAxisIndex: 1, yAxisIndex: 1, data: amounts.map((v, i) => (v == null ? null : { value: v, itemStyle: barTone(i) })) }],
      tooltipLines: (i) => [`成交额 ${amounts[i] ?? "—"}`],
    };
  }
  if (id === "turnover") {
    return {
      legend: ["换手率"], yAxisName: "换手率%",
      series: [{ name: "换手率", type: "bar", xAxisIndex: 1, yAxisIndex: 1, data: turns.map((v, i) => (v == null ? null : { value: v, itemStyle: barTone(i) })) }],
      tooltipLines: (i) => [`换手率 ${turns[i] != null ? turns[i] + "%" : "—"}`],
    };
  }
  if (id === "macd") {
    const { fast = 12, slow = 26, signal = 9 } = params.macd || {};
    const { dif, dea, hist } = macd(closes, fast, slow, signal);
    return {
      legend: ["DIF", "DEA", "MACD"], yAxisName: "MACD",
      series: [
        { name: "MACD", type: "bar", xAxisIndex: 1, yAxisIndex: 1, data: hist.map((v) => (v == null ? null : { value: v, itemStyle: { color: v >= 0 ? "#f87171" : "#4ade80", opacity: 0.85 } })) },
        line("DIF", dif, "#eab308"), line("DEA", dea, "#60a5fa"),
      ],
      tooltipLines: (i) => [dif[i] != null ? `DIF ${dif[i].toFixed(3)}` : null, dea[i] != null ? `DEA ${dea[i].toFixed(3)}` : null, hist[i] != null ? `MACD ${hist[i].toFixed(3)}` : null].filter(Boolean),
    };
  }
  if (id === "kdj") {
    const { period = 9, k: smoothK = 3, d: smoothD = 3 } = params.kdj || {};
    const { k, d, j } = kdj(highs, lows, closes, period, smoothK, smoothD);
    return {
      legend: ["K", "D", "J"], yAxisName: "KDJ",
      series: [line("K", k, "#eab308"), line("D", d, "#60a5fa"), line("J", j, "#f472b6")],
      tooltipLines: (i) => [k[i] != null ? `K ${k[i].toFixed(2)}` : null, d[i] != null ? `D ${d[i].toFixed(2)}` : null, j[i] != null ? `J ${j[i].toFixed(2)}` : null].filter(Boolean),
    };
  }
  if (id === "rsi") {
    const periods = params.rsi?.periods || [6, 12, 24];
    const colors = ["#eab308", "#60a5fa", "#a78bfa"];
    const curves = periods.map((period) => ({ name: `RSI${period}`, data: rsi(closes, period) }));
    return {
      legend: curves.map(({ name }) => name), yAxisName: "RSI",
      series: curves.map(({ name, data }, index) => line(name, data, colors[index % colors.length])),
      tooltipLines: (i) => curves.flatMap(({ name, data }) => (data[i] != null ? [`${name} ${data[i].toFixed(2)}`] : [])),
    };
  }
  if (id === "bias") {
    const b6 = bias(closes, 6), b12 = bias(closes, 12), b24 = bias(closes, 24);
    return {
      legend: ["BIAS6", "BIAS12", "BIAS24"], yAxisName: "BIAS%",
      series: [line("BIAS6", b6, "#eab308"), line("BIAS12", b12, "#60a5fa"), line("BIAS24", b24, "#a78bfa")],
      tooltipLines: (i) => [b6[i] != null ? `BIAS6 ${b6[i].toFixed(2)}` : null, b12[i] != null ? `BIAS12 ${b12[i].toFixed(2)}` : null, b24[i] != null ? `BIAS24 ${b24[i].toFixed(2)}` : null].filter(Boolean),
    };
  }
  if (id === "wr") {
    const w = wr(highs, lows, closes, 14);
    return { legend: ["WR"], yAxisName: "WR", series: [line("WR", w, "#f472b6")], tooltipLines: (i) => (w[i] != null ? [`WR ${w[i].toFixed(2)}`] : []) };
  }
  if (id === "cci") {
    const c = cci(highs, lows, closes, 14);
    return { legend: ["CCI"], yAxisName: "CCI", series: [line("CCI", c, "#34d399")], tooltipLines: (i) => (c[i] != null ? [`CCI ${c[i].toFixed(2)}`] : []) };
  }
  if (id === "obv") {
    const o = obv(closes, volumes);
    return { legend: ["OBV"], yAxisName: "OBV", series: [{ ...line("OBV", o, "#38bdf8"), areaStyle: { opacity: 0.06 } }], tooltipLines: (i) => [`OBV ${Math.round(o[i] || 0)}`] };
  }
  if (id === "atr") {
    const a = atr(highs, lows, closes, 14);
    return { legend: ["ATR"], yAxisName: "ATR", series: [line("ATR", a, "#fb923c")], tooltipLines: (i) => (a[i] != null ? [`ATR ${a[i].toFixed(3)}`] : []) };
  }
  if (id === "psy") {
    const p = psy(closes, 12);
    return { legend: ["PSY"], yAxisName: "PSY", series: [line("PSY", p, "#c084fc")], tooltipLines: (i) => (p[i] != null ? [`PSY ${p[i].toFixed(2)}`] : []) };
  }
  if (id === "roc") {
    const r = roc(closes, 12);
    return { legend: ["ROC"], yAxisName: "ROC%", series: [line("ROC", r, "#22d3ee")], tooltipLines: (i) => (r[i] != null ? [`ROC ${r[i].toFixed(2)}`] : []) };
  }
  if (id === "mtm") {
    const m = mtm(closes, 12);
    const ma = sma(m.map((x) => (x == null ? NaN : x)), 6).map((x) => (x != null && !Number.isNaN(x) ? x : null));
    return {
      legend: ["MTM", "MTM-MA6"], yAxisName: "MTM",
      series: [line("MTM", m, "#eab308"), line("MTM-MA6", ma, "#60a5fa", 1)],
      tooltipLines: (i) => [m[i] != null ? `MTM ${m[i].toFixed(3)}` : null, ma[i] != null ? `MA6 ${ma[i].toFixed(3)}` : null].filter(Boolean),
    };
  }
  if (id === "dma") {
    const { dif, ama } = dma(closes);
    return {
      legend: ["DMA", "AMA"], yAxisName: "DMA",
      series: [line("DMA", dif, "#eab308"), line("AMA", ama, "#60a5fa")],
      tooltipLines: (i) => [dif[i] != null ? `DMA ${dif[i].toFixed(3)}` : null, ama[i] != null ? `AMA ${ama[i].toFixed(3)}` : null].filter(Boolean),
    };
  }
  if (id === "trix") {
    const { trix: t, matrix } = trix(closes, 12);
    return {
      legend: ["TRIX", "MATRIX"], yAxisName: "TRIX",
      series: [line("TRIX", t, "#eab308"), line("MATRIX", matrix, "#60a5fa")],
      tooltipLines: (i) => [t[i] != null ? `TRIX ${t[i].toFixed(4)}` : null, matrix[i] != null ? `MATRIX ${matrix[i].toFixed(4)}` : null].filter(Boolean),
    };
  }
  if (id === "vr") {
    const v = vr(closes, volumes, 26);
    return { legend: ["VR"], yAxisName: "VR", series: [line("VR", v, "#fb7185")], tooltipLines: (i) => (v[i] != null ? [`VR ${v[i].toFixed(2)}`] : []) };
  }
  if (id === "emv") {
    const e = emv(highs, lows, volumes, 14);
    return { legend: ["EMV"], yAxisName: "EMV", series: [line("EMV", e, "#4ade80")], tooltipLines: (i) => (e[i] != null ? [`EMV ${e[i].toFixed(4)}`] : []) };
  }
  return buildSubIndicatorSeries(candles, "vol", { volumeAxisFormatter, params });
}
