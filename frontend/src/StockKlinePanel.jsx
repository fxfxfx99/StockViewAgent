import { forwardRef, useEffect, useImperativeHandle, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import ReactECharts from "echarts-for-react";
import { Card, Spin, Typography, Alert, message } from "antd";
import * as api from "./api";
import { buildCandleVolumeChartOption } from "./klineChartOption.js";
import { SUB_INDICATORS } from "./klineIndicators.js";
import { qk } from "./hooks/queryKeys.js";
import { useAppUrlState } from "./hooks/useAppUrlState.js";

const { Text } = Typography;
const SUB_IND_KEY = "sva_kline_sub_indicator";

/** 导出当前 bundle 中 K 线为 CSV（逐根 OHLCV） */
export function downloadKlineCsv(bundle, symbol, range, interval) {
  const candles = bundle?.candles || [];
  if (!candles.length) return;
  const esc = (v) => {
    const s = v == null ? "" : String(v);
    if (/[",\n\r]/.test(s)) return `"${s.replace(/"/g, '""')}"`;
    return s;
  };
  const header = ["日期时间(本地)", "开盘", "最高", "最低", "收盘", "成交量(股)", "成交额", "换手率%"];
  const lines = [header.join(",")];
  for (const c of candles) {
    const d = new Date((c.t || 0) * 1000);
    const ds =
      interval === "1d" || interval === "1wk" || interval === "1mo"
        ? `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`
        : d.toLocaleString("zh-CN", { hour12: false });
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

function fmtHttpErr(e) {
  if (!e) return "";
  const d = e.response?.data?.detail;
  if (typeof d === "string") return d;
  if (d) return JSON.stringify(d);
  return e.message || "加载失败";
}

/**
 * K 线面板。hideChrome 时不渲染代码/区间控件（由外层工具条托管）。
 * ref: { refresh, download, loading, canDownload }
 */
const StockKlinePanel = forwardRef(function StockKlinePanel(
  {
    embedded = false,
    chartHeight = 520,
    displayName = "",
    hideChrome = false,
    range = "1y",
    interval = "1d",
  },
  ref
) {
  const qc = useQueryClient();
  const { chartSymbol } = useAppUrlState();
  const [committedSymbol, setCommittedSymbol] = useState(() => chartSymbol || "");
  const [subIndicator, setSubIndicator] = useState(() => {
    try {
      const saved = localStorage.getItem(SUB_IND_KEY);
      if (saved && SUB_INDICATORS.some((x) => x.id === saved)) return saved;
    } catch {
      /* ignore */
    }
    return "vol";
  });

  const onSubIndicatorChange = (id) => {
    setSubIndicator(id);
    try {
      localStorage.setItem(SUB_IND_KEY, id);
    } catch {
      /* ignore */
    }
  };
  useEffect(() => {
    if (chartSymbol) setCommittedSymbol(chartSymbol);
  }, [chartSymbol]);

  const klineSymbol = chartSymbol ?? committedSymbol;

  const klineQuery = useQuery({
    queryKey: qk.kline(klineSymbol, range, interval),
    queryFn: () => api.getKline(klineSymbol, range, interval),
    enabled: !!klineSymbol?.trim(),
    staleTime: 60_000,
  });

  const bundle = klineQuery.data ?? null;
  const loading = klineQuery.isFetching;
  const err = klineQuery.isError ? fmtHttpErr(klineQuery.error) : "";

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
    [bundle, interval, klineSymbol, loading, qc, range]
  );

  const chartOption = useMemo(() => {
    const candles = bundle?.candles || [];
    if (!candles.length) return null;
    return buildCandleVolumeChartOption({
      candles,
      aggregateFromDaily: false,
      viewMode: "1d",
      defaultShowBars: 50,
      showVolume: true,
      subIndicator,
      volumeInLegend: false,
      layout: "main",
      formatDate: (c) => {
        const d = new Date(c.t * 1000);
        return interval === "1d" || interval === "1wk" || interval === "1mo"
          ? d.toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" })
          : d.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
      },
      tooltipExtraLines: (c) => {
        const tr = c.turnover_rate != null ? `${c.turnover_rate}%` : "—";
        const amt = c.amount != null ? fmtAmt(c.amount) : "—";
        return [`量 ${fmtVol(c.v)}　额 ${amt}　换手 ${tr}`];
      },
      volumeAxisFormatter: volAxisLabel,
    });
  }, [bundle, interval, subIndicator]);

  const m = bundle?.metrics;

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

  const body = (
    <>
      <Spin spinning={loading}>
        {metrics.length ? (
          <div className="sva-metrics">
            {metrics.map((item) => (
              <div
                key={item.key}
                className={`sva-metric${item.emphasis ? " is-emphasis" : ""}`}
              >
                <span className="sva-metric__label">{item.label}</span>
                <span
                  className={`sva-metric__value${
                    item.tone === "up" ? " is-up" : item.tone === "down" ? " is-down" : ""
                  }`}
                >
                  {item.value}
                  {item.unit ? <span className="sva-metric__unit">{item.unit}</span> : null}
                </span>
                {item.sub ? <span className="sva-metric__sub">{item.sub}</span> : null}
              </div>
            ))}
          </div>
        ) : null}

        {chartOption ? (
          <div className="sva-chart-wrap">
            <div className="sva-subind-picker">
              <select
                className="sva-subind-select"
                value={subIndicator}
                aria-label="副图指标"
                onChange={(e) => onSubIndicatorChange(e.target.value)}
              >
                {SUB_INDICATORS.map((opt) => (
                  <option key={opt.id} value={opt.id}>
                    {opt.label}
                  </option>
                ))}
              </select>
            </div>
            <ReactECharts option={chartOption} style={{ height: chartHeight, width: "100%" }} notMerge lazyUpdate />
          </div>
        ) : (
          !loading && <Text type="secondary">暂无数据</Text>
        )}
      </Spin>

      {err ? (
        <Alert type="error" showIcon style={{ marginTop: 10 }} message={err} />
      ) : bundle?.cache_stale && bundle?.data_as_of ? (
        <p className="sva-data-asof is-stale">数据截至 {bundle.data_as_of}（缓存）</p>
      ) : bundle?.data_as_of ? (
        <p className="sva-data-asof">数据截至 {bundle.data_as_of}</p>
      ) : null}
    </>
  );

  void hideChrome;
  void displayName;

  if (embedded) {
    return <div className="ex-stock-kline-embedded">{body}</div>;
  }

  return (
    <Card bordered={false} title="K 线">
      {body}
    </Card>
  );
});

export default StockKlinePanel;
