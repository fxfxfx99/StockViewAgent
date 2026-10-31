import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { Alert, Button, Collapse, Drawer, Select, Switch, Tabs } from "antd";
import SettingOutlined from "@ant-design/icons/es/icons/SettingOutlined";
import { useAuth } from "./AuthContext.jsx";
import { SUB_INDICATORS } from "./klineIndicators.js";
import { applyIndicatorPreset, getIndicatorPreset, indicatorSettingsKey, KLINE_INDICATOR_PRESETS, KLINE_VISIBLE_BARS, normalizeIndicatorSettings, validateIndicatorSettings } from "./klineIndicatorSettings.js";
import { indicatorDraftToSettings, indicatorSettingsToDraft, readKlineIndicatorPreferences, resetIndicatorDraftParameters, writeKlineIndicatorPreferences } from "./klineIndicatorPreferences.js";
import "./KlineIndicatorControls.css";

export function useKlineIndicatorSettings(scope) {
  const { user } = useAuth();
  const userId = user?.id;
  const key = indicatorSettingsKey(userId, scope);
  const stateKey = key || `anonymous:${scope || "stock"}`;
  const loaded = useMemo(() => readKlineIndicatorPreferences(userId, scope), [key, userId, scope]);
  const [state, setState] = useState(() => ({ key: stateKey, settings: loaded }));
  const current = state.key === stateKey ? state.settings : loaded;
  // A changed account uses its own settings immediately, before any effects run.
  const currentRef = useRef(current);
  currentRef.current = current;
  const setSettings = useCallback((next) => {
    const settings = normalizeIndicatorSettings(typeof next === "function" ? next(currentRef.current) : next);
    currentRef.current = settings;
    setState({ key: stateKey, settings });
    return key ? writeKlineIndicatorPreferences(userId, scope, settings) : true;
  }, [key, stateKey, userId, scope]);
  return { settings: current, setSettings };
}

function ParameterInput({ label, value, onChange, min = 1, max = 500, step = 1, text = false, hint }) {
  const id = useId();
  return <label className="kline-indicator-param" htmlFor={id}>
    <span>{label}</span>
    <input id={id} aria-label={label} type={text ? "text" : "number"} inputMode={text ? "text" : "decimal"}
      value={value} min={text ? undefined : min} max={text ? undefined : max} step={text ? undefined : step}
      autoComplete="off" onChange={(event) => onChange(event.target.value)} />
    {hint ? <small>{hint}</small> : null}
  </label>;
}

const MAIN_INDICATORS = [{ id: "ma", label: "MA", name: "MA 均线" }, { id: "boll", label: "BOLL", name: "BOLL 布林线" }];
const PARAMETER_GROUPS = [
  { key: "ma", title: "MA 均线", fields: [{ field: "periods", label: "MA 周期", text: true, hint: "1–3 个不同周期，范围 2–500，用逗号分隔" }] },
  { key: "boll", title: "BOLL 布林线", fields: [{ field: "period", label: "BOLL 周期", min: 2, max: 250 }, { field: "multiplier", label: "标准差倍数", min: 0.1, max: 10, step: 0.1 }] },
  { key: "macd", title: "MACD", fields: [{ field: "fast", label: "快线周期", min: 2, max: 250 }, { field: "slow", label: "慢线周期", min: 2, max: 250 }, { field: "signal", label: "信号周期", max: 120 }], note: "快线须小于慢线。柱值为 2 × (DIF − DEA)。" },
  { key: "kdj", title: "KDJ", fields: [{ field: "period", label: "RSV 周期", min: 2, max: 250 }, { field: "k", label: "K 平滑周期", max: 50 }, { field: "d", label: "D 平滑周期", max: 50 }] },
  { key: "rsi", title: "RSI", fields: [{ field: "periods", label: "RSI 周期", text: true, hint: "1–3 个不同周期，范围 2–500，用逗号分隔" }] },
];
const isGroupActive = (settings, key) => key === "ma" || key === "boll" ? settings.main[key] : settings.subIndicators.includes(key);
const subOptions = (selected) => SUB_INDICATORS.map(({ id, label }) => ({ value: id, label, disabled: selected.length >= 3 && !selected.includes(id) }));

function MainToggles({ main, onChange, fullLabels = false }) {
  return <div className="kline-indicator-chips" role="group" aria-label="主图叠加">
    {MAIN_INDICATORS.map(({ id, label, name }) => <label className={`kline-indicator-chip${main[id] ? " is-active" : ""}`} key={id}>
      <input type="checkbox" checked={main[id]} aria-label={name} onChange={(event) => onChange({ ...main, [id]: event.target.checked })} />
      <span>{fullLabels ? name : label}</span>
    </label>)}
  </div>;
}

export default function KlineIndicatorControls({ settings, onChange, compact = false }) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState(() => indicatorSettingsToDraft(settings));
  const [errors, setErrors] = useState([]);
  const [storageWarning, setStorageWarning] = useState(false);
  const [tab, setTab] = useState("display");
  const [expanded, setExpanded] = useState(["ma"]);
  const draftOwner = useRef(settings);
  const selectorId = useId(); const draftSubId = useId(); const barsId = useId(); const explanationId = useId();
  useEffect(() => {
    if (open && draftOwner.current !== settings) { setOpen(false); setErrors([]); }
  }, [settings, open]);
  const preset = getIndicatorPreset(settings);
  const selected = settings.subIndicators;
  const change = (next) => setStorageWarning(onChange(next) === false);
  const updateDraft = (next) => { setDraft(next); setErrors([]); };
  const setParam = (group, field, value) => updateDraft((old) => ({ ...old, params: { ...old.params, [group]: { ...old.params[group], [field]: value } } }));
  const openSettings = () => {
    draftOwner.current = settings;
    setDraft(indicatorSettingsToDraft(settings)); setErrors([]); setTab("display");
    setExpanded([PARAMETER_GROUPS.find((group) => isGroupActive(settings, group.key))?.key || "ma"]);
    setOpen(true);
  };
  const apply = () => {
    if (draftOwner.current !== settings) { setOpen(false); return; }
    const next = indicatorDraftToSettings(draft);
    const problems = validateIndicatorSettings(next);
    setErrors(problems);
    if (problems.length) { setTab("parameters"); return; }
    change(next); setOpen(false);
  };
  const groups = [...PARAMETER_GROUPS].sort((a, b) => Number(isGroupActive(draft, b.key)) - Number(isGroupActive(draft, a.key)));
  const parameterItems = groups.map((group) => ({ key: group.key,
    label: <div className="kline-parameter-heading"><strong>{group.title}</strong><span>{isGroupActive(draft, group.key) ? "已启用" : "未启用"}</span></div>,
    children: <div className="kline-indicator-parameter-fields">{group.fields.map(({ field, ...input }) => <ParameterInput key={field} {...input} value={draft.params[group.key][field]} onChange={(value) => setParam(group.key, field, value)} />)}{group.note ? <p>{group.note}</p> : null}</div>,
  }));
  const displayPanel = <div className="kline-settings-display">
    <section><h3>主图叠加</h3><MainToggles main={draft.main} fullLabels onChange={(main) => updateDraft((old) => ({ ...old, main }))} /></section>
    <section><label className="kline-settings-label" htmlFor={draftSubId}>副图指标 <span>最多 3 个，按选择顺序排列</span></label><Select id={draftSubId} mode="multiple" allowClear virtual={false} value={draft.subIndicators} options={subOptions(draft.subIndicators)} placeholder="不显示副图" aria-label="设置副图指标，最多 3 个" onChange={(subIndicators) => { if (subIndicators.length <= 3) updateDraft((old) => ({ ...old, subIndicators })); }} /></section>
    <section><label className="kline-settings-label" htmlFor={barsId}>初始显示范围</label><Select id={barsId} aria-label="初始显示 K 线根数" value={draft.display.visibleBars} options={KLINE_VISIBLE_BARS.map((value) => ({ value, label: `${value} 根 K 线` }))} onChange={(visibleBars) => updateDraft((old) => ({ ...old, display: { ...old.display, visibleBars } }))} /><p>应用后调整查看范围，已加载的历史数据仍可缩放查看。</p></section>
    <section className="kline-settings-switches"><label><span><strong>网格线</strong><small>辅助比较价格与指标位置</small></span><Switch aria-label="显示网格线" checked={draft.display.showGrid} onChange={(showGrid) => updateDraft((old) => ({ ...old, display: { ...old.display, showGrid } }))} /></label><label><span><strong>最新价线</strong><small>标记图中最后一根 K 线的收盘价</small></span><Switch aria-label="显示最新价线" checked={draft.display.showLastPrice} onChange={(showLastPrice) => updateDraft((old) => ({ ...old, display: { ...old.display, showLastPrice } }))} /></label></section>
  </div>;
  const parametersPanel = <div className="kline-settings-parameters"><div className="kline-parameters-intro"><p>按 K 线根数计算，周 K 的 20 表示 20 周。</p><Button type="link" size="small" onClick={() => updateDraft(resetIndicatorDraftParameters)}>恢复参数</Button></div><Collapse activeKey={expanded} onChange={setExpanded} items={parameterItems} bordered={false} /><p className="kline-settings-hint">未启用指标也可提前调整参数。恢复参数保留显示范围和图表选择。</p></div>;

  return <div className={`kline-indicator-controls${compact ? " is-compact" : ""}`}>
    <div className="kline-indicator-toolbar">
      <div className="kline-indicator-preset"><span>{preset === "custom" ? "自定义" : "方案"}</span><div className="kline-indicator-presets" role="group" aria-label="图表方案">{KLINE_INDICATOR_PRESETS.map(({ id, label }) => <Button key={id} autoInsertSpace={false} type={preset === id ? "primary" : "default"} aria-pressed={preset === id} onClick={() => change(applyIndicatorPreset(settings, id))}>{label}</Button>)}</div></div>
      <MainToggles main={settings.main} onChange={(main) => change({ ...settings, main })} />
      <div className="kline-indicator-sub"><label htmlFor={selectorId}>副图</label><Select id={selectorId} mode="multiple" maxTagCount="responsive" allowClear virtual={false} value={selected} options={subOptions(selected)} placeholder="不显示副图" aria-label="副图指标，最多 3 个" aria-describedby={explanationId} onChange={(subIndicators) => { if (subIndicators.length <= 3) change({ ...settings, subIndicators }); }} /></div>
      <Button className="kline-indicator-parameters" icon={<SettingOutlined aria-hidden="true" />} onClick={openSettings}>图表设置</Button>
    </div>
    <p className="kline-indicator-note" id={explanationId}>指标随 K 线周期重算，数据不足处留空。</p>
    {storageWarning ? <Alert type="warning" showIcon message="设置已应用，但未能保存到浏览器；刷新后可能丢失。" /> : null}
    <Drawer title="图表设置" open={open} onClose={() => setOpen(false)} width={400} rootClassName="kline-settings-drawer" destroyOnHidden footer={<div className="kline-settings-actions"><Button onClick={() => updateDraft(indicatorSettingsToDraft())}>恢复默认</Button><span /><Button autoInsertSpace={false} onClick={() => setOpen(false)}>取消</Button><Button autoInsertSpace={false} type="primary" onClick={apply}>应用</Button></div>}>
      <p className="kline-settings-intro">设置应用于当前页面，按账户保存在本浏览器。</p>
      {errors.length ? <Alert type="error" showIcon role="alert" className="kline-indicator-validation" message="请检查指标参数" description={<ul>{errors.map((error, index) => <li key={`${index}-${error}`}>{error}</li>)}</ul>} /> : null}
      <Tabs activeKey={tab} onChange={setTab} items={[{ key: "display", label: "显示", children: displayPanel }, { key: "parameters", label: "指标参数", children: parametersPanel }]} />
      <p className="kline-settings-hint">修改后点击“应用”生效。恢复默认会重置全部指标、参数和显示设置。</p>
    </Drawer>
  </div>;
}
