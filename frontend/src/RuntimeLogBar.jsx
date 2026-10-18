import { useId, useState } from "react";
import { Button, Segmented, Tag, Tooltip } from "antd";
import { DownOutlined, UpOutlined } from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import * as api from "./api.js";
import {
  RUNTIME_PERIODS, formatRuntimeCount, runtimeOverviewView, runtimePollInterval,
  runtimeQueryKey, runtimeTime, runtimeUnavailable,
} from "./runtimeLogViewModel.js";
import "./RuntimeLogBar.css";

function RuntimeMetric({ label, value, note, description }) {
  return <Tooltip title={description}><div className="runtime-log__metric">
    <dt>{label}</dt><dd>{formatRuntimeCount(value)}{note ? <span className="runtime-log__metric-note">{note}</span> : null}</dd>
  </div></Tooltip>;
}

function RuntimeLogContent({ userId, variant, defaultExpanded }) {
  const [period, setPeriod] = useState("today");
  const [expanded, setExpanded] = useState(defaultExpanded);
  const detailsId = useId();
  const query = useQuery({
    queryKey: runtimeQueryKey(userId, period),
    queryFn: ({ signal }) => api.getRuntimeOverview({ period, signal }),
    staleTime: 30_000,
    refetchInterval: runtimePollInterval,
    refetchIntervalInBackground: false,
    retry: (count, error) => !runtimeUnavailable(error) && count < 1,
  });
  if (runtimeUnavailable(query.error)) return null;
  const view = runtimeOverviewView(query.data);
  const latest = query.isError
    ? `暂时无法更新${query.data ? "，显示上次记录" : "，可展开重试"}`
    : query.isPending ? "正在读取运行记录…"
      : view.latest || "所选时段暂无运行记录";
  return <section className={`runtime-log${variant === "card" ? " runtime-log--card" : variant === "activity" ? " runtime-log--activity" : ""}`} aria-label="运行日志">
    <div className="runtime-log__summary">
      <h2 className="runtime-log__heading"><svg className="runtime-log__icon" viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="M2 10h3l2.5-6 5 12 2.5-6h3" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" /></svg>运行日志</h2>
      <dl className="runtime-log__metrics" aria-label="实际运行用量">
        <RuntimeMetric label="LLM 调用" value={view.calls} description={`实际模型请求 ${formatRuntimeCount(view.calls, false)} 次，含重试、失败和待完成请求。`} />
        <RuntimeMetric label="已报 Token" value={view.tokens} note={view.unknownTokenCalls > 0 ? "+?" : null} description={`${view.tokens === null ? "Token 用量未知。" : `供应商已报告 ${formatRuntimeCount(view.tokens, false)} Token。`}${view.unknownTokenCalls > 0 ? `另有 ${formatRuntimeCount(view.unknownTokenCalls, false)} 次请求用量未知。` : ""}`} />
        <RuntimeMetric label="采集记录" value={view.collected} description={`获取 ${formatRuntimeCount(view.collected, false)} 条，含本机共享公共来源；新增与修订量见展开记录。`} />
      </dl>
      <p className={`runtime-log__latest${query.isError ? " runtime-log__error" : ""}`} title={latest}>{latest}</p>
      <div className="runtime-log__controls">
        <Segmented size="small" options={RUNTIME_PERIODS} value={period} onChange={setPeriod} aria-label="运行日志统计周期" />
        <Tooltip title={`${expanded ? "收起运行日志" : "展开运行日志"} · ${latest}`}><Button className="runtime-log__toggle" type="text" size="small" aria-label={expanded ? "收起运行日志" : "展开运行日志"} aria-expanded={expanded} aria-controls={detailsId} icon={expanded ? <UpOutlined /> : <DownOutlined />} onClick={() => setExpanded((value) => !value)}>{variant === "activity" ? null : expanded ? "收起" : "展开"}</Button></Tooltip>
      </div>
    </div>
    {expanded ? <div className="runtime-log__details" id={detailsId}>
      <div className="runtime-log__details-heading"><strong>最近记录</strong><Button size="small" type="text" onClick={() => void query.refetch()} loading={query.isFetching}>刷新</Button></div>
      {variant === "activity" && !query.isError ? <p className="runtime-log__notice">{latest}</p> : null}
      {query.isError ? <p className="runtime-log__notice runtime-log__error" role="alert">记录更新失败，请重试。{query.data ? "当前显示上次记录。" : ""}</p> : null}
      <div className="runtime-log__facts">{view.facts.map((fact) => <span key={fact.label}>{fact.label} <strong>{fact.value}</strong></span>)}</div>
      <p className="runtime-log__notice">{view.notice}按北京时间统计。</p>
      {view.unknownTokenCalls > 0 ? <p className="runtime-log__notice">供应商未报告 {formatRuntimeCount(view.unknownTokenCalls, false)} 次请求的 Token 用量。</p> : null}
      {view.records.length ? <ol className="runtime-log__records" aria-label="最近运行日志">{view.records.map((record) => <li className="runtime-log__record" key={record.id}>
        <time title={runtimeTime(record.time, { full: true })}>{runtimeTime(record.time)}</time>
        <div><div className="runtime-log__record-title">{record.title}</div>{record.detail ? <div className="runtime-log__record-detail">{record.detail}</div> : null}</div>
        <Tag color={record.tone}>{record.statusLabel}</Tag>
      </li>)}</ol> : <p className="runtime-log__empty">{query.isPending ? "正在读取记录…" : query.isError ? "记录暂不可用" : "这个时段还没有运行记录。"}</p>}
    </div> : null}
  </section>;
}

export default function RuntimeLogBar({ userId, variant = "bar", defaultExpanded = false }) {
  return userId ? <RuntimeLogContent key={userId} userId={userId} variant={variant} defaultExpanded={defaultExpanded} /> : null;
}
