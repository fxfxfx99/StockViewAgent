import { useEffect, useId, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import ReactECharts from "echarts-for-react";
import { Alert, Button, DatePicker, Layout, Segmented, Select, Spin, Switch, Tag, Tooltip } from "antd";
import ReloadOutlined from "@ant-design/icons/es/icons/ReloadOutlined";
import SyncOutlined from "@ant-design/icons/es/icons/SyncOutlined";
import AppToolbar from "./AppToolbar.jsx";
import { ConfigConsoleButton } from "./ConfigConsole.jsx";
import { useAuth } from "./AuthContext.jsx";
import { useThemeMode } from "./ThemeModeProvider.jsx";
import * as api from "./api.js";
import { MERRILL_PHASES, buildMerrillChartOption, defaultMerrillSelection, merrillAssetClass, merrillClockPosition, merrillDate, merrillJobActive, merrillKey, merrillNumber, merrillSeriesColor, merrillValue, transformMerrillSeries } from "./merrillViewModel.js";
import "./MerrillClock.css";

const EMPTY = [];
const STATUS_LABELS = { ready: "就绪", stale: "数据过期", insufficient: "观测不足", transition: "阶段边界", not_configured: "待配置模型", not_generated: "尚未生成", queued: "排队中", running: "生成中", failed: "更新失败" };
const SOURCE_STATUS = { ready: "已入库", succeeded: "已同步", partial: "部分数据", stale: "数据陈旧", failed: "获取失败", error: "获取失败", pending: "待同步", empty: "暂无数据", unavailable: "来源不可用", never: "待同步" };
const KIND_LABELS = { index: "指数", etf: "ETF 代理", yield: "收益率", futures_price: "期货价格", spot_price: "现货价格" };

function ReadError({ query }) {
  return query.isError ? <Alert type="warning" showIcon message={api.getApiErrorMessage(query.error)} description={query.data ? "显示上次读取的结果。" : undefined} action={<Button size="small" onClick={() => void query.refetch()}>重试</Button>} /> : null;
}

function ClockDial({ cycle }) {
  const id = useId();
  const position = merrillClockPosition(cycle);
  const phase = MERRILL_PHASES.find((item) => item.id === cycle?.phase);
  const sectors = [
    { id: "recovery", d: "M210 210L54 210A156 156 0 0 1 210 54Z", x: 127, y: 128 },
    { id: "overheat", d: "M210 210L210 54A156 156 0 0 1 366 210Z", x: 293, y: 128 },
    { id: "stagflation", d: "M210 210L366 210A156 156 0 0 1 210 366Z", x: 293, y: 290 },
    { id: "reflation", d: "M210 210L210 366A156 156 0 0 1 54 210Z", x: 127, y: 290 },
  ];
  return <div className="merrill-dial-wrap"><svg className="merrill-dial" viewBox="0 0 420 420" role="img" aria-labelledby={`${id}-title ${id}-desc`}>
    <title id={`${id}-title`}>{position ? `${cycle.region}：${phase?.label || cycle.phase_label}` : `${cycle?.region || "经济周期"}：${STATUS_LABELS[cycle?.status] || "等待观测"}`}</title>
    <desc id={`${id}-desc`}>增长向上走强，通胀向右上行。左上复苏，右上过热，右下滞胀，左下衰退。{position ? `节点表示截至 ${cycle.as_of} 的宏观信号位置。` : "暂无周期定位。"}</desc>
    {sectors.map((sector) => { const item = MERRILL_PHASES.find((row) => row.id === sector.id); return <g key={sector.id} className={`merrill-sector ${position && cycle.phase === sector.id ? "is-current" : ""}`} style={{ "--phase-color": item.color }}><path d={sector.d} /><text x={sector.x} y={sector.y} className="merrill-sector-name">{item.label}</text><text x={sector.x} y={sector.y + 21} className="merrill-sector-asset">{item.asset}</text></g>; })}
    <circle cx="210" cy="210" r="156" className="merrill-dial-edge" />
    {Array.from({ length: 60 }, (_, index) => { const angle = index * Math.PI / 30; const inner = index % 5 === 0 ? 164 : 168; return <line key={index} x1={210 + Math.sin(angle) * inner} y1={210 - Math.cos(angle) * inner} x2={210 + Math.sin(angle) * 172} y2={210 - Math.cos(angle) * 172} className={`merrill-tick ${index % 5 === 0 ? "is-hour" : ""}`} />; })}
    <line x1="54" y1="210" x2="366" y2="210" className="merrill-dial-axis" /><line x1="210" y1="54" x2="210" y2="366" className="merrill-dial-axis" />
    <text x="210" y="23" className="merrill-axis-label">增长走强 ↑</text><text x="210" y="405" className="merrill-axis-label">增长放缓 ↓</text>
    <text x="6" y="204" className="merrill-axis-label merrill-axis-left"><tspan x="6">通胀</tspan><tspan x="6" dy="18">回落</tspan></text><text x="414" y="204" className="merrill-axis-label merrill-axis-right"><tspan x="414">通胀</tspan><tspan x="414" dy="18">上行</tspan></text>
    <circle cx="210" cy="210" r="27" className="merrill-dial-center" />
    {position ? <g style={{ color: phase?.color }}><line x1="210" y1="210" x2={position.x} y2={position.y} className="merrill-needle" /><circle cx="210" cy="210" r="5" className="merrill-hub" /><circle cx={position.x} cy={position.y} r="13" className="merrill-node-halo" /><circle cx={position.x} cy={position.y} r="6" className="merrill-node" /></g> : <text x="210" y="216" className="merrill-no-node">—</text>}
  </svg></div>;
}

function MacroEvidence({ metric, title }) {
  const value = merrillNumber(metric?.momentum);
  return <div className="merrill-evidence"><div><span>{title}</span><strong>{value === null ? "待观测" : `${value > 0 ? "+" : ""}${merrillValue(value)} ${metric?.momentum_unit || "百分点"}`}</strong></div><p>{metric?.label || "暂无宏观数据"}{merrillNumber(metric?.latest_metric) !== null ? ` · ${metric.value_label || "最新"} ${merrillValue(metric.latest_metric)}${metric.value_label === "同比" ? "%" : ""}` : ""}</p><span className="merrill-muted">观测 {merrillDate(metric?.latest_date)}</span></div>;
}

function MacroSources({ sources }) {
  return Object.entries(sources || {}).map(([id, source]) => <div className="merrill-macro-source" key={id}><strong>{source.name || id}</strong><p>{source.source_url && /^https?:\/\//.test(source.source_url) ? <a href={source.source_url} target="_blank" rel="noreferrer">{source.source || "数据来源"} ↗</a> : source.source || "来源待同步"} · {source.row_count || 0} 个月度观测</p><p>覆盖 {merrillDate(source.available_from)} — {merrillDate(source.available_to)}<br />最近抓取 {merrillDate(source.last_synced_at, true)}</p>{source.last_error ? <p className="merrill-warning-text">采集失败：{source.last_error}</p> : null}</div>);
}

function CoverageTable({ catalog, selectedIds, plotted }) {
  const baselines = new Map(plotted.map((item) => [item.id, item.baseline_date]));
  return <details className="merrill-card merrill-coverage"><summary><span>数据覆盖与来源</span><span className="merrill-muted">{catalog.length} 条序列</span></summary><p className="merrill-note">目标覆盖近 50 年；实际范围取决于资产存续期、来源和数据权限。</p><div className="merrill-table-scroll"><table><thead><tr><th>资产 / 口径</th><th>实际覆盖</th><th>周数 / 覆盖率</th><th>来源与代理说明</th><th>状态</th></tr></thead><tbody>{catalog.map((item, index) => <tr key={item.id} className={selectedIds.includes(item.id) ? "is-selected" : ""}><td><span className="merrill-series-name"><i style={{ background: merrillSeriesColor(item.id, index) }} />{item.name}</span><span className="merrill-cell-note">{merrillAssetClass(item)} · {KIND_LABELS[item.kind] || item.kind} · {item.unit || "—"}</span>{selectedIds.includes(item.id) && baselines.get(item.id) && item.kind !== "yield" ? <span className="merrill-cell-note">归一基准 {baselines.get(item.id)}</span> : null}</td><td>{item.available_from ? <>{merrillDate(item.available_from)}<br />— {merrillDate(item.available_to)}</> : "尚无数据"}<span className="merrill-cell-note">最近同步 {merrillDate(item.last_synced_at, true)}</span></td><td>{item.row_count ?? 0} 周<span className="merrill-cell-note">{merrillNumber(item.coverage_ratio) !== null ? `${merrillValue(Number(item.coverage_ratio) * 100, 1)}% 目标覆盖` : "覆盖待计算"}</span>{item.missing_weeks > 0 ? <span className="merrill-cell-note">目标缺口 {item.missing_weeks} 周</span> : null}</td><td>{item.source_url && /^https?:\/\//.test(item.source_url) ? <a href={item.source_url} target="_blank" rel="noreferrer">{item.source || "来源"} ↗</a> : item.source || "来源未标注"}{item.is_proxy ? <Tag className="merrill-proxy-tag">代理</Tag> : null}<span className="merrill-cell-note">{item.proxy_note || item.coverage_note || item.source_symbol}</span></td><td><span className={item.last_error ? "merrill-warning-text" : ""}>{SOURCE_STATUS[item.status] || item.status || "待同步"}</span>{item.last_error ? <span className="merrill-cell-note merrill-source-error">{item.last_error}</span> : null}</td></tr>)}</tbody></table></div></details>;
}

export default function MerrillClock() {
  const { user, authRequired, logout } = useAuth();
  const { isLight } = useThemeMode();
  const queryClient = useQueryClient();
  const [region, setRegion] = useState("US");
  const [years, setYears] = useState(10);
  const [curve, setCurve] = useState("normalized");
  const [baseline, setBaseline] = useState(null);
  const [assetClass, setAssetClass] = useState("全部");
  const [selection, setSelection] = useState(null);
  const chartRef = useRef(null);
  const chartContainer = useRef(null);
  const key = merrillKey(user?.id);
  const overview = useQuery({ queryKey: [...key, "overview", region], queryFn: ({ signal }) => api.getMerrillOverview(region, { signal }), staleTime: 30_000, retry: false, refetchInterval: (query) => merrillJobActive(query.state.data?.sync?.job) || query.state.data?.sync?.status === "running" || ["queued", "running"].includes(query.state.data?.analysis?.status) ? 3000 : 60_000 });
  const data = overview.data;
  const catalog = data?.catalog || EMPTY;
  const selectedIds = useMemo(() => (selection ?? defaultMerrillSelection(catalog)).filter((id) => catalog.some((item) => item.id === id)), [selection, catalog]);
  const seriesQuery = useQuery({ queryKey: [...key, "series", selectedIds.join(","), years], queryFn: ({ signal }) => api.getMerrillSeries(selectedIds, years, { signal }), enabled: selectedIds.length > 0, staleTime: 60_000, retry: false });
  const series = selectedIds.length ? seriesQuery.data?.series || EMPTY : EMPTY;
  const baselineDate = baseline?.format("YYYY-MM-DD") || null;
  const plotted = useMemo(() => transformMerrillSeries(series, curve, baselineDate), [series, curve, baselineDate]);
  const chartOption = useMemo(() => buildMerrillChartOption({ series, mode: curve, baselineDate, isLight, catalog }), [series, curve, baselineDate, isLight, catalog]);
  const classes = useMemo(() => ["全部", ...new Set(catalog.map(merrillAssetClass))], [catalog]);
  const options = useMemo(() => catalog.filter((item) => assetClass === "全部" || merrillAssetClass(item) === assetClass).map((item) => ({ value: item.id, label: `${item.name}${item.kind === "yield" ? " · 收益率" : item.is_proxy && !item.name?.includes("代理") ? " · 代理" : ""}` })), [catalog, assetClass]);
  const busy = merrillJobActive(data?.sync?.job) || data?.sync?.status === "running";
  const cycle = data?.cycle;
  const analysis = data?.analysis;
  const automation = data?.automation;
  const isAdmin = user?.role === "admin";
  const country = region === "CN" ? "中国" : "美国";
  const phase = MERRILL_PHASES.find((item) => item.id === cycle?.phase);
  const current = merrillClockPosition(cycle);
  const analysisBusy = ["queued", "running"].includes(analysis?.status);
  const selectedYields = series.some((item) => item.kind === "yield");
  const hasNegativePrices = series.some((item) => item.kind !== "yield" && item.points?.some((point) => merrillNumber(point.close) !== null && Number(point.close) < 0));
  const refresh = useMutation({ mutationFn: api.refreshMerrillData, onSuccess: () => queryClient.invalidateQueries({ queryKey: key }) });
  const analyze = useMutation({ mutationFn: (targetRegion) => api.analyzeMerrillClock(targetRegion), onSuccess: () => queryClient.invalidateQueries({ queryKey: key }) });
  const preferences = useMutation({ mutationFn: api.updateMerrillPreferences, onSuccess: () => queryClient.invalidateQueries({ queryKey: key }) });
  const syncVersion = `${data?.sync?.finished_at || ""}:${data?.sync?.job?.status || ""}`;
  useEffect(() => { if (data?.sync?.finished_at) void queryClient.invalidateQueries({ queryKey: [...merrillKey(user?.id), "series"] }); }, [syncVersion, queryClient, user?.id]);
  useEffect(() => {
    if (!chartContainer.current || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => chartRef.current?.getEchartsInstance()?.resize());
    observer.observe(chartContainer.current);
    return () => observer.disconnect();
  }, [Boolean(chartOption)]);

  return <Layout className="sva-layout merrill-page"><AppToolbar active="merrill-clock" user={user} authRequired={authRequired} onLogout={logout} settingsAction={<ConfigConsoleButton isAdmin={isAdmin} />} /><Layout.Content className="merrill-content">
    <header className="merrill-page-heading"><div><h1>美林时钟</h1><p>比较资产走势，查看中美经济周期。</p></div><div className="merrill-heading-status"><span className={`merrill-status-dot ${busy ? "is-busy" : ""}`} />{busy ? "周度数据同步中" : data?.sync?.finished_at ? `数据同步 ${merrillDate(data.sync.finished_at)}` : "尚未同步"}<Button type="text" icon={<ReloadOutlined aria-hidden="true" />} aria-label="刷新页面状态" loading={overview.isFetching} onClick={() => void overview.refetch()} /></div></header>
    <ReadError query={overview} />
    {overview.isPending ? <div className="merrill-loading"><Spin /><p>正在读取数据…</p></div> : !data ? <div className="merrill-card merrill-empty">数据读取失败，请检查连接后重试。</div> : <>
      <div className="merrill-main-grid">
        <section className="merrill-card merrill-chart-card"><div className="merrill-card-heading"><div><h2>资产走势</h2></div><Tag>周度观测</Tag></div>
          <div className="merrill-chart-controls"><label>时间范围<Select aria-label="历史时间范围" value={years} onChange={(value) => { setYears(value); setBaseline(null); }} options={[5, 10, 20, 50].map((value) => ({ value, label: `近 ${value} 年` }))} /></label><label>曲线口径<Select aria-label="曲线口径" value={curve} onChange={setCurve} options={[{ value: "normalized", label: "归一化 · 基准 100" }, { value: "raw", label: "原始数值" }, { value: "drawdown", label: "区间回撤" }]} /></label>{curve === "normalized" ? <label>归一基准日<DatePicker aria-label="归一基准日" value={baseline} onChange={setBaseline} placeholder="区间首个有效周" allowClear /></label> : null}</div>
          <div className="merrill-selection"><Select className="merrill-class-select" aria-label="资产类别筛选" value={assetClass} onChange={setAssetClass} options={classes.map((value) => ({ value, label: value === "全部" ? "全部资产" : value }))} /><Select mode="multiple" showSearch aria-label="选择资产曲线" className="merrill-asset-select" placeholder="选择要比较的资产" optionFilterProp="label" value={selectedIds} onChange={setSelection} options={options} maxTagCount="responsive" allowClear /></div>
          <ReadError query={seriesQuery} />
          {curve === "raw" && series.filter((item) => item.kind !== "yield").length > 1 ? <div className="merrill-inline-note">单位和量级不同，比较涨跌请切换「归一化」。</div> : null}
          <div className="merrill-chart" ref={chartContainer} aria-busy={seriesQuery.isFetching}>{chartOption ? <><ReactECharts ref={chartRef} option={chartOption} notMerge lazyUpdate style={{ height: "100%", width: "100%" }} />{seriesQuery.isFetching ? <div className="merrill-chart-pending"><Spin size="small" /> 更新曲线</div> : null}</> : <div className="merrill-chart-empty">{seriesQuery.isFetching ? <><Spin /><p>正在读取周度历史…</p></> : <><span className="merrill-empty-line">⌁</span><p>{selectedIds.length ? baseline ? "基准日后无数据，请调整日期。" : "所选资产暂无周度数据。" : "选择要比较的资产"}</p><Button size="small" onClick={() => selectedIds.length ? void seriesQuery.refetch() : setSelection(defaultMerrillSelection(catalog))}>{selectedIds.length ? "重试" : "显示默认组合"}</Button></>}</div>}</div>
          <p className="merrill-note">{curve === "normalized" ? "以基准日后首个有效周为 100，实际日期见覆盖表。" : curve === "drawdown" ? "回撤相对所选区间内此前高点计算。" : "按来源原始口径展示。"}缺失周留空。{hasNegativePrices && curve !== "raw" ? "负价格仅在「原始数值」中显示。" : ""}{selectedYields ? "右轴虚线为收益率（%），非债券投资回报。" : ""}</p>
        </section>
        <section className="merrill-card merrill-clock-card"><div className="merrill-card-heading"><div><h2>周期定位</h2></div><Segmented aria-label="切换国家时钟" value={region} onChange={setRegion} options={[{ value: "CN", label: "中国" }, { value: "US", label: "美国" }]} /></div><div className="merrill-phase-line"><strong style={current ? { color: phase?.color } : undefined}>{current ? phase?.label || cycle.phase_label : STATUS_LABELS[cycle?.status] || "等待观测"}</strong><span>{cycle?.region || `${country}宏观数据`}<br />共同观测截至 {merrillDate(cycle?.as_of)}</span></div><ClockDial cycle={cycle} />
          {!current ? <div className="merrill-cycle-notice">{cycle?.status === "stale" ? "数据过期，暂停定位。" : cycle?.status === "transition" ? "信号位于阶段分界。" : "增长或通胀数据不足，暂无法定位。"}</div> : <p className="merrill-node-caption"><i style={{ background: phase?.color }} />节点位置表示增长与通胀的信号强度</p>}
          <div className="merrill-evidence-grid"><MacroEvidence metric={cycle?.growth} title="增长动量" /><MacroEvidence metric={cycle?.inflation} title="通胀动量" /></div>
          <details className="merrill-method"><summary>定位方法与来源</summary><p>{cycle?.methodology || "按增长与通胀观测的变化方向判定阶段。"}</p>{(cycle?.limitations || EMPTY).map((text, index) => <p key={index}>{text}</p>)}<MacroSources sources={data.macro_sources} /></details>
        </section>
      </div>
      <div className="merrill-explanation-grid"><section className="merrill-card merrill-theory"><div className="merrill-card-heading"><div><h2>周期与资产</h2></div><span className="merrill-small-label">理论框架</span></div><p className="merrill-body-copy">{data.theory?.intro || "按增长和通胀的变化划分周期，比较大类资产表现。"}</p><div className="merrill-theory-list">{MERRILL_PHASES.map((item, index) => { const detail = data.theory?.phases?.find((row) => row.id === item.id); return <div key={item.id} className="merrill-theory-row"><span className="merrill-phase-number" style={{ color: item.color }}>0{index + 1}</span><div><h3>{item.label}<span>{item.growth} · {item.inflation}</span></h3><p>{detail?.description || `理论上，${item.asset}在此阶段相对占优。`}</p></div><span className="merrill-asset-pill" style={{ color: item.color }}>{item.asset}</span></div>; })}</div>{data.theory?.sources?.length ? <div className="merrill-theory-sources">{data.theory.sources.filter((item) => /^https?:\/\//.test(item.url || "")).map((item) => <a key={item.url} href={item.url} target="_blank" rel="noreferrer">{item.title} ↗</a>)}</div> : null}</section>
        <section className="merrill-card merrill-analysis"><div className="merrill-card-heading"><div><h2>{country}周期解读</h2></div><Tooltip title={!automation?.llm_configured ? "请先配置模型接口" : !current ? "需完整且未过期的宏观数据" : "调用当前账户的模型 API"}><Button disabled={!automation?.llm_configured || !current} loading={analyze.isPending || analysisBusy} onClick={() => analyze.mutate(region)}>{analysisBusy ? "解读生成中" : "生成解读"}</Button></Tooltip></div>
          <div className="merrill-analysis-meta"><span className="merrill-ai-badge">AI 解读</span><span>{STATUS_LABELS[analysis?.status] || (analysis?.text ? "已生成" : "尚未生成")}</span>{analysis?.model ? <span>{analysis.model}</span> : null}</div>
          {analyze.isError ? <Alert type="error" showIcon message={api.getApiErrorMessage(analyze.error)} /> : null}{analysis?.error ? <Alert type="warning" showIcon message="解读失败" description={analysis.error} /> : null}
          {analysis?.text ? <><p className="merrill-analysis-date">生成 {merrillDate(analysis.generated_at, true)} · 数据截至 {merrillDate(analysis.as_of)}</p>{analysis.status === "stale" ? <Alert type="warning" showIcon message="解读数据已过期，请重新生成。" /> : null}<div className="merrill-analysis-text">{analysis.text}</div></> : <div className="merrill-analysis-empty"><span>{analysisBusy ? "正在生成解读…" : !automation?.llm_configured ? "请先配置模型接口" : !current ? "数据未就绪" : "暂无解读"}</span><p>{!automation?.llm_configured ? "在设置中连接模型后，可生成解读或开启每周更新。" : !current ? "宏观数据补齐后可生成解读。" : "点击「生成解读」分析当前阶段与资产表现。"}</p></div>}
        </section></div>
      <section className="merrill-card merrill-automation"><div><h2>每周自动更新</h2><p>{automation?.schedule || "每周六 08:00（北京时间）"} · {automation?.enabled && automation?.worker_running ? "自动同步运行中" : "自动任务未运行"}</p><span className="merrill-muted">下次执行 {merrillDate(automation?.next_run_at, true)}（北京时间）</span></div><div className="merrill-automation-actions"><div className="merrill-auto-switch"><Switch aria-label="当前账户每周自动生成中美解读" checked={Boolean(automation?.auto_analysis)} disabled={!automation?.llm_configured && !automation?.auto_analysis} loading={preferences.isPending} onChange={(value) => preferences.mutate(value)} /><div><strong>每周 AI 解读</strong><span>当前账户 · 中国与美国</span></div></div><Tooltip title={isAdmin ? "同步资产与中美宏观数据" : "需管理员同步共享数据"}><Button icon={<SyncOutlined aria-hidden="true" />} disabled={!isAdmin || busy} loading={refresh.isPending || busy} onClick={() => refresh.mutate()}>同步数据</Button></Tooltip></div></section>
      {refresh.isError || preferences.isError ? <Alert type="error" showIcon message={api.getApiErrorMessage(refresh.error || preferences.error)} /> : null}
      {data.sync?.status === "partial" || data.sync?.status === "failed" ? <Alert type="warning" showIcon message={data.sync.status === "partial" ? "部分来源同步失败。" : "同步失败，显示历史数据。"} description="失败原因见「数据覆盖与来源」。" /> : null}
      <CoverageTable catalog={catalog} selectedIds={selectedIds} plotted={curve === "normalized" ? plotted : EMPTY} />
    </>}
  </Layout.Content></Layout>;
}
