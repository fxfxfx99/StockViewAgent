const automaticAttempts = new WeakMap();
const ACTIVE_STATUSES = new Set(["queued", "running"]);

export function commentsJobActive(snapshot) {
  return ACTIVE_STATUSES.has(snapshot?.status) || ACTIVE_STATUSES.has(snapshot?.job?.status);
}

export function commentsPollInterval(snapshot) {
  if (commentsJobActive(snapshot)) return 4_000;
  return snapshot?.schedule?.enabled && snapshot.schedule.next_run_at && commentsConfigured(snapshot) ? 60_000 : false;
}

export function commentsConfigured(snapshot) {
  return commentsSourceConfigured(snapshot)
    && snapshot?.prerequisites?.llm_configured === true;
}

function commentsSourceConfigured(snapshot) {
  return snapshot?.prerequisites?.xueqiu_auto_session === true
    || snapshot?.prerequisites?.xueqiu_configured === true;
}

export function commentsPrerequisiteNotice(snapshot, isAdmin) {
  if (!snapshot?.prerequisites) return null;
  const sessionStatus = snapshot.xueqiu_session?.status;
  const needsLogin = sessionStatus === "login_required";
  const needsVerification = sessionStatus === "verification_required";
  const automatic = snapshot.prerequisites.xueqiu_auto_session === true;
  const legacyCookieFailure = !automatic && snapshot.status === "blocked"
    && /cookie|雪球登录|雪球认证/i.test(commentsErrorText(snapshot.error));
  const needsCookie = needsLogin || needsVerification || legacyCookieFailure
    || (!automatic && snapshot.prerequisites.xueqiu_configured === false);
  const needsModel = snapshot.prerequisites.llm_configured === false;
  if (!needsCookie && !needsModel) return null;
  const sourceInstruction = needsLogin || needsVerification
    ? (isAdmin
      ? "登录或验证雪球后，在设置中更新 Cookie。"
      : "请联系管理员登录或验证雪球，更新 Cookie。")
    : (isAdmin ? "请在设置中更新雪球 Cookie。" : "请联系管理员更新雪球 Cookie。");
  return {
    message: needsVerification ? "雪球需要人工验证" : needsLogin ? "雪球需要登录" : needsCookie ? "请先连接雪球" : "请先配置模型接口",
    description: [needsCookie ? sourceInstruction : "", needsModel ? "在设置中填写模型 API Key。" : ""].filter(Boolean).join(" "),
    showSetup: needsModel || isAdmin,
  };
}

export function commentsSessionText(snapshot) {
  const session = snapshot?.xueqiu_session;
  if (session?.mode === "manual") return "使用管理员提供的雪球 Cookie。";
  if (snapshot?.prerequisites?.xueqiu_auto_session !== true && session?.mode !== "automatic") return "";
  const base = "雪球会话自动更新。";
  if (session?.status === "login_required" || session?.status === "verification_required") {
    if (commentsPrerequisiteNotice(snapshot, false)) return "";
    return session.status === "login_required" ? "雪球需要登录，请联系管理员处理。" : "雪球需要人工验证，请联系管理员处理。";
  }
  if (session?.status === "unavailable") return `雪球自动会话暂不可用${session.retry_at ? `，${commentsTime(session.retry_at)}（北京时间）后重试` : "，请稍后重试"}。`;
  if (session?.status === "ready") return "雪球自动会话可用。";
  return base;
}

export function markCommentsAttempt(client, queryKey) {
  let attempts = automaticAttempts.get(client);
  if (!attempts) {
    attempts = new Set();
    automaticAttempts.set(client, attempts);
  }
  const key = JSON.stringify(queryKey);
  if (attempts.has(key)) return false;
  attempts.add(key);
  return true;
}

/** 在发请求前认领，覆盖 StrictMode 重放、切股重挂载及失败后的刷新。 */
export function claimCommentsAutoStart(client, queryKey, snapshot) {
  if (snapshot?.status !== "not_started" || commentsJobActive(snapshot) || !commentsConfigured(snapshot)) return false;
  return markCommentsAttempt(client, queryKey);
}

/** 任务或失败快照尚未带回旧卡片时，保留当前结果而不借用其他股票。 */
export function mergeCommentsSnapshot(previous, next) {
  if (!previous || !next || previous.symbol !== next.symbol) return next;
  if ((commentsJobActive(next) || next.stale) && previous.items?.length && !next.items?.length) {
    return {
      ...next,
      items: previous.items,
      mode: previous.mode,
      target_date: previous.target_date,
      raw_count: previous.raw_count,
      analyzed_count: previous.analyzed_count,
      selected_count: previous.selected_count,
      rejected_count: previous.rejected_count,
      fetched_at: previous.fetched_at,
      updated_at: previous.updated_at,
      stale: true,
    };
  }
  return next;
}

export function commentText(value, maxLength = 4_000) {
  if (typeof value !== "string") return "";
  const named = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " " };
  return value
    .replace(/<(script|style)\b[^>]*>[\s\S]*?<\/\1\s*>/gi, "")
    .replace(/<\/?(?:p|div|br)\b[^>]*>/gi, "\n")
    .replace(/<[^>]+>/g, "")
    .replace(/&(#x[\da-f]+|#\d+|amp|lt|gt|quot|apos|nbsp);/gi, (match, entity) => {
      const lowered = entity.toLowerCase();
      if (!lowered.startsWith("#")) return named[lowered] || match;
      const code = lowered.startsWith("#x") ? parseInt(lowered.slice(2), 16) : parseInt(lowered.slice(1), 10);
      return code > 0 && code <= 0x10ffff && !(code >= 0xd800 && code <= 0xdfff) ? String.fromCodePoint(code) : "";
    })
    .replace(/[\t ]+/g, " ")
    .replace(/\n{3,}/g, "\n\n")
    .trim()
    .slice(0, maxLength);
}

/** 原文只链接到雪球主站的评论路径，禁止跳转接口、子域和携带凭据的链接。 */
export function safeXueqiuCommentUrl(value) {
  if (typeof value !== "string") return null;
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" || url.hostname !== "xueqiu.com" || url.username || url.password || url.port) return null;
    if (!/^\/\d+\/\d+\/?$/.test(url.pathname)) return null;
    return `${url.origin}${url.pathname}`;
  } catch {
    return null;
  }
}

export function commentsRange(snapshot) {
  if (snapshot?.mode === "previous_day") return snapshot.target_date ? `${snapshot.target_date}（北京时间）` : "昨日评论（北京时间）";
  return "最近 200 条评论";
}

export function commentsErrorText(error) {
  return commentText(typeof error === "string" ? error : error?.message, 700);
}

export function commentsServerErrorText(snapshot) {
  const error = commentsErrorText(snapshot?.error);
  const sessionNeedsAction = ["login_required", "verification_required"].includes(snapshot?.xueqiu_session?.status);
  // 登录和验证已有完整的操作引导，避免同一原因再出现一条红色错误。
  if (sessionNeedsAction && /cookie|xueqiu|雪球|登录|验证|authentication|verification/i.test(error)
    && !/\b(?:llm|model|ai)\b|模型|筛选|分析/i.test(error)) return "";
  const missingConfig = (!commentsSourceConfigured(snapshot) && snapshot?.prerequisites?.xueqiu_configured === false)
    || snapshot?.prerequisites?.llm_configured === false;
  // 配置引导已经解释缺少什么；真正的会话失效、抓取或模型错误仍须展示。
  if (snapshot?.status === "blocked" && missingConfig && /^(请先配置|未配置)/.test(error)) return "";
  return error;
}

export function commentsTime(value) {
  if (!value) return "尚未更新";
  const date = new Date(typeof value === "number" ? value * 1_000 : value);
  if (Number.isNaN(date.getTime())) return "时间未知";
  return date.toLocaleString("zh-CN", {
    timeZone: "Asia/Shanghai",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

export function commentsEmptyText(snapshot) {
  if (commentsJobActive(snapshot)) return "正在抓取与筛选评论…";
  if (!snapshot || snapshot.status === "not_started") return snapshot?.prerequisites?.xueqiu_auto_session === true
    ? "尚未采集评论，配置完成后自动开始。"
    : "配置完成后自动采集评论。";
  if (snapshot.status === "blocked" && ((!commentsSourceConfigured(snapshot) && snapshot.prerequisites?.xueqiu_configured === false) || snapshot.prerequisites?.llm_configured === false)) {
    return snapshot.prerequisites?.llm_configured === false
      ? "请先配置模型接口。"
      : "请先完成配置。";
  }
  if (snapshot.status === "error" || snapshot.status === "blocked") {
    if (snapshot.xueqiu_session?.status === "login_required") return "雪球需要登录，请联系管理员处理。";
    if (snapshot.xueqiu_session?.status === "verification_required") return "雪球需要人工验证，请联系管理员处理。";
    const error = `${snapshot.error_kind || ""} ${snapshot.error?.code || ""} ${commentsErrorText(snapshot.error)}`;
    if (/\b(?:llm|model|ai)\b|模型|筛选|分析/i.test(error)) return "筛选失败，请检查模型配置后重试。";
    if (/xueqiu|cookie|雪球|接口|抓取|来源|source|fetch/i.test(error)) return "雪球评论接口暂不可用，请稍后重试。";
    if (snapshot.status === "blocked") return "评论任务不可用，请查看错误说明。";
    return "更新失败，请查看错误说明。";
  }
  if (snapshot.status === "partial") return "已筛选的评论中暂无入选。";
  if (Number(snapshot.raw_count) === 0) return "未抓取到评论，可切换范围或稍后更新。";
  return "暂无符合筛选条件的评论。";
}
