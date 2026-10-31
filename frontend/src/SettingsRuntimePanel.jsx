import { Link } from "react-router-dom";
import { useAuth } from "./AuthContext.jsx";
import AutopilotStatus from "./AutopilotStatus.jsx";
import RuntimeLogBar from "./RuntimeLogBar.jsx";
import "./SettingsRuntimePanel.css";

export default function SettingsRuntimePanel({ userId, onNavigate }) {
  const { isAdmin } = useAuth();
  return <section className="sva-settings-runtime" aria-label="运行状态与日志">
    {isAdmin ? <p className="sva-settings-helper">采集计划与资源限额在 <Link to="/data" onClick={onNavigate}>后台采集设置</Link> 管理。研究任务使用当前账户模型预算。</p> : <p className="sva-settings-helper">研究任务使用当前账户模型预算。</p>}
    <AutopilotStatus userId={userId} compact onNavigate={onNavigate} />
    <RuntimeLogBar userId={userId} variant="card" defaultExpanded />
  </section>;
}
