import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Card, Col, Divider, Modal, Progress, Row, Space, Spin, Tag, Typography } from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import * as api from "./api";
import { qk } from "./hooks/queryKeys.js";

const { Text, Paragraph } = Typography;

function directionMeta(direction) {
  if (direction === "bullish") return { color: "red", label: "偏多" };
  if (direction === "bearish") return { color: "green", label: "偏空" };
  return { color: "blue", label: "中性" };
}

function scoreStatus(score) {
  if (score >= 70) return "success";
  if (score <= 35) return "exception";
  return "normal";
}

function fmtErr(e) {
  const d = e?.response?.data?.detail;
  if (typeof d === "string") return d;
  return e?.message || "加载失败";
}

export default function TransactionAgentPanel({ symbol, displayName = "", asOf = null }) {
  const [selectedView, setSelectedView] = useState(null);
  const query = useQuery({
    queryKey: qk.transactionAgentViews(symbol, asOf),
    queryFn: () => api.getTransactionAgentViews(symbol, undefined, asOf || undefined),
    enabled: !!symbol,
    staleTime: 120_000,
  });

  const data = query.data;
  const strategies = data?.strategies || [];
  const summary = data?.summary;
  const fmtText = (text) => {
    const raw = String(text || "");
    return displayName && symbol ? raw.replaceAll(symbol, displayName) : raw;
  };
  const sorted = useMemo(
    () => [...strategies].sort((a, b) => Number(b.score || 0) - Number(a.score || 0)),
    [strategies]
  );
  const selectedDetail = selectedView?.detailed_analysis || {};

  return (
    <Card
      size="small"
      bordered={false}
      className="ex-transaction-agent-card"
      title={
        <span className="sva-panel-title">多策略观点</span>
      }
      extra={
        <Space size={8}>
          {summary ? <span className="sva-meta">均分 {summary.average_score}</span> : null}
          <Button size="small" type="text" icon={<ReloadOutlined />} loading={query.isFetching} onClick={() => query.refetch()}>
            刷新
          </Button>
        </Space>
      }
    >
      <Spin spinning={query.isFetching && !data}>
        {query.isError ? (
          <Alert type="error" showIcon message={fmtErr(query.error)} />
        ) : null}

        {summary ? (
          <div className="sva-summary-row">
            <span data-tone="up">偏多 {summary.bullish_count}</span>
            <span data-tone="flat">中性 {summary.neutral_count}</span>
            <span data-tone="down">偏空 {summary.bearish_count}</span>
          </div>
        ) : null}

        <Row gutter={[10, 10]}>
          {sorted.map((view) => {
            const meta = directionMeta(view.direction);
            return (
              <Col xs={24} md={12} xl={8} key={view.strategy_id || view.strategy}>
                <div
                  className="ex-transaction-agent-view"
                  role="button"
                  tabIndex={0}
                  onClick={() => setSelectedView(view)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      setSelectedView(view);
                    }
                  }}
                >
                  <Space wrap align="center" style={{ width: "100%", justifyContent: "space-between" }}>
                    <Space size={6}>
                      <Text strong>{view.strategy}</Text>
                      <Tag color={meta.color}>{meta.label}</Tag>
                    </Space>
                    <Text type="secondary">{view.timeframe}</Text>
                  </Space>
                  <Progress
                    percent={Number(view.score || 0)}
                    size="small"
                    status={scoreStatus(Number(view.score || 0))}
                    style={{ marginTop: 8, marginBottom: 8 }}
                  />
                  <Paragraph style={{ marginBottom: 8 }}>
                    {fmtText(view.viewpoint)}
                  </Paragraph>
                  <div className="ex-transaction-agent-list">
                    {(view.reasons || []).slice(0, 2).map((x, i) => (
                      <Text key={`r-${i}`} type="secondary">
                        {fmtText(x)}
                      </Text>
                    ))}
                  </div>
                  <Text className="ex-transaction-agent-action">{fmtText(view.action_suggestion)}</Text>
                  <Button
                    type="link"
                    size="small"
                    className="ex-transaction-agent-detail-btn"
                    onClick={(e) => {
                      e.stopPropagation();
                      setSelectedView(view);
                    }}
                  >
                    查看详细分析
                  </Button>
                </div>
              </Col>
            );
          })}
        </Row>

        {!query.isFetching && !strategies.length && !query.isError ? (
          <Text type="secondary">暂无策略观点。</Text>
        ) : null}
      </Spin>
      <Modal
        open={!!selectedView}
        title={
          selectedView ? (
            <Space wrap>
              <span>{selectedView.strategy}</span>
              <Tag color={directionMeta(selectedView.direction).color}>{directionMeta(selectedView.direction).label}</Tag>
              <Tag>评分 {selectedView.score}</Tag>
              <Tag>{selectedView.timeframe}</Tag>
            </Space>
          ) : null
        }
        footer={null}
        width={880}
        onCancel={() => setSelectedView(null)}
        className="ex-transaction-agent-modal"
      >
        {selectedView ? (
          <div className="ex-transaction-agent-detail">
            <Paragraph>{fmtText(selectedDetail.headline || selectedView.viewpoint)}</Paragraph>
            <Text type="secondary">{selectedDetail.score_interpretation}</Text>

            <Divider orientation="left">信号核查</Divider>
            <ul>
              {(selectedDetail.signal_checks || selectedView.reasons || []).map((x, i) => (
                <li key={`signal-${i}`}>{fmtText(x)}</li>
              ))}
            </ul>

            <Divider orientation="left">策略知识库强化</Divider>
            <ul>
              {(selectedDetail.knowledge_points || []).length ? (
                selectedDetail.knowledge_points.map((x, i) => <li key={`kp-${i}`}>{fmtText(x)}</li>)
              ) : (
                <li>暂无额外知识要点，当前使用本地规则化评分。</li>
              )}
            </ul>

            <Divider orientation="left">风险与不做条件</Divider>
            <ul>
              {(selectedDetail.risk_controls || selectedView.risks || []).map((x, i) => (
                <li key={`risk-${i}`}>{fmtText(x)}</li>
              ))}
              {(selectedDetail.do_not_do || selectedView.do_not_do || []).map((x, i) => (
                <li key={`no-${i}`}>不做：{fmtText(x)}</li>
              ))}
            </ul>

            <Divider orientation="left">行动计划</Divider>
            <Paragraph>{fmtText(selectedDetail.action_plan || selectedView.action_suggestion)}</Paragraph>

            <Divider orientation="left">参考来源</Divider>
            <div className="ex-transaction-agent-source-list">
              {(selectedDetail.sources || []).length ? (
                selectedDetail.sources.map((src, i) => (
                  <div key={`${src.title || "source"}-${i}`} className="ex-transaction-agent-source">
                    {src.url ? (
                      <a href={src.url} target="_blank" rel="noreferrer">
                        {src.title || src.url}
                      </a>
                    ) : (
                      <Text>{src.title || "本地规则"}</Text>
                    )}
                    {src.publisher ? <Text type="secondary"> · {src.publisher}</Text> : null}
                  </div>
                ))
              ) : (
                <Text type="secondary">暂无外部来源，使用本地策略规则。</Text>
              )}
            </div>

            <Divider orientation="left">数据限制</Divider>
            <ul>
              {(selectedDetail.data_limitations || []).map((x, i) => (
                <li key={`limit-${i}`}>{x}</li>
              ))}
            </ul>
          </div>
        ) : null}
      </Modal>
    </Card>
  );
}
