const CUSTOM_PROVIDER = { value: "custom", label: "自定义 / OpenAI 兼容接口" };

export function normalizeLlmProviders(providers) {
  const seen = new Set(["custom"]);
  const valid = (Array.isArray(providers) ? providers : []).filter((provider) => {
    if (!provider || !["id", "label", "api_base", "model"].every((key) => typeof provider[key] === "string" && provider[key].trim()) || seen.has(provider.id)) return false;
    seen.add(provider.id);
    return true;
  }).map(({ id, label, api_base, model }) => ({ value: id, label, api_base, model }));
  return [...valid, CUSTOM_PROVIDER];
}

export function inferLlmProvider(llm, providers) {
  return providers.find((provider) => provider.api_base && provider.api_base.replace(/\/$/, "") === llm?.api_base?.replace(/\/$/, "") && provider.model === llm?.model)?.value || "custom";
}

export function llmPatchFromForm(values, existing = {}, providers = [CUSTOM_PROVIDER]) {
  const provider = providers.find((entry) => entry.value === values.llm_provider);
  if (!provider) throw new Error("请选择大模型供应商。");
  const api_base = (provider.api_base || values.llm_api_base || "").trim().replace(/\/$/, "");
  const model = (provider.model || values.llm_model || "").trim();
  if (!api_base || !model) throw new Error("请填写 API Base 和模型名称。");
  let endpoint;
  try { endpoint = new URL(api_base); } catch { throw new Error("API Base 需要完整的 http 或 https 地址。"); }
  if (!["https:", "http:"].includes(endpoint.protocol) || endpoint.username || endpoint.password || endpoint.search || endpoint.hash) {
    throw new Error("API Base 只能包含服务地址，不能包含密钥、查询参数或片段。");
  }
  const api_key = values.llm_api_key?.trim();
  if (!api_key && !existing.key_configured) throw new Error("请填写 API Key。");
  if (!api_key && existing.api_base?.replace(/\/$/, "") !== api_base) throw new Error("更换服务地址时请填写该供应商的 API Key。");
  return { api_base, model, ...(api_key ? { api_key } : {}) };
}

export const autopilotQueryKey = (userId) => ["autopilot", userId, "status"];

export function autopilotPresentation(status) {
  if (!status) return { label: "正在准备", tone: "processing", description: "正在读取自动运行状态。", action: null };
  const phases = {
    waiting_llm: ["待配置", "warning", "配置大模型，开启自动研究。", "setup"],
    waiting_watchlist: ["需关注", "warning", "添加 A 股自选后自动继续。", null],
    collecting: ["正在准备", "processing", "正在整理历史行情，完成后自动研究。", "research"],
    researching: ["运行中", "processing", "正在验证候选策略并记录结果。", "research"],
    observing: ["运行中", "success", "正在积累数据与模拟记录，达标后更新策略。", "research"],
    paused: ["需关注", "default", "候选研究已暂停，行情和模拟维护继续。", "research"],
    disabled: ["需关注", "default", "自动运行已关闭。", "research"],
    worker_offline: ["需关注", "warning", "后台任务尚未连接，请检查启动服务。", "research"],
    attention: ["需关注", "warning", "有任务需要处理，请查看详情。", "research"],
  };
  const [label, tone, fallback, action] = phases[status.phase] || phases.attention;
  return { label, tone, description: status.phase === "attention" && status.next_action ? status.next_action : fallback, action };
}

export function autopilotPollInterval(status) {
  return ["collecting", "researching"].includes(status?.phase) ? 5000 : 15000;
}
