import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, App as AntApp, Button, Input, InputNumber, Popconfirm, Select, Spin, Tabs, Upload } from "antd";
import { Link } from "react-router-dom";
import * as api from "./api.js";
import { macroKey, marketAnalysisEvidence, validateLearningUpload } from "./macroViewModel.js";
import { normalizeChartSymbol } from "./hooks/urlState.js";

const EMPTY = [];
const ANALYSIS_FIELDS = [["market_overview", "行情概况"], ["trend_judgment", "趋势判断"], ["support_levels", "关键支撑"], ["resistance_levels", "关键压力"], ["volume_analysis", "成交量变化"], ["indicator_analysis", "技术指标与结构"], ["risk_warnings", "风险提示"], ["action_reference", "后续观察"], ["data_basis", "分析依据"]];
function AnalysisEvidence({ result }) {
  const evidence = marketAnalysisEvidence(result);
  if (!evidence) return null;
  return <div className="macro-analysis-evidence" role="note" aria-label="本次解读的数据依据">
    <div className="macro-chart-meta"><span>行情截至 {evidence.asOf}</span><span>来源 {evidence.source}</span>{evidence.details.map((detail) => <span key={detail}>{detail}</span>)}</div>
    {evidence.stale ? <Alert className="macro-alert" type="warning" showIcon message="本次解读使用历史缓存，不能作为当前实时行情。" /> : null}
    {evidence.note ? <p className="macro-footnote">{evidence.note}</p> : null}
  </div>;
}
function StructuredResult({ result, variables, indexLabel }) {
  if (!result) return <div className="macro-analysis-placeholder">选择标的与参数，生成解读。</div>;
  return <div><p className="macro-footnote">{variables?.symbol || indexLabel} · {variables?.range} / {variables?.interval}{result.strategy_label ? ` · ${result.strategy_label}` : ""}{result.model ? ` · 模型 ${result.model}` : ""}</p><AnalysisEvidence result={result} />
    {result.analysis ? <div className="macro-analysis-grid">{ANALYSIS_FIELDS.map(([key, label]) => <section key={key}><h3>{label}</h3>{Array.isArray(result.analysis[key]) ? result.analysis[key].length ? <ul>{result.analysis[key].map((text, index) => <li key={index}>{String(text)}</li>)}</ul> : <p>依据不足</p> : <p>{result.analysis[key] || "依据不足"}</p>}</section>)}</div> : <p className="macro-answer">{result.interpretation || "未返回分析。"}</p>}
    {result.analysis?.disclaimer ? <p className="macro-footnote">{result.analysis.disclaimer}</p> : null}</div>;
}

function SharedMaterials({ userId, isAdmin }) {
  const qc = useQueryClient(); const { message } = AntApp.useApp();
  const query = useQuery({ queryKey: macroKey(userId, "materials"), queryFn: ({ signal }) => api.getKlineLearningMaterials({ signal }) });
  const upload = useMutation({ mutationFn: api.uploadKlineLearningMaterial, onSuccess: () => qc.invalidateQueries({ queryKey: macroKey(userId, "materials") }) });
  const remove = useMutation({ mutationFn: api.deleteKlineLearningMaterial, onSuccess: () => qc.invalidateQueries({ queryKey: macroKey(userId, "materials") }) });
  async function beforeUpload(file) {
    const error = validateLearningUpload(file); if (error) { message.error(error); return Upload.LIST_IGNORE; }
    try { await upload.mutateAsync(file); message.success("共享参考资料已上传"); } catch (error) { message.error(api.getApiErrorMessage(error)); }
    return false;
  }
  return <section className="macro-card"><div className="macro-card-heading"><div><h2>共享参考资料</h2><p>用于 K 线解读，由管理员维护。</p></div>{isAdmin ? <Upload accept=".txt,.md,.markdown,.pdf" beforeUpload={beforeUpload} showUploadList={false}><Button loading={upload.isPending}>上传资料</Button></Upload> : null}</div>
    <p className="macro-footnote">支持 TXT、Markdown、PDF，单文件不超过 5 MB。</p>
    {query.isError ? <Alert type="warning" showIcon message={api.getApiErrorMessage(query.error)} /> : null}
    {query.isPending ? <Spin /> : (query.data?.items || EMPTY).length ? <ul className="macro-materials">{query.data.items.map((item) => <li key={item.id}><div><strong>{item.original_name}</strong>{item.summary ? <p>{item.summary}</p> : null}</div>{isAdmin ? <Popconfirm title="删除这份共享参考资料？" description="之后的解读将不再引用这份资料。" onConfirm={async () => { try { await remove.mutateAsync(item.id); } catch (error) { message.error(api.getApiErrorMessage(error)); } }}><Button danger type="text" loading={remove.isPending}>删除</Button></Popconfirm> : null}</li>)}</ul> : <p className="macro-footnote">暂无共享资料。</p>}
  </section>;
}

export default function MacroAnalysisPanel({ userId, isAdmin, indexKey, indexLabel, range, view, initialSymbol }) {
  const [tab, setTab] = useState("index"); const [symbol, setSymbol] = useState(initialSymbol || ""); const [strategy, setStrategy] = useState("comprehensive"); const [rows, setRows] = useState(35); const [question, setQuestion] = useState(""); const [messages, setMessages] = useState([]);
  const prompt = useQuery({ queryKey: macroKey(userId, "learning-prompt"), queryFn: ({ signal }) => api.getKlineLearningPrompt({ signal }) });
  const strategies = prompt.data?.strategies || {};
  const analyze = useMutation({ mutationFn: (body) => body.symbol ? api.analyzeKlineStock(body) : api.analyzeKlineMarket(body) });
  const chat = useMutation({ mutationFn: api.chatKlineMarket, onSuccess: (data, variables) => { setMessages((previous) => [...previous, { question: variables.question, answer: data.answer, model: data.model, instrument: data.instrument, data_as_of: data.data_as_of, cache_stale: data.cache_stale, kline_source: data.kline_source }]); setQuestion(""); } });
  const normalized = normalizeChartSymbol(symbol);
  const params = { range, interval: view, candle_rows: rows || 35 };
  const canRun = tab === "index" ? Boolean(indexKey) : Boolean(normalized && strategies[strategy]);
  const switchTab = (value) => { setTab(value); analyze.reset(); };
  return <div className="macro-panel-stack"><section className="macro-card"><div className="macro-card-heading"><div><h2>K 线解读</h2><p>生成与问答会调用当前账户的模型 API。</p></div><Link to="/setup">模型设置</Link></div>
    <Tabs activeKey={tab} onChange={switchTab} items={[{ key: "index", label: "市场分析与问答" }, { key: "stock", label: "个股分析" }]} />
    <div className="macro-controls macro-analysis-controls">{tab === "stock" ? <><div className="macro-control macro-index-control"><label htmlFor="macro-stock-symbol">股票代码</label><Input id="macro-stock-symbol" value={symbol} onChange={(event) => setSymbol(event.target.value)} placeholder="例如 600519.SS" maxLength={16} /></div><div className="macro-control macro-index-control"><label htmlFor="macro-stock-strategy">解读策略</label><Select id="macro-stock-strategy" value={strategy} loading={prompt.isFetching} onChange={setStrategy} options={Object.entries(strategies).map(([key, value]) => ({ value: key, label: value.label || key }))} /></div></> : <p className="macro-selected-index">品种：<strong>{indexLabel || "未选择"}</strong></p>}<div className="macro-control"><label htmlFor="macro-candle-rows">参考 K 线数</label><InputNumber id="macro-candle-rows" value={rows} onChange={setRows} min={10} max={120} precision={0} /></div><Button type="primary" loading={analyze.isPending} disabled={!canRun || chat.isPending || !rows} onClick={() => analyze.mutate({ ...params, ...(tab === "stock" ? { symbol: normalized, strategy } : { index_key: indexKey }) })}>{tab === "stock" ? "生成个股解读" : "生成市场解读"}</Button></div>
    {tab === "stock" && symbol && !normalized ? <Alert className="macro-alert" type="warning" message="输入完整 A 股代码，如 600519.SS、000001.SZ。" /> : null}
    {tab === "stock" && prompt.isError ? <Alert className="macro-alert" type="warning" message={api.getApiErrorMessage(prompt.error)} /> : null}
    {tab === "stock" && strategies[strategy]?.prompt ? <p className="macro-footnote">{strategies[strategy].prompt}</p> : null}
    {analyze.isError ? <Alert className="macro-alert" type="warning" showIcon message={api.getApiErrorMessage(analyze.error)} /> : null}
    <StructuredResult result={analyze.data} variables={analyze.variables} indexLabel={indexLabel} />
    {tab === "index" ? <div className="macro-chat"><h3>市场问答</h3><Input.TextArea id="macro-question" aria-label="行情问题" value={question} onChange={(event) => setQuestion(event.target.value)} maxLength={2000} autoSize={{ minRows: 3, maxRows: 7 }} placeholder="输入行情问题" /><div className="macro-chat-actions"><Button onClick={() => setQuestion(`请解读${indexLabel || "当前品种"}的趋势、关键位置与风险，并列出三个后续观察信号。`)}>填入示例</Button><Button type="primary" loading={chat.isPending} disabled={!indexKey || question.trim().length < 2 || analyze.isPending} onClick={() => chat.mutate({ ...params, index_key: indexKey, question: question.trim() })}>发送问题</Button></div>{chat.isError ? <Alert className="macro-alert" type="warning" showIcon message={api.getApiErrorMessage(chat.error)} /> : null}{messages.map((item, index) => <div className="macro-conversation" key={index}><strong>{item.question}</strong><AnalysisEvidence result={item} /><p className="macro-answer">{item.answer || "未返回回答。"}</p><span className="macro-footnote">{item.model}</span></div>)}</div> : null}
  </section><SharedMaterials userId={userId} isAdmin={isAdmin} /></div>;
}
