import { useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Card, Col, Divider, Modal, Progress, Row, Space, Spin, Tag, Typography } from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import * as api from "./api";
import { qk } from "./hooks/queryKeys.js";
import { safeUrl } from "./components/format.js";
import { useAuth } from "./AuthContext.jsx";
import { ThesisComposer } from "./TrackingComponents.jsx";
import { strategyEvidence } from "./trackingViewModel.js";
import { strategyDate, strategyRetrievalSummary, strategyWorkspacePath } from "./strategyWorkspaceViewModel.js";

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

export default function TransactionAgentPanel({ symbol, displayName = "", asOf = null, headerControls }) {
  const { user } = useAuth();
  const [selectedView, setSelectedView] = useState(null);
  const [savingView, setSavingView] = useState(null);
  const pendingSaveView = useRef(null);
  const query = useQuery({
    queryKey: qk.transactionAgentViews(symbol, asOf, user?.id),
    queryFn: () => api.getTransactionAgentViews(symbol, undefined, asOf || undefined),
    enabled: !!symbol,
    staleTime: 120_000,
  });

  const data = query.data;
  const strategies = data?.strategies || [];
  const fmtText = (text) => {
    const raw = String(text || "");
    return displayName && symbol ? raw.replaceAll(symbol, displayName) : raw;
  };
  const selectedDetail = selectedView?.detailed_analysis || {};
  const selectedRetrieval = strategyRetrievalSummary(selectedView?.metadata?.knowledge_retrieval);

  return (
    <Card
      size="small"
      variant="borderless"
      className="ex-transaction-agent-card"
      title={
        <span className="sva-panel-title">多策略观点</span>
      }
      extra={
        <Space size={12} wrap>
          {headerControls}
          <Link to="/strategies">策略与知识库</Link>
          <Button size="small" type="text" icon={<ReloadOutlined />} loading={query.isFetching} onClick={() => query.refetch()}>
            刷新
          </Button>
        </Space>
      }
    >
      <Spin spinning={query.isFetching && !data}>
        {query.isError ? (
          <Alert type="error" showIcon message={api.getApiErrorMessage(query.error)} />
        ) : null}

        <Row gutter={[12, 12]}>
          {strategies.map((view) => {
            const meta = directionMeta(view.direction);
            return (
              <Col xs={24} lg={12} xxl={8} key={view.strategy_id || view.strategy}>
                <div
                  className="ex-transaction-agent-view"
                  role="button"
                  aria-label={`${view.strategy}：查看分析`}
                  tabIndex={0}
                  onClick={() => setSelectedView(view)}
                  onKeyDown={(e) => {
                    if (e.target !== e.currentTarget) return;
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      setSelectedView(view);
                    }
                  }}
                >
                  <div className="ex-transaction-agent-card-heading">
                    <Text strong>{view.strategy}</Text>
                    <Tag color={meta.color}>{meta.label}</Tag>
                  </div>
                  <div className="ex-transaction-agent-meta"><span>{view.timeframe}</span><span>信号强度 {Number(view.score || 0)} / 100</span></div>
                  <Progress
                    percent={Number(view.score || 0)}
                    size="small"
                    showInfo={false}
                    status={scoreStatus(Number(view.score || 0))}
                    style={{ marginTop: 5, marginBottom: 10 }}
                  />
                  <Paragraph className="ex-transaction-agent-viewpoint">
                    {fmtText(view.viewpoint)}
                  </Paragraph>
                  <div className="ex-transaction-agent-card-footer">
                    <Button
                      type="link"
                      size="small"
                      className="ex-transaction-agent-detail-btn"
                      onClick={(e) => {
                        e.stopPropagation();
                        setSelectedView(view);
                      }}
                    >
                      查看分析
                    </Button>
                    {view.strategy_id ? <Link to={strategyWorkspacePath(view.strategy_id)} onClick={(event) => event.stopPropagation()}>介绍与知识库</Link> : null}
                  </div>
                </div>
              </Col>
            );
          })}
        </Row>

        {!query.isFetching && !strategies.length && !query.isError ? (
          <Text type="secondary">暂无策略观点。</Text>
        ) : null}
      </Spin>
      {savingView ? <ThesisComposer key={`${user?.id}:${savingView.view.strategy_id}`} symbol={symbol}
        initial={{ reason: savingView.view.viewpoint || "", watch_conditions: (savingView.view.reasons || []).join("\n"), invalidation_conditions: (savingView.view.do_not_do?.length ? savingView.view.do_not_do : savingView.view.risks || []).join("\n") }}
        evidenceRefs={[savingView.evidence]} onClose={() => setSavingView(null)} /> : null}
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
        footer={<Space wrap>{selectedView?.strategy_id ? <Link to={strategyWorkspacePath(selectedView.strategy_id)}>介绍与知识库</Link> : null}<Button type="primary" onClick={() => { pendingSaveView.current = { view: selectedView, evidence: strategyEvidence(selectedView, data) }; setSelectedView(null); }}>保存为关注判断</Button></Space>}
        width={880}
        onCancel={() => { pendingSaveView.current = null; setSelectedView(null); }}
        afterClose={() => { const view = pendingSaveView.current; pendingSaveView.current = null; if (view) setSavingView(view); }}
        className="ex-transaction-agent-modal"
      >
        {selectedView ? (
          <div className="ex-transaction-agent-detail">
            <Paragraph>{fmtText(selectedDetail.headline || selectedView.viewpoint)}</Paragraph>
            <Text type="secondary">{selectedDetail.score_interpretation}</Text>

            {selectedRetrieval ? <details className="ex-transaction-agent-retrieval"><summary>{selectedView.metadata?.knowledge_retrieval?.enabled === false ? "本次未启用知识检索" : `本次引用 ${selectedRetrieval.retrieved} 份策略资料`} · 截止 {strategyDate(selectedRetrieval.asOf)}（北京）</summary><div>知识版本 <Text code>{selectedRetrieval.version}</Text>{selectedView.metadata?.rag_config_version !== undefined ? ` · 配置版本 ${selectedView.metadata.rag_config_version}` : ""}</div>{selectedRetrieval.stale || selectedRetrieval.afterCutoff ? <div>超出时效 {selectedRetrieval.stale} 份 · 截止日期后不可用 {selectedRetrieval.afterCutoff} 份</div> : null}<div>新资料与配置在下一次刷新分析时生效；历史分析保留当时的资料与配置。</div></details> : null}

            {(selectedDetail.signal_checks || selectedView.reasons || []).length ? <><Divider orientation="left">信号核查</Divider>
            <ul>
              {(selectedDetail.signal_checks || selectedView.reasons || []).map((x, i) => (
                <li key={`signal-${i}`}>{fmtText(x)}</li>
              ))}
            </ul></> : null}

            {selectedDetail.knowledge_points?.length ? <><Divider orientation="left">策略依据</Divider>
            <ul>
              {selectedDetail.knowledge_points.map((x, i) => <li key={`kp-${i}`}>{fmtText(x)}</li>)}
            </ul></> : null}

            {(selectedDetail.risk_controls || selectedView.risks || []).length || (selectedDetail.do_not_do || selectedView.do_not_do || []).length ? <><Divider orientation="left">风险与约束</Divider>
            <ul>
              {(selectedDetail.risk_controls || selectedView.risks || []).map((x, i) => (
                <li key={`risk-${i}`}>{fmtText(x)}</li>
              ))}
              {(selectedDetail.do_not_do || selectedView.do_not_do || []).map((x, i) => (
                <li key={`no-${i}`}>不做：{fmtText(x)}</li>
              ))}
            </ul></> : null}

            {selectedDetail.action_plan || selectedView.action_suggestion ? <><Divider orientation="left">行动计划</Divider>
            <Paragraph>{fmtText(selectedDetail.action_plan || selectedView.action_suggestion)}</Paragraph></> : null}

            {selectedDetail.sources?.length ? <><Divider orientation="left">参考来源</Divider>
            <div className="ex-transaction-agent-source-list">
              {selectedDetail.sources.map((src, i) => (
                  <div key={`${src.title || "source"}-${i}`} className="ex-transaction-agent-source">
                    {safeUrl(src.url) ? (
                      <a href={safeUrl(src.url)} target="_blank" rel="noreferrer">
                        {src.title || src.url}
                      </a>
                    ) : (
                      <Text>{src.title || "本地规则"}</Text>
                    )}
                    {src.publisher ? <Text type="secondary"> · {src.publisher}</Text> : null}
                    {src.version ? <Text type="secondary"> · 版本 {src.version}</Text> : null}
                    {src.updated_at ? <Text type="secondary"> · 更新 {strategyDate(src.updated_at)}（北京）</Text> : null}
                  </div>
                ))}
            </div></> : null}

            {selectedDetail.data_limitations?.length ? <details>
            <summary>数据说明</summary>
            <ul>
              {selectedDetail.data_limitations.map((x, i) => (
                <li key={`limit-${i}`}>{x}</li>
              ))}
            </ul>
            </details> : null}
          </div>
        ) : null}
      </Modal>
    </Card>
  );
}
