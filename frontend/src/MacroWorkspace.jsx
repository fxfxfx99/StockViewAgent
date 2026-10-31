import { lazy, Suspense, useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import ReactECharts from "echarts-for-react";
import { Alert, Button, Layout, Select, Spin, Tabs, Tag } from "antd";
import ReloadOutlined from "@ant-design/icons/es/icons/ReloadOutlined";
import AppToolbar from "./AppToolbar.jsx";
import { ConfigConsoleButton } from "./ConfigConsole.jsx";
import { useAuth } from "./AuthContext.jsx";
import { useThemeMode } from "./ThemeModeProvider.jsx";
import { buildCandleVolumeChartOption, candlePaletteFromStyles, getKlineChartHeight } from "./klineChartOption.js";
import KlineIndicatorControls, { useKlineIndicatorSettings } from "./KlineIndicatorControls.jsx";
import * as api from "./api.js";
import { MACRO_RANGES, MACRO_TABS, MACRO_VIEWS, candleSummary, macroDate, macroKey, macroNumber, macroPercent, validOption } from "./macroViewModel.js";
import "./MacroWorkspace.css";

const MacroCapitalPanel = lazy(() => import("./MacroCapitalPanel.jsx"));
const MacroAnalysisPanel = lazy(() => import("./MacroAnalysisPanel.jsx"));
const EMPTY = [];

export function MacroReadError({ query }) {
  return query.isError ? <Alert className="macro-alert" type="warning" showIcon message={api.getApiErrorMessage(query.error)} description={query.data ? "显示上次读取的数据。" : undefined} action={<Button size="small" onClick={() => void query.refetch()}>重试</Button>} /> : null;
}

function MarketChart({ query, title, view, aggregate = false, onRefresh, note, indicatorSettings }) {
  const { mode } = useThemeMode();
  const ref = useRef(null);
  const chartRef = useRef(null);
  const [palette, setPalette] = useState();
  const bundle = query.data;
  const candles = bundle?.candles || EMPTY;
  const summary = useMemo(() => candleSummary(candles), [candles]);
  useEffect(() => {
    const frame = requestAnimationFrame(() => { if (ref.current) setPalette(candlePaletteFromStyles(getComputedStyle(ref.current))); });
    return () => cancelAnimationFrame(frame);
  }, [mode]);
  useEffect(() => {
    if (!ref.current || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => chartRef.current?.getEchartsInstance()?.resize({ width: "auto", height: "auto" }));
    observer.observe(ref.current);
    return () => observer.disconnect();
  }, []);
  const option = useMemo(() => buildCandleVolumeChartOption({ candles, viewMode: view, aggregateFromDaily: aggregate, defaultShowBars: 80, palette, indicatorSettings, showVolume: candles.some((item) => item.v != null && Number(item.v) > 0), formatDate: (item) => macroDate(item.t) }), [candles, view, aggregate, palette, indicatorSettings]);
  const warning = bundle?.cache_warning || bundle?.warning || (bundle?.cache_stale ? "来源未更新，显示缓存数据。" : "");
  return <section className="macro-card macro-chart-card" ref={ref} aria-label={`${title}行情图`}>
    <div className="macro-card-heading"><div><h2>{title}</h2>{note ? <p>{note}</p> : null}</div><Button icon={<ReloadOutlined aria-hidden="true" />} loading={query.isFetching} onClick={onRefresh || (() => void query.refetch())}>刷新行情</Button></div>
    <MacroReadError query={query} />
    {warning ? <Alert className="macro-alert" type="warning" showIcon message={warning} /> : null}
    {bundle ? <><div className="macro-quotes"><div><span>最新收盘</span><strong>{macroNumber(summary.close)}</strong></div><div><span>本期涨跌</span><strong className={summary.change > 0 ? "macro-up" : summary.change < 0 ? "macro-down" : ""}>{macroPercent(summary.change)}</strong></div><div><span>区间涨跌</span><strong className={summary.totalChange > 0 ? "macro-up" : summary.totalChange < 0 ? "macro-down" : ""}>{macroPercent(summary.totalChange)}</strong></div><div><span>数据截至</span><strong className="macro-date-stat">{bundle.data_as_of || macroDate(summary.timestamp)}</strong></div></div>
      <div className="macro-chart-meta"><span>来源 {bundle.source || bundle.kline_source || bundle.provider || (bundle.tushare_doc_id ? "Tushare" : "来源未标注")}</span><span>{summary.count} 根原始 K 线</span>{bundle.served_from_cache ? <Tag color={bundle.cache_stale ? "orange" : "default"}>缓存</Tag> : null}{aggregate && view !== "1d" ? <span>由日线合成{view === "1wk" ? "周K" : "月K"}</span> : null}</div></> : null}
    {option ? <div className="macro-chart-canvas" style={{ "--macro-indicator-extra-height": `${getKlineChartHeight(460, indicatorSettings) - 460}px` }} role="img" aria-label={`${title}K线与所选技术指标`}><ReactECharts ref={chartRef} option={option} style={{ height: "100%", width: "100%" }} notMerge lazyUpdate /></div> : <div className="macro-empty">{query.isFetching ? <><Spin /><p>正在读取行情…</p></> : <p>暂无可显示的行情数据</p>}</div>}
    <p className="macro-footnote">拖动时间轴或缩放图表，可调整查看范围。</p>
  </section>;
}

function IndexPanel({ userId, indices, indexKey, range, view, indicatorSettings }) {
  const qc = useQueryClient();
  const key = macroKey(userId, "index", indexKey, range, view);
  const [refreshing, setRefreshing] = useState(false);
  const query = useQuery({ queryKey: key, queryFn: ({ signal }) => api.getMacroIndexKline({ index_key: indexKey, range, interval: view }, { signal }), enabled: Boolean(indexKey), staleTime: 60_000, retry: false });
  const title = indices.find((item) => item.key === indexKey)?.label || indexKey;
  async function refresh() {
    setRefreshing(true);
    try { await qc.fetchQuery({ queryKey: key, queryFn: ({ signal }) => api.getMacroIndexKline({ index_key: indexKey, range, interval: view, refresh: true }, { signal }), staleTime: 0, retry: false }); } catch { /* Query exposes the failure while retaining previous data. */ } finally { setRefreshing(false); }
  }
  return <MarketChart query={{ ...query, isFetching: query.isFetching || refreshing }} title={title} view={view} indicatorSettings={indicatorSettings} onRefresh={() => void refresh()} />;
}

function MacroSeriesPanel({ userId, range, view, seriesId, industryId, patch, indicatorSettings }) {
  const catalog = useQuery({ queryKey: macroKey(userId, "catalog"), queryFn: ({ signal }) => api.getMacroCatalog({ signal }), staleTime: 60_000 });
  const series = catalog.data?.items || EMPTY;
  const selected = series.find((item) => item.id === seriesId) || series.find((item) => item.id === "sh") || series[0];
  const available = selected?.available !== false;
  const seriesQuery = useQuery({ queryKey: macroKey(userId, "series", selected?.id, range), queryFn: ({ signal }) => api.getMacroKline(selected.id, range, { signal }), enabled: Boolean(selected?.id && available), staleTime: 60_000, retry: false });
  const industryEnabled = catalog.data?.tushare_configured === true;
  const industries = useQuery({ queryKey: macroKey(userId, "industries"), queryFn: ({ signal }) => api.getMacroIndustryIndices({ signal }), enabled: industryEnabled, staleTime: 5 * 60_000, retry: false });
  const industryItems = industries.data?.items || EMPTY;
  const industry = industryItems.find((item) => item.ts_code === industryId) || industryItems[0];
  const industryQuery = useQuery({ queryKey: macroKey(userId, "industry", industry?.ts_code, range), queryFn: ({ signal }) => api.getMacroIndustryKline(industry.ts_code, range, { signal }), enabled: industryEnabled && Boolean(industry?.ts_code), staleTime: 60_000, retry: false });
  return <div className="macro-panel-stack"><MacroReadError query={catalog} />
    <div className="macro-selection"><label htmlFor="macro-series">宏观序列</label><Select id="macro-series" loading={catalog.isFetching} value={selected?.id} onChange={(value) => patch({ series: value })} options={series.map((item) => ({ value: item.id, label: `${item.name}${item.requires_token ? " · Tushare" : ""}` }))} /></div>
    {selected && !available ? <div className="macro-card macro-access"><h2>{selected.name}</h2><p>{selected.note || selected.provider_hint}</p><Alert type="info" showIcon message="在设置中配置 Tushare Token 及数据权限后，刷新页面。" /></div> : selected ? <MarketChart query={seriesQuery} title={selected.name} note={selected.note || selected.provider_hint} view={view} indicatorSettings={indicatorSettings} aggregate /> : catalog.isPending ? <div className="macro-empty"><Spin /></div> : null}
    <section className="macro-industry"><div className="macro-section-heading"><h2>申万行业指数</h2><span>{industries.data?.trade_date ? `列表基准日 ${industries.data.trade_date}` : ""}</span></div>
      {!industryEnabled ? <Alert type="info" showIcon message="需配置 Tushare Token 及行业数据权限。" /> : <><MacroReadError query={industries} />{industries.data?.cache_warning ? <Alert className="macro-alert" type="warning" showIcon message={industries.data.cache_warning} /> : null}<div className="macro-selection"><label htmlFor="macro-industry">选择行业</label><Select id="macro-industry" showSearch optionFilterProp="label" loading={industries.isFetching} value={industry?.ts_code} onChange={(value) => patch({ industry: value })} options={industryItems.map((item) => ({ value: item.ts_code, label: `${item.name}（${item.ts_code}）` }))} placeholder="选择行业指数" /></div>{industry ? <MarketChart query={industryQuery} title={`${industry.name} · ${industry.ts_code}`} view={view} indicatorSettings={indicatorSettings} aggregate /> : industries.isFetching ? <div className="macro-empty"><Spin /></div> : <p className="macro-footnote">暂无可用行业列表。</p>}</>}
    </section>
  </div>;
}

export default function MacroWorkspace() {
  const { user, authRequired, logout } = useAuth();
  const [params, setParams] = useSearchParams();
  const { settings: indicatorSettings, setSettings: setIndicatorSettings } = useKlineIndicatorSettings("macro");
  const tab = validOption(params.get("tab"), MACRO_TABS.map((item) => item.key), "indices");
  const range = validOption(params.get("range"), MACRO_RANGES.map((item) => item.value), "1y");
  const view = validOption(params.get("view"), MACRO_VIEWS.map((item) => item.value), "1d");
  const indices = useQuery({ queryKey: macroKey(user?.id, "indices"), queryFn: ({ signal }) => api.getMacroIndices({ signal }), staleTime: 5 * 60_000 });
  const list = indices.data?.items || EMPTY;
  const indexKey = list.find((item) => item.key === params.get("index"))?.key || list.find((item) => item.key === "SH_COMP")?.key || list[0]?.key;
  function patch(values) { setParams((previous) => { const next = new URLSearchParams(previous); for (const [key, value] of Object.entries(values)) value ? next.set(key, value) : next.delete(key); return next; }, { replace: true }); }
  return <Layout className="sva-layout macro-layout"><AppToolbar active="macro" user={user} authRequired={authRequired} onLogout={logout} settingsAction={<ConfigConsoleButton isAdmin={user?.role === "admin"} />} />
    <Layout.Content className="macro-content"><div className="macro-heading"><div><h1>市场研判</h1></div><div className="macro-heading-links"><Link to={`/monitor?target=index&code=${encodeURIComponent(indexKey || "SH_COMP")}`}>监控此指数</Link><Link to="/calendar">经济日历</Link><Link to="/news">海外快讯</Link></div></div>
      <Tabs className="macro-tabs" activeKey={tab} onChange={(value) => patch({ tab: value })} items={MACRO_TABS} />
      {tab !== "capital" ? <div className="macro-controls">{tab === "indices" || tab === "analysis" ? <div className="macro-control macro-index-control"><label htmlFor="macro-index">大盘指数</label><Select id="macro-index" loading={indices.isFetching} value={indexKey} onChange={(value) => patch({ index: value })} options={list.map((item) => ({ value: item.key, label: item.label }))} /></div> : null}<div className="macro-control"><label htmlFor="macro-range">历史区间</label><Select id="macro-range" value={range} onChange={(value) => patch({ range: value })} options={MACRO_RANGES} /></div><div className="macro-control"><label htmlFor="macro-view">K线周期</label><Select id="macro-view" value={view} onChange={(value) => patch({ view: value })} options={MACRO_VIEWS} /></div></div> : null}
      {tab === "indices" || tab === "macro" ? <div className="macro-indicator-controls"><KlineIndicatorControls settings={indicatorSettings} onChange={setIndicatorSettings} /></div> : null}
      {(tab === "indices" || tab === "analysis") ? <MacroReadError query={indices} /> : null}
      <Suspense fallback={<div className="macro-empty"><Spin /></div>}>
        {tab === "indices" ? indexKey ? <IndexPanel key={indexKey} userId={user?.id} indices={list} indexKey={indexKey} range={range} view={view} indicatorSettings={indicatorSettings} /> : <div className="macro-empty">{indices.isPending ? <Spin /> : "暂无大盘指数目录"}</div> : null}
        {tab === "macro" ? <MacroSeriesPanel userId={user?.id} range={range} view={view} seriesId={params.get("series")} industryId={params.get("industry")} patch={patch} indicatorSettings={indicatorSettings} /> : null}
        {tab === "capital" ? <MacroCapitalPanel userId={user?.id} /> : null}
        {tab === "analysis" ? <MacroAnalysisPanel key={`${user?.id}:${indexKey}:${range}:${view}`} userId={user?.id} isAdmin={user?.role === "admin"} indexKey={indexKey} indexLabel={list.find((item) => item.key === indexKey)?.label} range={range} view={view} initialSymbol={params.get("chart")} /> : null}
      </Suspense>
    </Layout.Content></Layout>;
}
