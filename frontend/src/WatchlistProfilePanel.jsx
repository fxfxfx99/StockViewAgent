import { useState } from "react";
import { Link as RouterLink } from "react-router-dom";
import {
  Alert,
  Button,
  Descriptions,
  Form,
  Input,
  List,
  Modal,
  Space,
  Spin,
  Tag,
  Typography,
  App as AntApp,
} from "antd";
import { EditOutlined, ReloadOutlined } from "@ant-design/icons";
import * as api from "./api";
import { useWatchlistStockDetail } from "./hooks/useWatchlistStockDetail.js";
import { useXueqiuCompany } from "./hooks/useXueqiuCompany.js";
import { safeUrl } from "./components/format.js";

const { Text, Paragraph, Link } = Typography;
const AUTH_LABELS = {
  missing: "雪球未连接",
  expired: "雪球登录已失效",
  connected: "雪球已连接",
  anonymous: "雪球匿名连接",
  unavailable: "雪球暂不可用",
};

function stripHtml(s) {
  if (!s) return "";
  return String(s).replace(/<[^>]+>/g, "").trim();
}

function securityLabel(symbol, profile) {
  const sym = String(symbol || "").trim().toUpperCase();
  const name = String(profile?.name || "").trim();
  return name && name !== sym ? `${name}（${sym}）` : sym;
}

function formatXueqiuTime(v) {
  if (v == null || v === "") return "";
  const n = Number(v);
  const d = new Date(Number.isFinite(n) ? (n > 1e12 ? n : n * 1000) : v);
  if (Number.isNaN(d.getTime())) return String(v);
  const pad = (x) => String(x).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function mergeCompanyProfile(p, stockDetailProfile, xueqiuCompany) {
  const s = stockDetailProfile || {};
  const x = xueqiuCompany || {};
  const pick = (...vals) => {
    for (const v of vals) {
      const t = String(v ?? "").trim();
      if (t) return t;
    }
    return "";
  };
  return Object.fromEntries(["industry", "org_name", "intro", "name"].map((field) => {
    const values = p.manual_fields?.includes(field)
      ? [p[field], x[field], s[field]]
      : [x[field], p[field], s[field]];
    return [field, pick(...(field === "intro" ? values.map(stripHtml) : values))];
  }));
}

function CompanyTextBox({ multiline, children }) {
  return (
    <div
      className={
        multiline ? "ex-company-text-box ex-company-text-box--multi" : "ex-company-text-box ex-company-text-box--short"
      }
    >
      {children}
    </div>
  );
}

function SectionFreshness({ data, field }) {
  if (!data) return null;
  const fetchedAt = data.section_fetched_at?.[field];
  return (
    <Text type="secondary" style={{ marginLeft: 8, fontSize: 12 }}>
      资料采集：{fetchedAt ? formatXueqiuTime(fetchedAt) : "时间未知"}
      {data.stale_fields?.includes(field) ? " · 缓存" : ""}
    </Text>
  );
}

function FeedList({ items, emptyText, sourceLabel }) {
  if (!items?.length) {
    return <Text type="secondary">{emptyText}</Text>;
  }
  return (
    <List
      className="ex-xueqiu-feed-list"
      size="small"
      dataSource={items}
      renderItem={(item) => (
        <List.Item>
          <div className="ex-xueqiu-feed-item">
            <div className="ex-xueqiu-feed-meta">
              {item.created_at ? <Text type="secondary">{formatXueqiuTime(item.created_at)}</Text> : null}
              {item.event_type ? <Tag>{item.event_type}</Tag> : null}
              {item.source_label || sourceLabel ? <Text type="secondary">{item.source_label || sourceLabel}</Text> : null}
            </div>
            <Text strong className="ex-xueqiu-feed-title">{item.title || "—"}</Text>
            {item.message ? (
              <Paragraph className="ex-xueqiu-feed-body" ellipsis={{ rows: 3, expandable: true, symbol: "展开" }}>
                {item.message}
              </Paragraph>
            ) : null}
            {item.text_excerpt && item.text_excerpt !== item.title ? (
              <Paragraph className="ex-xueqiu-feed-body" ellipsis={{ rows: 3, expandable: true, symbol: "展开" }}>
                {item.text_excerpt}
              </Paragraph>
            ) : null}
            {safeUrl(item.url) ? (
              <Link href={safeUrl(item.url)} target="_blank" rel="noreferrer">
                查看原文
              </Link>
            ) : null}
          </div>
        </List.Item>
      )}
    />
  );
}

export default function WatchlistProfilePanel({
  symbols,
  profiles,
  loading,
  refreshing,
  onRefreshAll,
  onProfilesUpdate,
  singleSymbol = null,
  embedded = false,
}) {
  const { message } = AntApp.useApp();
  const [editOpen, setEditOpen] = useState(false);
  const [editSymbol, setEditSymbol] = useState("");
  const [saving, setSaving] = useState(false);
  const [form] = Form.useForm();

  const sym = String(singleSymbol || "").trim().toUpperCase();
  const isListed = symbols.includes(sym);
  const { data: stockDetail, loading: stockDetailLoading, error: stockDetailError } = useWatchlistStockDetail(isListed ? sym : "");
  const {
    data: xueqiuData,
    isFetching: xueqiuFetching,
    error: xueqiuError,
    isPending: xueqiuPending,
  } = useXueqiuCompany(isListed ? sym : "");
  const p = sym ? profiles[sym] || { symbol: sym } : null;

  const openEdit = () => {
    if (!sym || !p) return;
    setEditSymbol(sym);
    form.setFieldsValue({
      name: p.name || "",
      org_name: p.org_name || "",
      industry: p.industry || "",
      intro: p.intro || "",
    });
    setEditOpen(true);
  };

  const submitManual = async () => {
    setSaving(true);
    try {
      const v = await form.validateFields();
      const payload = { symbol: editSymbol };
      for (const k of ["name", "org_name", "industry", "intro"]) {
        const t = (v[k] || "").trim();
        payload[k] = t || null;
      }
      const d = await api.patchWatchlistProfileManual(payload);
      if (!d.ok) {
        message.error(d.detail || "保存失败");
        return;
      }
      onProfilesUpdate?.((prev) => ({
        ...prev,
        [editSymbol]: d.profile,
      }));
      setEditOpen(false);
      message.success("已保存手动补充");
    } catch (e) {
      if (e?.errorFields) return;
      message.error(api.getApiErrorMessage(e));
    } finally {
      setSaving(false);
    }
  };

  if (!sym) {
    return null;
  }

  if (!isListed) {
    return (
      <Alert
        type="info"
        showIcon
        message={`当前标的 ${sym} 不在股票列表中，请先添加后再查看公司信息。`}
      />
    );
  }

  const xqCompany = xueqiuData?.company || null;
  const merged = mergeCompanyProfile(p, stockDetail?.profile, xqCompany);
  const majorEvents = xueqiuData?.major_events || [];
  const xueqiuNews = xueqiuData?.news || [];
  const hasCompanyData = Boolean(merged.industry || merged.org_name || merged.intro);
  const companyErrors = (stockDetail?.partial_errors || []).filter((msg) =>
    String(msg).startsWith("公司资料")
  );
  const sectionErrors = [...new Set(Object.values(xueqiuData?.section_errors || {}).filter(Boolean))];
  const sources = xueqiuData?.sources || {};
  const authStatus = xueqiuData?.auth_status;
  const needsConnection = authStatus === "missing" || authStatus === "expired";
  const err = p.fetch_error;
  const xueqiuLoading = xueqiuPending && !hasCompanyData;

  return (
    <div style={{ marginTop: embedded ? 0 : 20 }}>
      <Spin spinning={(loading && !refreshing && !hasCompanyData) || xueqiuLoading}>
        {stockDetailError || xueqiuError ? (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 10 }}
            message={api.getApiErrorMessage(stockDetailError || xueqiuError)}
          />
        ) : null}
        <Space wrap style={{ marginBottom: 14 }}>
          <Text strong>{securityLabel(sym, { ...p, name: merged.name || p.name })}</Text>
          {sources.company ? <Tag color="blue">{sources.company}</Tag> : null}
          {p.manual_fields?.length ? <Tag color="purple">含手动补充</Tag> : null}
          {AUTH_LABELS[authStatus] ? (
            <Tag color={authStatus === "connected" ? "cyan" : authStatus === "expired" ? "orange" : "default"}>
              {AUTH_LABELS[authStatus]}
            </Tag>
          ) : null}
          {needsConnection ? <RouterLink to="/setup">{authStatus === "expired" ? "更新雪球连接" : "连接雪球（可选）"}</RouterLink> : null}
          {err ? (
            <Tag color="orange" title={err}>
              东财 F10：{err.length > 24 ? `${err.slice(0, 24)}…` : err}
            </Tag>
          ) : null}
        </Space>

        {xueqiuData ? (
          <Paragraph type="secondary" style={{ marginBottom: 10, fontSize: 12 }}>
            {xueqiuData.auto_refresh_enabled
              ? `每 ${Math.round((xueqiuData.refresh_interval_sec || 900) / 60)} 分钟自动更新`
              : "自动更新已关闭"}
            {xueqiuData.fetched_at ? ` · 最近更新：${formatXueqiuTime(xueqiuData.fetched_at)}` : " · 等待首次更新"}
            {xueqiuData.status === "stale" ? " · 当前保留上次成功数据" : ""}
            {xueqiuFetching ? " · 正在检查更新…" : ""}
          </Paragraph>
        ) : null}

        {needsConnection && xueqiuData?.auth_message ? (
          <Paragraph type="secondary" style={{ marginBottom: 10, fontSize: 12 }}>
            {xueqiuData.auth_message}
          </Paragraph>
        ) : null}

        {sectionErrors.length ? (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 10 }}
            message={xueqiuData?.status === "stale" ? "部分资料更新暂时失败，已保留上次成功数据" : "部分公司信息暂不可用"}
            description={sectionErrors.join("；")}
          />
        ) : null}

        {companyErrors.length ? (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 10 }}
            message="东财公司资料部分未加载"
            description={companyErrors.join("；")}
          />
        ) : null}

        <Space wrap style={{ marginBottom: 10 }}>
          <Button size="small" icon={<EditOutlined />} onClick={openEdit}>
            手动补充 / 覆盖
          </Button>
          <Button
            size="small"
            type="primary"
            ghost
            icon={<ReloadOutlined />}
            loading={refreshing || xueqiuFetching}
            onClick={onRefreshAll}
          >
            刷新公司资料
          </Button>
          {(stockDetailLoading || loading) && !hasCompanyData ? (
            <Text type="secondary">公司资料加载中…</Text>
          ) : null}
          {safeUrl(xueqiuData?.stock_url) ? (
            <Link href={safeUrl(xueqiuData.stock_url)} target="_blank" rel="noreferrer">
              雪球个股页
            </Link>
          ) : null}
        </Space>

        <div style={{ marginBottom: 6 }}>
          <Text strong>公司资料</Text>
          <SectionFreshness data={xueqiuData} field="company" />
        </div>
        <Descriptions column={1} size="small" bordered styles={{ label: { width: 108 } }}>
          <Descriptions.Item label="行业">
            <CompanyTextBox multiline={false}>
              <Text style={{ whiteSpace: "pre-wrap" }}>{merged.industry || "—"}</Text>
            </CompanyTextBox>
          </Descriptions.Item>
          <Descriptions.Item label="企业全称">
            <CompanyTextBox multiline={false}>
              <Text style={{ whiteSpace: "pre-wrap" }}>{merged.org_name || "—"}</Text>
            </CompanyTextBox>
          </Descriptions.Item>
          <Descriptions.Item label="简介">
            <CompanyTextBox multiline>
              <Paragraph
                style={{ margin: 0, whiteSpace: "pre-wrap" }}
                ellipsis={{ rows: 4, expandable: true, symbol: "展开" }}
              >
                {merged.intro || "—"}
              </Paragraph>
            </CompanyTextBox>
          </Descriptions.Item>
        </Descriptions>

        <div className="ex-xueqiu-section">
          <Text strong>最近大事</Text>
          <SectionFreshness data={xueqiuData} field="major_events" />
          <div className="ex-xueqiu-section-body">
            <FeedList items={majorEvents} emptyText="暂无最近大事" sourceLabel={sources.major_events} />
          </div>
        </div>

        <div className="ex-xueqiu-section">
          <Text strong>新闻</Text>
          <SectionFreshness data={xueqiuData} field="news" />
          <div className="ex-xueqiu-section-body">
            <FeedList items={xueqiuNews} emptyText="暂无个股新闻" sourceLabel={sources.news} />
          </div>
        </div>
      </Spin>

      <Modal
        title={`手动补充 — ${editSymbol}`}
        open={editOpen}
        onCancel={() => setEditOpen(false)}
        onOk={submitManual}
        confirmLoading={saving}
        width={640}
        destroyOnHidden
      >
        <Paragraph type="secondary" style={{ fontSize: 12 }}>
          保存时会提交本页全部字段：某行留空表示清除该字段的「手动值」。手动值优先于自动更新的公司资料。
        </Paragraph>
        <Form form={form} layout="vertical">
          <Form.Item name="name" label="证券简称">
            <Input placeholder="可选" />
          </Form.Item>
          <Form.Item name="org_name" label="企业全称">
            <Input placeholder="可选" />
          </Form.Item>
          <Form.Item name="industry" label="行业">
            <Input placeholder="可选" />
          </Form.Item>
          <Form.Item name="intro" label="简介">
            <Input.TextArea rows={5} placeholder="可选" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
