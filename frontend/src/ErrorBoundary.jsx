import { Component } from "react";
import { Alert, Button, Typography } from "antd";

const { Paragraph, Text } = Typography;

export class ErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    console.error("StockViewAgent UI error:", error, info?.componentStack);
  }

  render() {
    if (this.state.error) {
      return (
        <div className="sva-error-boundary">
          <Alert
            type="error"
            showIcon
            message="页面渲染出错"
            description={
              <div>
                <Paragraph className="sva-error-boundary__desc">
                  请刷新页面重试。若持续出现，请打开浏览器开发者工具 (F12) → Console 查看报错。
                </Paragraph>
                <Text code className="sva-error-boundary__code">
                  {String(this.state.error?.message || this.state.error)}
                </Text>
              </div>
            }
            action={
              <Button type="primary" onClick={() => window.location.reload()}>
                刷新
              </Button>
            }
            style={{ maxWidth: 720 }}
          />
        </div>
      );
    }
    return this.props.children;
  }
}
