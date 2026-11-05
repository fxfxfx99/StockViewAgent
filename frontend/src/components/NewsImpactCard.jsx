import { useState } from "react";
import { Alert, Button, Card, Collapse, Space, Tag, Tooltip, Typography } from "antd";
import { ThesisComposer } from "../TrackingComponents.jsx";
import { newsEvidence } from "../trackingViewModel.js";
import {
  directionLabels,
  formatTime,
  isAnalyzed,
  number,
  plainText,
  safeUrl,
  sentimentColors,
  sentimentLabels,
} from "./format.js";
const { Paragraph, Text } = Typography;
const types = {
  direct: "直接相关",
  industry: "行业",
  supply_chain: "上下游",
  competitor: "竞争对手",
  macro: "宏观",
};
const matchLabels = {
  company_keyword: "公司关键词",
  configured_keyword: "自定义关键词",
  provider: "平台关联",
  relevance: "相关度达标",
};
const horizons = {
  intraday: "日内",
  short_term: "短期",
  medium_term: "中期",
  long_term: "长期",
};
const eventTypes = {
  earnings: "业绩",
  order: "订单",
  policy: "政策",
  industry: "行业动态",
  corporate_action: "公司行动",
  product: "产品",
  risk: "风险事件",
  macro: "宏观事件",
  other: "其他事件",
};
const textKinds = {
  full_text: "来源正文",
  summary: "仅有摘要",
  title_only: "仅有标题",
};
const statuses = {
  pending: "待解读",
  analyzing: "正在解读",
  processing: "正在解读",
  irrelevant: "相关性不足，已跳过",
  unavailable: "请先配置模型接口",
  error: "解读失败，可重试",
  stale: "已超出分析时效",
};

function analysisReviewNotice(item, status) {
  const analysis = item.analysis;
  const logicChanged = analysis?.analysis_logic_changed === true || item.logic_recheck_required === true;
  // The archive's legacy feature_recheck_required flag also covers logic changes.
  const featuresChanged = analysis?.feature_context_changed === true
    || (item.feature_recheck_required === true && !logicChanged && analysis?.feature_context_changed !== false);
  const contentChanged = item.content_changed === true || analysis?.content_changed === true;
  const reasons = [
    logicChanged ? "新闻相关性知识库或提示词" : "",
    featuresChanged ? "个股特征库" : "",
    contentChanged ? "新闻原文" : "",
  ].filter(Boolean);
  if (!reasons.length) return null;
  const reviewing = status === "analyzing" || status === "processing";
  const retained = analysis?.status === "irrelevant"
    ? "此前判定为“相关性不足，已跳过”；旧版关联依据仍可展开查看。"
    : "下方保留旧版解读及证据，相关性与影响方向尚未按最新依据复核。";
  return {
    message: `${reasons.join("、")}已更新，旧结论待复核。`,
    description: `${retained}${reviewing ? "正在重新解读，完成后将显示新结论。" : ""}`,
  };
}

function strings(items) {
  return Array.isArray(items)
    ? items.filter((item) => typeof item === "string" && item.trim())
    : [];
}

function textKey(value) {
  return plainText(value).normalize("NFKC").toLocaleLowerCase()
    .replace(/[。！？!?；;]+$/u, "");
}

function uniqueText(items, shown = []) {
  const seen = new Set(shown.map(textKey).filter(Boolean));
  return strings(items).filter((value) => {
    const key = textKey(value);
    if (!key || seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function uniqueSections(sections, shown = []) {
  const seen = new Set(shown.map(textKey).filter(Boolean));
  return sections.map(([label, items]) => [label, (Array.isArray(items) ? items : []).filter((item) => {
    if (typeof item !== "string") return true;
    const key = textKey(item);
    if (!key || seen.has(key)) return false;
    seen.add(key);
    return true;
  })]);
}

function sourceTime(value) {
  if (!value) return "时间未知";
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? "时间未知" : date.toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

function TextItems({ label, items }) {
  if (!Array.isArray(items) || !items.length) return null;
  return (
    <section className="analysis-list">
      {label ? <Text strong>{label}</Text> : null}
      <ul>
        {items.map((item, index) => (
          <li key={index}>
            {typeof item === "string" ? item : JSON.stringify(item)}
          </li>
        ))}
      </ul>
    </section>
  );
}

function AnalysisEvidence({ sources, references }) {
  const sourceIds = new Set(sources.map((source) => source.source_id));
  const missing = [...new Set(references.map((ref) => ref.source_id))]
    .filter((sourceId) => !sourceIds.has(sourceId));
  return (
    <div className="analysis-source-list">
      {sources.map((source) => {
        const href = safeUrl(source.url);
        const refs = references.filter((ref) => ref.source_id === source.source_id);
        const quotes = [...new Set(strings(refs.map((ref) => ref.quote)))];
        const kind = source.source_id === "N0"
          ? "本条新闻"
          : source.source_id?.startsWith("P") ? "公司资料" : source.source_id?.startsWith("F") ? "个股特征库" : "历史背景";
        return (
          <section className="analysis-source" key={source.source_id}>
            <div className="analysis-source-heading">
              <Tag color={refs.length ? "cyan" : "default"}>{source.source_id}</Tag>
              {href ? (
                <a href={href} target="_blank" rel="noopener noreferrer">
                  {source.title || "查看来源"} ↗
                </a>
              ) : <Text strong>{source.title || "未命名来源"}</Text>}
            </div>
            <div className="analysis-source-meta">
              <span>{kind}{refs.length ? " · 本次引用" : " · 背景资料"}</span>
              <span>{source.source || "来源未提供"}</span>
              <span>{sourceTime(source.published_at || source.observed_at)}</span>
              {source.stock_code ? <span>{source.stock_code}</span> : null}
            </div>
            {quotes.map((quote, index) => (
              <blockquote className="analysis-source-quote" key={index}>
                {quote}
              </blockquote>
            ))}
            {refs.length > 0 && !quotes.length ? (
              <Text type="secondary" className="analysis-source-note">
                未提供原文摘录。
              </Text>
            ) : null}
            {source.text ? (
              <Paragraph
                className="analysis-source-text"
                ellipsis={{ rows: 3, expandable: true, symbol: "展开来源内容" }}
              >
                {source.text}
              </Paragraph>
            ) : null}
          </section>
        );
      })}
      {missing.length ? (
        <Text type="warning">引用来源 {missing.join("、")} 未保存，无法核对。</Text>
      ) : null}
    </div>
  );
}

function MatchEvidence({ evidence }) {
  if (!evidence.length) return null;
  const groups = Object.entries(matchLabels).map(([kind, label]) => {
    const matches = evidence.filter((entry) => entry.kind === kind);
    const values = [...new Set(matches.map((entry) => {
      const value = typeof entry.value === "string" ? entry.value.trim() : "";
      const source = kind === "provider" && typeof entry.source === "string" ? entry.source.trim() : "";
      return [value, source].filter(Boolean).join(" · ");
    }).filter(Boolean))];
    return matches.length ? { kind, label, values } : null;
  }).filter(Boolean);
  if (!groups.length) return null;
  return (
    <div className="news-match-evidence" aria-label="股票匹配依据">
      {groups.map(({ kind, label, values }) => (
        <span key={kind}>{label}{values.length ? `：${values.join("、")}` : ""}</span>
      ))}
    </div>
  );
}

function CombinedSources({ articles }) {
  const urls = new Set();
  const sources = articles.filter((article) => {
    if (!article || typeof article !== "object") return false;
    const href = safeUrl(article.url);
    if (!href || urls.has(href)) return false;
    urls.add(href);
    return true;
  });
  if (sources.length < 2) return null;
  return (
    <details className="news-combined-sources">
      <summary>合并报道（{sources.length} 篇）</summary>
      <ul>
        {sources.map((article) => (
          <li key={article.url}>
            <a href={safeUrl(article.url)} target="_blank" rel="noopener noreferrer">
              {article.title || "阅读报道"} ↗
            </a>
            <span>{article.source || "来源未知"} · {formatTime(article.published_ts)}</span>
          </li>
        ))}
      </ul>
    </details>
  );
}

function StockFeatureImpacts({ impacts, version, sources, context }) {
  const factors = new Map((context?.matched_factors || []).map((factor) => [factor.factor_id || factor.id, factor]));
  const features = new Map((context?.features || []).map((feature) => [feature.feature_id || feature.id, feature]));
  return <div className="analysis-source-list"><Paragraph type="secondary">依据个股特征库{version ? `版本 ${version}` : ""}。以下经营与估值传导属于待验证推断。</Paragraph>{impacts.map((impact, index) => <section className="analysis-source" key={`${impact.factor_id || "factor"}:${index}`}>
    <Space wrap><Text strong>{factors.get(impact.factor_id)?.label || factors.get(impact.factor_id)?.name || "个股传导路径"}</Text><Tag color="orange">待验证推断</Tag></Space>
    <Paragraph><Text strong>本公司暴露：</Text>{impact.exposure || "尚未说明对应特征。"}</Paragraph>
    <Paragraph><Text strong>经营影响：</Text>{impact.operating_impact || "影响尚待核实。"}</Paragraph>
    <Paragraph><Text strong>估值与预期：</Text>{impact.valuation_impact || "股价方向尚待验证。"}</Paragraph>
    <div className="evidence-grid"><TextItems label="成立条件" items={strings(impact.conditions)} /><TextItems label="反向因素" items={strings(impact.offsets)} /><TextItems label="核对指标" items={strings(impact.watch_points)} /></div>
    {strings(impact.feature_ids).length ? <Paragraph type="secondary">关联特征：{impact.feature_ids.map((id) => features.get(id)?.label || features.get(id)?.name || "已归档特征").join("、")}</Paragraph> : null}
    {strings(impact.evidence_source_ids).length ? <details><summary>查看个股依据 · {impact.evidence_source_ids.join("、")}</summary><AnalysisEvidence sources={sources.filter((source) => impact.evidence_source_ids.includes(source.source_id))} references={impact.evidence_source_ids.map((source_id) => ({ source_id }))} /></details> : <Paragraph type="secondary">尚无可追溯的个股证据引用。</Paragraph>}
  </section>)}</div>;
}

function AnalysisKnowledgeVersions({ analysis }) {
  const text = (value) => typeof value === "string" ? value.trim() : "";
  const promptNames = { system_prompt: "系统指令", news_relevance: "相关性判断", stock_impact_analysis: "潜在影响分析" };
  const prompts = analysis?.prompt_versions;
  const rows = prompts && typeof prompts === "object" && !Array.isArray(prompts)
    ? Object.entries(prompts).map(([name, version]) => ({
      key: `prompt:${name}`, label: `提示词 · ${promptNames[name] || name}`,
      fields: [["版本 ID", text(typeof version === "string" ? version : version?.revision_id)], ["内容哈希", text(version?.content_hash)]],
    })) : [];
  rows.push({ key: "relevance", label: "新闻相关性知识库", fields: [["版本 ID", text(analysis?.news_relevance_version)], ["内容哈希", text(analysis?.news_relevance_hash)]] });
  rows.push({ key: "signature", label: "分析逻辑", fields: [["签名", text(analysis?.analysis_logic_signature)]] });
  const recorded = rows.map((row) => ({ ...row, fields: row.fields.filter(([, value]) => value) })).filter((row) => row.fields.length);
  if (!recorded.length) return null;
  return <details className="analysis-list analysis-knowledge-versions">
    <summary>分析使用的知识版本</summary>
    <Paragraph type="secondary">记录本次结论实际使用的版本，便于核对历史依据。</Paragraph>
    <ul>{recorded.map((row) => <li key={row.key}><Text strong>{row.label}</Text>{row.fields.map(([label, value]) => <div key={label}>{label}：<Text code copyable={{ text: value }}>{value}</Text></div>)}</li>)}</ul>
  </details>;
}

export default function NewsImpactCard({ item, symbol }) {
  const [composerOpen, setComposerOpen] = useState(false);
  const analysis = item.analysis;
  const status = item.analysis_status || analysis?.status;
  const analyzed = isAnalyzed(analysis) && !["error", "unavailable", "irrelevant", "stale"].includes(status);
  const reviewNotice = analysisReviewNotice(item, status);
  const href = safeUrl(item.url || item.link);
  const sourceLinkLabel = href && new URL(href).hostname === "so.eastmoney.com" ? "站内检索" : "阅读原文";
  const attentionReason = typeof item.attention?.reason === "string" ? item.attention.reason.trim() : "";
  const matchEvidence = Array.isArray(item.match_evidence)
    ? item.match_evidence.filter((entry) => entry && matchLabels[entry.kind])
    : [];
  const hasRelevanceEvidence = matchEvidence.some((entry) =>
    entry.kind === "relevance" && typeof entry.value === "string" && entry.value.trim(),
  );
  const sourceArticles = Array.isArray(item.source_articles) ? item.source_articles : [];
  const eventSummary = uniqueText([analysis?.event_summary], [item.title])[0];
  const channels = uniqueText(analysis?.impact_channels, [item.title, eventSummary]);
  const expectationGap = uniqueText([analysis?.expectation_gap], [item.title, eventSummary, ...channels])[0];
  const hasInterpretation = Boolean(eventSummary || channels.length || expectationGap);
  const mainReasoning = !hasInterpretation
    ? uniqueText([analysis?.reasoning || analysis?.summary], [item.title])[0]
    : "";
  const visibleText = [item.title, eventSummary, ...channels, expectationGap, mainReasoning];
  const fullReasoning = hasInterpretation
    ? uniqueText([analysis?.reasoning], visibleText)[0]
    : "";
  const originalText = uniqueText([plainText(item.summary), plainText(item.content)],
    analyzed ? visibleText : [item.title]);
  const sameDirection = {
    positive: "bullish",
    neutral: "neutral",
    negative: "bearish",
  }[analysis?.sentiment] === analysis?.impact_direction;
  const quality = analysis?.analysis_context?.quality;
  const qualityNotes = uniqueText([
    ...strings(quality?.limitations),
    ...strings(analysis?.quality_notes),
  ]);
  const hasQualityNotice = textKinds[quality?.text_kind]
    || quality?.publication_date_known === false
    || (quality?.confidence_cap != null && quality.confidence_cap < 100)
    || qualityNotes.length > 0;
  const sources = Array.isArray(analysis?.analysis_context?.evidence_sources)
    ? analysis.analysis_context.evidence_sources.filter((source) => source && typeof source.source_id === "string")
    : [];
  const references = Array.isArray(analysis?.evidence)
    ? analysis.evidence.filter((ref) => ref && typeof ref.source_id === "string")
    : [];
  const featureImpacts = Array.isArray(analysis?.feature_impacts)
    ? analysis.feature_impacts.filter((impact) => impact && typeof impact === "object") : [];
  const failed = status === "error" || status === "unavailable";
  const failureReason = failed && typeof analysis?.reason === "string" && analysis.reason.trim();
  const relevanceReason = analysis?.relevance_reason || (!failed ? analysis?.reason : "");
  const followUpFields = uniqueSections([
    ["兑现条件", analysis?.realization_conditions],
    ["后续观察", analysis?.watch_points],
    ["失效条件", analysis?.invalidation_conditions],
    ["反向观点", analysis?.counter_arguments],
  ], visibleText);
  const followUpText = followUpFields.flatMap(([, values]) => strings(values));
  const confidenceReason = uniqueText([analysis?.confidence_reason], [...visibleText, ...followUpText])[0];
  const factFields = uniqueSections([
    ["事实", analysis?.facts],
    ["推断", analysis?.inferences],
    ["关键因素", analysis?.key_factors],
    ["风险点", analysis?.risk_points],
    ["不确定信息", analysis?.uncertainties],
  ], [...visibleText, ...followUpText, confidenceReason]);
  const reasoningDetail = uniqueText([fullReasoning], [
    ...followUpText, confidenceReason, ...factFields.flatMap(([, values]) => strings(values)),
  ])[0];
  const collapses = [];
  if (featureImpacts.length) {
    collapses.push({ key: "stock-features", label: `基于本公司特征的影响路径（${featureImpacts.length}）`, children: <StockFeatureImpacts impacts={featureImpacts} version={analysis.stock_feature_version} sources={sources} context={analysis.analysis_context?.stock_features?.[symbol]} /> });
  }
  if (eventSummary && (channels.length || expectationGap)) {
    collapses.push({
      key: "impact",
      label: "影响与预期差",
      children: <>
        {channels.length ? <section className="analysis-impact-channels">
          <Text strong>影响传导</Text>
          <ol>{channels.map((step, index) => <li key={index}>{step}</li>)}</ol>
        </section> : null}
        {expectationGap ? <Paragraph className="analysis-expectation-gap"><Text strong>预期差：</Text>{expectationGap}</Paragraph> : null}
      </>,
    });
  }
  if (matchEvidence.length || relevanceReason || analysis?.relevance_score != null || types[analysis?.relevance_type]) {
    collapses.push({
      key: "relevance",
      label: reviewNotice ? "旧版关联依据" : "关联依据",
      children: (
        <div>
          <Space wrap>
            {analysis?.relevance_score != null && !hasRelevanceEvidence ? (
              <Tag color="blue">相关性 {number(analysis.relevance_score, 0)} / 100</Tag>
            ) : null}
            {types[analysis?.relevance_type] ? <Tag>{types[analysis.relevance_type]}</Tag> : null}
          </Space>
          <MatchEvidence evidence={matchEvidence} />
          {relevanceReason && textKey(relevanceReason) !== textKey(failureReason) ? (
            <Paragraph className="relevance-reason" type="secondary">{relevanceReason}</Paragraph>
          ) : null}
        </div>
      ),
    });
  }
  if (originalText.length) {
    collapses.push({
      key: "original",
      label: "新闻内容",
      children: originalText.map((text, index) => (
        <Paragraph className="news-summary" key={index}>{text}</Paragraph>
      )),
    });
  }
  if (followUpFields.some(([, values]) => strings(values).length) || confidenceReason) {
    collapses.push({
      key: "follow-up",
      label: "后续跟踪",
      children: (
        <div className="evidence-grid">
          {followUpFields.map(([label, values]) => (
            <TextItems key={label} label={label} items={strings(values)} />
          ))}
          {confidenceReason ? (
            <section className="analysis-list analysis-confidence-reason">
              <Text strong>置信度依据</Text>
              <Paragraph>{confidenceReason}</Paragraph>
            </section>
          ) : null}
        </div>
      ),
    });
  }
  if (sources.length || references.length) {
    collapses.push({
      key: "sources",
      label: "证据与背景",
      children: <AnalysisEvidence sources={sources} references={references} />,
    });
  }
  if (factFields.some(([, values]) => Array.isArray(values) && values.length) || reasoningDetail) {
    collapses.push({
      key: "evidence",
      label: "事实、推断与风险",
      children: (
        <div className="evidence-grid">
          {reasoningDetail ? (
            <details className="analysis-list analysis-full-reasoning">
              <summary>完整分析</summary>
              <Paragraph>{reasoningDetail}</Paragraph>
            </details>
          ) : null}
          {factFields.map(([label, values]) => (
            <TextItems key={label} label={label} items={values} />
          ))}
        </div>
      ),
    });
  }
  if (analysis?.analysis_input) {
    collapses.push({
      key: "analysis-input",
      label: "解读时的新闻版本",
      children: (
        <div>
          <Text strong>{analysis.analysis_input.title}</Text>
          <Paragraph className="analysis-snapshot">
            {analysis.analysis_input.content_excerpt || "无正文。"}
          </Paragraph>
        </div>
      ),
    });
  }
  return (
    <Card className="news-card" variant="borderless">
      <div className="news-meta">
        <span>
          {formatTime(item.published_at || item.published_ts || item.published)}
        </span>
        <span>{item.source || "来源未知"}</span>
        {item.attention?.level === "major" ? (
          <Tooltip
            title={attentionReason || "优先解读事项"}
            trigger={["hover", "focus"]}
          >
            <Tag color="blue" tabIndex={0}>重大事项</Tag>
          </Tooltip>
        ) : null}
        {analysis?.analyzed_at ? (
          <span>分析于 {formatTime(analysis.analyzed_at)}</span>
        ) : null}
        {analyzed && !sameDirection ? (
          <Tag color={sentimentColors[analysis.sentiment]}>
            {sentimentLabels[analysis.sentiment] || analysis.sentiment}
          </Tag>
        ) : null}
        {href ? (
          <a className="original-link" href={href} target="_blank" rel="noopener noreferrer">
            {sourceLinkLabel} ↗
          </a>
        ) : null}
      </div>
      <h3 className="news-title">
        {href ? (
          <a href={href} target="_blank" rel="noopener noreferrer">
            {item.title}
          </a>
        ) : (
          item.title
        )}
      </h3>
      {reviewNotice ? (
        <Alert
          className="task-status analysis-recheck"
          type="warning"
          showIcon
          message={reviewNotice.message}
          description={reviewNotice.description}
        />
      ) : null}
      {analyzed ? (
        <Space className="analysis-tags" wrap>
          {reviewNotice ? <Tag color="orange">旧版解读 · 待复核</Tag> : null}
          {eventTypes[analysis.event_type] ? <Tag>{eventTypes[analysis.event_type]}</Tag> : null}
          <Tag color={sameDirection ? sentimentColors[analysis.sentiment] : undefined}>
            {directionLabels[analysis.impact_direction] || "方向待评估"}
          </Tag>
          <Tag>影响强度 {number(analysis.impact_strength, 0)} / 100</Tag>
          <Tag>{horizons[analysis.time_horizon] || "周期未知"}</Tag>
          <Tag>置信度 {number(analysis.confidence, 0)} / 100</Tag>
        </Space>
      ) : null}
      {hasQualityNotice ? (
        <div className="analysis-quality" role="note">
          {textKinds[quality?.text_kind] ? <Tag>依据：{textKinds[quality.text_kind]}</Tag> : null}
          {quality?.publication_date_known === false ? <Tag color="gold">发布日期未核实</Tag> : null}
          {quality?.confidence_cap != null && quality.confidence_cap < 100 ? (
            <span>置信度上限 {number(quality.confidence_cap, 0)} / 100</span>
          ) : null}
          {qualityNotes.length ? (
            <details>
              <summary>材料限制（{qualityNotes.length} 项）</summary>
              <TextItems items={qualityNotes} />
            </details>
          ) : null}
        </div>
      ) : null}
      {analyzed && (hasInterpretation || mainReasoning) ? (
        <div className={`impact-reason${hasInterpretation ? " analysis-interpretation" : ""}`}>
          {hasInterpretation ? (
            <>
              {eventSummary ? (
                <Paragraph className="analysis-event-summary">
                  <Text strong>{reviewNotice ? "旧版解读：" : "解读："}</Text>{eventSummary}
                </Paragraph>
              ) : null}
              {!eventSummary && channels.length ? (
                <section className="analysis-impact-channels">
                  <Text strong>影响传导</Text>
                  <ol>
                    {channels.map((step, index) => <li key={index}>{step}</li>)}
                  </ol>
                </section>
              ) : null}
              {!eventSummary && expectationGap ? (
                <Paragraph className="analysis-expectation-gap">
                  <Text strong>预期差：</Text>{expectationGap}
                </Paragraph>
              ) : null}
            </>
          ) : (
            <Paragraph>
              <Text strong>{reviewNotice ? "旧版解读：" : "解读："}</Text>
              {mainReasoning}
            </Paragraph>
          )}
        </div>
      ) : !analyzed && (!reviewNotice || failed) ? (
        <Alert
          type={status === "error" ? "error" : status === "unavailable" ? "warning" : "info"}
          showIcon
          message={
            failureReason ||
            statuses[status] ||
            "待解读"
          }
        />
      ) : null}
      {collapses.length ? (
        <Collapse
          ghost
          size="small"
          items={collapses}
        />
      ) : null}
      <AnalysisKnowledgeVersions analysis={analysis} />
      <CombinedSources articles={sourceArticles} />
      {symbol && item.id != null ? <div className="tracking-news-action"><Button type="link" onClick={() => setComposerOpen(true)}>保存为关注判断</Button></div> : null}
      {composerOpen ? <ThesisComposer symbol={symbol} initial={{ reason: analysis?.reasoning || analysis?.event_summary || item.title || "", watch_conditions: strings(analysis?.watch_points || analysis?.confirmation_conditions).join("\n"), invalidation_conditions: strings(analysis?.invalidation_conditions).join("\n") }} evidenceRefs={[newsEvidence(item)]} onClose={() => setComposerOpen(false)} /> : null}
    </Card>
  );
}
