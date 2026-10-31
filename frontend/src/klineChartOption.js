/**
 * K 线 + 副图指标 ECharts 配置：均线、默认展示最近 N 根、缩放。
 * 周/月 K 由日线在前端聚合（宏观 Tushare 等仅返回日线时）。
 */

import { boll, buildSubIndicatorSeries, sma } from "./klineIndicators.js";
import { normalizeIndicatorSettings } from "./klineIndicatorSettings.js";

export { sma } from "./klineIndicators.js";

function finite(value) {
  if (typeof value !== "number" && (typeof value !== "string" || !value.trim())) return null;
  const result = Number(value);
  return Number.isFinite(result) ? result : null;
}

/** A second/third indicator gets its own space, rather than shrinking prices. */
export function getKlineChartHeight(baseHeight = 520, settings) {
  const base = Number.isFinite(Number(baseHeight)) ? Math.max(280, Number(baseHeight)) : 520;
  const count = normalizeIndicatorSettings(settings).subIndicators.length;
  return base + Math.max(0, count - 1) * 160;
}

const dateFormatters = new Map();

/** A trading date uses the instrument's exchange timezone, never the browser's. */
export function formatCandleDate(tsSec, timeZone = "Asia/Shanghai") {
  if (!dateFormatters.has(timeZone)) dateFormatters.set(timeZone, new Intl.DateTimeFormat("en-CA", { timeZone, year: "numeric", month: "2-digit", day: "2-digit" }));
  const parts = dateFormatters.get(timeZone).formatToParts(new Date(tsSec * 1000));
  const value = (type) => parts.find((part) => part.type === type)?.value;
  return `${value("year")}-${value("month")}-${value("day")}`;
}

function periodKey(tsSec, mode, timeZone) {
  const day = formatCandleDate(tsSec, timeZone);
  if (mode === "1mo") return day.slice(0, 7);
  const date = new Date(`${day}T00:00:00Z`);
  date.setUTCDate(date.getUTCDate() - (date.getUTCDay() + 6) % 7);
  return date.toISOString().slice(0, 10);
}

/**
 * 日线升序 → 按周 / 月合成 OHLCV（t 取该周期最后一根）
 * @param {Array<{t:number,o:number,h:number,l:number,c:number,v?:number,amount?:number}>} daily
 * @param {'1d'|'1wk'|'1mo'} mode
 */
export function aggregateCandlesFromDaily(daily, mode, timeZone = "Asia/Shanghai") {
  const sorted = [...daily].sort((a, b) => a.t - b.t);
  if (mode === "1d" || !sorted.length) return sorted;

  /** @type {Map<string|number, typeof sorted>} */
  const buckets = new Map();
  for (const c of sorted) {
    const key = periodKey(c.t, mode, timeZone);
    if (!buckets.has(key)) buckets.set(key, []);
    buckets.get(key).push(c);
  }

  const keys = [...buckets.keys()].sort();

  return keys.map((key) => {
    const bars = buckets.get(key).slice().sort((a, b) => a.t - b.t);
    const first = bars[0];
    const last = bars[bars.length - 1];
    const validPrices = bars.every((bar) => [bar.o, bar.h, bar.l, bar.c].every((value) => finite(value) != null));
    const sumKnown = (field) => {
      if (!bars.every((bar) => finite(bar[field]) != null && finite(bar[field]) >= 0)) return null;
      return finite(bars.reduce((total, bar) => total + finite(bar[field]), 0));
    };
    return {
      ...last,
      t: last.t,
      o: validPrices ? finite(first.o) : null,
      h: validPrices ? Math.max(...bars.map((bar) => finite(bar.h))) : null,
      l: validPrices ? Math.min(...bars.map((bar) => finite(bar.l))) : null,
      c: validPrices ? finite(last.c) : null,
      v: sumKnown("v"),
      amount: sumKnown("amount"),
      // Daily turnover percentages do not define a single aggregate denominator.
      turnover_rate: null,
    };
  });
}

/**
 * 默认展示最近 `showBars` 根 K 的 dataZoom 百分比
 * @param {number} total
 * @param {number} showBars
 */
export function dataZoomPercentLastN(total, showBars) {
  if (!total || total <= showBars) return { start: 0, end: 100 };
  const start = ((total - showBars) / (total - 1)) * 100;
  return { start: Math.max(0, start), end: 100 };
}

const DEFAULT_CHART_PALETTE = {
  text: "#e5e7eb", muted: "#9ca3af", line: "#4b5563", grid: "rgba(75,85,99,0.35)",
  up: "#f87171", down: "#4ade80", accent: "#60a5fa", selection: "rgba(96,165,250,0.15)",
  surface: "#1c1c1e", ma20: "#eab308", ma60: "#60a5fa", ma250: "#a78bfa",
};

/** Canvas colors resolve CSS tokens before reaching ECharts (which cannot use var()). */
export function candlePaletteFromStyles(styles) {
  const tokens = {
    text: "--sva-text", muted: "--sva-text-secondary", line: "--sva-separator-strong",
    grid: "--sva-separator", up: "--sva-stock-up", down: "--sva-stock-down",
    accent: "--sva-accent", selection: "--sva-accent-soft", surface: "--sva-panel-bg",
    ma20: "--sva-chart-ma20", ma60: "--sva-chart-ma60", ma250: "--sva-chart-ma250",
  };
  return Object.fromEntries(Object.entries(tokens).map(([key, token]) =>
    [key, styles.getPropertyValue(token).trim() || DEFAULT_CHART_PALETTE[key]]));
}

/**
 * Main overlays and up to three independently scaled indicator panes.
 * Explicit indicatorSettings takes precedence over legacy showVolume/subIndicator.
 * Every series uses the complete supplied prefix, then dataZoom selects its view.
 */
export function buildCandleVolumeChartOption({
  candles: rawCandles,
  viewMode = "1d",
  aggregateFromDaily = false,
  defaultShowBars = 50,
  showVolume = true,
  subIndicator = "vol",
  indicatorSettings,
  volumeInLegend = true,
  layout = "main",
  compact = false,
  timeZone = "Asia/Shanghai",
  legendExtra = [],
  formatDate,
  tooltipDateLine,
  tooltipExtraLines,
  volumeAxisFormatter,
  palette,
}) {
  const colors = { ...DEFAULT_CHART_PALETTE, ...palette };
  const candles = aggregateFromDaily ? aggregateCandlesFromDaily(rawCandles || [], viewMode, timeZone) : rawCandles || [];
  if (!candles.length) return null;
  const preferences = normalizeIndicatorSettings(indicatorSettings ?? {
    subIndicators: showVolume ? [subIndicator || "vol"] : [],
  });
  const { main, params: settings, display } = preferences;
  const mini = layout === "mini";
  const defaultFmt = (c) => formatCandleDate(c.t, timeZone);
  const dates = candles.map((c) => (formatDate ? formatDate(c) : defaultFmt(c)));
  const closes = candles.map((c) => finite(c.c));
  const overlays = [];
  const lineColors = [colors.ma20, colors.ma60, colors.ma250];
  function overlay(name, data, color, dashed = false) {
    overlays.push({ name, data, type: "line", showSymbol: false, smooth: false, connectNulls: false,
      lineStyle: { width: 1.2, color, ...(dashed ? { type: "dashed" } : {}) },
      itemStyle: { color }, emphasis: { disabled: true } });
  }
  if (main.ma) settings.ma.periods.forEach((period, index) => overlay(`MA${period}`, sma(closes, period), lineColors[index % lineColors.length]));
  if (main.boll) {
    const bands = boll(closes, settings.boll.period, settings.boll.multiplier);
    overlay("BOLL中轨", bands.mid, colors.accent, true);
    overlay("BOLL上轨", bands.upper, colors.ma250);
    overlay("BOLL下轨", bands.lower, colors.ma20);
  }
  const subplots = preferences.subIndicators.map((id) => ({ id,
    ...buildSubIndicatorSeries(candles, id, { volumeAxisFormatter, params: settings }),
  }));
  const count = subplots.length;
  const indices = Array.from({ length: count + 1 }, (_, index) => index);
  const legendItems = ["K线", ...overlays.map((line) => line.name),
    ...(volumeInLegend ? subplots.flatMap((sub) => sub.legend) : []), ...legendExtra];
  const referenceHeight = getKlineChartHeight(mini ? 330 : 520, preferences);
  const top = mini ? 32 : 38;
  const bottom = mini ? 32 : 62;
  const gap = mini ? 24 : 32;
  const contentHeight = referenceHeight - top - bottom - count * gap;
  const priceHeight = count ? contentHeight * [1, 0.68, 0.56, 0.48][count] : contentHeight;
  const subHeight = count ? (contentHeight - priceHeight) / count : 0;
  const percent = (value) => `${(value / referenceHeight) * 100}%`;
  const margins = { left: mini ? 2 : 8, right: mini ? 2 : compact ? 58 : 76 };
  const grid = [{ ...margins, top: percent(top), height: percent(priceHeight) }];
  const titles = [];
  const paneLabel = (sub) => {
    if (sub.id === "vol") return "成交量 · MA5 / MA10";
    if (sub.id === "macd") return `MACD (${settings.macd.fast}, ${settings.macd.slow}, ${settings.macd.signal}) · DIF / DEA`;
    if (sub.id === "kdj") return `KDJ (${settings.kdj.period}, ${settings.kdj.k}, ${settings.kdj.d}) · K / D / J`;
    if (sub.id === "rsi") return `RSI (${settings.rsi.periods.join(", ")})`;
    return sub.yAxisName || sub.id.toUpperCase();
  };
  subplots.forEach((sub, index) => {
    const paneTop = top + priceHeight + gap + index * (subHeight + gap);
    grid.push({ ...margins, top: percent(paneTop), height: percent(subHeight) });
    titles.push({ text: paneLabel(sub), left: margins.left, top: percent(paneTop - 23), padding: 0,
      textStyle: { color: colors.muted, fontSize: mini ? 10 : 11, fontWeight: "normal" } });
  });
  const xAxis = indices.map((index) => ({ type: "category", gridIndex: index, data: dates, boundaryGap: true,
    axisLine: mini ? { show: false } : { lineStyle: { color: colors.line } },
    axisTick: { show: false }, axisLabel: { show: !mini && index === count, hideOverlap: true, color: colors.muted, fontSize: 11, margin: 8 },
  }));
  const yAxis = indices.map((index) => ({
    scale: !index || !subplots[index - 1].series.some((item) => item.type === "bar"),
    gridIndex: index, position: "right", ...(index ? { splitNumber: 2 } : {}),
    ...(index && subplots[index - 1].id === "rsi" ? { min: 0, max: 100 } : {}),
    ...(index && subplots[index - 1].id === "kdj" ? {
      min: (extent) => Math.min(0, extent.min), max: (extent) => Math.max(100, extent.max),
    } : {}),
    splitLine: { show: !mini && display.showGrid, lineStyle: { color: colors.grid, type: "dashed" } },
    axisLabel: { show: !mini, hideOverlap: true, color: colors.muted, fontSize: compact ? 10 : 11, margin: 8,
      formatter: (value) => Math.abs(value) >= 1000000 ? `${(value / 1000000).toFixed(1)}M` : Math.abs(value) >= 10000 ? `${(value / 10000).toFixed(2)}万` : Number(value.toFixed(2)).toLocaleString("en-US"),
      ...(index && subplots[index - 1].yAxisFormatter ? { formatter: subplots[index - 1].yAxisFormatter } : {}),
    },
  }));
  const { start, end } = dataZoomPercentLastN(candles.length, indicatorSettings ? display.visibleBars : defaultShowBars);
  const dataZoom = [{ type: "inside", xAxisIndex: indices, start, end }];
  if (candles.length > 1) dataZoom.push({ type: "slider", xAxisIndex: indices, start, end,
    height: mini ? 20 : 24, bottom: mini ? 0 : 8, left: margins.left, right: margins.right,
    showDetail: !compact, brushSelect: false,
    borderColor: colors.line, fillerColor: colors.selection,
    handleStyle: { color: colors.accent }, moveHandleStyle: { color: colors.accent },
    dataBackground: { lineStyle: { color: colors.muted }, areaStyle: { color: colors.grid } },
    selectedDataBackground: { lineStyle: { color: colors.accent }, areaStyle: { color: colors.selection } },
    textStyle: { color: colors.muted, fontSize: 12, lineHeight: 18 },
  });
  const series = [{ name: "K线", type: "candlestick",
    data: candles.map((c) => [c.o, c.c, c.l, c.h].every((value) => finite(value) != null)
      ? [c.o, c.c, c.l, c.h].map(finite) : ["-", "-", "-", "-"]),
    itemStyle: { color: colors.up, color0: colors.down, borderColor: colors.up, borderColor0: colors.down },
  }, ...overlays];
  const latestClose = finite(candles.at(-1)?.c);
  if (!mini && display.showLastPrice && latestClose != null) series[0].markLine = {
    silent: true, symbol: "none", animation: false,
    lineStyle: { color: latestClose >= finite(candles.at(-1)?.o) ? colors.up : colors.down, type: "dashed", width: 1, opacity: 0.75 },
    label: { show: false }, data: [{ yAxis: latestClose }],
  };
  subplots.forEach((sub, panelIndex) => {
    let colorIndex = 0;
    series.push(...sub.series.map((item) => {
      const axes = { xAxisIndex: panelIndex + 1, yAxisIndex: panelIndex + 1 };
      if (item.type === "line") {
        const color = lineColors[colorIndex++ % lineColors.length];
        return { ...item, ...axes, connectNulls: false, lineStyle: { ...item.lineStyle, color }, itemStyle: { ...item.itemStyle, color } };
      }
      if (item.type === "bar") return { ...item, ...axes, itemStyle: { ...item.itemStyle, color: colors.up }, data: item.data.map((point, index) => {
        if (point == null) return null;
        const value = typeof point === "object" ? point.value : point;
        const rises = sub.id === "macd" ? Number(value) >= 0 : Number(candles[index].c) >= Number(candles[index].o);
        return { ...(typeof point === "object" ? point : {}), value,
          itemStyle: { ...(point?.itemStyle || {}), color: rises ? colors.up : colors.down } };
      }) };
      return { ...item, ...axes };
    }));
  });
  const tooltipFormatter = (points) => {
    if (!points?.length) return "";
    const index = points[0].dataIndex;
    const candle = candles[index];
    if (!candle) return "";
    const header = tooltipDateLine ? tooltipDateLine(index, candle) : `<div style="font-weight:600">${dates[index]}</div>`;
    const lines = [`开 ${finite(candle.o) ?? "—"}　收 ${finite(candle.c) ?? "—"}　高 ${finite(candle.h) ?? "—"}　低 ${finite(candle.l) ?? "—"}`];
    if (tooltipExtraLines) lines.push(...tooltipExtraLines(candle));
    for (const item of overlays) {
      const value = item.data[index];
      lines.push(`${item.name}: ${value == null ? "—" : value.toFixed(3)}`);
    }
    for (const sub of subplots) {
      const values = sub.tooltipLines?.(index, candle) || [];
      lines.push(...(values.length ? values : [`${sub.yAxisName}: —（数据不足）`]));
    }
    return [header, ...lines].join("<br/>");
  };
  return {
    backgroundColor: "transparent",
    textStyle: { color: colors.text, fontSize: 12, lineHeight: 18, fontFamily: "-apple-system, BlinkMacSystemFont, PingFang SC, sans-serif" },
    animation: false,
    title: titles,
    legend: { data: legendItems, top: mini ? 0 : 2, type: "scroll", left: 0, right: 0,
      textStyle: { color: colors.muted, fontSize: 12, lineHeight: 18 }, itemWidth: 16, itemHeight: 10, itemGap: mini ? 8 : 16,
      pageIconSize: 16, pageButtonItemGap: 8, pageButtonGap: 12, pageIconColor: colors.muted,
      pageIconInactiveColor: colors.line, pageTextStyle: { color: colors.muted, fontSize: 12, lineHeight: 18 },
    },
    tooltip: { trigger: "axis", backgroundColor: colors.surface, borderColor: colors.line,
      padding: [10, 12], textStyle: { color: colors.text, fontSize: 12, lineHeight: 20 },
      axisPointer: { type: "cross", lineStyle: { color: colors.muted }, crossStyle: { color: colors.muted },
        label: { backgroundColor: colors.surface, color: colors.text, fontSize: 12, padding: [4, 8] } },
      confine: true, formatter: tooltipFormatter,
    },
    axisPointer: { link: [{ xAxisIndex: indices }] },
    grid, xAxis, yAxis, dataZoom, series,
  };
}

/** Surface missing fields and warm-up gaps without inventing zeroes or changing preferences. */
export function getIndicatorDataHints(candles, indicatorSettings) {
  if (!candles?.length) return [];
  const settings = normalizeIndicatorSettings(indicatorSettings);
  const hints = [];
  if (settings.main.ma && settings.params.ma.periods.some((period) => period > candles.length)) {
    hints.push(`部分均线需要更多历史数据（当前 ${candles.length} 根）`);
  }
  const fields = { vol: ["v", "成交量"], amount: ["amount", "成交额"], turnover: ["turnover_rate", "换手率"], obv: ["v", "OBV"], vr: ["v", "VR"] };
  for (const id of settings.subIndicators) {
    const sub = buildSubIndicatorSeries(candles, id, { params: settings.params });
    const hasValue = sub.series.some((series) => series.data.some((point) => finite(typeof point === "object" && point ? point.value : point) != null));
    if (!hasValue) {
      const field = fields[id];
      const missing = field && !candles.some((candle) => finite(candle[field[0]]) != null);
      hints.push(`${field?.[1] || id.toUpperCase()}：${missing ? "数据源未提供所需字段" : "有效样本不足"}`);
    }
  }
  return hints;
}
