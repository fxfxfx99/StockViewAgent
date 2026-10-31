import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Collapse,
  Drawer,
  Form,
  Input,
  Select,
  Space,
  Spin,
  Switch,
  Table,
  Tabs,
  Tag,
  Typography,
  App as AntApp,
} from "antd";
import { ReloadOutlined, SettingOutlined } from "@ant-design/icons";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import * as api from "./api.js";
import { useAuth } from "./AuthContext.jsx";
import { autopilotPresentation, autopilotQueryKey, inferLlmProvider, normalizeLlmProviders, llmPatchFromForm } from "./autopilotViewModel.js";
import LocalDataDirectory from "./LocalDataDirectory.jsx";
import SettingsRuntimePanel from "./SettingsRuntimePanel.jsx";
import AppToolbar from "./AppToolbar.jsx";
import "./ConfigConsole.css";

const { Text, Paragraph, Title } = Typography;

const MARKET_OPTIONS = [
  {
    value: "klineshare",
    label: "KlineShare",
    hint: "A 股行情与 K 线；API Key 可在 data.klineshare.cn 申请。",
    doc: "https://data.klineshare.cn/docs",
  },
  {
    value: "tushare",
    label: "Tushare Pro",
    hint: "宏观、行业数据与备用 K 线来源。",
    doc: "https://tushare.pro/document/2",
  },
  {
    value: "public",
    label: "公开行情",
    hint: "东财、腾讯、百度等公开行情。",
    doc: null,
  },
];

export function SetupOnboardingBanner({ setupStatus }) {
  if (!setupStatus || setupStatus.llm_configured || setupStatus.ready) return null;
  return (
    <Alert
      type="warning"
      showIcon
      banner
      className="sva-setup-banner"
      message="配置大模型，开启量化自动研究"
      action={
        <Link to="/setup">
          <Button type="primary" size="small">打开设置</Button>
        </Link>
      }
    />
  );
}

export default function ConfigConsole({ open, onClose, isAdmin = false, embedded = false }) {
  const { user } = useAuth();
  return <ConfigConsoleContent key={user?.id ?? "local"} open={open} onClose={onClose} isAdmin={isAdmin} userId={user?.id} embedded={embedded} />;
}

function ConfigSurface({ embedded, children, ...drawerProps }) {
  return embedded
    ? <section className="sva-config-console sva-config-embedded" aria-label="设置">{children}</section>
    : <Drawer {...drawerProps}>{children}</Drawer>;
}

function ConfigConsoleContent({ open, onClose, isAdmin, userId, embedded }) {
  const { message } = AntApp.useApp();
  const qc = useQueryClient();
  const [loading, setLoading] = useState(false);
  const [integrations, setIntegrations] = useState(null);
  const [providers, setProviders] = useState(() => normalizeLlmProviders([]));
  const [presetsUnavailable, setPresetsUnavailable] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [testingKlineshare, setTestingKlineshare] = useState(false);
  const [testingTushare, setTestingTushare] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveMode, setSaveMode] = useState(null);
  const [activation, setActivation] = useState(null);
  const [tab, setTab] = useState("config");
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [form] = Form.useForm();
  const advancedActive = open && tab === "config" && isAdmin && advancedOpen;
  const centerQuery = useQuery({
    queryKey: ["settings", "data-center", userId],
    queryFn: api.getDataCenter,
    enabled: advancedActive,
    // 仅轮询后台任务状态，避免重载配置表单覆盖尚未保存的输入。
    refetchInterval: (query) => advancedActive && query.state.data?.refresh_running ? 2_000 : false,
  });
  const center = centerQuery.data;

  const load = useCallback(async (signal) => {
    setLoading(true);
    setActivation(null);
    try {
      const [integrationsResult, presetsResult] = await Promise.allSettled([
        api.getSettingsIntegrations({ signal }), api.getLlmProviderPresets({ signal }),
      ]);
      if (signal.aborted) return;
      if (integrationsResult.status === "rejected") throw integrationsResult.reason;
      const ig = integrationsResult.value;
      const options = normalizeLlmProviders(presetsResult.status === "fulfilled" ? presetsResult.value.providers : []);
      setProviders(options);
      setPresetsUnavailable(presetsResult.status === "rejected");
      setIntegrations(ig);
      const llm = ig?.llm || {};
      const market = ig?.market_data || {};
      form.setFieldsValue({
        llm_provider: inferLlmProvider(llm, options),
        llm_api_key: "",
        llm_api_base: llm.api_base || "",
        llm_model: llm.model || "",
        market_data_provider: !market.provider || market.provider === "unset" ? "public" : market.provider,
        klineshare_api_key: "",
        tushare_token: "",
        xueqiu_cookies: "",
        adata_proxy_enabled: ig?.adata?.proxy_enabled || false,
        adata_proxy_ip: "",
        adata_proxy_url: "",
      });
    } catch (e) {
      if (!signal.aborted) message.error(api.getApiErrorMessage(e));
    } finally {
      if (!signal.aborted) setLoading(false);
    }
  }, [form, message]);

  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [open, load]);

  const refreshDataCenter = async () => {
    setRefreshing(true);
    try {
      const state = await api.refreshDataCenter();
      message.success("已提交数据更新");
      qc.setQueryData(["settings", "data-center", userId], (prev) => ({
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

  const save = async (startAutomatically = true) => {
    setSaving(true);
    setSaveMode(startAutomatically ? "activate" : "advanced");
    setActivation(null);
    let saved = false;
    try {
      const advancedFields = ["market_data_provider", "klineshare_api_key", "tushare_token", "xueqiu_cookies", "adata_proxy_enabled", "adata_proxy_ip", "adata_proxy_url"];
      await form.validateFields(startAutomatically ? undefined : advancedFields);
      const v = form.getFieldsValue(true);
      const body = {};
      if (v.market_data_provider) body.market_data_provider = v.market_data_provider;
      if (v.klineshare_api_key?.trim()) body.klineshare_api_key = v.klineshare_api_key.trim();
      if (v.tushare_token?.trim()) body.tushare_token = v.tushare_token.trim();
      if (isAdmin) {
        if (typeof v.adata_proxy_enabled === "boolean") body.adata_proxy_enabled = v.adata_proxy_enabled;
        if (v.adata_proxy_ip?.trim()) body.adata_proxy_ip = v.adata_proxy_ip.trim();
        if (v.adata_proxy_url?.trim()) body.adata_proxy_url = v.adata_proxy_url.trim();
        if (v.xueqiu_cookies !== undefined && v.xueqiu_cookies !== "") {
          body.xueqiu_cookies = v.xueqiu_cookies;
        }
      }
      if (startAutomatically) body.llm = llmPatchFromForm(v, integrations?.llm, providers);

      const ig = await api.putSettingsIntegrations(body);
      saved = true;
      setIntegrations(ig);
      form.setFieldsValue({
        llm_api_key: "",
        klineshare_api_key: "",
        tushare_token: "",
        xueqiu_cookies: "",
        adata_proxy_ip: "",
        adata_proxy_url: "",
      });
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
      if (startAutomatically) {
        await api.updateResearchConfig({ enabled: true, paused: false });
        const status = await api.reconcileAutopilot();
        qc.setQueryData(autopilotQueryKey(userId), status);
        void qc.invalidateQueries({ queryKey: ["watchlist", userId] });
        void qc.invalidateQueries({ queryKey: ["research", userId] });
        const view = autopilotPresentation(status);
        setActivation({ type: view.tone === "warning" || view.label === "需关注" ? "warning" : "success", message: `配置已保存 · ${view.label}`, description: view.description });
      } else {
        message.success("高级设置已保存");
      }
    } catch (e) {
      if (e?.errorFields) return;
      const detail = e?.response || e?.code ? api.getApiErrorMessage(e) : e.message || "请检查配置后重试。";
      if (saved && startAutomatically) setActivation({ type: "warning", message: "配置已保存，自动启动待重试", description: "再次点击「保存并开启量化自动研究」重试。" });
      else message.error(detail);
    } finally {
      setSaving(false);
      setSaveMode(null);
    }
  };

  const llm = integrations?.llm;
  const klineshare = integrations?.klineshare;
  const tushare = integrations?.tushare;
  const startup = center?.last_startup_refresh;

  const marketProvider = Form.useWatch("market_data_provider", form);
  const llmProvider = Form.useWatch("llm_provider", form);

  const changeProvider = (value) => {
    const provider = providers.find((entry) => entry.value === value);
    if (provider?.api_base) form.setFieldsValue({ llm_api_base: provider.api_base, llm_model: provider.model });
    form.setFieldValue("llm_api_key", "");
  };

  const marketDoc = useMemo(
    () => MARKET_OPTIONS.find((o) => o.value === marketProvider),
    [marketProvider]
  );

  return (
    <ConfigSurface
      embedded={embedded}
      title="设置"
      placement="right"
      width={560}
      open={open}
      onClose={onClose}
      className="sva-config-console"
      destroyOnHidden
    >
      <Tabs
        className="sva-settings-tabs"
        activeKey={tab}
        onChange={setTab}
        destroyOnHidden={false}
        items={[{
          key: "config",
          label: "连接与数据",
          forceRender: true,
          children: <Spin spinning={loading}>
        <Form form={form} layout="vertical" size="small">
        <Space direction="vertical" size={24} className="sva-settings-stack">
          <section className="sva-config-welcome">
            <Title level={3}>连接大模型</Title>
            <Paragraph type="secondary">
              用于研究分析与策略验证；模型调用受当前账户研究预算限制。
            </Paragraph>
          </section>

          <section className="sva-config-model">
            <div className="sva-settings-fields">
            <Form.Item name="llm_provider" label="大模型供应商" rules={[{ required: true, message: "请选择供应商" }]}>
              <Select options={providers.map(({ value, label }) => ({ value, label }))} onChange={changeProvider} />
            </Form.Item>
            {presetsUnavailable ? <Paragraph type="secondary">供应商列表加载失败，可手动填写接口地址和模型。</Paragraph> : null}
            <Form.Item
              name="llm_api_key"
              label="API Key"
              rules={[{ required: !llm?.key_configured, whitespace: true, message: "请填写 API Key" }]}
              extra={llm?.key_configured ? "更换服务地址时需填写新密钥。" : "密钥保存在本机。"}
            >
              <Input.Password placeholder={llm?.key_configured ? "留空沿用现有密钥" : "粘贴供应商提供的 API Key"} autoComplete="new-password" visibilityToggle={false} />
            </Form.Item>
            {llmProvider === "custom" ? (
              <>
                <Form.Item name="llm_api_base" label="API Base" rules={[{ required: true, whitespace: true, message: "请填写 API Base" }]}>
                  <Input placeholder="https://your-provider.example/v1" autoComplete="off" />
                </Form.Item>
                <Form.Item name="llm_model" label="模型名称" rules={[{ required: true, whitespace: true, message: "请填写模型名称" }]}>
                  <Input placeholder="填写供应商支持的模型 ID" autoComplete="off" />
                </Form.Item>
              </>
            ) : null}
            </div>
            <div className="sva-settings-primary-action">
            <Button type="primary" disabled={loading || !integrations || saving} loading={saving && saveMode === "activate"} onClick={() => void save(true)}>
              保存并开启量化自动研究
            </Button>
            </div>
            <Paragraph className="sva-config-note" type="secondary">
              首次运行会添加示例自选，研究结果保存在本机。
            </Paragraph>
            {activation ? <Alert showIcon {...activation} action={activation.type === "success" ? <Button size="small" onClick={() => setTab("runtime")}>查看运行状态</Button> : null} /> : null}
          </section>

          <Collapse
            className="sva-config-advanced"
            ghost
            onChange={(keys) => setAdvancedOpen(keys.includes("advanced"))}
            items={[{ key: "advanced", label: "高级设置", children: (
              <Space direction="vertical" size={24} className="sva-settings-stack">
          <section className="sva-admin-section sva-settings-group">
            <Title level={5}>行情来源</Title>
            <div className="sva-settings-source-state"><Text type="secondary">KlineShare {klineshare?.configured ? "已配置" : "可选"}</Text><Text type="secondary">Tushare {tushare?.configured ? "已配置" : "可选"}</Text></div>
            <div className="sva-settings-fields sva-settings-fields--inset">
              <Form.Item name="market_data_provider" label="行情提供商">
                <Select options={MARKET_OPTIONS.map(({ value, label }) => ({ value, label }))} />
              </Form.Item>
              {marketProvider === "klineshare" ? (
                <Form.Item name="klineshare_api_key" label="KlineShare Key" extra="留空沿用现有密钥。">
                  <Input.Password placeholder="从 KlineShare 用户中心复制" autoComplete="new-password" visibilityToggle={false} />
                </Form.Item>
              ) : null}
              {marketProvider === "tushare" ? (
                <Form.Item name="tushare_token" label="Tushare Token" extra="留空沿用现有密钥。">
                  <Input.Password placeholder="个人 Token" autoComplete="new-password" visibilityToggle={false} />
                </Form.Item>
              ) : null}
            </div>
              {marketDoc?.hint ? <Paragraph type="secondary" className="sva-settings-helper">{marketDoc.hint}{marketDoc.doc ? <> <a href={marketDoc.doc} target="_blank" rel="noreferrer">查看文档</a></> : null}</Paragraph> : null}
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
          </section>

          {isAdmin ? (
            <>
              <section className="sva-admin-section sva-settings-group"><Text strong>后台公共数据采集</Text><Paragraph className="sva-settings-helper">按计划采集，无需配置模型。</Paragraph><Link to="/data" onClick={onClose}>管理采集计划与资源限额 ↗</Link></section>
              <LocalDataDirectory userId={userId} active={advancedActive} />
              <section className="sva-admin-section sva-settings-group">
                {centerQuery.error ? <Alert type="warning" showIcon message={api.getApiErrorMessage(centerQuery.error)} /> : null}
                <div className="sva-admin-section__head">
                  <Text strong>一次性数据维护</Text>
                  <Button
                    size="small"
                    icon={<ReloadOutlined />}
                    loading={refreshing || center?.refresh_running}
                    disabled={center?.refresh_running}
                    onClick={() => void refreshDataCenter()}
                  >
                    {center?.refresh_running ? "后台更新中" : "联网更新一次"}
                  </Button>
                </div>
                <Paragraph type="secondary" className="sva-settings-helper">
                  更新股票列表、新闻和行情缓存。
                </Paragraph>
                {startup ? (
                  <Tag color={startup.status === "ok" ? "success" : "warning"}>
                    最近任务：{startup.status || "—"}
                  </Tag>
                ) : (
                  <Text type="secondary" style={{ fontSize: 12 }}>尚未更新</Text>
                )}
                <Table
                  size="small"
                  className="sva-settings-maintenance-table"
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

              <section className="sva-admin-section sva-settings-group">
                <Text strong>连接与代理</Text>
                <div className="sva-settings-fields sva-settings-fields--inset">
                  <Form.Item
                    name="xueqiu_cookies"
                    label="雪球 Cookie"
                    extra="需登录验证时可手动填写。"
                  >
                    <Input.TextArea rows={2} placeholder="留空沿用现有 Cookie" />
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

          <div className="sva-settings-secondary-action"><Button disabled={loading || !integrations || saving} loading={saving && saveMode === "advanced"} onClick={() => void save(false)}>
            保存高级设置
          </Button></div>
              </Space>
            ) }]}
          />
        </Space>
        </Form>
          </Spin>,
        }, {
          key: "runtime",
          label: "运行日志",
          children: open && tab === "runtime" ? <SettingsRuntimePanel userId={userId} onNavigate={onClose} /> : null,
        }]}
      />
    </ConfigSurface>
  );
}

/** 顶栏配置台入口（所有用户可见） */
export function ConfigConsoleButton({ isAdmin = false }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button
        type="text"
        className="sva-header-settings sva-settings-trigger"
        icon={<SettingOutlined />}
        onClick={() => setOpen(true)}
        aria-label="设置"
      >
        设置
      </Button>
      <ConfigConsole open={open} onClose={() => setOpen(false)} isAdmin={isAdmin} />
    </>
  );
}

/** 独立配置页（深链 /setup） */
export function SetupPage() {
  const { user, authRequired, logout } = useAuth();
  return (
    <div className="sva-setup-layout">
      <AppToolbar active="settings" user={user} authRequired={authRequired} onLogout={logout} />
      <main className="sva-setup-page">
      <ConfigConsole
        open
        embedded
        isAdmin={user?.role === "admin"}
      />
      </main>
    </div>
  );
}
