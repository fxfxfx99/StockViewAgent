import { lazy, Suspense, useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import ReactECharts from "echarts-for-react";
import { Alert, Button, Layout, Segmented, Select, Spin, Tabs, Tag } from "antd";
import FullscreenOutlined from "@ant-design/icons/es/icons/FullscreenOutlined";
import FullscreenExitOutlined from "@ant-design/icons/es/icons/FullscreenExitOutlined";
import useKlineViewport from "./useKlineViewport.js";
import ReloadOutlined from "@ant-design/icons/es/icons/ReloadOutlined";
import AppToolbar from "./AppToolbar.jsx";
import { ConfigConsoleButton } from "./ConfigConsole.jsx";
import { useAuth } from "./AuthContext.jsx";
import { useThemeMode } from "./ThemeModeProvider.jsx";
import { aggregateCandlesFromDaily, buildCandleVolumeChartOption, candlePaletteFromStyles, getKlineChartHeight, getIndicatorDataHints } from "./klineChartOption.js";
import KlineIndicatorControls, { useKlineIndicatorSettings } from "./KlineIndicatorControls.jsx";
import * as api from "./api.js";
import { MACRO_RANGES, MACRO_TABS, MACRO_VIEWS, candleSummary, collectionStatus, macroDate, macroDateTime, macroKey, macroNumber, macroPercent, marketGroup, marketGroups, selectMarketInstrument, validOption } from "./macroViewModel.js";
import "./MacroWorkspace.css";

const MacroCapitalPanel = lazy(() => import("./MacroCapitalPanel.jsx"));
const MacroAnalysisPanel = lazy(() => import("./MacroAnalysisPanel.jsx"));
const EMPTY = [];

export function MacroReadError({ query }) {
  return query.isError ? <Alert className="macro-alert" type="warning" showIcon message={api.getApiErrorMessage(query.error)} description={query.data ? "显示上次读取的数据。" : undefined} action={<Button size="small" onClick={() => void query.refetch()}>重试</Button>} /> : null;
}

function MarketChart({ query, title, view, aggregate = false, onRefresh, note, indicatorSettings, onIndicatorSettingsChange, instrument, contextKey }) {
  const { mode } = useThemeMode();
  const ref = useRef(null);
  const chartRef = useRef(null);
  const [palette, setPalette] = useState();
  const [expanded, setExpanded] = useState(false);
  const [compact, setCompact] = useState(false);
  const bundle = query.data;
  const metadata = bundle?.instrument || instrument;
  const timezone = bundle?.timezone || metadata?.timezone || "Asia/Shanghai";
  const unit = bundle?.unit || metadata?.unit;
  const exchange = bundle?.exchange || metadata?.exchange;
  const currency = bundle?.currency || metadata?.currency;
  const volumeUnit = bundle?.volume_unit || metadata?.volume_unit;
  const isFutures = (bundle?.asset_class || metadata?.asset_class) === "futures";
  const rawCandles = bundle?.candles || EMPTY;
  const candles = useMemo(() => aggregate ? aggregateCandlesFromDaily(rawCandles, view, timezone) : rawCandles, [rawCandles, aggregate, view, timezone]);
  const summary = useMemo(() => candleSummary(candles), [candles]);
  const { applyViewport, onEvents, resetViewport, showAll, revision } = useKlineViewport({ contextKey, candles, visibleBars: indicatorSettings.display.visibleBars });
  const hints = useMemo(() => getIndicatorDataHints(candles, indicatorSettings), [candles, indicatorSettings]);
  useEffect(() => {
    const frame = requestAnimationFrame(() => { if (ref.current) setPalette(candlePaletteFromStyles(getComputedStyle(ref.current))); });
    return () => cancelAnimationFrame(frame);
  }, [mode]);
  useEffect(() => {
    if (!ref.current || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(([entry]) => {
      setCompact(entry.contentRect.width < 600);
      chartRef.current?.getEchartsInstance()?.resize({ width: "auto", height: "auto" });
    });
    observer.observe(ref.current);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    if (!expanded) return;
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const close = (event) => { if (event.key === "Escape" && !document.querySelector(".ant-drawer-open")) setExpanded(false); };
    window.addEventListener("keydown", close);
    return () => { document.body.style.overflow = previous; window.removeEventListener("keydown", close); };
  }, [expanded]);
  const option = useMemo(() => applyViewport(buildCandleVolumeChartOption({ candles, viewMode: view, palette, indicatorSettings, compact, timeZone: timezone, volumeInLegend: false, formatDate: (item) => macroDate(item.t, timezone) })), [candles, view, palette, indicatorSettings, compact, timezone, applyViewport, revision]);
  const warning = bundle?.cache_warning || bundle?.warning || (bundle?.cache_stale ? "来源未更新，显示缓存数据。" : "");
  const source = bundle?.source || bundle?.kline_source || bundle?.provider || (bundle?.tushare_doc_id ? "Tushare" : "来源未标注");
  return <section className={`macro-card macro-chart-card${expanded ? " is-expanded" : ""}`} ref={ref} aria-label={`${title}行情图`}>
    <div className="macro-card-heading"><div><h2>{title}</h2><p>{[metadata?.symbol, exchange, unit].filter(Boolean).join(" · ") || note}</p></div><div className="macro-chart-actions"><Button size="small" icon={<ReloadOutlined aria-hidden="true" />} loading={query.isFetching} onClick={onRefresh || (() => void query.refetch())}>刷新行情</Button><Button size="small" icon={expanded ? <FullscreenExitOutlined /> : <FullscreenOutlined />} onClick={() => setExpanded(!expanded)} aria-label={expanded ? "退出展开图表" : "展开图表"}>{expanded ? "收起" : "展开"}</Button></div></div>
    <MacroReadError query={query} />
    {warning && !query.isError ? <Alert className="macro-alert" type="warning" showIcon message={warning} /> : null}
    {bundle ? <div className="macro-quotes"><div className="macro-primary-quote"><span>最新收盘{unit ? `（${unit}）` : ""}</span><strong>{macroNumber(summary.close)}</strong></div><div><span>{view === "1wk" ? "周K" : view === "1mo" ? "月K" : "日K"}涨跌</span><strong className={summary.change > 0 ? "macro-up" : summary.change < 0 ? "macro-down" : ""}>{macroPercent(summary.change)}</strong></div><div><span>区间价格变化</span><strong className={summary.totalChange > 0 ? "macro-up" : summary.totalChange < 0 ? "macro-down" : ""}>{macroPercent(summary.totalChange)}</strong></div><div><span>数据截至 · 交易日</span><strong className="macro-date-stat">{macroDate(bundle.data_as_of || summary.timestamp, timezone)}</strong></div></div> : null}
    <div className="macro-chart-tools"><KlineIndicatorControls settings={indicatorSettings} onChange={onIndicatorSettingsChange} compact /><div className="macro-viewport-actions"><span>查看范围</span><Button size="small" type="text" onClick={resetViewport}>最近 {indicatorSettings.display.visibleBars} 根</Button><Button size="small" type="text" onClick={showAll}>全部</Button></div></div>
    {option ? <div className="macro-chart-canvas" style={{ "--macro-indicator-extra-height": `${getKlineChartHeight(460, indicatorSettings) - 460}px` }} role="img" aria-label={`${title}K线与所选技术指标`}><ReactECharts ref={chartRef} option={option} onEvents={onEvents} style={{ height: "100%", width: "100%" }} notMerge lazyUpdate /></div> : <div className="macro-empty">{query.isFetching ? <><Spin /><p>正在读取行情…</p></> : <p>暂无可显示的行情数据</p>}</div>}
    {hints.length ? <p className="macro-indicator-hint">{hints.join("；")}。缺失部分保持空白。</p> : null}
    {bundle ? <div className="macro-chart-footer"><span>来源 {source} · {summary.count} 根{view === "1wk" ? "周K" : view === "1mo" ? "月K" : "日K"}</span>{bundle.served_from_cache || bundle.cache_stale ? <Tag color={bundle.cache_stale ? "orange" : "default"}>{bundle.cache_stale ? "缓存待更新" : "缓存"}</Tag> : null}<span className="macro-zoom-help">滚轮缩放 · 拖动时间轴</span></div> : null}
    <details className="macro-chart-details"><summary>数据说明{isFutures ? "与合约规则" : "与口径"}</summary>
      {note ? <p className="macro-footnote">{note}</p> : null}
      <div className="macro-chart-meta macro-instrument-meta">{currency ? <span>币种 {currency}</span> : null}{volumeUnit ? <span>成交量单位 {volumeUnit}</span> : null}<span>日期时区 {timezone}</span>{aggregate && view !== "1d" ? <span>由日线合成{view === "1wk" ? "周K" : "月K"}</span> : null}{bundle?.cache_saved_at ? <span>缓存写入 {macroDateTime(bundle.cache_saved_at)}（北京时间）</span> : null}</div>
      <p className="macro-footnote">区间变化按已载入首尾收盘价计算，不随图表缩放改变。{instrument ? "公开行情可能延迟，最新一根 K 线在交易时段内仍会变化。" : ""}</p>
      {isFutures ? <p className="macro-footnote macro-contract-note">近月连续合约会换月，切换合约可能产生价格跳空；图中价格不代表固定交割月份的可交易合约，也不等同于现货报价。</p> : null}
    </details>
  </section>;
}

function CollectionStatusPanel({ userId, indexKey }) {
  const query = useQuery({ queryKey: macroKey(userId, "collection-status"), queryFn: ({ signal }) => api.getMacroCollectionStatus({ signal }), staleTime: 30_000, refetchInterval: 30_000, retry: false });
  const data = query.data;
  const current = data?.items?.find((item) => item.key === indexKey);
  const status = collectionStatus(current?.status);
  return <section className="macro-collection" aria-label="自动采集状态">
    <div className="macro-collection-heading"><h2>自动采集</h2>{data ? <Tag color={data.enabled ? "blue" : "default"}>{data.enabled ? `计划每 ${data.interval_minutes} 分钟检查` : "定时采集已关闭"}</Tag> : null}<Button type="text" size="small" loading={query.isFetching} onClick={() => void query.refetch()}>刷新状态</Button></div>
    <MacroReadError query={query} />
    {data ? <><div className="macro-collection-summary"><span>覆盖 <strong>{data.summary?.total ?? data.items?.length ?? 0}</strong> 个品种</span><span>成功 {data.summary?.success ?? 0}</span><span>待更新 {data.summary?.stale ?? 0}</span><span>失败 {data.summary?.failed ?? 0}</span><span>等待 {data.summary?.pending ?? 0}</span>{data.summary?.running > 0 ? <span>采集中 {data.summary.running}</span> : null}</div>
      {current ? <div className="macro-collection-current"><span>{current.label || indexKey}</span><Tag color={status.color}>{status.label}</Tag><span>上次成功 {macroDateTime(current.last_success_at)}</span>{current.next_retry_at ? <span>下次重试 {macroDateTime(current.next_retry_at)}</span> : null}{current.consecutive_failures > 0 ? <span>连续失败 {current.consecutive_failures} 次</span> : null}</div> : null}
      <p className="macro-footnote">采集状态时间为北京时间；数据截至日期以行情卡片的交易日为准。{data.enabled === false ? "仍可通过“刷新行情”主动更新当前品种。" : ""}</p>
    </> : query.isPending ? <span className="macro-footnote">正在读取采集状态…</span> : null}
  </section>;
}

function IndexPanel({ userId, indices, indexKey, range, view, indicatorSettings, onIndicatorSettingsChange }) {
  const qc = useQueryClient();
  const key = macroKey(userId, "index", indexKey, range, view);
  const [refreshing, setRefreshing] = useState(false);
  const query = useQuery({ queryKey: key, queryFn: ({ signal }) => api.getMacroIndexKline({ index_key: indexKey, range, interval: view }, { signal }), enabled: Boolean(indexKey), staleTime: 60_000, retry: false });
  const instrument = indices.find((item) => item.key === indexKey);
  const title = instrument?.label || indexKey;
  async function refresh() {
    setRefreshing(true);
    try { await qc.fetchQuery({ queryKey: key, queryFn: ({ signal }) => api.getMacroIndexKline({ index_key: indexKey, range, interval: view, refresh: true }, { signal }), staleTime: 0, retry: false }); } catch { /* Query exposes the failure while retaining previous data. */ } finally { setRefreshing(false); }
  }
  return <div className="macro-panel-stack"><MarketChart query={{ ...query, isFetching: query.isFetching || refreshing }} title={title} note={instrument?.description} instrument={instrument} view={view} indicatorSettings={indicatorSettings} onIndicatorSettingsChange={onIndicatorSettingsChange} contextKey={`${userId}:${indexKey}:${range}:${view}`} onRefresh={() => void refresh()} /><details className="macro-collection-disclosure"><summary>自动采集状态<span>查看覆盖情况与下次重试时间</span></summary><CollectionStatusPanel userId={userId} indexKey={indexKey} /></details></div>;
}

function MacroSeriesPanel({ userId, range, view, seriesId, industryId, patch, indicatorSettings, onIndicatorSettingsChange }) {
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
    {selected && !available ? <div className="macro-card macro-access"><h2>{selected.name}</h2><p>{selected.note || selected.provider_hint}</p><Alert type="info" showIcon message="在设置中配置 Tushare Token 及数据权限后，刷新页面。" /></div> : selected ? <MarketChart query={seriesQuery} title={selected.name} note={selected.note || selected.provider_hint} view={view} indicatorSettings={indicatorSettings} onIndicatorSettingsChange={onIndicatorSettingsChange} contextKey={`${userId}:macro:${selected.id}:${range}:${view}`} aggregate /> : catalog.isPending ? <div className="macro-empty"><Spin /></div> : null}
    <section className="macro-industry"><div className="macro-section-heading"><h2>申万行业指数</h2><span>{industries.data?.trade_date ? `列表基准日 ${industries.data.trade_date}` : ""}</span></div>
      {!industryEnabled ? <Alert type="info" showIcon message="需配置 Tushare Token 及行业数据权限。" /> : <><MacroReadError query={industries} />{industries.data?.cache_warning ? <Alert className="macro-alert" type="warning" showIcon message={industries.data.cache_warning} /> : null}<div className="macro-selection"><label htmlFor="macro-industry">选择行业</label><Select id="macro-industry" showSearch optionFilterProp="label" loading={industries.isFetching} value={industry?.ts_code} onChange={(value) => patch({ industry: value })} options={industryItems.map((item) => ({ value: item.ts_code, label: `${item.name}（${item.ts_code}）` }))} placeholder="选择行业指数" /></div>{industry ? <MarketChart query={industryQuery} title={`${industry.name} · ${industry.ts_code}`} view={view} indicatorSettings={indicatorSettings} onIndicatorSettingsChange={onIndicatorSettingsChange} contextKey={`${userId}:industry:${industry.ts_code}:${range}:${view}`} aggregate /> : industries.isFetching ? <div className="macro-empty"><Spin /></div> : <p className="macro-footnote">暂无可用行业列表。</p>}</>}
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
  const groups = useMemo(() => marketGroups(list), [list]);
  const instrument = selectMarketInstrument(list, params.get("index"));
  const indexKey = instrument?.key;
  const groupKey = marketGroup(instrument);
  const groupItems = groups.find((group) => group.key === groupKey)?.items || EMPTY;
  const canMonitor = ["SH_COMP", "SZ_COMP", "CYB", "HS300", "SSE50"].includes(indexKey);
  function patch(values) { setParams((previous) => { const next = new URLSearchParams(previous); for (const [key, value] of Object.entries(values)) value ? next.set(key, value) : next.delete(key); return next; }, { replace: true }); }
  return <Layout className="sva-layout macro-layout"><AppToolbar active="macro" user={user} authRequired={authRequired} onLogout={logout} settingsAction={<ConfigConsoleButton isAdmin={user?.role === "admin"} />} />
    <Layout.Content className="macro-content"><div className="macro-heading"><div><h1>市场研判</h1><p>A股、美股与主要商品期货行情</p></div><div className="macro-heading-links">{canMonitor ? <Link to={`/monitor?target=index&code=${encodeURIComponent(indexKey)}`}>监控此指数</Link> : null}<Link to="/calendar">经济日历</Link><Link to="/news">海外快讯</Link></div></div>
      <Tabs className="macro-tabs" activeKey={tab} onChange={(value) => patch({ tab: value })} items={MACRO_TABS} />
      {tab !== "capital" ? <div className="macro-controls">{tab === "indices" || tab === "analysis" ? <><div className="macro-control macro-group-control"><label id="macro-market-label">市场类别</label><Segmented aria-labelledby="macro-market-label" id="macro-market" value={instrument ? groupKey : undefined} onChange={(value) => patch({ index: groups.find((group) => group.key === value)?.items[0]?.key })} options={groups.map((group) => ({ value: group.key, label: group.label }))} /></div><div className="macro-control macro-index-control"><label htmlFor="macro-index">{groupKey === "futures" ? "期货品种" : "市场指数"}</label><Select id="macro-index" showSearch optionFilterProp="label" loading={indices.isFetching} value={indexKey} onChange={(value) => patch({ index: value })} options={groupItems.map((item) => ({ value: item.key, label: item.label }))} /></div></> : null}<div className="macro-control"><label htmlFor="macro-range">历史区间</label><Select id="macro-range" value={range} onChange={(value) => patch({ range: value })} options={MACRO_RANGES} /></div><div className="macro-control macro-period-control"><label id="macro-view-label">K线周期</label><Segmented aria-labelledby="macro-view-label" id="macro-view" value={view} onChange={(value) => patch({ view: value })} options={MACRO_VIEWS} /></div></div> : null}
      {(tab === "indices" || tab === "analysis") ? <MacroReadError query={indices} /> : null}
      <Suspense fallback={<div className="macro-empty"><Spin /></div>}>
        {tab === "indices" ? indexKey ? <IndexPanel key={indexKey} userId={user?.id} indices={list} indexKey={indexKey} range={range} view={view} indicatorSettings={indicatorSettings} onIndicatorSettingsChange={setIndicatorSettings} /> : <div className="macro-empty">{indices.isPending ? <Spin /> : "暂无市场行情目录"}</div> : null}
        {tab === "macro" ? <MacroSeriesPanel userId={user?.id} range={range} view={view} seriesId={params.get("series")} industryId={params.get("industry")} patch={patch} indicatorSettings={indicatorSettings} onIndicatorSettingsChange={setIndicatorSettings} /> : null}
        {tab === "capital" ? <MacroCapitalPanel userId={user?.id} /> : null}
        {tab === "analysis" ? <MacroAnalysisPanel key={`${user?.id}:${indexKey}:${range}:${view}`} userId={user?.id} isAdmin={user?.role === "admin"} indexKey={indexKey} indexLabel={list.find((item) => item.key === indexKey)?.label} range={range} view={view} initialSymbol={params.get("chart")} /> : null}
      </Suspense>
    </Layout.Content></Layout>;
}
