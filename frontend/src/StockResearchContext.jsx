import { useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { Alert, Button, Spin, Tag } from "antd";
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
  const plans = monitoring.data?.plans || [];
  const allEmpty = !research.isPending && !monitoring.isPending && !research.isError && !monitoring.isError && !dossiers.length && !plans.length;
  const researchUrl = investmentUrl(null, symbol);
  const monitorUrl = `/monitor?${new URLSearchParams({ symbol })}`;
  return <div className="stock-research-context">
    {monitorRun ? <Alert className="stock-context-alert" type="info" showIcon message={`监控运行日期 ${context.data?.trigger_date || params.get("as_of") || "读取中"}`} description={<><span>可能与事件日期不同。</span> <Link to={`/research?${new URLSearchParams({ monitor_run: monitorRun })}`}>转入研究 ↗</Link></>} /> : null}
    {research.isError ? <Alert className="stock-context-alert" type="warning" message="研究更新失败" action={<Button onClick={() => research.refetch()}>重试</Button>} /> : null}
    {monitoring.isError ? <Alert className="stock-context-alert" type="warning" message="监控状态暂不可用" action={<Button onClick={() => monitoring.refetch()}>重试</Button>} /> : null}
    {allEmpty ? <div className="stock-context-empty"><div><h2>研究与监控</h2><p>为这只股票建立研究档案，或设置持续观察的条件。</p></div><div className="stock-context-actions"><Link to={researchUrl}>建立研究档案 ↗</Link><Link to={monitorUrl}>新建监控 ↗</Link></div></div> : <div className="stock-context-grid">
      <section className="stock-context-research"><div className="stock-context-heading"><h2>当前研究</h2><Link to={researchUrl}>进入个股研究 ↗</Link></div>
        {research.isPending ? <Spin /> : dossiers.length ? <div className="stock-context-research-list">{dossiers.map((item) => <article key={item.id}><div className="stock-context-heading"><Link to={investmentUrl(item.id, symbol)}>{item.title}</Link><div>{item.unread_count > 0 ? <Tag color="blue">{item.unread_count}条待看</Tag> : null}{item.active_jobs > 0 ? <Tag>任务进行中</Tag> : null}</div></div>{item.adopted_report_summary || item.latest_report_summary ? <><span className="stock-context-note">{item.adopted_report_summary ? "已采纳结论" : "最新结果 · 未采纳"}</span><p>{item.adopted_report_summary || item.latest_report_summary}</p></> : <p className="stock-context-note">{item.objective}</p>}</article>)}</div> : !research.isError ? <p className="stock-context-note">暂无研究档案。</p> : null}
      </section>
      <section className="stock-context-monitor"><div className="stock-context-heading"><h2>关联监控</h2><Link to={monitorUrl}>查看或新建 ↗</Link></div><div className="stock-context-monitors">{plans.map((plan) => <Link key={plan.id} to={`/monitor?${new URLSearchParams({ plan: plan.id, symbol })}`}><Tag>{PLAN_STATUS[plan.status] || plan.status}</Tag>{plan.title}</Link>)}{monitoring.isPending ? <Spin /> : !plans.length && !monitoring.isError ? <p className="stock-context-note">暂无关联监控计划。</p> : null}</div></section>
    </div>}
  </div>;
}
