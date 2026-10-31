export const MERRILL_PHASES = [
  { id: "recovery", label: "复苏", growth: "增长走强", inflation: "通胀回落", asset: "股票", color: "#4e9b8c" },
  { id: "overheat", label: "过热", growth: "增长走强", inflation: "通胀上行", asset: "商品", color: "#c59750" },
  { id: "stagflation", label: "滞胀", growth: "增长放缓", inflation: "通胀上行", asset: "现金", color: "#b27680" },
  { id: "reflation", label: "衰退", growth: "增长放缓", inflation: "通胀回落", asset: "债券", color: "#6d8ec0" },
];

export const MERRILL_COLORS = ["#668fd0", "#bd9550", "#4e9b8c", "#aa7fc3", "#c37880", "#55a8b5", "#d28b5c", "#8b9b58", "#7f80bf", "#b77c9b", "#549c73", "#bda25e", "#65899d", "#9e8771", "#ba7164", "#7b9bad", "#8fa475", "#b289bf", "#7394bf", "#9e7f6b"];

export function merrillSeriesColor(id, index = 0) {
  return ({ sp500: "#668fd0", gold: "#bd9550", treasury_long: "#4e9b8c", usd: "#aa7fc3" })[id] || MERRILL_COLORS[Math.max(0, index) % MERRILL_COLORS.length];
}

export function merrillNumber(value) {
  if (value === null || value === undefined || value === "" || typeof value === "boolean") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

export function merrillKey(userId, ...parts) { return ["merrill-clock", userId ?? "local", ...parts]; }

export function merrillDate(value, withTime = false) {
  if (value === null || value === undefined || value === "" || typeof value === "boolean") return "—";
  if (!withTime && /^\d{4}-\d{2}-\d{2}/.test(String(value))) return String(value).slice(0, 10);
  const numeric = typeof value === "number" || /^-?\d+(\.\d+)?$/.test(String(value)) ? Number(value) : null;
  const date = new Date(numeric === null ? value : Math.abs(numeric) < 100_000_000_000 ? numeric * 1000 : numeric);
  if (!Number.isFinite(date.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit", ...(withTime ? { hour: "2-digit", minute: "2-digit", hour12: false } : {}) }).format(date);
}

export function merrillValue(value, digits = 2) {
  const number = merrillNumber(value);
  return number === null ? "—" : number.toLocaleString("zh-CN", { maximumFractionDigits: digits, minimumFractionDigits: digits });
}

export function merrillAssetClass(item) {
  const kind = item.asset_class || item.kind || "其他";
  return ({ equity: "股票", equities: "股票", stock: "股票", bond: "债券", bonds: "债券", fixed_income: "债券", credit: "信用债", commodity: "商品", commodities: "商品", currency: "汇率", currencies: "汇率", fx: "汇率", real_estate: "房地产", reit: "房地产", cash: "现金", volatility: "波动率", macro: "宏观" })[kind] || kind;
}

export function defaultMerrillSelection(catalog) {
  const patterns = [/(sp500|s&p\s*500|标普\s*500)/i, /(gold|黄金)/i, /(treasury_long|长期.*国债|国债.*ETF)/i, /(\busd\b|dollar|美元指数|dxy)/i];
  const selected = [];
  for (const pattern of patterns) {
    const item = catalog.find((row) => !selected.includes(row.id) && pattern.test(`${row.id} ${row.name} ${row.symbol || ""}`));
    if (item) selected.push(item.id);
  }
  for (const item of catalog) {
    if (selected.length >= 4) break;
    if (!selected.includes(item.id)) selected.push(item.id);
  }
  return selected;
}

export function merrillClockPosition(cycle) {
  if (cycle?.status !== "ready") return null;
  const x = merrillNumber(cycle.position?.x);
  const y = merrillNumber(cycle.position?.y);
  if (x === null || y === null || !MERRILL_PHASES.some((phase) => phase.id === cycle.phase)) return null;
  // Preserve signs and direction while keeping the node inside the dial.
  const length = Math.hypot(x, y);
  const scale = length > 1 ? 1 / length : 1;
  return { x: 210 + x * scale * 132, y: 210 - y * scale * 132 };
}

function validPoints(points) {
  return (points || []).filter((point) => /^\d{4}-\d{2}-\d{2}$/.test(point.date || "") && Number.isFinite(Date.parse(point.date))).slice().sort((a, b) => a.date.localeCompare(b.date));
}

export function transformMerrillSeries(series, mode = "normalized", baselineDate = null) {
  return (series || []).map((item) => {
    const points = validPoints(item.points);
    const baseline = points.find((point) => (!baselineDate || point.date >= baselineDate) && merrillNumber(point.close) !== null && merrillNumber(point.close) > 0);
    let peak = null;
    return { ...item, baseline_date: baseline?.date || null, points: points.map((point) => {
      const value = merrillNumber(point.close);
      let plotted = value;
      if (mode === "normalized" && item.kind !== "yield") plotted = !baseline || point.date < baseline.date || value === null || value < 0 ? null : value / Number(baseline.close) * 100;
      if (mode === "drawdown" && item.kind !== "yield") {
        if (value === null || value < 0 || (peak === null && value === 0)) plotted = null;
        else { peak = peak === null ? value : Math.max(peak, value); plotted = (value / peak - 1) * 100; }
      }
      return { ...point, value: plotted };
    }) };
  });
}

export function buildMerrillChartOption({ series = [], mode = "normalized", baselineDate = null, isLight = false, catalog = [] }) {
  const transformed = transformMerrillSeries(series, mode, baselineDate);
  if (!transformed.some((item) => item.points.some((point) => point.value !== null))) return null;
  const dates = [...new Set(transformed.flatMap((item) => item.points.map((point) => point.date)))].sort();
  // Explicit empty weekly buckets ensure missing weeks never become a connecting line.
  const weeklyDates = [];
  for (let index = 0; index < dates.length; index++) {
    weeklyDates.push(dates[index]);
    if (index + 1 < dates.length) {
      let next = Date.parse(dates[index]) + 7 * 86_400_000;
      while (next < Date.parse(dates[index + 1])) {
        weeklyDates.push(new Date(next).toISOString().slice(0, 10));
        next += 7 * 86_400_000;
      }
    }
  }
  const text = isLight ? "#626c7e" : "#b0b6c3";
  const line = isLight ? "#e7ebf0" : "#3b3f46";
  const hasYield = transformed.some((item) => item.kind === "yield");
  return {
    animation: false,
    backgroundColor: "transparent",
    textStyle: { fontFamily: "-apple-system, BlinkMacSystemFont, PingFang SC, sans-serif", color: text },
    aria: { enabled: true, label: { description: "所选资产的历史周度曲线。空缺周不连线；曲线数值可通过悬浮读取。" } },
    tooltip: { trigger: "axis", renderMode: "richText", confine: true, backgroundColor: isLight ? "#ffffff" : "#282a2f", borderColor: line, textStyle: { color: text }, valueFormatter: (value) => value == null ? "无观测" : `${merrillValue(value)}${mode === "drawdown" ? "%" : ""}` },
    legend: { type: "scroll", bottom: 0, itemWidth: 16, itemHeight: 3, icon: "roundRect", textStyle: { color: text }, pageTextStyle: { color: text }, pageIconColor: text },
    grid: { left: 16, right: 22, top: 34, bottom: 92, containLabel: true },
    xAxis: { type: "category", data: weeklyDates, boundaryGap: false, axisLine: { lineStyle: { color: line } }, axisTick: { show: false }, axisLabel: { color: text, hideOverlap: true, formatter: (value) => value.slice(0, 7) } },
    yAxis: [{ type: "value", name: mode === "normalized" ? "基准 = 100" : mode === "drawdown" ? "回撤 %" : "原始单位", nameTextStyle: { color: text, align: "left" }, axisLabel: { color: text, formatter: (value) => Math.abs(value) >= 10000 ? `${(value / 10000).toFixed(1)}万` : merrillValue(value, Math.abs(value) < 10 ? 1 : 0) }, splitLine: { lineStyle: { color: line, type: "dashed" } }, scale: mode === "raw" }, ...(hasYield ? [{ type: "value", name: "收益率 %", position: "right", nameTextStyle: { color: text, align: "right" }, axisLabel: { color: text, formatter: (value) => `${merrillValue(value, 1)}%` }, splitLine: { show: false }, scale: true }] : [])],
    dataZoom: [{ type: "inside", filterMode: "none", start: 0, end: 100 }, { type: "slider", bottom: 35, height: 18, borderColor: line, showDetail: false, textStyle: { color: text }, fillerColor: isLight ? "rgba(102,143,208,.12)" : "rgba(102,143,208,.16)", dataBackground: { areaStyle: { color: line }, lineStyle: { color: text } }, selectedDataBackground: { areaStyle: { color: "#668fd0" }, lineStyle: { color: "#668fd0" } } }],
    series: transformed.map((item, index) => {
      const values = new Map(item.points.map((point) => [point.date, point.value]));
      const catalogIndex = catalog.findIndex((row) => row.id === item.id);
      const isYield = item.kind === "yield";
      return { id: item.id, name: `${item.name || item.id}${isYield ? " · 右轴%" : ""}`, type: "line", yAxisIndex: isYield ? 1 : 0, showSymbol: false, symbolSize: 5, connectNulls: false, lineStyle: { width: 1.7, type: isYield ? "dashed" : "solid" }, tooltip: { valueFormatter: (value) => value == null ? "无观测" : `${merrillValue(value)}${isYield || mode === "drawdown" ? "%" : mode === "raw" ? ` ${item.unit || ""}` : ""}` }, itemStyle: { color: merrillSeriesColor(item.id, catalogIndex < 0 ? index : catalogIndex) }, emphasis: { focus: "series", lineStyle: { width: 3 } }, data: weeklyDates.map((date) => values.get(date) ?? null) };
    }),
  };
}

export function merrillJobActive(job) { return ["queued", "running", "pending", "retrying"].includes(job?.status); }
