import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Descriptions,
  Drawer,
  Form,
  Input,
  Radio,
  Space,
  Spin,
  Switch,
  Table,
  Tag,
  Typography,
  App as AntApp,
} from "antd";
import { ReloadOutlined, SettingOutlined } from "@ant-design/icons";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import * as api from "./api.js";
import { useAuth } from "./AuthContext.jsx";

const { Text, Paragraph, Title } = Typography;

const MARKET_OPTIONS = [
  {
    value: "klineshare",
    label: "KlineShare（推荐）",
    hint: "在 data.klineshare.cn 申请 API Key，支持 A 股 K 线/行情增强",
    doc: "https://data.klineshare.cn/docs",
  },
  {
    value: "tushare",
    label: "Tushare Pro",
    hint: "宏观/行业与 K 线兜底",
    doc: "https://tushare.pro/document/2",
  },
  {
    value: "public",
    label: "仅公开源",
    hint: "东财/腾讯/百度等，无需 Key；稳定性依赖公开接口",
    doc: null,
  },
];

export function SetupOnboardingBanner({ setupStatus }) {
  if (!setupStatus || setupStatus.ready) return null;
  return (
    <Alert
      type="warning"
      showIcon
      banner
      className="sva-setup-banner"
      message={setupStatus.onboarding_title || "请先完成配置"}
      description={<Text>{setupStatus.onboarding_message}</Text>}
      action={
        <Link to="/setup">
          <Button type="primary" size="small">打开配置台</Button>
        </Link>
      }
    />
  );
}

export default function ConfigConsole({ open, onClose, isAdmin = false }) {
  const { message } = AntApp.useApp();
  const qc = useQueryClient();
  const [loading, setLoading] = useState(false);
  const [integrations, setIntegrations] = useState(null);
  const [refreshing, setRefreshing] = useState(false);
  const [testingKlineshare, setTestingKlineshare] = useState(false);
  const [testingTushare, setTestingTushare] = useState(false);
  const [saving, setSaving] = useState(false);
  const [form] = Form.useForm();
  const centerQuery = useQuery({
    queryKey: ["settings", "data-center"],
    queryFn: api.getDataCenter,
    enabled: open && isAdmin,
    // 仅轮询后台任务状态，避免重载配置表单覆盖尚未保存的输入。
    refetchInterval: (query) => query.state.data?.refresh_running ? 2_000 : false,
  });
  const center = centerQuery.data;

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const ig = await api.getSettingsIntegrations();
      setIntegrations(ig);
      const llm = ig?.llm || {};
      const market = ig?.market_data || {};
      form.setFieldsValue({
        llm_api_key: "",
        llm_api_base: llm.api_base || "",
        llm_model: llm.model || "",
        market_data_provider: market.provider === "unset" ? undefined : market.provider,
        klineshare_api_key: "",
        tushare_token: "",
        xueqiu_cookies: "",
        adata_proxy_enabled: ig?.adata?.proxy_enabled || false,
        adata_proxy_ip: "",
        adata_proxy_url: "",
      });
    } catch (e) {
      message.error(api.getApiErrorMessage(e));
    } finally {
      setLoading(false);
    }
  }, [form, message]);

  useEffect(() => {
    if (open) void load();
  }, [open, load]);

  const refreshDataCenter = async () => {
    setRefreshing(true);
    try {
      const state = await api.refreshDataCenter();
      message.success("已触发后台数据更新");
      qc.setQueryData(["settings", "data-center"], (prev) => ({
        ...prev,
        refresh_running: true,
        last_startup_refresh: state,
      }));
      void centerQuery.refetch();
    } catch (e) {
      message.error(api.getApiErrorMessage(e));
    } finally {
      setRefreshing(false);
    }
  };

  const testKlineshare = async () => {
    setTestingKlineshare(true);
    try {
      const d = await api.testKlineshareIntegration();
      message.success(d.message || "KlineShare 连接成功");
    } catch (e) {
      message.error(api.getApiErrorMessage(e));
    } finally {
      setTestingKlineshare(false);
    }
  };

  const testTushare = async () => {
    setTestingTushare(true);
    try {
      const d = await api.testTushareIntegration();
      message.success(`Tushare 可用（${d.source}）${d.sample?.name || ""}`);
    } catch (e) {
      message.error(api.getApiErrorMessage(e));
    } finally {
      setTestingTushare(false);
    }
  };

  const save = async () => {
    setSaving(true);
    try {
      const v = await form.validateFields();
      const body = {};
      if (v.market_data_provider) body.market_data_provider = v.market_data_provider;
      if (v.klineshare_api_key?.trim()) body.klineshare_api_key = v.klineshare_api_key.trim();
      if (v.tushare_token?.trim()) body.tushare_token = v.tushare_token.trim();
      if (isAdmin) {
        body.adata_proxy_enabled = v.adata_proxy_enabled;
        if (v.adata_proxy_ip?.trim()) body.adata_proxy_ip = v.adata_proxy_ip.trim();
        if (v.adata_proxy_url?.trim()) body.adata_proxy_url = v.adata_proxy_url.trim();
        if (v.xueqiu_cookies !== undefined && v.xueqiu_cookies !== "") {
          body.xueqiu_cookies = v.xueqiu_cookies;
        }
      }
      const llmPatch = {};
      if (v.llm_api_key?.trim()) llmPatch.api_key = v.llm_api_key.trim();
      if (v.llm_api_base?.trim()) llmPatch.api_base = v.llm_api_base.trim();
      if (v.llm_model?.trim()) llmPatch.model = v.llm_model.trim();
      if (Object.keys(llmPatch).length) body.llm = llmPatch;

      const ig = await api.putSettingsIntegrations(body);
      setIntegrations(ig);
      form.setFieldsValue({
        llm_api_key: "",
        klineshare_api_key: "",
        tushare_token: "",
        xueqiu_cookies: "",
        adata_proxy_ip: "",
        adata_proxy_url: "",
      });
      message.success("配置已保存");
      await qc.invalidateQueries({ queryKey: ["settings", "setup-status"] });
      for (const queryKey of [
        ["market"],
        ["transaction-agent"],
        ["xueqiu"],
        ["watchlist", "price-context"],
        ["watchlist", "stock-detail"],
      ]) {
        void qc.invalidateQueries({ queryKey });
      }
    } catch (e) {
      if (e?.errorFields) return;
      message.error(api.getApiErrorMessage(e));
    } finally {
      setSaving(false);
    }
  };

  const llm = integrations?.llm;
  const klineshare = integrations?.klineshare;
  const tushare = integrations?.tushare;
  const market = integrations?.market_data;
  const startup = center?.last_startup_refresh;

  const marketProvider = Form.useWatch("market_data_provider", form);

  const marketDoc = useMemo(
    () => MARKET_OPTIONS.find((o) => o.value === marketProvider),
    [marketProvider]
  );

  return (
    <Drawer
      title="配置台"
      placement="right"
      width={560}
      open={open}
      onClose={onClose}
      className="sva-config-console"
      destroyOnHidden
    >
      <Spin spinning={loading}>
        <Form form={form} layout="vertical" size="small">
        <Space direction="vertical" size={16} style={{ width: "100%" }}>
          <Alert
            type="info"
            showIcon
            message="自托管首次使用"
            description="请配置大模型 API（OpenAI 兼容）与行情信息接口。推荐 KlineShare；也可选 Tushare 或仅公开源。"
          />

          <section className="sva-admin-section">
            <Title level={5} style={{ margin: 0 }}>大模型 API</Title>
            <Descriptions size="small" column={1} style={{ marginTop: 8 }} bordered>
              <Descriptions.Item label="状态">
                {llm?.key_configured ? (
                  <Tag color="green">已配置 ···{llm.key_suffix}</Tag>
                ) : (
                  <Tag color="orange">未配置</Tag>
                )}
                <Text type="secondary" style={{ fontSize: 11 }}>
                  {llm?.model} @ {llm?.api_base}
                </Text>
              </Descriptions.Item>
            </Descriptions>
            <div style={{ marginTop: 10 }}>
              <Form.Item name="llm_api_key" label="API Key（留空不修改）">
                <Input.Password placeholder="sk-..." autoComplete="off" />
              </Form.Item>
              <Form.Item name="llm_api_base" label="API Base">
                <Input placeholder="https://api.openai.com/v1" />
              </Form.Item>
              <Form.Item name="llm_model" label="Model">
                <Input placeholder="gpt-4o-mini / moonshot-v1-32k" />
              </Form.Item>
            </div>
          </section>

          <section className="sva-admin-section">
            <Title level={5} style={{ margin: 0 }}>行情信息接口</Title>
            <Paragraph type="secondary" style={{ fontSize: 12, margin: "6px 0" }}>
              选择一家提供商并填写对应密钥。K 线默认走公开源链，配置后将优先使用所选提供商。
            </Paragraph>
            <Descriptions size="small" column={1} bordered>
              <Descriptions.Item label="当前选择">
                {market?.provider && market.provider !== "unset" ? (
                  <Tag color="blue">{market.provider}</Tag>
                ) : (
                  <Tag color="orange">未选择</Tag>
                )}
                {market?.configured ? <Tag color="green">已就绪</Tag> : <Tag>待配置</Tag>}
              </Descriptions.Item>
              <Descriptions.Item label="KlineShare">
                {klineshare?.configured ? (
                  <Tag color="green">Key ···{klineshare.key_suffix}</Tag>
                ) : (
                  <Tag>未配置</Tag>
                )}
              </Descriptions.Item>
              <Descriptions.Item label="Tushare">
                {tushare?.configured ? (
                  <Tag color="green">Token ···{tushare.token_suffix}</Tag>
                ) : (
                  <Tag>未配置</Tag>
                )}
              </Descriptions.Item>
            </Descriptions>
            <div style={{ marginTop: 10 }}>
              <Form.Item name="market_data_provider" label="行情提供商">
                <Radio.Group>
                  <Space direction="vertical">
                    {MARKET_OPTIONS.map((o) => (
                      <Radio key={o.value} value={o.value}>
                        {o.label}
                        {o.doc ? (
                          <a href={o.doc} target="_blank" rel="noreferrer" style={{ marginLeft: 8, fontSize: 12 }}>
                            文档
                          </a>
                        ) : null}
                      </Radio>
                    ))}
                  </Space>
                </Radio.Group>
              </Form.Item>
              {marketDoc?.hint ? (
                <Paragraph type="secondary" style={{ fontSize: 12 }}>{marketDoc.hint}</Paragraph>
              ) : null}
              {marketProvider === "klineshare" ? (
                <Form.Item name="klineshare_api_key" label="KlineShare API Key（留空不修改）">
                  <Input.Password placeholder="从 KlineShare 用户中心复制" autoComplete="off" />
                </Form.Item>
              ) : null}
              {marketProvider === "tushare" ? (
                <Form.Item name="tushare_token" label="Tushare Token（留空不修改）">
                  <Input.Password placeholder="个人 Token" autoComplete="off" />
                </Form.Item>
              ) : null}
              <Space wrap>
                {marketProvider === "klineshare" ? (
                  <Button loading={testingKlineshare} onClick={() => void testKlineshare()}>
                    测试 KlineShare
                  </Button>
                ) : null}
                {marketProvider === "tushare" ? (
                  <Button loading={testingTushare} onClick={() => void testTushare()}>
                    测试 Tushare
                  </Button>
                ) : null}
              </Space>
            </div>
          </section>

          {isAdmin ? (
            <>
              <section className="sva-admin-section">
                {centerQuery.error ? <Alert type="warning" showIcon message={api.getApiErrorMessage(centerQuery.error)} /> : null}
                <div className="sva-admin-section__head">
                  <Text strong>数据中心</Text>
                  <Button
                    size="small"
                    icon={<ReloadOutlined />}
                    loading={refreshing || center?.refresh_running}
                    disabled={center?.refresh_running}
                    onClick={() => void refreshDataCenter()}
                  >
                    {center?.refresh_running ? "后台更新中" : "立即后台更新"}
                  </Button>
                </div>
                <Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 8 }}>
                  刷新股票列表、新闻入库、行情缓存等。
                </Paragraph>
                {startup ? (
                  <Tag color={startup.status === "ok" ? "success" : "warning"}>
                    最近任务：{startup.status || "—"}
                  </Tag>
                ) : (
                  <Text type="secondary" style={{ fontSize: 12 }}>尚未运行启动预热</Text>
                )}
                <Table
                  size="small"
                  style={{ marginTop: 10 }}
                  pagination={false}
                  rowKey="id"
                  dataSource={center?.domains || []}
                  columns={[
                    { title: "数据域", dataIndex: "name", ellipsis: true },
                    { title: "文件", width: 52, render: (_, r) => r.assets?.files ?? "—" },
                    {
                      title: "更新",
                      width: 88,
                      ellipsis: true,
                      render: (_, r) => {
                        const t = r.assets?.last_saved_at;
                        return t ? String(t).slice(0, 19).replace("T", " ") : "—";
                      },
                    },
                  ]}
                />
              </section>

              <section className="sva-admin-section">
                <Text strong>高级集成（管理员）</Text>
                <div style={{ marginTop: 8 }}>
                  <Form.Item
                    name="xueqiu_cookies"
                    label="雪球 Cookie（可选，留空不修改）"
                    extra="用于连接雪球补充数据；登录失效后需重新登录雪球并更新 Cookie。未连接时仍会自动更新可用的公开资料。"
                  >
                    <Input.TextArea rows={2} placeholder="公司信息/讨论补充" />
                  </Form.Item>
                  <Form.Item name="adata_proxy_enabled" label="行情 HTTP 代理" valuePropName="checked">
                    <Switch />
                  </Form.Item>
                  <Form.Item name="adata_proxy_ip" label="代理 host:port">
                    <Input placeholder="127.0.0.1:7890" />
                  </Form.Item>
                  <Form.Item name="adata_proxy_url" label="代理池 URL">
                    <Input />
                  </Form.Item>
                </div>
              </section>
            </>
          ) : null}

          <Button type="primary" block disabled={loading || !integrations} loading={saving} onClick={() => void save()}>
            保存全部配置
          </Button>

          <Alert
            type="info"
            showIcon
            message="本地存储"
            description={
              integrations?.storage
                ? `用户凭据与集成：${integrations.storage.integrations_file}`
                : "backend/data/"
            }
          />
        </Space>
        </Form>
      </Spin>
    </Drawer>
  );
}

/** 顶栏配置台入口（所有用户可见） */
export function ConfigConsoleButton({ isAdmin = false }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button
        type="text"
        className="sva-header-settings"
        icon={<SettingOutlined />}
        onClick={() => setOpen(true)}
        aria-label="配置台"
      >
        配置台
      </Button>
      <ConfigConsole open={open} onClose={() => setOpen(false)} isAdmin={isAdmin} />
    </>
  );
}

/** 独立配置页（深链 /setup） */
export function SetupPage() {
  const navigate = useNavigate();
  const { user } = useAuth();
  return (
    <div className="sva-setup-page">
      <Paragraph>
        <Link to="/">← 返回主界面</Link>
      </Paragraph>
      <ConfigConsole
        open
        onClose={() => navigate("/")}
        isAdmin={user?.role === "admin"}
      />
    </div>
  );
}
