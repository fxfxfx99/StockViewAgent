import { useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { Alert, Button, Empty, Spin, Tag } from "antd";
import { useAuth } from "./AuthContext.jsx";
import { investmentKey, investmentUrl } from "./investmentViewModel.js";
import { monitoringKey, PLAN_STATUS } from "./monitoringViewModel.js";
import * as api from "./api.js";
import "./StockResearchContext.css";

export default function StockResearchContext({ symbol }) {
  const { user } = useAuth();
  const [params] = useSearchParams();
  const monitorRun = params.get("monitor_run");
  const research = useQuery({ queryKey: investmentKey(user?.id, "overview", symbol), queryFn: ({ signal }) => api.getInvestmentOverview({ symbol }, { signal }), enabled: Boolean(symbol), retry: false, refetchInterval: 15000 });
  const monitoring = useQuery({ queryKey: monitoringKey(user?.id, "overview", symbol), queryFn: ({ signal }) => api.getMonitoringOverview({ symbol, signal }), enabled: Boolean(symbol), retry: false, refetchInterval: 15000 });
  const context = useQuery({ queryKey: monitoringKey(user?.id, "run-context", monitorRun), queryFn: ({ signal }) => api.getMonitoringRunContext(monitorRun, { signal }), enabled: Boolean(monitorRun), retry: false });
  const dossiers = (research.data?.dossiers || []).filter((item) => item.status === "active");
  return <div className="stock-research-context">{monitorRun ? <Alert className="stock-context-alert" type="info" showIcon message={`监控运行日期 ${context.data?.trigger_date || params.get("as_of") || "读取中"}`} description={<><span>可能与事件日期不同。</span> <Link to={`/research?${new URLSearchParams({ monitor_run: monitorRun })}`}>转入研究 ↗</Link></>} /> : null}
    <div className="stock-context-heading"><div><h2>当前研究</h2><p>采纳的结论显示为当前研究。</p></div><Link to={investmentUrl(null, symbol)}>进入个股研究 ↗</Link></div>
    {research.isError ? <Alert className="stock-context-alert" type="warning" message="研究更新失败" action={<Button onClick={() => research.refetch()}>重试</Button>} /> : null}
    {research.isPending ? <Spin /> : dossiers.length ? <div className="stock-context-research-list">{dossiers.map((item) => <article key={item.id}><div className="stock-context-heading"><Link to={investmentUrl(item.id, symbol)}>{item.title}</Link><div>{item.unread_count > 0 ? <Tag color="blue">{item.unread_count}条待看</Tag> : null}{item.active_jobs > 0 ? <Tag>任务进行中</Tag> : null}</div></div><p>{item.adopted_report_summary || "尚未采纳结论"}</p>{!item.adopted_report_id && item.latest_report_summary ? <p className="stock-context-note">最新结果（未采纳）：{item.latest_report_summary}</p> : null}<span className="stock-context-note">研究目标：{item.objective}</span></article>)}</div> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无研究，可进入研究页添加。" />}
    <div className="stock-context-heading"><h2>关联监控</h2><Link to={`/monitor?${new URLSearchParams({ symbol })}`}>查看或新建监控 ↗</Link></div>{monitoring.isError ? <Alert type="warning" message="监控状态暂不可用" action={<Button onClick={() => monitoring.refetch()}>重试</Button>} /> : null}<div className="stock-context-monitors">{(monitoring.data?.plans || []).map((plan) => <Link key={plan.id} to={`/monitor?${new URLSearchParams({ plan: plan.id, symbol })}`}><Tag>{PLAN_STATUS[plan.status] || plan.status}</Tag>{plan.title}</Link>)}{!monitoring.isPending && !monitoring.data?.plans?.length ? <p className="stock-context-note">暂无关联监控计划。</p> : null}</div>
  </div>;
}
