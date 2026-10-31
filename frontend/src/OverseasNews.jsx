import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Alert, Badge, Button, Input, Layout, Pagination, Select, Spin } from "antd";
import GlobalOutlined from "@ant-design/icons/es/icons/GlobalOutlined";
import LinkOutlined from "@ant-design/icons/es/icons/LinkOutlined";
import SearchOutlined from "@ant-design/icons/es/icons/SearchOutlined";
import { Link } from "react-router-dom";
import DisplayRefreshControl, { useDisplayPolling } from "./DisplayRefreshControl.jsx";
import { COLLECTION_REASONS } from "./dataCenterViewModel.js";
import AppToolbar from "./AppToolbar.jsx";
import { ConfigConsoleButton } from "./ConfigConsole.jsx";
import { useAuth } from "./AuthContext.jsx";
import { plainText, safeUrl } from "./components/format.js";
import * as api from "./api.js";
import {
  OVERSEAS_PAGE_SIZE, OVERSEAS_POLL_MS, OVERSEAS_SOURCES,
  overseasEmptyState, overseasSourceStatus, overseasTime, overseasTransport,
} from "./overseasNewsViewModel.js";
import "./OverseasNews.css";

const EMPTY = [];

function SourceHealth({ sources }) {
  const healthy = sources.filter((source) => source.status === "ok" && !source.content_stale).length;
  return <details className="overseas-health">
    <summary>来源状态 <span>{healthy} / {sources.length} 正常</span></summary>
    <div className="overseas-health-grid">
      {sources.map((source) => {
        const info = overseasSourceStatus(source.content_stale && source.status === "ok" ? "stale" : source.status);
        const url = safeUrl(source.active_url);
        return <section className="overseas-source" key={source.id} aria-label={`${source.label}来源状态`}>
          <div className="overseas-source-heading"><strong>{source.label}</strong><Badge status={info.tone} text={info.label} /></div>
          <div className="overseas-source-transport">{overseasTransport(source.transport)}</div>
          {source.access_note ? <p>{source.access_note}</p> : null}
          <dl>
            <div><dt>最新发布</dt><dd>{overseasTime(source.newest_published_at)}</dd></div>
            <div><dt>最近成功</dt><dd>{overseasTime(source.last_success_at)}</dd></div>
            <div><dt>最近尝试</dt><dd>{overseasTime(source.last_attempt_at)}</dd></div>
            <div><dt>下次检查</dt><dd>{overseasTime(source.next_poll_at)}</dd></div>
            {source.items_received != null ? <div><dt>最近返回</dt><dd>{source.items_received} 条</dd></div> : null}
          </dl>
          {source.error ? <p className="overseas-source-error">{source.error}</p> : null}
          {url ? <a className="overseas-source-url" href={url} target="_blank" rel="noopener noreferrer">来源地址 <LinkOutlined aria-hidden="true" /></a> : null}
        </section>;
      })}
    </div>
    <p className="overseas-access-note">Benzinga 授权 API 需配置凭据；公开订阅与搜索索引可能延迟。</p>
  </details>;
}

function NewsItem({ item }) {
  const url = safeUrl(item.url);
  const title = plainText(item.title) || "未提供标题";
  const summary = plainText(item.summary);
  return <article className="overseas-item">
    <div className="overseas-item-meta">
      <span className="overseas-publisher">{item.source || item.source_id || "来源未标注"}</span>
      {item.transport ? <span>{overseasTransport(item.transport)}</span> : null}
    </div>
    <h2>{url ? <a href={url} target="_blank" rel="noopener noreferrer">{title}</a> : title}</h2>
    {summary && summary !== title ? <p className="overseas-item-summary">{summary}</p> : null}
    <div className="overseas-item-footer">
      <div className="overseas-item-times"><span>发布 {overseasTime(item.published_ts)}</span><span>采集 {overseasTime(item.fetched_at)}</span></div>
      {url ? <a href={url} target="_blank" rel="noopener noreferrer" aria-label={`查看原文：${title}`}>查看原文 <LinkOutlined aria-hidden="true" /></a> : <span>原文链接不可用</span>}
    </div>
  </article>;
}

function OverseasNewsContent() {
  const { user, authRequired, logout } = useAuth();
  const polling = useDisplayPolling(user?.id, "overseas-news", "15");
  const [source, setSource] = useState("");
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  useEffect(() => {
    const timer = window.setTimeout(() => { setQuery(search.trim()); setPage(1); }, 350);
    return () => window.clearTimeout(timer);
  }, [search]);

  const news = useQuery({
    queryKey: ["overseas-news", user?.id, source, query, page],
    queryFn: ({ signal }) => api.getOverseasNews({ source, query, limit: OVERSEAS_PAGE_SIZE, offset: (page - 1) * OVERSEAS_PAGE_SIZE }, { signal }),
    staleTime: OVERSEAS_POLL_MS,
    refetchInterval: polling.interval,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: false,
  });
  const status = useQuery({
    queryKey: ["overseas-news-status", user?.id],
    queryFn: ({ signal }) => api.getOverseasNewsStatus({ signal }),
    staleTime: OVERSEAS_POLL_MS,
    refetchInterval: polling.interval,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: false,
  });
  const items = news.data?.items || EMPTY;
  const sources = status.data?.sources || OVERSEAS_SOURCES;
  const total = news.data?.total || 0;
  const filtered = Boolean(source || query);
  const empty = overseasEmptyState({ filtered, enabled: status.data?.enabled, hasError: news.isError });
  const resourceReason = status.data?.resources?.pressure ? "high_memory" : status.data?.reason;
  const cycleLabel = status.isError ? "采集状态不可用" : !status.data ? "读取采集状态中" : !status.data.enabled ? "自动采集未启用" : resourceReason && COLLECTION_REASONS[resourceReason] ? "采集暂缓" : status.data.collecting ? "采集中" : status.data.running === false ? "采集服务未运行" : "等待下次采集";
  const refreshing = news.isFetching || status.isFetching;
  function clearFilters() { setSource(""); setSearch(""); setQuery(""); setPage(1); }

  return <Layout className="sva-layout overseas-layout">
    <AppToolbar active="news" user={user} authRequired={authRequired} onLogout={logout} settingsAction={<ConfigConsoleButton isAdmin={user?.role === "admin"} />} />
    <Layout.Content className="overseas-content">
      <div className="overseas-heading">
        <div><h1>海外快讯</h1><p>Reuters、Bloomberg、Benzinga、WSJ、FT 等财经消息。</p></div>
        {user?.role === "admin" ? <Link to="/data">后台采集设置 ↗</Link> : null}
      </div>

      <DisplayRefreshControl polling={polling} refreshing={refreshing} onRefresh={() => { void news.refetch(); void status.refetch(); }} />
      <div className="overseas-status-bar">
        <Badge status={status.isError ? "warning" : status.data?.enabled ? "processing" : "default"} text={cycleLabel} />
        {status.data?.poll_interval_sec ? <span>后台检查间隔 {status.data.poll_interval_sec} 秒</span> : null}
        <span>最近采集 {overseasTime(status.data?.last_cycle_at)}</span>
        <span>北京时间 UTC+8</span>
      </div>
      <p className="overseas-disclosure">后台按独立计划采集；公开资讯不含付费终端的完整实时流。</p>
      {status.isError ? <Alert className="overseas-alert" type="warning" showIcon message={`来源状态读取失败：${api.getApiErrorMessage(status.error)}`} /> : null}
      {resourceReason && COLLECTION_REASONS[resourceReason] ? <Alert className="overseas-alert" type="warning" showIcon message={COLLECTION_REASONS[resourceReason]} /> : null}
      <SourceHealth sources={sources} />

      <section className="overseas-feed" aria-label="海外财经新闻">
        <div className="overseas-filters">
          <div className="overseas-filter-field"><label htmlFor="overseas-source">新闻来源</label><Select id="overseas-source" value={source} onChange={(value) => { setSource(value); setPage(1); }} options={[{ value: "", label: "全部来源" }, ...sources.map((entry) => ({ value: entry.id, label: entry.label }))]} /></div>
          <div className="overseas-filter-field overseas-search"><label htmlFor="overseas-search">搜索消息</label><Input id="overseas-search" allowClear maxLength={200} prefix={<SearchOutlined aria-hidden="true" />} value={search} placeholder="搜索标题或摘要" onChange={(event) => setSearch(event.target.value)} /></div>
          {filtered || search ? <Button onClick={clearFilters}>清除筛选</Button> : null}
        </div>
        <div className="overseas-feed-meta"><span>{news.data ? `${total.toLocaleString()} 条归档${filtered ? " · 已筛选" : ""}` : "正在读取归档"}</span><span>归档更新 {overseasTime(news.data?.updated_at)}</span></div>
        {news.isError ? <Alert className="overseas-alert" type="warning" showIcon message={`${items.length ? "更新失败，显示上次结果。" : "消息读取失败。"}${api.getApiErrorMessage(news.error)}`} action={<Button size="small" onClick={() => void news.refetch()}>重试</Button>} /> : null}
        {news.isPending ? <div className="overseas-empty" role="status"><Spin /><p>正在读取海外快讯…</p></div> : items.length ? <div className="overseas-items">{items.map((item) => <NewsItem key={item.id} item={item} />)}</div> : <div className="overseas-empty"><GlobalOutlined aria-hidden="true" /><h2>{page > 1 ? "本页暂无消息" : empty.title}</h2><p>{page > 1 ? "返回第一页查看最新消息。" : empty.description}</p>{page > 1 ? <Button onClick={() => setPage(1)}>返回第一页</Button> : filtered ? <Button onClick={clearFilters}>清除筛选</Button> : null}</div>}
        {total > OVERSEAS_PAGE_SIZE || page > 1 ? <div className="overseas-pagination"><Pagination current={page} total={total} pageSize={OVERSEAS_PAGE_SIZE} showSizeChanger={false} onChange={setPage} showLessItems /></div> : null}
      </section>
    </Layout.Content>
  </Layout>;
}

export default function OverseasNews() { const { user } = useAuth(); return <OverseasNewsContent key={user?.id ?? "guest"} />; }
