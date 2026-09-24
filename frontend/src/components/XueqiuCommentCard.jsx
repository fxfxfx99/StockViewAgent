import { Tag } from "antd";
import { commentText, commentsTime, safeXueqiuCommentUrl } from "../hooks/xueqiuCommentsState.js";

const STANCES = {
  bullish: { label: "用户观点 · 偏多", color: "green" },
  bearish: { label: "用户观点 · 偏空", color: "red" },
  neutral: { label: "用户观点 · 中性", color: "default" },
};
const CATEGORIES = {
  fundamentals: "基本面",
  fundamental: "基本面",
  valuation: "估值",
  industry: "行业",
  earnings: "业绩",
  risk: "风险",
  technical: "技术面",
  other: "其他",
};

function score(value) {
  const number = Number(value);
  return value != null && value !== "" && Number.isFinite(number) && number >= 0 && number <= 100
    ? Math.round(number)
    : "—";
}

function count(value) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.max(0, Math.floor(number)).toLocaleString("zh-CN") : "—";
}

export default function XueqiuCommentCard({ item }) {
  const href = safeXueqiuCommentUrl(item.url);
  const title = commentText(item.title, 180);
  const summary = commentText(item.summary, 800);
  const original = commentText(item.text);
  const category = commentText(item.category, 40);
  const stance = STANCES[item.stance] || STANCES.neutral;
  const risks = Array.isArray(item.risks)
    ? item.risks.map((risk) => commentText(risk, 300)).filter(Boolean).slice(0, 5)
    : [];

  return (
    <article className="sva-comment-card">
      <div className="sva-comment-card__heading">
        <div className="sva-comment-card__tags">
          <Tag color={stance.color}>{stance.label}</Tag>
          {category ? <Tag>{CATEGORIES[category] || category}</Tag> : null}
          <span className="sva-comment-card__relevance">相关性 {score(item.relevance_score)} / 100</span>
        </div>
        <span className="sva-comment-card__score" aria-label={`研究价值分 ${score(item.value_score)}，满分 100`}>
          价值分 <strong>{score(item.value_score)}</strong><span> / 100</span>
        </span>
      </div>
      {title ? <h3 className="sva-comment-card__title">{title}</h3> : null}
      <p className="sva-comment-card__summary"><strong>观点摘要：</strong>{summary || "模型未提供摘要，请核对原文。"}</p>
      <p className="sva-comment-card__reason"><strong>入选理由：</strong>{commentText(item.reason, 700) || "模型未提供入选理由。"}</p>
      <div className="sva-comment-card__risks" role="note">
        <strong>风险提示</strong>
        {risks.length ? (
          <ul>{risks.map((risk, index) => <li key={index}>{risk}</li>)}</ul>
        ) : <p>模型未列出具体风险，观点仍需自行核实。</p>}
      </div>
      {original ? (
        <details className="sva-comment-card__original">
          <summary>展开原文节选</summary>
          <blockquote>{original}</blockquote>
          <p>仅显示原文节选；完整内容以雪球原文为准。</p>
        </details>
      ) : null}
      <footer className="sva-comment-card__meta">
        <span>{commentText(item.author?.name, 80) || "作者未知"}</span>
        <span>{item.created_at ? commentsTime(item.created_at) : "发表时间未知"}（北京时间）</span>
        <span>赞 {count(item.like_count)} · 回复 {count(item.reply_count)}</span>
        {href ? (
          <a href={href} target="_blank" rel="noopener noreferrer nofollow">雪球原文 ↗</a>
        ) : <span>原文链接不可用</span>}
      </footer>
    </article>
  );
}
