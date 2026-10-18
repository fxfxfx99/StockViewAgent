import { Button, Space, Tag, Tooltip } from "antd";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import * as api from "./api.js";
import { autopilotPollInterval, autopilotPresentation, autopilotQueryKey } from "./autopilotViewModel.js";
import "./ConfigConsole.css";

export default function AutopilotStatus({ userId, compact = false, onNavigate }) {
  const query = useQuery({
    queryKey: autopilotQueryKey(userId),
    queryFn: ({ signal }) => api.getAutopilotStatus({ signal }),
    enabled: Boolean(userId),
    staleTime: 5000,
    refetchInterval: (current) => autopilotPollInterval(current.state.data),
    refetchIntervalInBackground: false,
    retry: (count, error) => ![403, 404].includes(error?.response?.status) && count < 1,
  });
  // Public trial deployments intentionally omit local research control routes.
  if (!userId || [403, 404].includes(query.error?.response?.status)) return null;
  const view = autopilotPresentation(query.data);
  const description = query.isError ? "状态读取失败，请检查服务并重试。" : view.description;
  const label = query.isError ? "需关注" : view.label;
  const tone = query.isError ? "warning" : view.tone;
  return (
    <div className={`sva-autopilot-status${compact ? " sva-autopilot-status--compact" : ""}`} role="status" aria-live="polite">
      <div className="sva-autopilot-status__heading">
        {compact ? <Tooltip title={description}><span className="sva-autopilot-status__summary" tabIndex={0}><i className={`sva-autopilot-dot sva-autopilot-dot--${tone}`} aria-hidden="true" />自动运行 · {label}</span></Tooltip> : <><span>自动运行</span><Tag color={tone}>{label}</Tag></>}
      </div>
      <p className={`sva-autopilot-status__description${compact && label !== "需关注" ? " sva-autopilot-status__description--accessible" : ""}`}>
        {description}
      </p>
      <Space size={8} className="sva-autopilot-status__actions">
        {query.isError ? <Button size="small" onClick={() => void query.refetch()} loading={query.isFetching}>重试</Button> : null}
        {!query.isError && view.action === "setup" ? <Link to="/setup" onClick={onNavigate}><Button type="primary" size="small">配置大模型</Button></Link> : null}
        {!query.isError && view.action === "research" ? <Link to="/research" onClick={onNavigate}>查看进度</Link> : null}
      </Space>
    </div>
  );
}
