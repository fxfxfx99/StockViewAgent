import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Input, Select, Spin, Table, Tabs, Tag } from "antd";
import ReactECharts from "echarts-for-react";
import ReloadOutlined from "@ant-design/icons/es/icons/ReloadOutlined";
import { useThemeMode } from "./ThemeModeProvider.jsx";
import { candlePaletteFromStyles } from "./klineChartOption.js";
import { finiteValue, macroDate, macroKey, macroMoney, macroPercent } from "./macroViewModel.js";
import * as api from "./api.js";
import { normalizeChartSymbol } from "./hooks/urlState.js";

const EMPTY = [];
function ReadState({ query }) {
  return <>{query.isError ? <Alert className="macro-alert" showIcon type="warning" message={api.getApiErrorMessage(query.error)} description={query.data ? "显示上次读取的数据。" : undefined} action={<Button size="small" onClick={() => void query.refetch()}>重试</Button>} /> : null}{query.data?.cache_warning || query.data?.source_warning ? <Alert className="macro-alert" type="warning" showIcon message={query.data.cache_warning || query.data.source_warning} /> : null}</>;
}
const moneyCell = (value) => <span className={finiteValue(value) > 0 ? "macro-up" : finiteValue(value) < 0 ? "macro-down" : ""}>{macroMoney(value)}</span>;
const sectorColumns = [
  { title: "行业", dataIndex: "sector_name", width: 130 },
  { title: "主力净流入（元）", dataIndex: "main_net_inflow", render: moneyCell, width: 160 },
  { title: "占比", dataIndex: "main_net_inflow_pct", render: macroPercent, width: 100 },
  { title: "涨跌", dataIndex: "change_pct", render: macroPercent, width: 100 },
  { title: "超大单（元）", dataIndex: "super_large_net_inflow", render: moneyCell, width: 140 },
  { title: "大单（元）", dataIndex: "large_net_inflow", render: moneyCell, width: 140 },
  { title: "最大流入股", dataIndex: "leader_name", width: 130 },
];
const reversalColumns = [
  { title: "行业", dataIndex: "sector_name", width: 140 }, { title: "方向", dataIndex: "direction", width: 100 },
  { title: "单日净额（元）", dataIndex: "main_net_inflow_1d", render: moneyCell, width: 150 },
  { title: "五日净额（元）", dataIndex: "main_net_inflow_5d", render: moneyCell, width: 150 },
  { title: "十日净额（元）", dataIndex: "main_net_inflow_10d", render: moneyCell, width: 150 },
];

function FundFlow({ userId }) {
  const [period, setPeriod] = useState("1d");
  const query = useQuery({ queryKey: macroKey(userId, "fund-flow"), queryFn: ({ signal }) => api.getMarketFundFlowDashboard({ signal }), staleTime: 5 * 60_000, retry: false });
  const groups = query.data?.sector_flows || EMPTY;
  const group = groups.find((item) => item.period === period) || groups[0];
  return <div className="macro-panel-stack"><section className="macro-card">
    <div className="macro-card-heading"><div><h2>行业资金</h2></div><Button icon={<ReloadOutlined aria-hidden="true" />} loading={query.isFetching} onClick={() => void query.refetch()}>刷新资金</Button></div>
    <ReadState query={query} />
    {query.isPending ? <div className="macro-empty"><Spin /><p>正在读取资金数据…</p></div> : <><div className="macro-selection"><label htmlFor="macro-fund-period">比较周期</label><Select id="macro-fund-period" value={group?.period} onChange={setPeriod} options={groups.map((item) => ({ value: item.period, label: item.label }))} /></div>
    {group?.source_warning ? <Alert className="macro-alert" type="warning" showIcon message={group.source_warning} /> : null}
    <div className="macro-chart-meta"><span>来源 {group?.source || "暂无来源"}</span>{group?.served_from_cache ? <Tag>历史缓存</Tag> : null}<span>数据日期 {macroDate(group?.cache_saved_at || query.data?.as_of)}</span></div>
    <Table size="small" rowKey={(row) => row.sector_code || row.sector_name} columns={group?.period === "reversal" ? reversalColumns : sectorColumns} dataSource={group?.items || EMPTY} pagination={{ pageSize: 8, showSizeChanger: false, hideOnSinglePage: true }} scroll={{ x: group?.period === "reversal" ? 690 : 900 }} locale={{ emptyText: "暂无行业资金数据" }} /></>}
  </section>
  <section className="macro-card"><div className="macro-card-heading"><div><h2>机构与情绪代理指标</h2><p>使用公开代理指标，非账户实际资金流。</p></div></div>
    {query.data?.institutional_flows?.errors ? <Alert className="macro-alert" type="warning" showIcon message="部分代理数据源不可用。" /> : null}
    <Table size="small" rowKey="type" dataSource={query.data?.institutional_flows?.items || EMPTY} pagination={false} scroll={{ x: 840 }} locale={{ emptyText: "暂无机构代理指标" }} columns={[
      { title: "观察分类", dataIndex: "label", width: 150 }, { title: "信号", dataIndex: "signal", width: 150 },
      { title: "净额/变化（元）", dataIndex: "net_flow_yuan", width: 150, render: moneyCell },
      { title: "解释与来源", key: "description", render: (_, row) => <div>{row.description || "口径未说明"}<div className="macro-footnote">{row.source}</div></div> },
    ]} />
  </section>
  <details className="macro-methodology"><summary>资金数据口径与来源</summary>{query.data?.methodology?.map((item) => <p key={item}>{item}</p>)}{query.data?.sources?.map((source) => <p key={source.name}><strong>{source.name}</strong> · {source.status === "ok" ? "读取成功" : "部分数据不可用"} · {source.role}</p>)}</details></div>;
}

function HistoryChart({ query, title, fields }) {
  const { mode } = useThemeMode(); const ref = useRef(null); const [palette, setPalette] = useState();
  const chartRef = useRef(null);
  const rows = query.data?.items || EMPTY;
  useEffect(() => { const frame = requestAnimationFrame(() => { if (ref.current) setPalette(candlePaletteFromStyles(getComputedStyle(ref.current))); }); return () => cancelAnimationFrame(frame); }, [mode]);
  useEffect(() => {
    if (!ref.current || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => chartRef.current?.getEchartsInstance()?.resize({ width: "auto", height: "auto" }));
    observer.observe(ref.current);
    return () => observer.disconnect();
  }, []);
  const option = useMemo(() => {
    if (!rows.length) return null;
    const ordered = rows.slice().sort((a, b) => String(a.trade_date).localeCompare(String(b.trade_date)));
    return { animation: false, color: [palette?.accent, palette?.ma20, palette?.ma250], textStyle: { color: palette?.text },
      tooltip: { trigger: "axis", confine: true, valueFormatter: (value) => value == null ? "暂无数据" : `${Number(value).toFixed(2)} 亿元` },
      legend: { type: "scroll", top: 0, textStyle: { color: palette?.muted } }, grid: { left: 62, right: 20, top: 50, bottom: 74 },
      xAxis: { type: "category", data: ordered.map((row) => row.trade_date), axisLabel: { color: palette?.muted, hideOverlap: true } },
      yAxis: { type: "value", name: "亿元", scale: true, axisLabel: { color: palette?.muted }, splitLine: { lineStyle: { color: palette?.grid } } },
      dataZoom: [{ type: "inside", start: 60, end: 100 }, { type: "slider", start: 60, end: 100, bottom: 8, height: 22 }],
      series: fields.map(([name, field]) => ({ name, type: "line", showSymbol: false, connectNulls: false, data: ordered.map((row) => { const value = finiteValue(row[field]); return value === null ? null : value / 1e8; }) })),
    };
  }, [rows, fields, palette]);
  return <section className="macro-card" ref={ref}><div className="macro-card-heading"><h2>{title}</h2><Button loading={query.isFetching} onClick={() => void query.refetch()}>刷新</Button></div><ReadState query={query} />
    {query.data?.served_from_cache ? <Tag>历史缓存 · {macroDate(query.data.cache_saved_at)}</Tag> : null}
    {option ? <div role="img" aria-label={`${title}历史走势图`}><ReactECharts ref={chartRef} option={option} style={{ height: 320 }} notMerge lazyUpdate /></div> : <div className="macro-empty">{query.isFetching ? <Spin /> : "暂无历史数据"}</div>}
    <p className="macro-footnote">缺失数据留空。</p></section>;
}
const NORTH_FIELDS = [["北向净流入", "net_tgt"]];
const MARGIN_FIELDS = [["融资余额", "rzye"], ["融券余额", "rqye"], ["两融余额", "rzrqye"]];
function CapitalHistory({ userId }) {
  const north = useQuery({ queryKey: macroKey(userId, "north-flow"), queryFn: ({ signal }) => api.getMarketNorthFlow({ signal }), staleTime: 5 * 60_000, retry: false });
  const margin = useQuery({ queryKey: macroKey(userId, "margin"), queryFn: ({ signal }) => api.getMarketMargin({ signal }), staleTime: 5 * 60_000, retry: false });
  return <div className="macro-panel-stack"><HistoryChart query={north} title="北向资金" fields={NORTH_FIELDS} /><HistoryChart query={margin} title="融资与融券余额" fields={MARGIN_FIELDS} /></div>;
}
function MarketPopularity({ userId }) {
  const query = useQuery({ queryKey: macroKey(userId, "popularity"), queryFn: ({ signal }) => api.getMarketHot({ signal }), staleTime: 5 * 60_000, retry: false });
  const [group, setGroup] = useState("east_pop_100");
  const concept = group === "ths_hot_concept_20";
  return <><ReadState query={query} /><div className="macro-selection"><label htmlFor="macro-popularity">热度榜单</label><Select id="macro-popularity" value={group} onChange={setGroup} options={[{ value: "east_pop_100", label: "东财人气100" }, { value: "ths_hot_100", label: "同花顺热股100" }, { value: "ths_hot_concept_20", label: "热门概念20" }]} /><Button loading={query.isFetching} onClick={() => void query.refetch()}>刷新榜单</Button></div>
    {query.data?.errors?.[group] ? <Alert className="macro-alert" type="warning" showIcon message={`此榜单暂不可用：${query.data.errors[group]}`} /> : null}
    <Table size="small" rowKey={(row) => row.stock_code || row.concept_code || String(row.rank)} loading={query.isPending} dataSource={query.data?.[group] || EMPTY} pagination={{ pageSize: 10, showSizeChanger: false, hideOnSinglePage: true }} scroll={{ x: 550 }} columns={[
      { title: "排名", dataIndex: "rank", width: 60 }, { title: "代码", dataIndex: concept ? "concept_code" : "stock_code", width: 100 },
      { title: "名称", dataIndex: concept ? "concept_name" : "short_name", width: 150 }, { title: "涨跌", dataIndex: "change_pct", render: macroPercent, width: 100 },
      { title: group === "east_pop_100" ? "价格" : "热度", dataIndex: group === "east_pop_100" ? "price" : "hot_value", render: (value) => value ?? "—" },
    ]} locale={{ emptyText: "暂无榜单数据" }} /></>;
}
function EtfQuote({ userId }) {
  const [input, setInput] = useState("510300"); const [code, setCode] = useState("");
  const query = useQuery({ queryKey: macroKey(userId, "etf", code), queryFn: ({ signal }) => api.getEtfCurrent(code, { signal }), enabled: Boolean(code), staleTime: 60_000, retry: false });
  const rows = query.data?.items || EMPTY;
  const labels = { fund_code: "代码", name: "名称", short_name: "名称", price: "现价", change: "涨跌额", change_pct: "涨跌幅", volume: "成交量", amount: "成交额", trade_time: "报价时间", trade_date: "交易日" };
  const columns = [...new Set(rows.flatMap((row) => Object.keys(row)))].map((field) => ({ title: labels[field] || field, dataIndex: field, width: 130, render: (value) => value == null ? "—" : typeof value === "object" ? JSON.stringify(value) : String(value) }));
  return <><div className="macro-selection"><label htmlFor="macro-etf">ETF 代码</label><Input id="macro-etf" value={input} onChange={(event) => setInput(event.target.value)} maxLength={6} placeholder="6 位 ETF 代码" /><Button type="primary" disabled={!/^\d{6}$/.test(input.trim())} loading={query.isFetching} onClick={() => input.trim() === code ? void query.refetch() : setCode(input.trim())}>查询ETF</Button></div><ReadState query={query} />{code ? <Table size="small" rowKey="_row_id" columns={columns} dataSource={rows.map((row, index) => ({ ...row, _row_id: `${code}-${index}` }))} loading={query.isPending} pagination={false} scroll={{ x: Math.max(600, columns.length * 130) }} locale={{ emptyText: "暂无 ETF 报价" }} /> : <p className="macro-footnote">报价可能延迟。</p>}</>;
}
const STOCK_FLOW_FIELDS = [["主力净流入", "main_net_inflow"], ["大单净流入", "lg_net_inflow"], ["超大单净流入", "max_net_inflow"]];
function StockCapital({ userId }) {
  const [input, setInput] = useState(""); const [symbol, setSymbol] = useState("");
  const normalized = normalizeChartSymbol(input);
  const query = useQuery({ queryKey: macroKey(userId, "stock-capital", symbol), queryFn: ({ signal }) => api.getStockCapitalFlow(symbol, { signal }), enabled: Boolean(symbol), staleTime: 5 * 60_000, retry: false });
  return <><div className="macro-selection"><label htmlFor="macro-capital-stock">股票代码</label><Input id="macro-capital-stock" value={input} onChange={(event) => setInput(event.target.value)} maxLength={16} placeholder="例如 600519.SS" /><Button type="primary" disabled={!normalized} loading={query.isFetching} onClick={() => symbol === normalized ? void query.refetch() : setSymbol(normalized)}>查询资金流</Button></div>{symbol ? <HistoryChart query={query} title={`${symbol} · 个股资金流`} fields={STOCK_FLOW_FIELDS} /> : <p className="macro-footnote">支持 A 股主力、大单和超大单净流入查询。</p>}</>;
}
function ExtraMarketData({ userId }) {
  const [opened, setOpened] = useState({});
  const panels = [["hot", "人气与热门概念", MarketPopularity], ["etf", "ETF 报价", EtfQuote], ["stock", "个股资金流", StockCapital]];
  return <div className="macro-panel-stack">{panels.map(([key, title, Component]) => <details className="macro-methodology" key={key} onToggle={(event) => { const open = event.currentTarget.open; setOpened((previous) => ({ ...previous, [key]: open })); }}><summary>{title}</summary>{opened[key] ? <div className="macro-extra-content"><Component userId={userId} /></div> : null}</details>)}</div>;
}
export default function MacroCapitalPanel({ userId }) {
  const [tab, setTab] = useState("sector");
  return <><Tabs activeKey={tab} onChange={setTab} items={[{ key: "sector", label: "板块与机构" }, { key: "history", label: "北向与两融" }, { key: "extra", label: "更多市场数据" }]} />{tab === "sector" ? <FundFlow userId={userId} /> : tab === "history" ? <CapitalHistory userId={userId} /> : <ExtraMarketData userId={userId} />}</>;
}
