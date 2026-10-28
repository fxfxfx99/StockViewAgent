import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import dayjs from "dayjs";
import {
  Alert, App as AntApp, Badge, Button, Card, Checkbox, DatePicker, Descriptions, Drawer,
  Empty, Form, Input, InputNumber, Layout, Select, Space, Spin, Switch, Table, Tag, Typography,
} from "antd";
import CalendarOutlined from "@ant-design/icons/es/icons/CalendarOutlined";
import LeftOutlined from "@ant-design/icons/es/icons/LeftOutlined";
import RightOutlined from "@ant-design/icons/es/icons/RightOutlined";
import ReloadOutlined from "@ant-design/icons/es/icons/ReloadOutlined";
import SettingOutlined from "@ant-design/icons/es/icons/SettingOutlined";
import LinkOutlined from "@ant-design/icons/es/icons/LinkOutlined";
import AppToolbar from "./AppToolbar.jsx";
import { ConfigConsoleButton } from "./ConfigConsole.jsx";
import { useAuth } from "./AuthContext.jsx";
import { safeUrl } from "./components/format.js";
import * as api from "./api.js";
import { LinkEvidenceToThesis } from "./TrackingComponents.jsx";
import { calendarEvidence } from "./trackingViewModel.js";
import {
  CALENDAR_TIMEZONE, CATEGORIES, EVENT_STATUSES, IMPORTANCE_OPTIONS, REGIONS,
  calendarConfigPayload, calendarQueryKey, calendarTime, eventDateKey, eventNeedsVerification, eventStatusInfo,
  eventValue, groupCalendarEvents, monthCalendarDays, shanghaiDateKey, sourceStatusInfo,
} from "./calendarViewModel.js";
import "./EconomicCalendar.css";

const { Title, Text, Paragraph } = Typography;
const EMPTY = [];
const WEEKDAYS = ["一", "二", "三", "四", "五", "六", "日"];
const labelFor = (options, value) => options.find((option) => option.value === value)?.label || value || "—";
const importanceLabel = (importance) => ({ 1: "一般", 2: "重要", 3: "重大" }[importance] || "未评级");
const eventClock = (event, includeDate = false) => event.all_day
  ? `${includeDate ? `${eventDateKey(event)} ` : ""}时间待定`
  : calendarTime(event.scheduled_at, { includeDate });

function SourceLink({ url, children }) {
  const safe = safeUrl(url);
  return safe ? <a href={safe} target="_blank" rel="noopener noreferrer">{children} <LinkOutlined aria-hidden="true" /></a> : <span>{children}</span>;
}

function EventStatus({ event, now }) {
  const info = eventStatusInfo(event, now);
  return <Tag color={info.color}>{info.label}</Tag>;
}

function EventValues({ event }) {
  return <dl className="event-values">
    <div><dt>实际</dt><dd>{eventValue(event.actual, event.unit)}</dd></div>
    <div><dt>预期</dt><dd>{eventValue(event.forecast, event.unit)}</dd></div>
    <div><dt>前值</dt><dd>{eventValue(event.previous, event.unit)}</dd></div>
  </dl>;
}

function CalendarEventDetail({ event, userId, onClose, now }) {
  const detail = useQuery({
    queryKey: calendarQueryKey(userId, "detail", event?.id),
    queryFn: ({ signal }) => api.getCalendarEvent(event.id, { signal }),
    enabled: Boolean(event?.id),
  });
  const current = detail.data?.event || event;
  const revisions = detail.data?.revisions || EMPTY;
  return <Drawer title="事件详情与证据" open={Boolean(event)} onClose={onClose} width={620} rootClassName="calendar-overlay">
    {current ? <>
      <div className="event-detail-heading"><Space wrap><Tag>{labelFor(REGIONS, current.region)}</Tag><Tag color={current.importance === 3 ? "orange" : "default"}>{importanceLabel(current.importance)}</Tag><EventStatus event={current} now={now} /></Space>
        <Title level={3}>{current.title}</Title>
        <Text type="secondary">{eventClock(current, true)} · {current.all_day ? "来源日历日期，尚无准确发布时间" : "北京时间"}</Text>
      </div>
      <EventValues event={current} />
      <Paragraph className="calendar-caption">“—”表示来源未提供该数值。待公布事件的实际值以官方发布为准。</Paragraph>
      {current.description ? <Paragraph className="event-description">{current.description}</Paragraph> : null}
      <Descriptions bordered column={1} size="small" items={[
        { key: "category", label: "事件类别", children: labelFor(CATEGORIES, current.category) },
        { key: "currency", label: "关联货币", children: current.currency || "—" },
        { key: "source", label: "发布来源", children: <SourceLink url={current.source_url}>{current.source || "官方来源"}</SourceLink> },
        { key: "timezone", label: "来源时区", children: current.timezone || "—" },
        { key: "observed", label: "证据采集时间", children: `${calendarTime(current.observed_at, { includeDate: true })}（北京时间）` },
        { key: "reference", label: "证据引用", children: <Text code className="event-evidence-id">{`event:${current.id}${current.revision_id ? `:${current.revision_id}` : ""}`}</Text> },
      ]} />
      <LinkEvidenceToThesis key={`${userId}:${current.id}`} evidence={calendarEvidence(current)} />
      <Title level={5} className="event-revisions-heading">修订记录</Title>
      <Paragraph className="calendar-caption">Agent 按分析时点检索当时已采集的事件版本，保留证据来源与修订记录。</Paragraph>
      {detail.isError ? <Alert type="warning" showIcon message={api.getApiErrorMessage(detail.error)} action={<Button size="small" onClick={() => detail.refetch()}>重试</Button>} /> : <Table
        size="small" loading={detail.isLoading} dataSource={revisions} rowKey={(row) => row.revision_id || row.id} pagination={{ pageSize: 6, hideOnSinglePage: true }} scroll={{ x: 480 }}
        columns={[
          { title: "采集时间（北京）", key: "observed_at", render: (_, row) => calendarTime(row.observed_at, { includeDate: true }) },
          { title: "状态", key: "status", render: (_, row) => <EventStatus event={row} now={now} /> },
          { title: "实际", key: "actual", render: (_, row) => eventValue(row.actual, row.unit) },
          { title: "预期", key: "forecast", render: (_, row) => eventValue(row.forecast, row.unit) },
        ]} locale={{ emptyText: "暂无修订记录" }}
      />}
    </> : null}
  </Drawer>;
}

function AgentConfigForm({ config, onSave, pending }) {
  const [form] = Form.useForm();
  const enabled = Form.useWatch("enabled", form);
  return <Form form={form} layout="vertical" initialValues={config} onFinish={onSave} className="calendar-config-form">
    <Alert type="info" showIcon message="让宏观事件进入 Agent 的检索证据" description="保存后，所选 Agent 会按地区、类别、重要性与时间窗口检索事件及修订版本，并将证据用于解读。设置仅对当前账户生效。" />
    <Form.Item name="enabled" label="启用经济事件检索" valuePropName="checked"><Switch /></Form.Item>
    <fieldset disabled={enabled === false} className="calendar-config-fields">
      <Form.Item label="使用事件证据的 Agent" className="calendar-agent-toggles">
        <Form.Item name={["agents", "news"]} valuePropName="checked" noStyle><Checkbox disabled={enabled === false}>新闻解读</Checkbox></Form.Item>
        <Form.Item name={["agents", "research"]} valuePropName="checked" noStyle><Checkbox disabled={enabled === false}>量化研究</Checkbox></Form.Item>
        <Form.Item name={["agents", "strategies"]} valuePropName="checked" noStyle><Checkbox disabled={enabled === false}>多策略研判</Checkbox></Form.Item>
        <Form.Item name={["agents", "kline"]} valuePropName="checked" noStyle><Checkbox disabled={enabled === false}>K线与市场研判</Checkbox></Form.Item>
      </Form.Item>
      <div className="calendar-config-numbers">
        <Form.Item name="lookback_days" label="回看天数" rules={[{ required: true, message: "请填写回看天数" }]}><InputNumber min={0} max={365} precision={0} disabled={enabled === false} /></Form.Item>
        <Form.Item name="lookahead_days" label="前瞻天数" rules={[{ required: true, message: "请填写前瞻天数" }]}><InputNumber min={0} max={365} precision={0} disabled={enabled === false} /></Form.Item>
        <Form.Item name="top_k" label="每次最多检索条数" rules={[{ required: true, message: "请填写条数" }]}><InputNumber min={1} max={30} precision={0} disabled={enabled === false} /></Form.Item>
      </div>
      <Form.Item name="min_importance" label="最低重要性"><Select options={IMPORTANCE_OPTIONS} disabled={enabled === false} /></Form.Item>
      <Form.Item name="regions" label="关注地区" extra="留空表示全部地区"><Select mode="multiple" options={REGIONS} placeholder="全部地区" allowClear disabled={enabled === false} /></Form.Item>
      <Form.Item name="categories" label="事件类别" extra="留空表示全部类别"><Select mode="multiple" options={CATEGORIES} placeholder="全部类别" allowClear disabled={enabled === false} /></Form.Item>
    </fieldset>
    <Paragraph className="calendar-caption">公共事件监控由服务器定时运行。回看历史时，尚未采集的修订和未来公布值不会作为当时已知事实。</Paragraph>
    <Button type="primary" htmlType="submit" loading={pending}>保存 Agent 检索配置</Button>
  </Form>;
}

export default function EconomicCalendar() {
  const { user, authRequired, logout } = useAuth();
  const { message } = AntApp.useApp();
  const queryClient = useQueryClient();
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 60_000);
    return () => window.clearInterval(timer);
  }, []);
  const [selectedDate, setSelectedDate] = useState(() => shanghaiDateKey());
  const [month, setMonth] = useState(() => shanghaiDateKey().slice(0, 7));
  const [region, setRegion] = useState();
  const [category, setCategory] = useState();
  const [importance, setImportance] = useState(2);
  const [status, setStatus] = useState("all");
  const [search, setSearch] = useState("");
  const [selectedEvent, setSelectedEvent] = useState(null);
  const [configOpen, setConfigOpen] = useState(false);
  const days = useMemo(() => monthCalendarDays(month), [month]);
  const params = useMemo(() => ({ start: days[0], end: days[41], min_importance: importance, ...(region ? { region } : {}), ...(category ? { category } : {}), ...(search ? { q: search } : {}) }), [days, importance, region, category, search]);
  const calendar = useQuery({ queryKey: calendarQueryKey(user?.id, "calendar", params), queryFn: ({ signal }) => api.getEventCalendar(params, { signal }), refetchInterval: 60_000 });
  const config = useQuery({ queryKey: calendarQueryKey(user?.id, "config"), queryFn: ({ signal }) => api.getEventCalendarConfig({ signal }) });
  const refresh = useMutation({
    mutationFn: api.refreshEventCalendar,
    onSuccess: (result) => {
      const failed = (result.results || []).filter((source) => sourceStatusInfo(source).status === "error").length;
      if (failed) message.warning(`刷新完成，${failed} 个来源需关注，请查看采集状态。`);
      else message.success("事件来源已刷新。");
      void queryClient.invalidateQueries({ queryKey: ["events", user?.id ?? "guest"] });
    },
    onError: (error) => message.error(api.getApiErrorMessage(error)),
  });
  const save = useMutation({
    mutationFn: (values) => api.updateEventCalendarConfig(calendarConfigPayload(values)),
    onSuccess: (result) => {
      queryClient.setQueryData(calendarQueryKey(user?.id, "config"), result);
      setConfigOpen(false);
      message.success("Agent 事件检索配置已保存。");
    },
    onError: (error) => message.error(error?.response ? api.getApiErrorMessage(error) : error.message || "保存失败，请重试。"),
  });
  const items = calendar.data?.items || EMPTY;
  const sources = calendar.data?.sources || EMPTY;
  const grouped = useMemo(() => groupCalendarEvents(items, status), [items, status]);
  const monthEvents = useMemo(() => [...grouped.entries()].filter(([key]) => key.startsWith(month)).flatMap(([, events]) => events), [grouped, month]);
  const selectedEvents = grouped.get(selectedDate) || EMPTY;
  const today = shanghaiDateKey(now);
  const healthySources = sources.filter((source) => sourceStatusInfo(source).status === "success").length;
  const chooseMonth = (next) => {
    if (!next || !next.isValid()) return;
    const key = next.format("YYYY-MM");
    setMonth(key);
    setSelectedDate(key === today.slice(0, 7) ? today : `${key}-01`);
  };

  return <Layout className="sva-layout calendar-layout">
    <AppToolbar active="calendar" user={user} authRequired={authRequired} onLogout={logout} settingsAction={<ConfigConsoleButton />} />
    <Layout.Content className="calendar-content">
      <div className="calendar-heading">
        <div><Title level={1}>全球经济日历</Title><Paragraph type="secondary">关注重大经济数据与央行决策，将事件证据纳入 Agent 解读。</Paragraph>
          <Space wrap size={[12, 6]}><Text className="calendar-caption">北京时间 · {CALENDAR_TIMEZONE}</Text><Badge status={config.data?.enabled ? "processing" : "default"} text={config.data ? `Agent 检索${config.data.enabled ? "已启用" : "已关闭"}` : "正在读取检索配置"} /></Space>
        </div>
        <div className="calendar-heading-actions"><Button icon={<SettingOutlined />} onClick={() => setConfigOpen(true)}>Agent 解读与检索</Button><Button icon={<ReloadOutlined />} loading={refresh.isPending} onClick={() => refresh.mutate()}>刷新事件</Button></div>
      </div>
      {calendar.isError ? <Alert className="calendar-banner" type="error" showIcon message={api.getApiErrorMessage(calendar.error)} action={<Button size="small" onClick={() => calendar.refetch()}>重试</Button>} /> : null}
      {config.isError ? <Alert className="calendar-banner" type="warning" showIcon message="Agent 检索配置读取失败" description={api.getApiErrorMessage(config.error)} action={<Button size="small" onClick={() => config.refetch()}>重试</Button>} /> : null}
      <div className="calendar-summary-band">
        <div><span>本月匹配事件</span><strong>{calendar.isLoading ? "—" : monthEvents.length}</strong></div>
        <div><span>重大事件</span><strong>{calendar.isLoading ? "—" : monthEvents.filter((event) => event.importance === 3).length}</strong></div>
        <div><span>计划事件（含待核验）</span><strong>{calendar.isLoading ? "—" : monthEvents.filter((event) => event.status === "scheduled").length}</strong></div>
        <div><span>采集来源</span><strong>{healthySources}<small> / {sources.length} 正常</small></strong></div>
      </div>
      <div className="calendar-filters" aria-label="事件筛选">
        <label><span>地区</span><Select aria-label="地区筛选" placeholder="全部地区" options={REGIONS} value={region} onChange={setRegion} allowClear /></label>
        <label><span>重要性</span><Select aria-label="重要性筛选" options={IMPORTANCE_OPTIONS} value={importance} onChange={setImportance} /></label>
        <label><span>类别</span><Select aria-label="类别筛选" placeholder="全部类别" options={CATEGORIES} value={category} onChange={setCategory} allowClear /></label>
        <label><span>状态</span><Select aria-label="状态筛选" value={status} onChange={setStatus} options={[{ value: "all", label: "全部状态" }, ...Object.entries(EVENT_STATUSES).map(([value, item]) => ({ value, label: value === "scheduled" ? "计划事件（含待核验）" : item.label }))]} /></label>
        <label className="calendar-search"><span>搜索事件</span><Input.Search placeholder="事件名称或关键词" aria-label="搜索经济事件" allowClear onSearch={(value) => setSearch(value.trim())} enterButton /></label>
      </div>
      <div className="calendar-main-grid">
        <Card className="calendar-card calendar-month" title={<Space><CalendarOutlined /><span>{month.replace("-", " 年 ")} 月</span></Space>} extra={<Space size={4}>
          <Button type="text" icon={<LeftOutlined />} aria-label="上个月" onClick={() => chooseMonth(dayjs(`${month}-01`).subtract(1, "month"))} />
          <DatePicker picker="month" value={dayjs(`${month}-01`)} onChange={chooseMonth} allowClear={false} aria-label="选择月份" format="YYYY-MM" inputReadOnly />
          <Button type="text" icon={<RightOutlined />} aria-label="下个月" onClick={() => chooseMonth(dayjs(`${month}-01`).add(1, "month"))} />
          <Button size="small" onClick={() => chooseMonth(dayjs(today))}>今天</Button>
        </Space>}>
          <Spin spinning={calendar.isLoading}>
            <div className="calendar-weekdays" aria-hidden="true">{WEEKDAYS.map((day) => <span key={day}>{day}</span>)}</div>
            <div className="calendar-day-grid" aria-label={`${month} 事件月历`}>
              {days.map((date) => {
                const events = grouped.get(date) || EMPTY;
                const awaiting = events.filter((event) => eventNeedsVerification(event, now)).length;
                return <button type="button" key={date} onClick={() => setSelectedDate(date)} aria-pressed={date === selectedDate} aria-label={`${date}${date === today ? " 今天" : ""}，${events.length} 个事件${awaiting ? `，${awaiting} 项日程已过·结果待核验` : ""}`} className={`calendar-day${date === selectedDate ? " is-selected" : ""}${!date.startsWith(month) ? " is-outside" : ""}${date === today ? " is-today" : ""}`}>
                  <span className="calendar-day-number">{Number(date.slice(-2))}</span>
                  <span className="calendar-day-count">{events.length ? `${events.length} 项` : ""}</span>
                  {awaiting ? <span className="calendar-day-verification" title="日程已过·结果待核验">{awaiting} 待核验</span> : null}
                  <span className="calendar-day-events">{events.slice(0, 2).map((event) => {
                    const info = eventStatusInfo(event, now);
                    return <span key={event.id} className={`calendar-mini-event importance-${event.importance}${event.status === "cancelled" ? " is-cancelled" : ""}${eventNeedsVerification(event, now) ? " is-unverified" : ""}`} title={`${event.title} · ${info.label}`}>{event.title} · {info.label}</span>;
                  })}{events.length > 2 ? <span className="calendar-more">另 {events.length - 2} 项</span> : null}</span>
                </button>;
              })}
            </div>
          </Spin>
          <div className="calendar-month-note"><span><i />重大事件</span><span>点击日期查看当日议程</span></div>
        </Card>
        <Card className="calendar-card calendar-agenda" title={`${selectedDate.replaceAll("-", "/")} 议程`} extra={<Text type="secondary">{selectedEvents.length} 项</Text>}>
          {calendar.isLoading ? <div className="calendar-loading"><Spin /></div> : selectedEvents.length ? <div className="calendar-agenda-list">{selectedEvents.map((event) => <article className="calendar-event" key={event.id}>
            <div className="calendar-event-meta"><span>{eventClock(event)}</span><EventStatus event={event} now={now} /></div>
            <button type="button" className="calendar-event-title" onClick={() => setSelectedEvent(event)}>{event.title}</button>
            <Space wrap size={[4, 4]}><Tag>{labelFor(REGIONS, event.region)}</Tag><Tag>{labelFor(CATEGORIES, event.category)}</Tag><Tag color={event.importance === 3 ? "orange" : "default"}>{importanceLabel(event.importance)}</Tag></Space>
            <EventValues event={event} />
            <div className="calendar-event-source"><span>{event.source || "来源待确认"}</span><Button type="link" size="small" onClick={() => setSelectedEvent(event)}>查看证据</Button></div>
          </article>)}</div> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={<><span>当日暂无匹配事件</span><p className="calendar-caption">可选择其他日期或调整筛选条件；首次使用请刷新事件来源。</p></>} />}
        </Card>
      </div>
      <Card className="calendar-card calendar-sources" title="监控来源与采集状态" extra={<Text className="calendar-caption">最近更新：{calendarTime(calendar.data?.updated_at, { includeDate: true })}</Text>}>
        <Paragraph className="calendar-caption">由服务器定时监控。各来源覆盖范围不同；未提供的预期、前值或实际值均显示“—”。来源异常时请核对最后成功采集时间。</Paragraph>
        {sources.length ? <div className="calendar-source-grid">{sources.map((source) => { const info = sourceStatusInfo(source); return <div className="calendar-source" key={source.id}>
          <div className="calendar-source-heading"><SourceLink url={source.url}>{source.name || source.id}</SourceLink><Badge status={info.status} text={info.label} /></div>
          <div className="calendar-caption">{source.event_count ?? 0} 项事件 · 最近成功 {calendarTime(source.last_success_at, { includeDate: true })}</div>
          {source.error ? <Text type="warning" className="calendar-source-error">{source.error}</Text> : null}
        </div>; })}</div> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={calendar.isLoading ? "正在读取来源状态" : "尚无采集记录，点击“刷新事件”开始采集"} />}
      </Card>
    </Layout.Content>
    <CalendarEventDetail event={selectedEvent} now={now} userId={user?.id} onClose={() => setSelectedEvent(null)} />
    <Drawer title="Agent 解读与事件检索" open={configOpen} onClose={() => setConfigOpen(false)} width={560} rootClassName="calendar-overlay" destroyOnClose>
      {config.isLoading ? <Spin /> : config.isError ? <Alert type="error" showIcon message={api.getApiErrorMessage(config.error)} action={<Button onClick={() => config.refetch()}>重试</Button>} /> : config.data ? <AgentConfigForm config={config.data} onSave={(values) => save.mutate(values)} pending={save.isPending} /> : null}
    </Drawer>
  </Layout>;
}
