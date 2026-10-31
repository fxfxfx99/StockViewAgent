import { useEffect, useState } from "react";
import { Button, Select } from "antd";
import { DISPLAY_POLL_OPTIONS, displayPollingChoice, displayPollingInterval, displayPollingKey } from "./displayPolling.js";
import "./DisplayRefreshControl.css";

export function useDisplayPolling(userId, page, fallback = "30") {
  const key = displayPollingKey(userId, page);
  const read = () => { try { return displayPollingChoice(localStorage.getItem(key), fallback); } catch { return fallback; } };
  const [saved, setSaved] = useState(() => ({ key, choice: read() }));
  const [visible, setVisible] = useState(() => !document.hidden);
  const choice = saved.key === key ? saved.choice : read();
  useEffect(() => { const update = () => setVisible(!document.hidden); document.addEventListener("visibilitychange", update); return () => document.removeEventListener("visibilitychange", update); }, []);
  const setChoice = (value) => { const next = displayPollingChoice(value, fallback); setSaved({ key, choice: next }); try { localStorage.setItem(key, next); } catch { /* This tab still keeps its choice. */ } };
  return { choice, setChoice, visible, interval: displayPollingInterval(choice, visible) };
}

export default function DisplayRefreshControl({ polling, onRefresh, refreshing, children }) {
  return <section className="sva-display-refresh" aria-label="页面显示设置"><div><strong>页面显示设置</strong><p>页面刷新只读取已保存数据；采集计划在后台单独设置。</p>{children}</div><div className="sva-display-refresh-actions"><Select aria-label="页面显示刷新频率" value={polling.choice} onChange={polling.setChoice} options={DISPLAY_POLL_OPTIONS} /><Button onClick={onRefresh} loading={refreshing}>更新显示</Button></div></section>;
}
