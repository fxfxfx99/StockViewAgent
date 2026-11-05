export const KNOWLEDGE_KINDS = [
  { value: "stock_features", label: "个股特征库" },
  { value: "news_relevance", label: "新闻相关性知识库" },
  { value: "prompts", label: "分析提示词" },
];

export const knowledgeKey = (userId, section, params) => ["data-center", userId ?? "guest", "knowledge", ...(section ? [section] : []), ...(params === undefined ? [] : [params])];
export const knowledgeKind = (kind) => KNOWLEDGE_KINDS.find((item) => item.value === kind)?.label || kind || "—";
export const knowledgeContent = (content) => typeof content === "string" ? content : content == null ? "" : JSON.stringify(content, null, 2);
export const shortRevision = (revision) => typeof revision === "string" && revision ? revision.slice(0, 12) : "—";

export function knowledgeParams({ kind, q, page = 1, pageSize = 20 } = {}) {
  const limit = [20, 50, 100].includes(pageSize) ? pageSize : 20;
  return { ...(kind ? { kind } : {}), ...(q?.trim() ? { q: q.trim() } : {}), offset: (Math.max(1, page) - 1) * limit, limit };
}

export function knowledgeSource(metadata = {}) {
  const source = metadata.provenance || {};
  return [source.source || source.generator || source.source_type, source.source_version || source.version].filter((item) => typeof item === "string" || typeof item === "number").join(" · ") || "本地生成";
}

export function knowledgeTask(result) {
  const task = result?.active_job || result?.recent_jobs?.[0] || result?.job || result?.task || result;
  if (!task?.status || task.status === "idle") return null;
  const running = ["queued", "running", "pending"].includes(task.status);
  const failed = ["failed", "error"].includes(task.status);
  const partial = task.status === "partial";
  const complete = ["completed", "success", "succeeded"].includes(task.status);
  const total = Number(task.total ?? task.total_documents ?? task.progress?.total);
  const processed = Number(task.processed ?? task.completed ?? task.progress?.processed ?? task.progress?.completed);
  const hasProgress = Number.isFinite(total) && total > 0 && Number.isFinite(processed);
  return { ...task, running, type: failed ? "error" : partial ? "warning" : complete ? "success" : "info", label: task.status === "queued" || task.status === "pending" ? "重建任务已排队" : running ? "正在重建本地知识缓存" : failed ? "重建未完成" : partial ? "部分重建完成" : complete ? "重建已完成" : "最近一次重建", percent: hasProgress ? Math.min(100, Math.max(0, Math.round(processed / total * 100))) : null, progressText: hasProgress ? `${processed.toLocaleString("zh-CN")} / ${total.toLocaleString("zh-CN")} 项` : null };
}

export function promptPayload(content, reason, revision) {
  if (!reason?.trim()) throw new Error("请填写本次变更的原因，便于追踪与回溯。");
  if (reason.trim().length > 400) throw new Error("变更原因最多 400 字。");
  if (!revision) throw new Error("尚未获取当前版本，请刷新后再保存。");
  if (content !== undefined && !content.trim()) throw new Error("提示词内容不能为空。");
  if (content?.length > 20000) throw new Error("提示词最多 20,000 字。");
  return { ...(content === undefined ? {} : { content }), reason: reason.trim(), expected_revision: revision };
}
