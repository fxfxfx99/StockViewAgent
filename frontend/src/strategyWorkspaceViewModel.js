export function strategyWorkspaceKey(userId, strategyId) {
  const key = ["strategy-workspace", userId ?? "guest"];
  return strategyId ? [...key, strategyId] : key;
}

export function strategyCatalogKey(userId) {
  return [...strategyWorkspaceKey(userId), "catalog"];
}

export function strategyWorkspacePath(strategyId, tab) {
  return `/strategies/${encodeURIComponent(strategyId)}${tab && tab !== "overview" ? `?tab=${encodeURIComponent(tab)}` : ""}`;
}

export function strategyTab(value) {
  return ["overview", "knowledge", "rag"].includes(value) ? value : "overview";
}

export function strategyDate(value) {
  if (!value) return "暂无记录";
  if (/^\d{4}-\d{2}-\d{2}$/.test(String(value))) return value;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "日期未知";
  return date.toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
}

export function knowledgeDocumentPayload(values) {
  const title = String(values.title || "").trim();
  const content = String(values.content || "");
  const source_url = String(values.source_url || "").trim();
  if (!title || title.length > 160) throw new Error("标题需为 1–160 个字符。");
  if (!content.trim() || content.length > 20_000) throw new Error("资料正文需为 1–20,000 个字符。");
  if (source_url) {
    let url;
    try { url = new URL(source_url); } catch { throw new Error("来源链接需为有效的 HTTP 或 HTTPS 网址。"); }
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) throw new Error("来源链接需为不含登录信息的有效 HTTP 或 HTTPS 网址。");
  }
  const tags = [...new Set(String(values.tags || "").split(/[,，\n]/).map((tag) => tag.trim()).filter(Boolean))];
  if (tags.length > 20 || tags.some((tag) => tag.length > 40)) throw new Error("最多 20 个标签，每个标签不超过 40 个字符。");
  const published_at = values.published_at || null;
  if (published_at) {
    const date = new Date(`${published_at}T00:00:00Z`);
    if (!/^\d{4}-\d{2}-\d{2}$/.test(published_at) || Number.isNaN(date.getTime()) || date.toISOString().slice(0, 10) !== published_at) throw new Error("请输入有效的资料发布日期。");
  }
  return { title, content, source_url, published_at, tags };
}

export function strategyRagPayload(values) {
  const top_k = Number(values.top_k);
  const max_age_days = values.max_age_days === null || values.max_age_days === undefined || values.max_age_days === "" ? null : Number(values.max_age_days);
  if (!Number.isInteger(top_k) || top_k < 1 || top_k > 20) throw new Error("检索条数需为 1–20 的整数。");
  if (max_age_days !== null && (!Number.isInteger(max_age_days) || max_age_days < 1 || max_age_days > 3650)) throw new Error("资料时效需为 1–3650 天，留空表示不限。");
  return { enabled: Boolean(values.enabled), top_k, max_age_days, include_builtin: Boolean(values.include_builtin), prefer_recent: Boolean(values.prefer_recent) };
}

export function strategyRetrievalSummary(metadata) {
  if (!metadata) return null;
  return {
    version: metadata.knowledge_version || "—",
    retrieved: Number(metadata.retrieved_count || 0),
    candidates: Number(metadata.candidate_count || 0),
    stale: Number(metadata.stale_count || 0),
    afterCutoff: Number(metadata.excluded_after_cutoff_count || 0),
    asOf: metadata.as_of || metadata.retrieved_at || null,
  };
}
