import AutopilotStatus from "./AutopilotStatus.jsx";
import RuntimeLogBar from "./RuntimeLogBar.jsx";
import "./SettingsRuntimePanel.css";

export default function SettingsRuntimePanel({ userId, onNavigate }) {
  return <section className="sva-settings-runtime" aria-label="运行状态与日志">
    <AutopilotStatus userId={userId} compact onNavigate={onNavigate} />
    <RuntimeLogBar userId={userId} variant="card" defaultExpanded />
  </section>;
}
