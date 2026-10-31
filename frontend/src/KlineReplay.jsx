import { useCallback, useEffect, useMemo, useState } from "react";
import { Alert, App as AntApp, AutoComplete, Button, DatePicker, Input, Layout, Segmented, Select, Slider, Spin, Switch, Tag, Tooltip } from "antd";
import PlayCircleOutlined from "@ant-design/icons/es/icons/PlayCircleOutlined";
import PauseCircleOutlined from "@ant-design/icons/es/icons/PauseCircleOutlined";
import StepBackwardOutlined from "@ant-design/icons/es/icons/StepBackwardOutlined";
import StepForwardOutlined from "@ant-design/icons/es/icons/StepForwardOutlined";
import UndoOutlined from "@ant-design/icons/es/icons/UndoOutlined";
import DownloadOutlined from "@ant-design/icons/es/icons/DownloadOutlined";
import DeleteOutlined from "@ant-design/icons/es/icons/DeleteOutlined";
import InfoCircleOutlined from "@ant-design/icons/es/icons/InfoCircleOutlined";
import LineChartOutlined from "@ant-design/icons/es/icons/LineChartOutlined";
import EditOutlined from "@ant-design/icons/es/icons/EditOutlined";
import ReloadOutlined from "@ant-design/icons/es/icons/ReloadOutlined";
import ReactECharts from "echarts-for-react";
import dayjs from "dayjs";
import { useSearchParams } from "react-router-dom";
import AppToolbar from "./AppToolbar.jsx";
import { ConfigConsoleButton } from "./ConfigConsole.jsx";
import { useAuth } from "./AuthContext.jsx";
import { useThemeMode } from "./ThemeModeProvider.jsx";
import { candlePaletteFromStyles, getKlineChartHeight } from "./klineChartOption.js";
import KlineIndicatorControls, { useKlineIndicatorSettings } from "./KlineIndicatorControls.jsx";
import { buildReplayChartOption, candleDate, clampReplayCursor, exportReplayNotesCsv, findReplayCursor, getVisibleReplayNotes, initialReplayCursor, normalizeReplayCode } from "./replayViewModel.js";
import * as api from "./api.js";
import "./KlineReplay.css";

const EMPTY = [];
const PERIODS = [{ label: "日 K", value: "1d" }, { label: "周 K", value: "1wk" }, { label: "月 K", value: "1mo" }];
const STANCES = [{ label: "看多", value: "bullish" }, { label: "观察", value: "observe" }, { label: "看空", value: "bearish" }];
const STANCE_LABEL = { bullish: "看多", bearish: "看空", observe: "观察" };
const numberValue = (value) => value === null || value === undefined || typeof value === "boolean" || (typeof value === "string" && !value.trim()) || !Number.isFinite(Number(value)) ? null : Number(value);
const fixed = (value, digits = 2) => numberValue(value) === null ? "—" : Number(value).toLocaleString("zh-CN", { minimumFractionDigits: digits, maximumFractionDigits: digits });
const percentage = (value, signed = false) => numberValue(value) === null ? "—" : `${signed && Number(value) > 0 ? "+" : ""}${fixed(value)}%`;
const amountText = (value, unit = "元") => {
  const n = numberValue(value);
  if (n === null) return "—";
  if (Math.abs(n) >= 1e8) return `${fixed(n / 1e8)} 亿${unit}`;
  if (Math.abs(n) >= 1e4) return `${fixed(n / 1e4)} 万${unit}`;
  return `${fixed(n, 0)} ${unit}`;
};
const signClass = (value) => numberValue(value) === null || Number(value) === 0 ? "" : Number(value) > 0 ? "replay-up" : "replay-down";
const volumeUnit = (value) => ({ shares: "股", lots: "手" }[value] || value || "股");
const priceBasis = (value) => ({ raw: "不复权", qfq: "前复权", hfq: "后复权" }[value] || value || "来源未标注");
const volumeAxisFormatter = (value) => {
  const number = numberValue(value);
  if (number === null) return "";
  if (Math.abs(number) >= 1e8) return `${(number / 1e8).toFixed(1)}亿`;
  if (Math.abs(number) >= 1e4) return `${(number / 1e4).toFixed(0)}万`;
  return String(Math.round(number));
};
const readNotes = (key) => {
  try {
    const data = JSON.parse(localStorage.getItem(key) || "[]");
    return Array.isArray(data) ? data.filter((item) => item && typeof item.id === "string" && /^\d{4}-\d{2}-\d{2}$/.test(item.date) && typeof item.text === "string").slice(-2000) : [];
  } catch { return []; }
};
function metadataText(meta, definition, fallback) {
  const definitionText = typeof definition === "string" ? definition : definition?.description || definition?.definition || "";
  return [definitionText || fallback, meta?.source ? `来源：${meta.source}` : "", meta?.note, meta?.report_period ? `报告期：${meta.report_period}` : "", meta?.published_at ? `公告日期：${meta.published_at}` : "", meta?.as_of ? `截至：${meta.as_of}` : ""].filter(Boolean).join("；");
}
function metricValue(candle, key) {
  const meta = candle?.metrics?.[key];
  if (meta && Object.hasOwn(meta, "value")) return meta.value;
  return candle?.[key];
}
function MetricTable({ candle, data }) {
  const rows = [
    [
      { key: "market_cap", label: "总市值", value: amountText(metricValue(candle, "market_cap")), note: "回放日的总市值。", financial: true },
      { key: "profit_ttm", label: "净利润 TTM", value: amountText(metricValue(candle, "profit_ttm")), note: "截至回放日已公布的滚动净利润。", financial: true },
      { key: "pe_ttm", label: "PE TTM", value: fixed(metricValue(candle, "pe_ttm")), note: "回放日的市盈率 TTM。", financial: true },
      { key: "volume", label: "成交量", value: amountText(candle?.v, volumeUnit(data?.volume_unit)), note: `当日成交量，单位：${volumeUnit(data?.volume_unit)}。` },
    ],
    [
      { key: "volume_ratio", label: "日量比", value: fixed(metricValue(candle, "volume_ratio")), note: "当日成交量 / 此前 5 个交易日平均成交量；不足 5 日时留空。" },
      { key: "amount", label: "成交额", value: amountText(candle?.amount), note: "当日累计成交金额。" },
      { key: "turnover_rate", label: "换手率", value: percentage(candle?.turnover_rate), note: "当日换手率。" },
      { key: "amplitude_pct", label: "振幅", value: percentage(candle?.amplitude_pct), note: "当日最高价与最低价的价差 / 前一交易日收盘价。" },
    ],
    [
      { key: "o", label: "开盘", value: fixed(candle?.o), note: "当日开盘价。" },
      { key: "h", label: "最高", value: fixed(candle?.h), note: "当日最高价。" },
      { key: "l", label: "最低", value: fixed(candle?.l), note: "当日最低价。" },
      { key: "c", label: "收盘", value: fixed(candle?.c), note: "回放日收盘价。" },
    ],
  ];
  return <table className="replay-metrics-table"><caption className="replay-sr-only">{candleDate(candle)} 历史行情与估值数据</caption><tbody>{rows.map((row, rowIndex) => <tr key={rowIndex}>{row.map((item) => <td key={item.key}>
    <Tooltip title={metadataText(candle?.metrics?.[item.key], data?.metric_definitions?.[item.key], item.note)}><span tabIndex={0} className="replay-metric-label">{item.label}<InfoCircleOutlined aria-hidden="true" /></span></Tooltip>
    <strong>{item.value}</strong>
    {item.financial ? <span className={`replay-metric-status${candle?.metrics?.[item.key]?.status === "estimated" ? " is-estimated" : ""}`}>{item.value === "—" ? "缺少历史数据" : candle?.metrics?.[item.key]?.status === "estimated" ? "估算 · 历史市值 / PE" : candle?.metrics?.[item.key]?.status === "calculated" ? "按当时已公告财报计算" : "历史口径"}</span> : null}
  </td>)}</tr>)}</tbody></table>;
}
function PracticeNotebook({ userId, symbol, date, close, blind, onFocus }) {
  const { message } = AntApp.useApp();
  const storageKey = `sva:replay-notes:v1:${encodeURIComponent(String(userId ?? "local"))}`;
  const [notes, setNotes] = useState(() => readNotes(storageKey));
  const [drafts, setDrafts] = useState({});
  const [storageError, setStorageError] = useState(false);
  const stockNotes = useMemo(() => notes.filter((note) => normalizeReplayCode(note.symbol) === normalizeReplayCode(symbol)), [notes, symbol]);
  const visibleNotes = useMemo(() => getVisibleReplayNotes(stockNotes, date).slice().sort((a, b) => b.date.localeCompare(a.date)), [stockNotes, date]);
  const noteId = `${normalizeReplayCode(symbol)}:${date}`;
  const saved = stockNotes.find((note) => note.id === noteId);
  const draft = drafts[noteId] || { stance: saved?.stance || "observe", text: saved?.text || "" };
  const updateDraft = (update) => setDrafts((previous) => ({ ...previous, [noteId]: { ...draft, ...update } }));
  const persist = (next) => {
    setNotes(next);
    try { localStorage.setItem(storageKey, JSON.stringify(next)); setStorageError(false); }
    catch { setStorageError(true); }
  };
  const save = () => {
    if (!date || !draft.text.trim()) return;
    const next = [...notes.filter((note) => note.id !== noteId), { id: noteId, date, symbol, stance: draft.stance, text: draft.text.trim(), price: close, createdAt: new Date().toISOString() }].slice(-2000);
    persist(next);
    message.success("当日判断已保存");
  };
  const remove = (note) => {
    persist(notes.filter((item) => item.id !== note.id));
    setDrafts((previous) => { const next = { ...previous }; delete next[note.id]; return next; });
  };
  const download = () => {
    const csv = exportReplayNotesCsv(visibleNotes, date);
    const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8;" }));
    const anchor = document.createElement("a");
    anchor.href = url; anchor.download = `K线复盘-${blind ? "盲练" : normalizeReplayCode(symbol)}-${date}.csv`;
    document.body.appendChild(anchor); anchor.click(); anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  return <aside className="replay-notebook replay-panel" aria-label="盘感练习笔记"><div className="replay-panel-heading"><div><h2><EditOutlined aria-hidden="true" /> 复盘笔记</h2></div><Tooltip title="导出截至回放日的笔记"><Button type="text" icon={<DownloadOutlined />} aria-label="导出复盘笔记" disabled={!visibleNotes.length} onClick={download} /></Tooltip></div>
    <div className="replay-note-composer"><div className="replay-note-date"><span>判断日期</span><strong>{date || "等待载入"}</strong></div>
      <Segmented block aria-label="当前方向判断" options={STANCES} value={draft.stance} disabled={!date} onChange={(stance) => { onFocus(); updateDraft({ stance }); }} />
      <Input.TextArea aria-label="当前交易日的判断依据" value={draft.text} onFocus={onFocus} onChange={(event) => updateDraft({ text: event.target.value })} placeholder="记录判断依据、关键价位和后续观察条件" autoSize={{ minRows: 5, maxRows: 9 }} maxLength={2000} showCount disabled={!date} />
      <Button type="primary" block onClick={save} disabled={!date || !draft.text.trim()}>{saved ? "更新当日判断" : "记录当日判断"}</Button>
      <p className="replay-caption">笔记按账户保存在本浏览器，仅显示截至回放日的记录。</p>
      {storageError ? <Alert type="warning" showIcon message="浏览器无法保存笔记，请在离开前导出。" /> : null}
    </div>
    <div className="replay-note-history"><div className="replay-between"><h3>历史笔记</h3><span>{visibleNotes.length} 条</span></div>{visibleNotes.length ? <ol>{visibleNotes.map((note) => <li key={note.id}><div className="replay-between"><time dateTime={note.date}>{note.date}</time><Tag color={note.stance === "bullish" ? "red" : note.stance === "bearish" ? "green" : "default"}>{STANCE_LABEL[note.stance] || "观察"}</Tag></div><p>{note.text}</p><div className="replay-between"><span className="replay-caption">当日收盘 {fixed(note.price)}</span><Button type="text" size="small" icon={<DeleteOutlined />} aria-label={`删除 ${note.date} 的复盘笔记`} onClick={() => remove(note)} /></div></li>)}</ol> : <div className="replay-notes-empty"><EditOutlined aria-hidden="true" /><p>暂无笔记</p></div>}</div>
  </aside>;
}
function KlineReplayWorkspace({ user, authRequired, logout, initialSymbol }) {
  const { mode } = useThemeMode();
  const today = useMemo(() => dayjs(candleDate({ t: Date.now() / 1000 })), []);
  const [stockInput, setStockInput] = useState(initialSymbol || "600519");
  const [stockOptions, setStockOptions] = useState([]);
  const [searchTerm, setSearchTerm] = useState("");
  const [range, setRange] = useState(() => [today.subtract(1, "year"), today]);
  const [startDate, setStartDate] = useState(null);
  const [request, setRequest] = useState(() => ({ symbol: initialSymbol || "600519", start_date: today.subtract(1, "year").format("YYYY-MM-DD"), end_date: today.format("YYYY-MM-DD"), replayStart: "", version: 0 }));
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [formError, setFormError] = useState("");
  const [cursor, setCursor] = useState(0);
  const [origin, setOrigin] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [period, setPeriod] = useState("1d");
  const { settings: indicatorSettings, setSettings: setIndicatorSettings } = useKlineIndicatorSettings("replay");
  const [blind, setBlind] = useState(false);
  const [jumpDate, setJumpDate] = useState(null);
  const [jumpNotice, setJumpNotice] = useState("");
  const [palette, setPalette] = useState(() => candlePaletteFromStyles(getComputedStyle(document.documentElement)));
  const candles = data?.candles || EMPTY;
  const currentIndex = clampReplayCursor(cursor, candles.length);
  const current = candles[currentIndex];
  const currentDate = current ? candleDate(current) : "";
  const firstDate = candles.length ? candleDate(candles[0]) : "";
  const lastDate = candles.length ? candleDate(candles[candles.length - 1]) : "";
  const canReplay = Boolean(current) && !loading;
  const atEnd = canReplay && currentIndex >= candles.length - 1;
  const paused = useCallback(() => setPlaying(false), []);

  useEffect(() => {
    if (!searchTerm.trim()) { setStockOptions([]); return undefined; }
    let alive = true;
    const timer = window.setTimeout(() => {
      api.searchStocks(searchTerm.trim(), 12).then((result) => {
        if (!alive) return;
        setStockOptions((result.items || []).map((item) => ({ value: item.symbol || item.code, label: `${item.code || item.symbol}  ${item.name || ""}` })));
      }).catch(() => { if (alive) setStockOptions([]); });
    }, 250);
    return () => { alive = false; window.clearTimeout(timer); };
  }, [searchTerm]);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError(""); setData(null); setPlaying(false); setCursor(0); setOrigin(0); setJumpDate(null); setJumpNotice("");
    api.getKlineReplay(request.symbol, { start_date: request.start_date, end_date: request.end_date, refresh: request.version > 0, signal: controller.signal }).then((result) => {
      if (controller.signal.aborted) return;
      const nextCandles = Array.isArray(result?.candles) ? result.candles : [];
      const nextIndex = initialReplayCursor(nextCandles, request.replayStart);
      setData({ ...result, candles: nextCandles }); setCursor(nextIndex); setOrigin(nextIndex);
      if (nextCandles.length) setJumpDate(dayjs(candleDate(nextCandles[nextIndex])));
    }).catch((failure) => {
      if (!controller.signal.aborted) setError(api.getApiErrorMessage(failure));
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [request]);

  useEffect(() => {
    if (!playing || !canReplay || atEnd) { if (atEnd) setPlaying(false); return undefined; }
    const timer = window.setInterval(() => setCursor((previous) => Math.min(previous + 1, candles.length - 1)), 1000 / speed);
    return () => window.clearInterval(timer);
  }, [playing, canReplay, atEnd, candles.length, speed]);
  useEffect(() => {
    const onHidden = () => { if (document.hidden) setPlaying(false); };
    document.addEventListener("visibilitychange", onHidden);
    return () => document.removeEventListener("visibilitychange", onHidden);
  }, []);
  useEffect(() => { if (currentDate) setJumpDate(dayjs(currentDate)); }, [currentDate]);
  const step = useCallback((direction) => {
    if (!canReplay) return;
    setPlaying(false); setJumpNotice("");
    setCursor((previous) => clampReplayCursor(previous + direction, candles.length));
  }, [canReplay, candles.length]);
  useEffect(() => {
    const onKey = (event) => {
      if (!canReplay || event.altKey || event.ctrlKey || event.metaKey || event.repeat) return;
      const target = event.target;
      if (target instanceof Element && (target.closest("input, textarea, select, button, a, [contenteditable='true'], [role='slider'], [role='dialog'], .ant-select, .ant-picker") || target.isContentEditable)) return;
      if (event.key === "ArrowLeft") { event.preventDefault(); step(-1); }
      else if (event.key === "ArrowRight") { event.preventDefault(); step(1); }
      else if (event.code === "Space" && !atEnd) { event.preventDefault(); setPlaying((value) => !value); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [canReplay, atEnd, step]);

  useEffect(() => {
    const frame = requestAnimationFrame(() => setPalette(candlePaletteFromStyles(getComputedStyle(document.documentElement))));
    return () => cancelAnimationFrame(frame);
  }, [mode]);
  const chartOption = useMemo(() => buildReplayChartOption(candles, currentIndex, period, { palette, indicatorSettings, defaultShowBars: 80, formatDate: candleDate, volumeAxisFormatter }), [candles, currentIndex, period, palette, indicatorSettings]);
  const submit = (event) => {
    event.preventDefault();
    const symbol = normalizeReplayCode(stockInput);
    if (!symbol) { setFormError("请输入 6 位 A 股代码，或从搜索结果选择股票。"); return; }
    if (!range?.[0] || !range?.[1] || range[0].isAfter(range[1], "day")) { setFormError("请选择有效的数据起止日期。"); return; }
    if (range[1].diff(range[0], "day") > 3660) { setFormError("单次复盘最多 10 年，请缩小范围。"); return; }
    if (startDate && (startDate.isBefore(range[0], "day") || startDate.isAfter(range[1], "day"))) { setFormError("起练日期需在数据范围内。"); return; }
    setFormError(""); setPlaying(false);
    setRequest({ symbol, start_date: range[0].format("YYYY-MM-DD"), end_date: range[1].format("YYYY-MM-DD"), replayStart: startDate?.format("YYYY-MM-DD") || "", version: request.version + 1 });
  };
  const seek = (index) => { setPlaying(false); setJumpNotice(""); setCursor(clampReplayCursor(index, candles.length)); };
  const jump = () => {
    if (!jumpDate || !canReplay) return;
    const target = jumpDate.format("YYYY-MM-DD");
    const index = findReplayCursor(candles, target);
    seek(index);
    const actual = candleDate(candles[index]);
    setJumpNotice(actual !== target ? `${target} 无交易数据，已定位至 ${actual}。` : "");
  };
  const reset = () => { seek(origin); if (candles[origin]) setJumpDate(dayjs(candleDate(candles[origin]))); };
  const warnings = Array.isArray(data?.warnings) ? data.warnings : EMPTY;
  const warningText = warnings.map((item) => typeof item === "string" ? item : item?.message || item?.note || "").filter(Boolean);
  const progress = candles.length > 1 ? Math.round(currentIndex / (candles.length - 1) * 100) : canReplay ? 100 : 0;
  const periodNote = period === "1d" ? "每次推进 1 个交易日" : `${period === "1wk" ? "周" : "月"} K 合成至回放日，周期内会继续变化`;
  const stockTitle = blind ? "盲练标的" : data?.name || data?.symbol || request.symbol;

  return <Layout className="sva-layout replay-layout"><AppToolbar active="replay" settingsAction={<ConfigConsoleButton isAdmin={user?.role === "admin"} />} user={user} authRequired={authRequired} onLogout={logout} />
    <Layout.Content className="replay-content"><header className="replay-heading"><div><h1>K 线复盘</h1><p>逐日回放行情，记录判断。</p></div><div className="replay-blind-control"><div><strong>隐藏标的</strong></div><Switch checked={blind} onChange={setBlind} aria-label="隐藏股票名称和代码" /></div></header>
      <form className="replay-setup replay-panel" onSubmit={submit} aria-label="设置 K 线复盘"><div className="replay-field replay-stock-field"><label htmlFor="replay-stock-input">复盘标的</label>{blind ? <div className="replay-blind-stock"><LineChartOutlined aria-hidden="true" /><span>标的已隐藏</span><Button size="small" type="text" onClick={() => setBlind(false)}>更换</Button></div> : <AutoComplete id="replay-stock-input" value={stockInput} options={stockOptions} onSearch={setSearchTerm} onChange={setStockInput} onSelect={() => setSearchTerm("")} placeholder="输入股票代码或简称" aria-label="股票代码或简称" allowClear />}</div>
        <div className="replay-field replay-range-field"><label htmlFor="replay-data-start">数据范围</label><DatePicker.RangePicker id={{ start: "replay-data-start", end: "replay-data-end" }} value={range} onChange={setRange} allowClear={false} disabledDate={(date) => date.isAfter(today, "day")} inputReadOnly aria-label="历史数据起止日期" /></div>
        <div className="replay-field replay-start-field"><label htmlFor="replay-start-date">起练日期 <span>可选</span></label><DatePicker id="replay-start-date" value={startDate} onChange={setStartDate} placeholder="默认预览前 60 根" inputReadOnly disabledDate={(date) => date.isAfter(today, "day") || (range?.[0] && date.isBefore(range[0], "day")) || (range?.[1] && date.isAfter(range[1], "day"))} /></div>
        <Button type="primary" htmlType="submit" icon={<ReloadOutlined aria-hidden="true" />} loading={loading}>载入复盘</Button>
      </form>
      {formError ? <Alert className="replay-alert" type="warning" showIcon message={formError} closable onClose={() => setFormError("")} /> : null}
      {data?.cache_stale ? <Alert className="replay-alert" type="warning" showIcon message="历史来源不可用，显示上次保存的数据。" /> : null}
      {error ? <Alert className="replay-alert" type="error" showIcon message="历史行情加载失败" description={error} action={<Button onClick={() => setRequest((previous) => ({ ...previous, version: previous.version + 1 }))}>重试</Button>} /> : null}
      {loading ? <section className="replay-loading replay-panel" aria-busy="true"><Spin size="large" /><h2>正在载入历史行情…</h2></section> : !current ? <section className="replay-empty replay-panel"><LineChartOutlined aria-hidden="true" /><h2>{error ? "复盘数据未载入" : "所选区间暂无行情"}</h2><p>{error ? "请重试或调整股票与日期范围。" : "请检查代码或扩大日期范围。"}</p></section> : <div className="replay-workspace"><main className="replay-main">
        <section className="replay-snapshot replay-panel" aria-label="当前回放日的数据快照"><div className="replay-snapshot-heading"><div className="replay-stock-heading"><h2>{stockTitle}</h2>{!blind && data?.name && data.name !== data.symbol ? <span>{data.symbol}</span> : null}<Tag>{blind ? "盲练中" : "历史复盘"}</Tag></div><div className="replay-current-quote"><strong className={signClass(current.change_pct)}>{fixed(current.c)}</strong><span className={signClass(current.change_pct)}>{percentage(current.change_pct, true)}</span></div></div>
          <div className="replay-snapshot-date"><span>回放日期</span><strong><time dateTime={currentDate}>{currentDate}</time><span>当日收盘</span></strong></div><MetricTable candle={current} data={data} />
        </section>
        <section className="replay-chart-panel replay-panel" aria-label="逐根 K 线图与回放控制"><div className="replay-chart-toolbar"><Segmented options={PERIODS} value={period} onChange={setPeriod} aria-label="K 线周期" /></div><KlineIndicatorControls settings={indicatorSettings} onChange={(next) => { paused(); return setIndicatorSettings(next); }} compact />
          <div className="replay-chart-context"><span><i aria-hidden="true" />截至 {currentDate}</span><span>{periodNote}</span></div>
            <div className="replay-transport replay-transport-top"><div className="replay-play-buttons"><Tooltip title="回到本次起练日期"><Button icon={<UndoOutlined />} aria-label="重置到起练日期" onClick={reset} disabled={currentIndex === origin} /></Tooltip><Tooltip title="回退 1 个交易日（←）"><Button icon={<StepBackwardOutlined />} aria-label="回退 1 个交易日" onClick={() => step(-1)} disabled={currentIndex === 0} /></Tooltip><Button type="primary" className="replay-play-button" icon={playing ? <PauseCircleOutlined /> : <PlayCircleOutlined />} onClick={() => setPlaying((value) => !value)} disabled={atEnd} aria-label={playing ? "暂停自动回放" : "开始自动回放"}>{playing ? "暂停" : "播放"}</Button><Tooltip title="推进 1 个交易日（→）"><Button icon={<StepForwardOutlined />} aria-label="推进 1 个交易日" onClick={() => step(1)} disabled={atEnd} /></Tooltip><Select value={speed} onChange={setSpeed} options={[0.5, 1, 2, 4].map((value) => ({ label: `${value}×`, value }))} aria-label="回放速度" popupMatchSelectWidth={false} /></div>
              <div className="replay-jump"><DatePicker value={jumpDate} onChange={setJumpDate} allowClear={false} inputReadOnly aria-label="跳转至历史日期" disabledDate={(date) => date.format("YYYY-MM-DD") < firstDate || date.format("YYYY-MM-DD") > lastDate} /><Button onClick={jump} disabled={!jumpDate}>跳转</Button></div></div>
          {chartOption ? <ReactECharts option={chartOption} notMerge lazyUpdate style={{ height: getKlineChartHeight(400, indicatorSettings), width: "100%" }} opts={{ renderer: "canvas" }} aria-label={`截至 ${currentDate} 的历史 K 线图`} /> : null}
          <div className="replay-player"><div className="replay-player-top"><div className="replay-player-date"><span>当前交易日</span><strong>{currentDate}</strong></div><span className={`replay-play-state${playing ? " is-playing" : ""}`} role="status">{atEnd ? "本段复盘完成" : playing ? "自动推进中" : "已暂停"}</span></div>
            <Slider min={0} max={Math.max(1, candles.length - 1)} value={currentIndex} onChange={seek} disabled={candles.length < 2} tooltip={{ formatter: (index) => candles[index] ? candleDate(candles[index]) : "" }} aria-label="逐日回放进度" ariaValueTextFormatterForHandle={(index) => candles[index] ? candleDate(candles[index]) : ""} />
            <div className="replay-progress-labels"><span>{firstDate}</span><span>{currentIndex + 1} / {candles.length} 个交易日 · {progress}%</span><span>{lastDate}</span></div>
            {jumpNotice ? <p className="replay-jump-notice" role="status">{jumpNotice}</p> : null}<div className="replay-shortcuts"><span><kbd>←</kbd><kbd>→</kbd> 逐日推进</span><span><kbd>空格</kbd> 播放 / 暂停</span></div>
          </div>
        </section>
        <details className="replay-data-notes replay-panel"><summary><InfoCircleOutlined aria-hidden="true" /><span>数据来源与历史口径</span>{warningText.length ? <Tag color="gold">{warningText.length} 项说明</Tag> : null}</summary><div><p>行情来源：{data.source || "历史行情"} · 价格口径：{priceBasis(data.price_basis)} · 成交量单位：{volumeUnit(data.volume_unit)}</p><p>图表和指标仅使用截至回放日的数据；表格展示回放日数据。</p><p>日量比 = 当日成交量 / 前 5 日均量，与盘中量比不同。缺失数据以「—」表示。</p>{warningText.length ? <ul>{warningText.map((warning, index) => <li key={index}>{warning}</li>)}</ul> : null}</div></details>
      </main><PracticeNotebook userId={user?.id} symbol={data.symbol || request.symbol} date={currentDate} close={current.c} blind={blind} onFocus={paused} /></div>}
    </Layout.Content>
  </Layout>;
}
export default function KlineReplay() {
  const { user, authRequired, logout } = useAuth();
  const [params] = useSearchParams();
  const initialSymbol = normalizeReplayCode(params.get("symbol")) || "600519";
  return <KlineReplayWorkspace key={`${user?.id ?? "local"}:${initialSymbol}`} user={user} authRequired={authRequired} logout={logout} initialSymbol={initialSymbol} />;
}
