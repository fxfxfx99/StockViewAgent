import { Alert, Card, Collapse, Space, Tag, Tooltip, Typography } from "antd";
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
  full_text: "源提供正文",
  summary: "仅有摘要",
  title_only: "仅有标题",
};
const statuses = {
  pending: "待解读",
  analyzing: "正在解读",
  processing: "正在解读",
  irrelevant: "相关性未达阈值，未进行影响分析",
  unavailable: "AI 分析尚不可用，请在设置中配置大模型",
  error: "本次 AI 分析未成功，可重试",
  stale: "新闻已超出分析时效，仅保留历史记录",
};
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
          : source.source_id?.startsWith("P") ? "公司资料" : "历史背景";
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
              <span>{sourceTime(source.published_at)}</span>
              {source.stock_code ? <span>{source.stock_code}</span> : null}
            </div>
            {quotes.map((quote, index) => (
              <blockquote className="analysis-source-quote" key={index}>
                {quote}
              </blockquote>
            ))}
            {refs.length > 0 && !quotes.length ? (
              <Text type="secondary" className="analysis-source-note">
                引用该来源，未提供逐字摘录。
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
        <Text type="warning">引用来源 {missing.join("、")} 未保存在分析背景中，无法核对。</Text>
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
      <summary>已合并 {sources.length} 篇重复报道 · 查看来源</summary>
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

export default function NewsImpactCard({ item }) {
  const analysis = item.analysis;
  const status = item.analysis_status || analysis?.status;
  const analyzed = isAnalyzed(analysis) && !["error", "unavailable", "irrelevant", "stale"].includes(status);
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
  const failed = status === "error" || status === "unavailable";
  const failureReason = failed && typeof analysis?.reason === "string" && analysis.reason.trim();
  const relevanceReason = analysis?.relevance_reason || (!failed ? analysis?.reason : "");
  const followUpFields = [
    ["兑现条件", analysis?.realization_conditions],
    ["后续观察", analysis?.watch_points],
    ["失效条件", analysis?.invalidation_conditions],
    ["反向观点", analysis?.counter_arguments],
  ];
  const factFields = [
    ["已知事实", analysis?.facts],
    ["分析推断", analysis?.inferences],
    ["关键因素", analysis?.key_factors],
    ["风险点", analysis?.risk_points],
    ["不确定信息", analysis?.uncertainties],
  ];
  const collapses = [];
  if (matchEvidence.length || relevanceReason || analysis?.relevance_score != null || types[analysis?.relevance_type]) {
    collapses.push({
      key: "relevance",
      label: "关联说明",
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
  if (followUpFields.some(([, values]) => strings(values).length) || analysis?.confidence_reason) {
    collapses.push({
      key: "follow-up",
      label: "兑现条件与后续跟踪",
      children: (
        <div className="evidence-grid">
          {followUpFields.map(([label, values]) => (
            <TextItems key={label} label={label} items={strings(values)} />
          ))}
          {analysis.confidence_reason ? (
            <section className="analysis-list analysis-confidence-reason">
              <Text strong>置信度依据</Text>
              <Paragraph>{analysis.confidence_reason}</Paragraph>
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
  if (factFields.some(([, values]) => Array.isArray(values) && values.length) || fullReasoning) {
    collapses.push({
      key: "evidence",
      label: "事实、推断与风险",
      children: (
        <div className="evidence-grid">
          {fullReasoning ? (
            <section className="analysis-list analysis-full-reasoning">
              <Text strong>完整分析理由</Text>
              <Paragraph>{fullReasoning}</Paragraph>
            </section>
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
      label: "查看分析时的新闻内容",
      children: (
        <div>
          <Text strong>{analysis.analysis_input.title}</Text>
          <Paragraph className="analysis-snapshot">
            {analysis.analysis_input.content_excerpt || "当时没有可用的新闻正文。"}
          </Paragraph>
        </div>
      ),
    });
  }
  return (
    <Card className="news-card" bordered={false}>
      <div className="news-meta">
        <span>
          {formatTime(item.published_at || item.published_ts || item.published)}
        </span>
        <span>{item.source || "来源未知"}</span>
        {item.attention?.level === "major" ? (
          <Tooltip
            title={`优先核查与解读，不代表利好或影响强度${attentionReason ? `。${attentionReason}` : ""}`}
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
      {analyzed ? (
        <Space className="analysis-tags" wrap>
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
          {textKinds[quality?.text_kind] ? <Tag>分析依据：{textKinds[quality.text_kind]}</Tag> : null}
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
      {analyzed && (item.content_changed || analysis.content_changed) ? (
        <Alert
          className="task-status"
          type="warning"
          showIcon
          message="新闻内容已更新，以下判断基于分析时的内容。"
        />
      ) : null}
      {analyzed && (hasInterpretation || mainReasoning) ? (
        <div className={`impact-reason${hasInterpretation ? " analysis-interpretation" : ""}`}>
          {hasInterpretation ? (
            <>
              {eventSummary ? (
                <Paragraph className="analysis-event-summary">
                  <Text strong>事件解读：</Text>{eventSummary}
                </Paragraph>
              ) : null}
              {channels.length ? (
                <section className="analysis-impact-channels">
                  <Text strong>影响传导</Text>
                  <ol>
                    {channels.map((step, index) => <li key={index}>{step}</li>)}
                  </ol>
                </section>
              ) : null}
              {expectationGap ? (
                <Paragraph className="analysis-expectation-gap">
                  <Text strong>预期差：</Text>{expectationGap}
                </Paragraph>
              ) : null}
            </>
          ) : (
            <Paragraph>
              <Text strong>潜在影响分析：</Text>
              {mainReasoning}
            </Paragraph>
          )}
        </div>
      ) : !analyzed ? (
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
      <CombinedSources articles={sourceArticles} />
    </Card>
  );
}
