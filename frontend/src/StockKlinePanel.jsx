import { forwardRef, useEffect, useId, useImperativeHandle, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import ReactECharts from "echarts-for-react";
import { App as AntApp, Button, Card, Spin, Alert, Grid } from "antd";
import { DownOutlined, LineChartOutlined } from "@ant-design/icons";
import { useThemeMode } from "./ThemeModeProvider.jsx";
import { useAuth } from "./AuthContext.jsx";
import * as api from "./api";
import { buildCandleVolumeChartOption, candlePaletteFromStyles, formatCandleDate, getIndicatorDataHints, getKlineChartHeight } from "./klineChartOption.js";
import KlineIndicatorControls, { useKlineIndicatorSettings } from "./KlineIndicatorControls.jsx";
import useKlineViewport from "./useKlineViewport.js";
import { candleFocus, focusCandleOption } from "./stockContextViewModel.js";
import { qk } from "./hooks/queryKeys.js";
import "./KlineViewportControls.css";

const EMPTY_CANDLES = [];
const MARKET_TIME_ZONE = "Asia/Shanghai";
const intradayTime = new Intl.DateTimeFormat("zh-CN", { timeZone: MARKET_TIME_ZONE, hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" });
const isDailyInterval = (interval) => ["1d", "1wk", "1mo"].includes(interval);
const candleLabel = (candle, interval) => `${formatCandleDate(candle.t, MARKET_TIME_ZONE)}${isDailyInterval(interval) ? "" : ` ${intradayTime.format(new Date(candle.t * 1000))}`}`;

/** 导出当前 bundle 中 K 线为 CSV（逐根 OHLCV） */
export function downloadKlineCsv(bundle, symbol, range, interval) {
  const candles = bundle?.candles || [];
  if (!candles.length) return;
  const esc = (v) => {
    const s = v == null ? "" : String(v);
    if (/[",\n\r]/.test(s)) return `"${s.replace(/"/g, '""')}"`;
    return s;
  };
  const header = ["日期时间(Asia/Shanghai)", "开盘", "最高", "最低", "收盘", "成交量(股)", "成交额", "换手率%"];
  const lines = [header.join(",")];
  for (const c of candles) {
    const ds = candleLabel(c, interval);
    lines.push(
      [
        esc(ds),
        c.o ?? "",
        c.h ?? "",
        c.l ?? "",
        c.c ?? "",
        c.v ?? "",
        c.amount ?? "",
        c.turnover_rate ?? "",
      ].join(",")
    );
  }
  const blob = new Blob(["\ufeff" + lines.join("\n")], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `kline_${symbol}_${range}_${interval}.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
}

function fmtVol(n) {
  if (n == null || Number.isNaN(n)) return "—";
  if (n >= 1e8) return `${(n / 1e8).toFixed(2)} 亿`;
  if (n >= 1e4) return `${(n / 1e4).toFixed(2)} 万`;
  return String(Math.round(n));
}

function fmtAmt(n) {
  if (n == null || Number.isNaN(n)) return "—";
  if (n >= 1e12) return `${(n / 1e12).toFixed(2)} 万亿`;
  if (n >= 1e8) return `${(n / 1e8).toFixed(2)} 亿`;
  if (n >= 1e4) return `${(n / 1e4).toFixed(2)} 万`;
  return n.toFixed(0);
}

function volAxisLabel(v) {
  const n = Number(v);
  if (Number.isNaN(n)) return "";
  if (n >= 1e8) return `${(n / 1e8).toFixed(1)}亿`;
  if (n >= 1e4) return `${(n / 1e4).toFixed(0)}万`;
  return String(Math.round(n));
}

function fmtMcapYuan(n) {
  if (n == null || Number.isNaN(n)) return "—";
  if (n >= 1e12) return `${(n / 1e12).toFixed(2)} 万亿`;
  if (n >= 1e8) return `${(n / 1e8).toFixed(2)} 亿`;
  return `${(n / 1e4).toFixed(2)} 万`;
}

function fmtWanLots(lots) {
  if (lots == null || Number.isNaN(Number(lots))) return "—";
  return `${(Number(lots) / 10000).toFixed(2)}万`;
}

/**
 * K 线面板。hideChrome 时不渲染代码/区间控件（由外层工具条托管）。
 * ref: { refresh, download, loading, canDownload }
 */
const StockKlinePanel = forwardRef(function StockKlinePanel(
  {
    embedded = false,
    symbol = "",
    chartHeight = 520,
    displayName = "",
    hideChrome = false,
    range = "1y",
    interval = "1d",
    highlightDate = null,
  },
  ref
) {
  const { message } = AntApp.useApp();
  const qc = useQueryClient();
  const { mode } = useThemeMode();
  const { user } = useAuth();
  const screens = Grid.useBreakpoint();
  const compact = !screens.md;
  const panelRef = useRef(null);
  const [palette, setPalette] = useState(undefined);
  const [detailsOpen, setDetailsOpen] = useState(false);
  const detailsId = useId();

  useEffect(() => {
    // ThemeModeProvider applies data-theme in its effect. Read after that commit
    // so canvas tokens and the surrounding CSS always use the same appearance.
    const frame = requestAnimationFrame(() => {
      if (panelRef.current) setPalette(candlePaletteFromStyles(getComputedStyle(panelRef.current)));
    });
    return () => cancelAnimationFrame(frame);
  }, [mode]);
  const { settings: indicatorSettings, setSettings: setIndicatorSettings } = useKlineIndicatorSettings("stock");
  const resolvedChartHeight = getKlineChartHeight(chartHeight, indicatorSettings);
  const klineSymbol = symbol.trim().toUpperCase();

  const klineQuery = useQuery({
    queryKey: qk.kline(klineSymbol, range, interval),
    queryFn: () => api.getKline(klineSymbol, range, interval),
    enabled: !!klineSymbol?.trim(),
    staleTime: 60_000,
  });

  const bundle = klineQuery.data ?? null;
  const candles = bundle?.candles || EMPTY_CANDLES;
  const visibleBars = indicatorSettings.display.visibleBars;
  const { applyViewport, onEvents, resetViewport, showAll, revision } = useKlineViewport({
    contextKey: JSON.stringify([user?.id ?? "anonymous", klineSymbol, range, interval, highlightDate]),
    candles,
    visibleBars,
  });
  const indicatorHints = useMemo(() => getIndicatorDataHints(candles, indicatorSettings), [candles, indicatorSettings]);
  const loading = klineQuery.isFetching;
  const err = klineQuery.isError ? api.getApiErrorMessage(klineQuery.error) : "";

  useImperativeHandle(
    ref,
    () => ({
      refresh: () => {
        const s = (klineSymbol || "").trim().toUpperCase();
        if (!s) {
          message.warning("请先选择股票");
          return;
        }
        void qc.invalidateQueries({ queryKey: qk.kline(s, range, interval) });
      },
      download: () => {
        if (!bundle?.candles?.length) {
          message.warning("暂无 K 线数据");
          return;
        }
        downloadKlineCsv(bundle, klineSymbol, range, interval);
      },
      get loading() {
        return loading;
      },
      get canDownload() {
        return Boolean(bundle?.candles?.length);
      },
    }),
    [bundle, interval, klineSymbol, loading, message, qc, range]
  );

  const chartOption = useMemo(() => {
    if (!candles.length) return null;
    const option = buildCandleVolumeChartOption({
      candles,
      palette,
      aggregateFromDaily: false,
      viewMode: "1d",
      defaultShowBars: visibleBars,
      compact,
      timeZone: MARKET_TIME_ZONE,
      showVolume: true,
      indicatorSettings,
      volumeInLegend: false,
      layout: "main",
      formatDate: (c) => candleLabel(c, interval),
      tooltipExtraLines: (c) => {
        const tr = c.turnover_rate != null ? `${c.turnover_rate}%` : "—";
        const amt = c.amount != null ? fmtAmt(c.amount) : "—";
        return [`量 ${fmtVol(c.v)}　额 ${amt}　换手 ${tr}`];
      },
      volumeAxisFormatter: volAxisLabel,
    });
    return applyViewport(focusCandleOption(option, candles, highlightDate));
  }, [candles, interval, indicatorSettings, palette, highlightDate, compact, visibleBars, applyViewport, revision]);
  const focus = candleFocus(candles, highlightDate);

  const m = bundle?.metrics || {};

  const metrics = m
    ? [
        {
          key: "price",
          label: "最新价",
          value: m.latest_close != null ? String(m.latest_close) : "—",
          unit: m.currency === "CNY" ? "元" : m.currency || "",
          tone: null,
          emphasis: true,
        },
        {
          key: "chg",
          label: "涨跌幅",
          value:
            m.change_pct != null ? `${m.change_pct > 0 ? "+" : ""}${m.change_pct}%` : "—",
          tone: m.change_pct > 0 ? "up" : m.change_pct < 0 ? "down" : null,
          emphasis: true,
        },
        { key: "prev", label: "昨收", value: m.previous_close ?? "—" },
        { key: "open", label: "今开", value: m.latest_open ?? "—" },
        { key: "high", label: "最高", value: m.latest_high ?? "—" },
        { key: "low", label: "最低", value: m.latest_low ?? "—" },
        { key: "vol", label: "成交量", value: fmtVol(m.latest_volume) },
        { key: "amt", label: "成交额", value: fmtAmt(m.latest_amount) },
        {
          key: "turn",
          label: "换手率",
          value: m.latest_turnover_rate != null ? `${m.latest_turnover_rate}%` : "—",
        },
        { key: "pe", label: "市盈率", value: m.pe_ttm != null ? String(m.pe_ttm) : "—" },
        {
          key: "mcap",
          label: "总市值",
          value: m.total_market_cap_yuan != null ? fmtMcapYuan(m.total_market_cap_yuan) : "—",
          sub:
            m.float_market_cap_yuan != null
              ? `流通 ${fmtMcapYuan(m.float_market_cap_yuan)}`
              : null,
        },
        {
          key: "up",
          label: "涨停",
          value: m.limit_up != null ? Number(m.limit_up).toFixed(2) : "—",
          tone: "up",
        },
        {
          key: "down",
          label: "跌停",
          value: m.limit_down != null ? Number(m.limit_down).toFixed(2) : "—",
          tone: "down",
        },
        {
          key: "vr",
          label: "量比",
          value: m.volume_ratio != null ? Number(m.volume_ratio).toFixed(2) : "—",
        },
        { key: "inner", label: "内盘", value: fmtWanLots(m.inner_lots), tone: "down" },
        { key: "outer", label: "外盘", value: fmtWanLots(m.outer_lots), tone: "up" },
      ]
    : [];

  const primary = metrics.find((item) => item.key === "price");
  const change = metrics.find((item) => item.key === "chg");
  const coreKeys = ["open", "high", "low", "vol", "turn"];
  const coreMetrics = coreKeys.map((key) => metrics.find((item) => item.key === key));
  const detailMetrics = metrics.filter((item) => !["price", "chg", ...coreKeys].includes(item.key));
  const renderMetric = (item) => <div className="market-quote-stat" key={item.key}>
    <dt>{item.label}</dt>
    <dd className={item.tone ? `is-${item.tone}` : undefined}>{item.value}</dd>
    {item.sub ? <span className="market-quote-stat__note">{item.sub}</span> : null}
  </div>;

  const body = (
    <div className="market-kline" ref={panelRef}>
      <Spin spinning={loading}>
        <div className="market-quote-summary">
          <div className="market-quote-price">
            <div className="market-quote-price__value">{primary.value}
              {primary.unit ? <span>{primary.unit}</span> : null}
            </div>
            <div className={`market-quote-price__change${change.tone ? ` is-${change.tone}` : ""}`}>
              {m.latest_close != null ? <><span>{change.value}</span><span className="market-quote-price__label">涨跌幅</span></> : "等待行情数据"}
            </div>
          </div>
          <dl className="market-quote-core">{coreMetrics.map(renderMetric)}</dl>
          <Button type="text" size="small" className="market-quote-details-toggle"
            aria-expanded={detailsOpen} aria-controls={detailsId}
            onClick={() => setDetailsOpen((value) => !value)}>
            行情详情 <DownOutlined rotate={detailsOpen ? 180 : 0} />
          </Button>
        </div>
        <div className="market-quote-details" id={detailsId} hidden={!detailsOpen}>
          <dl>{detailMetrics.map(renderMetric)}</dl>
        </div>

        <div className="market-chart-caption stock-replay-chart-toolbar">
          <span>{interval === "1wk" ? "周线走势" : interval === "1mo" ? "月线走势" : "日线走势"}</span>
          <div className="stock-replay-viewport-actions" role="group" aria-label="股票 K 线显示范围">
            <Button size="small" onClick={resetViewport} disabled={!candles.length}>最近 {visibleBars} 根</Button>
            <Button size="small" onClick={showAll} disabled={!candles.length}>全部</Button>
          </div>
        </div>
        <KlineIndicatorControls settings={indicatorSettings} onChange={setIndicatorSettings} compact={compact} />
        {highlightDate ? <p className="market-data-asof">日期定位 {highlightDate}：{focus ? `${focus.exact ? "对应行情" : "最近可用行情"} ${focus.date}${interval !== "1d" ? "（当前为聚合周期）" : ""}` : "已加载行情未覆盖该日期"}。</p> : null}
        {chartOption ? (
          <div className="market-canvas" aria-label={`${displayName || klineSymbol} K 线及技术指标`}>
            <ReactECharts option={chartOption} onEvents={onEvents} style={{ height: resolvedChartHeight, width: "100%" }} notMerge lazyUpdate autoResize />
          </div>
        ) : <div className="market-chart-empty" style={{ minHeight: chartHeight }} role="status">
          <LineChartOutlined aria-hidden="true" />
          <strong>{loading ? "正在读取行情" : err ? "行情暂不可用" : "暂无行情数据"}</strong>
          {!loading ? <span>{err ? "刷新重试" : "刷新或选择其他股票"}</span> : null}
        </div>}
        {candles.length && indicatorHints.length ? <div className="stock-replay-indicator-hints" role="status">{indicatorHints.map((hint) => <p key={hint}>{hint}</p>)}</div> : null}
      </Spin>

      {err ? <Alert type="error" showIcon className="market-data-alert" message={err} /> : null}
      {bundle?.data_as_of ? <p className={`market-data-asof${bundle.cache_stale ? " is-stale" : ""}`}>
        <span className="market-data-asof__dot" aria-hidden="true" />
        数据截至 {bundle.data_as_of}{bundle.cache_stale ? " · 缓存数据" : ""}
      </p> : null}
    </div>
  );

  void hideChrome;
  return embedded ? body : <Card variant="borderless" title="K 线">{body}</Card>;
});

export default StockKlinePanel;
