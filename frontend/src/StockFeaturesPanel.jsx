import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Collapse, Empty, Pagination, Spin, Table, Tag } from "antd";
import ReloadOutlined from "@ant-design/icons/es/icons/ReloadOutlined";
import { useAuth } from "./AuthContext.jsx";
import { safeUrl } from "./components/format.js";
import { coverageGaps, directionText, factorExposures, featureAssessment, featureKey, featurePercent, featureRefreshNotice, featureSnapshot, featureText, featureTime, featureValue, groupFeatures, stringList } from "./stockFeaturesViewModel.js";
import * as api from "./api.js";
import "./StockFeaturesPanel.css";

const EMPTY = [];
const FEATURE_PAGE_SIZE = 6;

function Lines({ items, empty }) {
  const values = stringList(items);
  return values.length ? <ul className="stock-feature-lines">{values.map((value, index) => <li key={index}>{value}</li>)}</ul> : empty ? <p className="stock-feature-note">{empty}</p> : null;
}

function Sources({ ids, evidence, quote, sources }) {
  const [open, setOpen] = useState(false);
  const cited = stringList(ids);
  if (!cited.length) return <p className="stock-feature-note">尚无可追溯来源。</p>;
  const index = open ? new Map(sources.map((source) => [source.source_id, source])) : null;
  return <details className="stock-feature-sources" onToggle={(event) => setOpen(event.currentTarget.open)}><summary>查看依据 · {cited.join("、")}</summary>{open ? <>{quote ? <blockquote>{quote}</blockquote> : null}{cited.map((id) => {
    const source = index.get(id);
    const href = safeUrl(source?.url);
    const quotes = [...new Set((Array.isArray(evidence) ? evidence : EMPTY).filter((item) => item.source_id === id).map((item) => item.quote).filter(Boolean))];
    return <article key={id}><div className="stock-feature-meta"><Tag>{id}</Tag>{href ? <a href={href} target="_blank" rel="noopener noreferrer">{source.title || "查看原始来源"} ↗</a> : <strong>{source?.title || "引用来源未收录"}</strong>}</div>{source ? <><p className="stock-feature-note">资料时间 {featureTime(source.as_of || source.published_at)}</p>{quotes.map((text, i) => <blockquote key={i}>{text}</blockquote>)}{!quotes.length && source.quote ? <p className="stock-feature-source-text">来源摘录：{source.quote.slice(0, 1200)}{source.quote.length > 1200 ? "…" : ""}</p> : !quotes.length ? <p className="stock-feature-note">未保存来源摘录。</p> : null}{source.text || source.quote ? <SourceFullText text={source.text || source.quote} /> : null}</> : <p className="stock-feature-note">当前版本无法核对该引用。</p>}</article>;
  })}</> : null}</details>;
}

function SourceFullText({ text }) {
  const [open, setOpen] = useState(false);
  return <details className="stock-feature-full-source" onToggle={(event) => setOpen(event.currentTarget.open)}><summary>查看已保存的来源内容</summary>{open ? <p className="stock-feature-source-text">{text}</p> : null}</details>;
}

function FeatureGroup({ group, sources }) {
  const [page, setPage] = useState(1);
  const current = Math.min(page, Math.max(1, Math.ceil(group.items.length / FEATURE_PAGE_SIZE)));
  const start = (current - 1) * FEATURE_PAGE_SIZE;
  const shown = group.items.slice(start, start + FEATURE_PAGE_SIZE);
  return <div className="stock-feature-group"><div className="stock-feature-facts">{shown.map((feature) => {
    const assessment = featureAssessment(feature, sources);
    return <article key={feature.id} className="stock-feature-fact"><div className="stock-feature-meta"><strong>{feature.label || feature.name || feature.id}</strong><Tag color={assessment.color}>{assessment.label}</Tag></div><p className="stock-feature-value">{featureValue(feature)}</p>{feature.as_of ? <p className="stock-feature-note">资料时间 {featureTime(feature.as_of)}</p> : null}<Sources ids={feature.source_ids} evidence={feature.evidence} quote={feature.quote} sources={sources} /></article>;
  })}</div>{group.items.length > FEATURE_PAGE_SIZE ? <div className="stock-feature-pagination"><span>第 {start + 1}–{start + shown.length} 项，共 {group.items.length} 项</span><Pagination size="small" simple current={current} pageSize={FEATURE_PAGE_SIZE} total={group.items.length} onChange={setPage} showSizeChanger={false} /></div> : null}</div>;
}

function FeatureFacts({ features, sources }) {
  const groups = groupFeatures(features);
  return <section className="stock-feature-section stock-feature-fact-section"><div className="stock-feature-section-heading"><h3>这家公司的特征</h3><span>{features.length} 项记录</span></div><p className="stock-feature-note">按类别查看，每页最多 6 项；保留资料时间和原文依据，待核实信息不会作为已知事实使用。</p>{groups.length ? <Collapse accordion destroyOnHidden defaultActiveKey={groups.find((group) => group.key === "business")?.key || groups[0].key} items={groups.map((group) => ({ key: group.key, label: <div className="stock-feature-meta"><strong>{group.label}</strong><span>{group.items.length} 项</span></div>, children: <FeatureGroup group={group} sources={sources} /> }))} /> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有可提取的公司特征，需先补充公司资料或财务数据。" />}</section>;
}

function FactorDetail({ factor, features, sources }) {
  const exposures = factorExposures(factor, features, sources);
  return <div className="stock-feature-factor"><h4>为什么与这家公司有关</h4>{exposures.length ? <ul className="stock-feature-lines">{exposures.map((feature) => <li key={feature.id}><strong>{feature.label}</strong>：{feature.value}{!feature.known ? <Tag color="orange">待核实</Tag> : null}</li>)}</ul> : <p className="stock-feature-note">尚无对应的个股特征依据，关联有待核实。</p>}<div className="stock-feature-mechanism"><h4>传导路径</h4><p>{featureText(factor.mechanism, "传导路径尚待补充。")}</p><p>{directionText(factor.direction)}</p></div><div className="stock-feature-condition-grid"><section><h4>成立条件</h4><Lines items={factor.conditions} empty="尚未明确，不能据此判断方向。" /></section><section><h4>反向因素与失效条件</h4><Lines items={factor.counter_factors} empty="尚待补充反向证据。" /></section><section><h4>后续核对</h4><Lines items={factor.watch_points} empty="尚未指定观察指标。" /></section></div><Lines items={factor.missing} /><Sources ids={factor.source_ids} evidence={factor.evidence} sources={sources} /></div>;
}

function FactorLinks({ factors, features, sources }) {
  return <section className="stock-feature-section"><div className="stock-feature-section-heading"><h3>事件因素如何传导</h3><span>{factors.length} 条条件性关联</span></div><p className="stock-feature-note">由个股特征推导经营影响，再核对市场预期与股价反应。下列路径均需结合具体事件验证。</p>{factors.length ? <Collapse items={factors.map((factor) => ({ key: factor.id || factor.factor_id, label: <div className="stock-feature-meta"><strong>{factor.label || factor.name || factor.factor}</strong><Tag color="orange">待验证推断</Tag></div>, children: <FactorDetail factor={factor} features={features} sources={sources} /> }))} /> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="现有资料不足以建立个股传导路径。" />}</section>;
}

function HistoryStats({ stats = {}, sources }) {
  const behavior = stats.market_behavior || {};
  const reactions = stats.event_reactions || {};
  const items = reactions.items || EMPTY;
  return <section className="stock-feature-section"><div className="stock-feature-section-heading"><h3>历史观测</h3><span>描述已发生的反应</span></div><div className="stock-feature-observations"><div><span>日收益样本</span><strong>{behavior.sample_size ?? 0} 个交易日</strong></div><div><span>20 日区间涨跌幅</span><strong>{featurePercent(behavior.return_20d_pct)}</strong></div><div><span>年化波动率 · {behavior.volatility_sample_size || 60} 日样本</span><strong>{featurePercent(behavior.volatility_60d_pct)}</strong></div><div><span>历史事件样本</span><strong>{reactions.sample_size ?? 0} 条</strong></div></div>{behavior.window_start || behavior.window_end ? <p className="stock-feature-note">行情窗口 {featureText(behavior.window_start, "未知")} 至 {featureText(behavior.window_end, "未知")} · 市场基准 {featureText(behavior.benchmark, "未提供")}</p> : null}<Lines items={behavior.limitations} /><p className="stock-feature-note">事件之后的收益反映时间上的关联；同期市场、行业和其他消息也会影响价格。</p>{items.length ? <Collapse items={[{ key: "reactions", label: `查看事件反应样本（${items.length}）`, children: <Table size="small" pagination={{ pageSize: 6, hideOnSinglePage: true }} scroll={{ x: 800 }} rowKey={(item) => `${item.event_id}:${item.window_sessions}`} dataSource={items} columns={[
    { title: "事件 / 日期", key: "event", render: (_, item) => <div>{item.title || item.event_id}<div className="stock-feature-note">{item.event_date}</div><Sources ids={item.source_ids} evidence={item.evidence} sources={sources} /></div> },
    { title: "观察窗口", dataIndex: "window_sessions", width: 100, render: (value) => `${value} 个交易日` },
    { title: "个股收益", dataIndex: "stock_return_pct", render: featurePercent, width: 100 },
    { title: "基准收益", dataIndex: "benchmark_return_pct", render: featurePercent, width: 100 },
    { title: "超额收益", dataIndex: "excess_return_pct", render: featurePercent, width: 100 },
    { title: "收益起点", dataIndex: "baseline", render: (value) => value === "event_day_or_prior_close" ? "事件日或前一交易日收盘" : featureText(value, "未提供"), width: 140 },
  ]} /> }]} /> : <p className="stock-feature-note">尚无可用的事件反应样本，不能估计事件影响的幅度。</p>}<Lines items={reactions.limitations} /></section>;
}

function VersionHistory({ history }) {
  return <section className="stock-feature-section"><div className="stock-feature-section-heading"><h3>版本与每周变化</h3><span>{history.length} 个版本</span></div>{history.length ? <Collapse items={history.map((version) => ({ key: version.version_id, label: <div className="stock-feature-meta"><span>{featureTime(version.created_at)}</span><span>版本 {version.version_id}</span></div>, children: <VersionChanges version={version} /> }))} /> : <p className="stock-feature-note">建立第一份特征库后，会保留版本和变化记录。</p>}</section>;
}

function VersionChanges({ version }) {
  const changes = version.changes || {};
  const added = changes.added || EMPTY;
  const changed = changes.changed || EMPTY;
  const removed = changes.removed || EMPTY;
  return <div className="stock-feature-version"><p className="stock-feature-note">资料截至 {featureText(version.as_of, "未标注")}</p>{added.length || changed.length || removed.length ? <ul className="stock-feature-lines">{added.map((item, index) => <li key={`add-${index}`}><Tag color="blue">新增</Tag>{featureText(item.label || item.name || item.id || item)}{item.value != null ? `：${featureText(item.value)}` : ""}</li>)}{changed.map((item, index) => <li key={`change-${index}`}><Tag color="orange">更新</Tag>{item.label || item.name || item.id}：{featureText(item.before)} → {featureText(item.after)}</li>)}{removed.map((item, index) => <li key={`remove-${index}`}><Tag>移出</Tag>{featureText(item.label || item.name || item.id || item)}</li>)}</ul> : <p className="stock-feature-note">{version.change_summary || "此版本未记录特征值变化。"}</p>}</div>;
}

function FeaturesContent({ symbol, userId, isAdmin }) {
  const client = useQueryClient();
  const query = useQuery({ queryKey: featureKey(userId, symbol), queryFn: ({ signal }) => api.getStockFeatures(symbol, { signal }), enabled: Boolean(symbol), retry: false, staleTime: 30_000 });
  const refresh = useMutation({ mutationFn: () => api.refreshStockFeatures(symbol), retry: false, onSuccess: async () => { await client.invalidateQueries({ queryKey: featureKey(userId, symbol) }); } });
  const data = query.data;
  const snapshot = featureSnapshot(data, symbol);
  const mismatch = Boolean(data?.snapshot && !snapshot);
  const features = snapshot?.features || EMPTY;
  const factors = snapshot?.factor_links || EMPTY;
  const sources = snapshot?.evidence_sources || EMPTY;
  const schedule = data?.schedule || {};
  const coverage = snapshot?.coverage || {};
  const gaps = coverageGaps(coverage, features);
  const error = query.error || refresh.error;
  const refreshNotice = !refresh.isPending && !refresh.error ? featureRefreshNotice(refresh.data?.refresh_status) : null;
  return <div className="stock-features-panel"><header className="stock-feature-heading"><div><h2>个股特征库</h2><p>围绕 {snapshot?.name || symbol} 的业务、财务与风险敞口，逐周积累可追溯的判断依据。</p></div><Button icon={<ReloadOutlined aria-hidden="true" />} loading={refresh.isPending} disabled={!symbol || query.isPending} onClick={() => refresh.mutate()}>更新特征库</Button></header>
    <div className="stock-feature-update-info"><span>{schedule.enabled === false ? "自动更新已暂停" : schedule.cadence === "weekly" ? "每周更新" : "更新计划待确认"} · 北京时间</span><span>版本创建 {featureTime(snapshot?.created_at)}</span><span>最后检查 {featureTime(schedule.last_success_at || snapshot?.last_checked_at)}</span><span>下次检查 {featureTime(schedule.next_due_at)}</span></div>
    <p className="stock-feature-note">更新使用已采集的本地资料，保留特征变化与检查记录。资料尚未补充的字段会标记为未知。{isAdmin ? <> <Link to="/data?tab=knowledge">缓存与版本管理 →</Link></> : null}</p>
    {error ? <Alert type="warning" showIcon message={refresh.error ? "特征库更新未完成" : "特征库读取失败"} description={api.getApiErrorMessage(error)} action={query.isError ? <Button size="small" onClick={() => query.refetch()}>重试</Button> : null} /> : null}
    {refreshNotice ? <Alert showIcon {...refreshNotice} /> : null}
    {schedule.last_error ? <Alert type="warning" showIcon message="最近一次资料读取失败" description={schedule.enabled === false ? "已有版本已保留。可点击「更新特征库」重试。" : "已有版本已保留，将在下次计划检查时自动重试。"} /> : null}
    {mismatch ? <Alert type="error" showIcon message="返回资料与当前股票不一致，已停止展示。" /> : null}
    {query.isPending ? <div className="stock-feature-loading"><Spin aria-label="读取个股特征库" /></div> : snapshot ? <>
      <section className="stock-feature-coverage"><div className="stock-feature-meta"><Tag color={coverage.status === "ready" ? "blue" : "orange"}>{coverage.status === "ready" ? "现有采集范围已覆盖" : coverage.status === "empty" ? "资料尚不足" : "资料覆盖不完整"}</Tag><span>{sources.length} 份来源</span><span>版本 {snapshot.version_id}</span></div>{gaps.length ? <details open={coverage.status === "empty"}><summary>待补充与未知（{gaps.length}）</summary><Lines items={gaps} /></details> : null}<Lines items={coverage.warnings} /></section>
      <FactorLinks factors={factors} features={features} sources={sources} /><FeatureFacts features={features} sources={sources} /><HistoryStats stats={snapshot.stats} sources={sources} /><VersionHistory history={data?.history || EMPTY} />
    </> : !mismatch && !query.isError ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="尚未建立特征库。点击「更新特征库」从已采集资料创建第一版。" /> : null}
  </div>;
}

export default function StockFeaturesPanel({ symbol }) {
  const { user, isAdmin } = useAuth();
  return symbol ? <FeaturesContent key={`${user?.id}:${symbol}`} symbol={symbol} userId={user?.id} isAdmin={isAdmin} /> : null;
}
