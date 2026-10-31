import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { Alert, Button, Modal, Select } from "antd";
import { SettingOutlined } from "@ant-design/icons";
import { useAuth } from "./AuthContext.jsx";
import { SUB_INDICATORS } from "./klineIndicators.js";
import { indicatorSettingsKey, normalizeIndicatorSettings, validateIndicatorSettings } from "./klineIndicatorSettings.js";
import { indicatorDraftToSettings, indicatorSettingsToDraft, readKlineIndicatorPreferences, writeKlineIndicatorPreferences } from "./klineIndicatorPreferences.js";
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
    <input id={id} type={text ? "text" : "number"} inputMode={text ? "text" : "decimal"}
      value={value} min={text ? undefined : min} max={text ? undefined : max} step={text ? undefined : step}
      autoComplete="off" onChange={(event) => onChange(event.target.value)} />
    {hint ? <small>{hint}</small> : null}
  </label>;
}

export default function KlineIndicatorControls({ settings, onChange, compact = false }) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState(() => indicatorSettingsToDraft(settings));
  const [errors, setErrors] = useState([]);
  const [storageWarning, setStorageWarning] = useState(false);
  const draftOwner = useRef(settings);
  const selectorId = useId();
  const explanationId = useId();
  useEffect(() => {
    if (open && draftOwner.current !== settings) {
      setOpen(false);
      setErrors([]);
    }
  }, [settings, open]);
  const selected = settings.subIndicators;
  const options = SUB_INDICATORS.map(({ id, label }) => ({
    value: id, label, disabled: selected.length >= 3 && !selected.includes(id),
  }));
  const change = (next) => setStorageWarning(onChange(next) === false);
  const setParam = (group, field, value) => {
    setDraft((old) => ({ ...old, params: { ...old.params, [group]: { ...old.params[group], [field]: value } } }));
    setErrors([]);
  };
  const openParameters = () => {
    draftOwner.current = settings;
    setDraft(indicatorSettingsToDraft(settings));
    setErrors([]);
    setOpen(true);
  };
  const apply = () => {
    if (draftOwner.current !== settings) { setOpen(false); return; }
    const next = indicatorDraftToSettings(draft);
    const problems = validateIndicatorSettings(next);
    setErrors(problems);
    if (!problems.length) {
      change(next);
      setOpen(false);
    }
  };

  return <div className={`kline-indicator-controls${compact ? " is-compact" : ""}`}>
    <div className="kline-indicator-toolbar">
      <fieldset className="kline-indicator-main">
        <legend>主图叠加</legend>
        {[{ id: "ma", label: "MA 均线" }, { id: "boll", label: "BOLL 布林线" }].map(({ id, label }) =>
          <label className="kline-indicator-toggle" key={id}>
            <input type="checkbox" checked={settings.main[id]}
              onChange={(event) => change({ ...settings, main: { ...settings.main, [id]: event.target.checked } })} />
            <span>{label}</span>
          </label>)}
      </fieldset>
      <div className="kline-indicator-sub">
        <label htmlFor={selectorId}>副图 <span>最多 3 个</span></label>
        <Select id={selectorId} mode="multiple" allowClear virtual={false} value={selected} options={options}
          placeholder="关闭副图" aria-label="副图指标，最多 3 个" aria-describedby={explanationId}
          onChange={(value) => { if (value.length <= 3) change({ ...settings, subIndicators: value }); }} />
      </div>
      <Button className="kline-indicator-parameters" icon={<SettingOutlined aria-hidden="true" />} onClick={openParameters}>指标参数</Button>
    </div>
    <p className="kline-indicator-note" id={explanationId}>指标随 K 线周期重算，数据不足处留空。</p>
    {storageWarning ? <Alert type="warning" showIcon message="设置已应用，但未能保存到浏览器；刷新后可能丢失。" /> : null}
    <Modal title="K 线指标参数" open={open} onCancel={() => setOpen(false)} width={640}
      className="kline-indicator-modal" footer={<div className="kline-indicator-modal-actions">
        <Button onClick={() => { setDraft(indicatorSettingsToDraft()); setErrors([]); }}>恢复默认</Button>
        <span />
        <Button onClick={() => setOpen(false)}>取消</Button>
        <Button type="primary" onClick={apply}>应用</Button>
      </div>}>
      <p className="kline-indicator-modal-intro">周期按 K 线根数计算，例如周线的 20 表示 20 周。设置在本浏览器内按账户和页面保存。</p>
      <div className="kline-indicator-parameter-groups">
        <fieldset><legend>MA 均线</legend>
          <ParameterInput label="MA 周期" value={draft.params.ma.periods} onChange={(value) => setParam("ma", "periods", value)} text hint="1 至 3 个不同周期，范围 2–500，用逗号分隔" />
        </fieldset>
        <fieldset><legend>BOLL 布林线</legend>
          <ParameterInput label="BOLL 周期" value={draft.params.boll.period} onChange={(value) => setParam("boll", "period", value)} min={2} max={250} />
          <ParameterInput label="BOLL 标准差倍数" value={draft.params.boll.multiplier} onChange={(value) => setParam("boll", "multiplier", value)} min={0.1} max={10} step={0.1} />
        </fieldset>
        <fieldset><legend>MACD</legend>
          <ParameterInput label="MACD 快线周期" value={draft.params.macd.fast} onChange={(value) => setParam("macd", "fast", value)} min={2} max={250} />
          <ParameterInput label="MACD 慢线周期" value={draft.params.macd.slow} onChange={(value) => setParam("macd", "slow", value)} min={2} max={250} />
          <ParameterInput label="MACD 信号周期" value={draft.params.macd.signal} onChange={(value) => setParam("macd", "signal", value)} max={120} />
          <p>快线周期须小于慢线周期。柱值为 2 × (DIF − DEA)。</p>
        </fieldset>
        <fieldset><legend>KDJ</legend>
          <ParameterInput label="KDJ RSV 周期" value={draft.params.kdj.period} onChange={(value) => setParam("kdj", "period", value)} min={2} max={250} />
          <ParameterInput label="KDJ K 平滑周期" value={draft.params.kdj.k} onChange={(value) => setParam("kdj", "k", value)} max={50} />
          <ParameterInput label="KDJ D 平滑周期" value={draft.params.kdj.d} onChange={(value) => setParam("kdj", "d", value)} max={50} />
        </fieldset>
        <fieldset><legend>RSI</legend>
          <ParameterInput label="RSI 周期" value={draft.params.rsi.periods} onChange={(value) => setParam("rsi", "periods", value)} text hint="1 至 3 个不同周期，范围 2–500，用逗号分隔" />
        </fieldset>
      </div>
      {errors.length ? <Alert type="error" showIcon role="alert" className="kline-indicator-validation"
        message="请检查指标参数" description={<ul>{errors.map((error, index) => <li key={`${index}-${error}`}>{error}</li>)}</ul>} /> : null}
      <p className="kline-indicator-modal-intro">恢复默认会重置参数和图表选择，点击“应用”生效。其他副图使用标准参数。</p>
    </Modal>
  </div>;
}
