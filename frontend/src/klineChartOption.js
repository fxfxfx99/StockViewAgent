/**
 * K 线 + 副图指标 ECharts 配置：均线、默认展示最近 N 根、缩放。
 * 周/月 K 由日线在前端聚合（宏观 Tushare 等仅返回日线时）。
 */

import { buildSubIndicatorSeries } from "./klineIndicators.js";

/** @param {number[]} closes */
export function sma(closes, period) {
  const n = closes.length;
  const out = new Array(n).fill(null);
  if (period <= 0 || n < period) return out;
  for (let i = period - 1; i < n; i++) {
    let s = 0;
    for (let j = 0; j < period; j++) s += closes[i - j];
    out[i] = s / period;
  }
  return out;
}

/** 周一 00:00 本地时间戳（秒）用于分桶 */
function mondayStartTs(tsSec) {
  const d = new Date(tsSec * 1000);
  const day = d.getDay();
  const diff = d.getDate() - day + (day === 0 ? -6 : 1);
  const m = new Date(d.getFullYear(), d.getMonth(), diff);
  m.setHours(0, 0, 0, 0);
  return Math.floor(m.getTime() / 1000);
}

function monthKey(tsSec) {
  const d = new Date(tsSec * 1000);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

/**
 * 日线升序 → 按周 / 月合成 OHLCV（t 取该周期最后一根）
 * @param {Array<{t:number,o:number,h:number,l:number,c:number,v?:number,amount?:number}>} daily
 * @param {'1d'|'1wk'|'1mo'} mode
 */
export function aggregateCandlesFromDaily(daily, mode) {
  const sorted = [...daily].sort((a, b) => a.t - b.t);
  if (mode === "1d" || !sorted.length) return sorted;

  /** @type {Map<string|number, typeof sorted>} */
  const buckets = new Map();
  for (const c of sorted) {
    let key;
    if (mode === "1wk") key = mondayStartTs(c.t);
    else key = monthKey(c.t);
    if (!buckets.has(key)) buckets.set(key, []);
    buckets.get(key).push(c);
  }

  const keys =
    mode === "1wk"
      ? [...buckets.keys()].sort((a, b) => a - b)
      : [...buckets.keys()].sort();

  return keys.map((key) => {
    const bars = buckets.get(key).slice().sort((a, b) => a.t - b.t);
    const first = bars[0];
    const last = bars[bars.length - 1];
    let h = -Infinity;
    let l = Infinity;
    let v = 0;
    let amount = 0;
    for (const b of bars) {
      h = Math.max(h, Number(b.h));
      l = Math.min(l, Number(b.l));
      v += Number(b.v) || 0;
      amount += Number(b.amount) || 0;
    }
    return {
      ...last,
      t: last.t,
      o: Number(first.o),
      h,
      l,
      c: Number(last.c),
      v,
      amount,
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
  const start = ((total - showBars) / total) * 100;
  return { start: Math.max(0, start), end: 100 };
}

const MA_COLORS = {
  ma20: "#eab308",
  ma60: "#60a5fa",
  ma250: "#a78bfa",
};

/**
 * @param {object} opts
 * @param {Array<{t:number,o:number,h:number,l:number,c:number,v?:number,amount?:number,turnover_rate?:number}>} opts.candles
 * @param {'1d'|'1wk'|'1mo'} [opts.viewMode='1d'] 仅当 aggregateFromDaily=true 时使用
 * @param {boolean} [opts.aggregateFromDaily=false]
 * @param {number} [opts.defaultShowBars=50]
 * @param {boolean} [opts.showVolume=true] 是否显示副图；mini 图仍可用
 * @param {string} [opts.subIndicator='vol'] 副图指标 id，见 klineIndicators.SUB_INDICATORS
 * @param {boolean} [opts.volumeInLegend=true] 副图系列是否进入顶部图例
 * @param {'main'|'mini'} [opts.layout='main']
 * @param {string[]} [opts.legendExtra]
 * @param {(c: object) => string} [opts.formatDate] 横轴与 tooltip 日期文案
 * @param {(idx: number, c: object) => string} [opts.tooltipDateLine]
 * @param {(c: object) => string[]} [opts.tooltipExtraLines]
 * @param {(v: number) => string} [opts.volumeAxisFormatter]
 */
export function buildCandleVolumeChartOption({
  candles: rawCandles,
  viewMode = "1d",
  aggregateFromDaily = false,
  defaultShowBars = 50,
  showVolume = true,
  subIndicator = "vol",
  volumeInLegend = true,
  layout = "main",
  legendExtra = [],
  formatDate,
  tooltipDateLine,
  tooltipExtraLines,
  volumeAxisFormatter,
}) {
  const candles = aggregateFromDaily ? aggregateCandlesFromDaily(rawCandles || [], viewMode) : rawCandles || [];
  if (!candles.length) return null;

  const defaultFmt = (c) => {
    const d = new Date(c.t * 1000);
    return d.toLocaleDateString("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit" });
  };
  const dates = candles.map((c) => (formatDate ? formatDate(c) : defaultFmt(c)));

  const closes = candles.map((c) => Number(c.c));
  const ma20 = sma(closes, 20);
  const ma60 = sma(closes, 60);
  const ma250 = sma(closes, 250);

  const kData = candles.map((c) => [c.o, c.c, c.l, c.h]);

  const sub = showVolume
    ? buildSubIndicatorSeries(candles, subIndicator || "vol", { volumeAxisFormatter })
    : null;

  const { start: dzStart, end: dzEnd } = dataZoomPercentLastN(candles.length, defaultShowBars);

  const legendItems = ["K线", "MA20", "MA60", "MA250"];
  if (showVolume && volumeInLegend && sub) legendItems.push(...sub.legend);
  legendItems.push(...legendExtra);

  const miniTopMain = layout === "mini" && showVolume && !volumeInLegend ? 14 : layout === "mini" ? 22 : 32;
  const miniHMain = layout === "mini" && showVolume && !volumeInLegend ? "46%" : layout === "mini" ? "42%" : "50%";
  const miniTopVol = layout === "mini" && showVolume && !volumeInLegend ? "66%" : layout === "mini" ? "68%" : "72%";
  const miniHVol = layout === "mini" && showVolume && !volumeInLegend ? "24%" : layout === "mini" ? "22%" : "18%";

  const gridMain = layout === "mini"
    ? [
        { left: 2, right: 2, top: miniTopMain, height: miniHMain },
        { left: 2, right: 2, top: miniTopVol, height: miniHVol },
      ]
    : [
        { left: 56, right: 48, top: 36, height: "48%" },
        { left: 56, right: 48, top: "72%", height: "18%" },
      ];

  const xAxisMain = [
    {
      type: "category",
      data: dates,
      boundaryGap: true,
      axisLine: layout === "mini" ? { show: false } : { lineStyle: { color: "#4b5563" } },
      axisTick: layout === "mini" ? { show: false } : undefined,
      axisLabel:
        layout === "mini"
          ? { show: false }
          : { color: "#9ca3af", fontSize: 10 },
    },
  ];
  if (showVolume) {
    xAxisMain.push({
      type: "category",
      data: dates,
      gridIndex: 1,
      boundaryGap: true,
      axisLine: layout === "mini" ? { show: false } : { lineStyle: { color: "#4b5563" } },
      axisTick: layout === "mini" ? { show: false } : undefined,
      axisLabel: { show: false },
    });
  }

  const yAxisMain = [
    {
      scale: true,
      splitLine: { lineStyle: { color: layout === "mini" ? "transparent" : "rgba(75,85,99,0.35)" } },
      axisLabel: layout === "mini" ? { show: false } : { color: "#9ca3af", fontSize: 10 },
    },
  ];
  if (showVolume && sub) {
    yAxisMain.push({
      scale: true,
      gridIndex: 1,
      splitNumber: 2,
      name: layout === "mini" ? undefined : undefined,
      splitLine: { lineStyle: { color: layout === "mini" ? "transparent" : "rgba(75,85,99,0.25)" } },
      axisLabel:
        layout === "mini"
          ? { show: false }
          : {
              color: "#9ca3af",
              fontSize: 10,
              ...(sub.yAxisFormatter ? { formatter: sub.yAxisFormatter } : {}),
            },
    });
  }

  const dataZoom =
    showVolume && candles.length > 1
      ? [
          { type: "inside", xAxisIndex: [0, 1], start: dzStart, end: dzEnd },
          {
            type: "slider",
            xAxisIndex: [0, 1],
            start: dzStart,
            end: dzEnd,
            height: layout === "mini" ? 14 : 18,
            bottom: layout === "mini" ? 0 : 6,
            borderColor: "#374151",
            fillerColor: "rgba(34,197,94,0.15)",
            handleStyle: { color: "#22c55e" },
            textStyle: { color: "#9ca3af", fontSize: layout === "mini" ? 9 : 11 },
          },
        ]
      : [{ type: "inside", xAxisIndex: [0], start: dzStart, end: dzEnd }];

  const series = [
    {
      name: "K线",
      type: "candlestick",
      data: kData,
      itemStyle: {
        color: "#f87171",
        color0: "#4ade80",
        borderColor: "#f87171",
        borderColor0: "#4ade80",
      },
    },
    {
      name: "MA20",
      type: "line",
      data: ma20,
      smooth: false,
      showSymbol: false,
      lineStyle: { width: 1, color: MA_COLORS.ma20 },
      emphasis: { disabled: true },
    },
    {
      name: "MA60",
      type: "line",
      data: ma60,
      smooth: false,
      showSymbol: false,
      lineStyle: { width: 1, color: MA_COLORS.ma60 },
      emphasis: { disabled: true },
    },
    {
      name: "MA250",
      type: "line",
      data: ma250,
      smooth: false,
      showSymbol: false,
      lineStyle: { width: 1, color: MA_COLORS.ma250 },
      emphasis: { disabled: true },
    },
  ];

  if (showVolume && sub) {
    series.push(...sub.series);
  }

  const tooltipFormatter = (params) => {
    if (!params?.length) return "";
    const idx = params[0].dataIndex;
    const c = candles[idx];
    if (!c) return "";
    const header = tooltipDateLine
      ? tooltipDateLine(idx, c)
      : `<div style="font-weight:600">${dates[idx]}</div>`;
    const lines = [`开 ${c.o}　收 ${c.c}　高 ${c.h}　低 ${c.l}`];
    if (tooltipExtraLines) lines.push(...tooltipExtraLines(c));
    if (sub?.tooltipLines) lines.push(...sub.tooltipLines(idx, c));
    for (const p of params) {
      if (p.seriesType === "line" && p.seriesName?.startsWith("MA") && p.value != null && p.value !== "") {
        lines.push(`${p.seriesName}: ${Number(p.value).toFixed(3)}`);
      }
    }
    return [header, ...lines].join("<br/>");
  };

  return {
    backgroundColor: "transparent",
    animation: false,
    legend: {
      data: legendItems,
      top: layout === "mini" ? 0 : 2,
      textStyle: { color: "#9ca3af", fontSize: layout === "mini" ? 9 : 11 },
      itemWidth: 14,
      itemHeight: layout === "mini" ? 8 : 10,
      ...(layout === "mini" ? { itemGap: 5, left: 0, right: 0 } : {}),
    },
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "cross" },
      confine: true,
      formatter: tooltipFormatter,
    },
    axisPointer: { link: [{ xAxisIndex: showVolume ? [0, 1] : [0] }] },
    grid: showVolume ? gridMain : [gridMain[0]],
    xAxis: xAxisMain,
    yAxis: yAxisMain,
    dataZoom,
    series,
  };
}
