import { useState } from "react";
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
  message,
} from "antd";
import { EditOutlined, ReloadOutlined } from "@ant-design/icons";
import * as api from "./api";
import { useWatchlistStockDetail } from "./hooks/useWatchlistStockDetail.js";
import { useXueqiuCompany } from "./hooks/useXueqiuCompany.js";

const { Text, Paragraph, Link } = Typography;

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
  if (!Number.isFinite(n)) return String(v);
  const ms = n > 1e12 ? n : n * 1000;
  const d = new Date(ms);
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
  return {
    industry: pick(p.industry, x.industry, s.industry),
    org_name: pick(p.org_name, x.org_name, s.org_name),
    intro: pick(stripHtml(p.intro), x.intro, stripHtml(s.intro)),
    name: pick(p.name, x.name, s.name),
  };
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

function FeedList({ items, emptyText }) {
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
            {item.url ? (
              <Link href={item.url} target="_blank" rel="noreferrer">
                雪球原文
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
  const [editOpen, setEditOpen] = useState(false);
  const [editSymbol, setEditSymbol] = useState("");
  const [saving, setSaving] = useState(false);
  const [form] = Form.useForm();

  const sym = String(singleSymbol || "").trim().toUpperCase();
  const { data: stockDetail, loading: stockDetailLoading } = useWatchlistStockDetail(sym);
  const {
    data: xueqiuData,
    isFetching: xueqiuFetching,
    refetch: refetchXueqiu,
    isPending: xueqiuPending,
  } = useXueqiuCompany(sym);
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
    const v = await form.validateFields();
    setSaving(true);
    try {
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
      message.error(e?.message || "保存失败");
    } finally {
      setSaving(false);
    }
  };

  const refreshAll = async () => {
    await onRefreshAll?.();
    await refetchXueqiu();
  };

  if (!sym) {
    return null;
  }

  if (!symbols.includes(sym)) {
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
  const xueqiuErrors = xueqiuData?.errors || [];
  const err = p.fetch_error;
  const xueqiuLoading = xueqiuPending || xueqiuFetching;

  return (
    <div style={{ marginTop: embedded ? 0 : 20 }}>
      <Spin spinning={(loading && !refreshing) || xueqiuLoading}>
        <Space wrap style={{ marginBottom: 14 }}>
          <Text strong>{securityLabel(sym, { ...p, name: merged.name || p.name })}</Text>
          <Tag color="blue">雪球</Tag>
          {p.manual_fields?.length ? <Tag color="purple">含手动补充</Tag> : null}
          {xueqiuData?.cookies_configured ? (
            <Tag color="cyan">Cookie 已配置</Tag>
          ) : (
            <Tag color="default">匿名 warm-up</Tag>
          )}
          {err ? (
            <Tag color="orange" title={err}>
              东财 F10：{err.length > 24 ? `${err.slice(0, 24)}…` : err}
            </Tag>
          ) : null}
        </Space>

        {xueqiuErrors.length ? (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 10 }}
            message="雪球部分数据未加载"
            description={
              <>
                {xueqiuErrors.join("；")}
                {!xueqiuData?.cookies_configured ? (
                  <div style={{ marginTop: 6 }}>
                    可在管理员设置中配置雪球 Cookie，或登录 xueqiu.com 后复制 Cookie 到 integrations。
                  </div>
                ) : null}
              </>
            }
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

        <Space style={{ marginBottom: 10 }}>
          <Button size="small" icon={<EditOutlined />} onClick={openEdit}>
            手动补充 / 覆盖
          </Button>
          <Button
            size="small"
            type="primary"
            ghost
            icon={<ReloadOutlined />}
            loading={refreshing || xueqiuFetching}
            onClick={refreshAll}
          >
            刷新公司资料
          </Button>
          {(stockDetailLoading || loading) && !hasCompanyData ? (
            <Text type="secondary">公司资料加载中…</Text>
          ) : null}
          {xueqiuData?.stock_url ? (
            <Link href={xueqiuData.stock_url} target="_blank" rel="noreferrer">
              雪球个股页
            </Link>
          ) : null}
        </Space>

        <Descriptions column={1} size="small" bordered labelStyle={{ width: 108 }}>
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
          <div className="ex-xueqiu-section-body">
            <FeedList items={majorEvents} emptyText="暂无大事件（需雪球 Cookie 或有效匿名会话）" />
          </div>
        </div>

        <div className="ex-xueqiu-section">
          <Text strong>新闻</Text>
          <div className="ex-xueqiu-section-body">
            <FeedList items={xueqiuNews} emptyText="暂无个股新闻" />
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
        destroyOnClose
      >
        <Paragraph type="secondary" style={{ fontSize: 12 }}>
          保存时会提交本页全部字段：某行留空表示清除该字段的「手动值」。公司信息主源为雪球；手动值优先于雪球与东财自动拉取。
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
