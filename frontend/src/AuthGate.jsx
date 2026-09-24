import { useState } from "react";
import { Alert, Button, Card, Form, Input, Segmented, Space, Spin, Typography } from "antd";
import { useAuth } from "./AuthContext.jsx";
import { getApiErrorMessage } from "./api.js";
import ThemeModeToggle from "./ThemeModeToggle.jsx";

const { Title, Paragraph, Text } = Typography;
const USERNAME_PATTERN = /^[a-zA-Z0-9_]{3,32}$/;

function LoginForm() {
  const { login, register, registrationEnabled, error: sessionError } = useAuth();
  const [mode, setMode] = useState("login");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const isRegistration = mode === "register" && registrationEnabled;

  const submit = async (values) => {
    setSubmitting(true);
    setError("");
    try {
      await (isRegistration ? register(values) : login(values));
    } catch (e) {
      setError(getApiErrorMessage(e));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Card className="sva-auth-card" variant="borderless">
      <div className="sva-auth-heading">
        <span className="sva-brand">StockViewAgent</span>
        <ThemeModeToggle />
      </div>
      <Title level={2}>{isRegistration ? "创建试用账号" : "欢迎使用"}</Title>
      <Paragraph type="secondary">登录后管理自选股、查看行情与 AI 新闻解读。</Paragraph>
      {registrationEnabled ? (
        <Segmented
          block
          value={mode}
          disabled={submitting}
          options={[{ label: "登录", value: "login" }, { label: "注册", value: "register" }]}
          onChange={(value) => { setMode(value); setError(""); }}
          className="sva-auth-tabs"
        />
      ) : null}
      {error || sessionError ? <Alert type="error" showIcon message={error || sessionError} className="sva-auth-alert" /> : null}
      <Form key={mode} layout="vertical" onFinish={submit} disabled={submitting} requiredMark={false}>
        <Form.Item
          label="用户名"
          name="username"
          rules={[
            { required: true, message: "请输入用户名" },
            ...(isRegistration ? [{ pattern: USERNAME_PATTERN, message: "使用 3–32 位字母、数字或下划线" }] : []),
          ]}
        >
          <Input autoComplete="username" maxLength={isRegistration ? 32 : 64} placeholder="用户名" autoCapitalize="none" spellCheck={false} />
        </Form.Item>
        <Form.Item
          label="密码"
          name="password"
          rules={[
            { required: true, message: "请输入密码" },
            ...(isRegistration ? [{ min: 10, max: 128, message: "密码需要 10–128 个字符" }] : []),
          ]}
        >
          <Input.Password autoComplete={isRegistration ? "new-password" : "current-password"} maxLength={isRegistration ? 128 : 256} placeholder={isRegistration ? "至少 10 个字符" : "密码"} />
        </Form.Item>
        <Button block type="primary" htmlType="submit" loading={submitting}>
          {isRegistration ? "注册并开始试用" : "登录"}
        </Button>
      </Form>
      <Alert
        type="info"
        showIcon
        message="使用自己的 API Key 试用"
        description="登录后请在「设置」配置你的数据源和 AI 模型 API Key。密钥对应的服务用量由你自己的服务商账号承担。"
      />
      <Paragraph className="sva-auth-footnote" type="secondary">仅供研究参考，不构成投资建议。</Paragraph>
    </Card>
  );
}

/** 等待认证结束再挂载业务页面，避免未登录请求与跨账号缓存。 */
export default function AuthGate({ children }) {
  const { user, loading, error, authRequired, configReady, refreshMe } = useAuth();
  if (loading) {
    return <main className="sva-auth-page"><Spin size="large" aria-label="正在连接服务" /></main>;
  }
  if (!configReady || (!authRequired && !user)) {
    return (
      <main className="sva-auth-page">
        <Card className="sva-auth-card" variant="borderless">
          <Space direction="vertical" size="large">
            <Text strong>StockViewAgent</Text>
            <Alert type="error" showIcon message={error || "连接服务失败，请重试。"} />
            <Button type="primary" onClick={() => void refreshMe()}>重新连接</Button>
          </Space>
        </Card>
      </main>
    );
  }
  if (authRequired && !user) return <main className="sva-auth-page"><LoginForm /></main>;
  return children;
}
