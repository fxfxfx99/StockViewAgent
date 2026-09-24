import { useState } from "react";
import { Alert, Button, Card, Empty, Pagination, Progress, Space, Spin, Tag, Typography } from "antd";
import { Link } from "react-router-dom";
import { useAuth } from "./AuthContext.jsx";
import { getApiErrorMessage } from "./api.js";
import { useXueqiuComments } from "./hooks/useXueqiuComments.js";
import {
  commentsConfigured,
  commentsEmptyText,
  commentsErrorText,
  commentsJobActive,
  commentsPrerequisiteNotice,
  commentsRange,
  commentsServerErrorText,
  commentsSessionText,
  commentsTime,
} from "./hooks/xueqiuCommentsState.js";
import XueqiuCommentCard from "./components/XueqiuCommentCard.jsx";

const { Text } = Typography;
const PAGE_SIZE = 6;

function count(value) {
  const number = Number(value);
  return value != null && Number.isFinite(number) ? Math.max(0, Math.floor(number)) : "—";
}

function requestErrorText(error) {
  return error && !error.isAxiosError && typeof error.message === "string"
    ? error.message
    : getApiErrorMessage(error);
}

function PrerequisitesNotice({ snapshot, isAdmin }) {
  const notice = commentsPrerequisiteNotice(snapshot, isAdmin);
  if (!notice) return null;
  return (
    <Alert
      type="warning"
      showIcon
      message={notice.message}
      description={notice.description}
      action={notice.showSetup ? <Link to="/setup"><Button size="small">打开配置台</Button></Link> : null}
    />
  );
}

function ScheduleNote({ schedule }) {
  if (!schedule) return <span>自动更新计划：状态暂不可用</span>;
  const time = typeof schedule.time === "string" ? schedule.time : "09:00";
  return (
    <span>
      自选股每天北京时间 {time} 自动更新昨日评论：{schedule.enabled ? "已开启" : "未开启"}
      {schedule.enabled && schedule.next_run_at ? ` · 下次 ${commentsTime(schedule.next_run_at)}` : ""}
    </span>
  );
}

function JobProgress({ data, isEnqueueing, requestedMode }) {
  const job = data?.job;
  const running = job?.status === "running" || data?.status === "running";
  const progress = Math.max(0, Number(job?.progress) || 0);
  const total = Math.max(0, Number(job?.total) || 0);
  const percent = total ? Math.min(100, Math.round(progress / total * 100)) : 0;
  const task = job || { mode: requestedMode || data?.mode, target_date: data?.target_date };
  return (
    <div className="sva-comments-progress" role="status" aria-live="polite">
      <div className="sva-comments-progress__label">
        <Spin size="small" />
        <span>{isEnqueueing ? "正在提交更新任务" : running ? "正在抓取与筛选" : "任务已排队"} · {commentsRange(task)}</span>
        {total ? <span>{count(progress)} / {count(total)}</span> : <span>等待进度</span>}
      </div>
      {total ? <Progress percent={percent} size="small" status="active" showInfo={false} /> : null}
      {data?.items?.length ? <Text type="secondary">更新期间保留上次精选结果，完成后自动替换。</Text> : null}
    </div>
  );
}

export default function XueqiuCommentsPanel({ symbol, displayName }) {
  const { user, isAdmin } = useAuth();
  const comments = useXueqiuComments(symbol, user?.id);
  const [page, setPage] = useState(1);
  const data = comments.data;
  const active = commentsJobActive(data);
  const busy = active || comments.isEnqueueing;
  const configured = commentsConfigured(data);
  const items = Array.isArray(data?.items) ? data.items.filter((item) => item && typeof item === "object").slice(0, 20) : [];
  const currentPage = Math.min(page, Math.max(1, Math.ceil(items.length / PAGE_SIZE)));
  const visibleItems = items.slice((currentPage - 1) * PAGE_SIZE, currentPage * PAGE_SIZE);
  const warnings = Array.isArray(data?.warnings)
    ? [...new Set(data.warnings.map(commentsErrorText).filter(Boolean))]
    : [];
  const serverError = commentsServerErrorText(data);
  const sessionText = commentsSessionText(data);
  const requestError = comments.refreshError || comments.error;

  const update = (mode) => {
    setPage(1);
    comments.refresh(mode);
  };

  if (!symbol) return null;

  return (
    <Card
      size="small"
      variant="borderless"
      className="ex-section-card sva-panel sva-comments-panel"
      title="雪球评论精选"
    >
      <div className="sva-comments-toolbar">
        <Text type="secondary">{displayName || symbol} · AI 筛选有研究价值的用户观点</Text>
        <Space wrap size={6}>
          <Button size="small" disabled={!configured || busy} onClick={() => update("previous_day")}>更新昨日</Button>
          <Button size="small" type="primary" ghost disabled={!configured || busy} onClick={() => update("latest")}>更新最近200条</Button>
        </Space>
      </div>

      <p className="sva-comments-disclaimer" role="note">
        内容来自雪球用户观点，由模型筛选，未经独立核实。价值分衡量研究参考价值，不是涨跌概率或投资建议。
      </p>

      <div className="sva-comments-status-stack">
        <PrerequisitesNotice snapshot={data} isAdmin={isAdmin} />
        {requestError ? (
          <Alert
            type="error"
            showIcon
            message={comments.refreshError ? "更新任务提交失败" : "评论状态读取失败"}
            description={requestErrorText(requestError)}
            action={<Button size="small" onClick={() => void comments.refetch()}>重新读取状态</Button>}
          />
        ) : null}
        {serverError ? (
          <Alert type={items.length ? "warning" : "error"} showIcon message={serverError} />
        ) : null}
        {data?.status === "partial" || data?.stale ? (
          <Alert
            type="warning"
            showIcon
            message={data.status === "partial" ? "本次仅完成部分抓取或筛选" : "当前结果为上次缓存"}
            description={items.length ? "以下已有精选仍可查看，请结合更新时间判断；可以再次更新。" : "本次尚无可展示的精选，请查看任务说明后重试。"}
          />
        ) : null}
        {warnings.length ? (
          <Alert type="warning" showIcon message="本次数据限制" description={warnings.slice(0, 4).join("；")} />
        ) : null}
        {busy ? <JobProgress data={data} isEnqueueing={comments.isEnqueueing} requestedMode={comments.requestedMode} /> : null}
      </div>

      <div className="sva-comments-stats" aria-label="评论抓取和筛选状态">
        <span>{items.length && busy ? "缓存范围" : "抓取范围"}：{commentsRange(data)}</span>
        <span>实际抓取 {count(data?.raw_count)} 条</span>
        <span>已筛选 {count(data?.analyzed_count)} 条</span>
        <span>入选 {count(data?.selected_count)} 条</span>
        <span>未入选 {count(data?.rejected_count)} 条</span>
        <span>更新于 {commentsTime(data?.updated_at || data?.fetched_at)}（北京时间）</span>
        {data?.stale && data.last_attempt_at ? <span>最近尝试 {commentsTime(data.last_attempt_at)}</span> : null}
      </div>
      <div className="sva-comments-schedule"><ScheduleNote schedule={data?.schedule} /></div>
      {sessionText ? <div className="sva-comments-schedule" role="status">{sessionText}</div> : null}

      {comments.isPending && !data ? (
        <div className="sva-comments-loading"><Spin aria-label="正在读取评论精选" /></div>
      ) : visibleItems.length ? (
        <div className="sva-comments-list">
          {visibleItems.map((item, index) => <XueqiuCommentCard key={`${item.id ?? "comment"}:${index}`} item={item} />)}
        </div>
      ) : (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={comments.error && !data ? "无法读取评论状态，请重试。" : comments.isEnqueueing ? "正在提交抓取与筛选任务，结果会自动显示。" : commentsEmptyText(data)} />
      )}

      {items.length > PAGE_SIZE ? (
        <Pagination
          size="small"
          current={currentPage}
          pageSize={PAGE_SIZE}
          total={items.length}
          showSizeChanger={false}
          onChange={setPage}
          className="sva-news-pager"
        />
      ) : null}
      {data?.status === "ready" && items.length ? <Tag className="sva-comments-result-note">最多展示 20 条精选 · 每页 6 条</Tag> : null}
    </Card>
  );
}
