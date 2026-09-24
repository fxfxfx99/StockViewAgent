import assert from "node:assert/strict";
import test from "node:test";
import { qk } from "../src/hooks/queryKeys.js";
import {
  claimCommentsAutoStart,
  commentText,
  commentsConfigured,
  commentsEmptyText,
  commentsJobActive,
  commentsPollInterval,
  commentsPrerequisiteNotice,
  commentsRange,
  commentsServerErrorText,
  commentsSessionText,
  commentsTime,
  markCommentsAttempt,
  mergeCommentsSnapshot,
  safeXueqiuCommentUrl,
} from "../src/hooks/xueqiuCommentsState.js";

const notStarted = {
  symbol: "600519.SS",
  status: "not_started",
  prerequisites: { xueqiu_configured: true, llm_configured: true },
};
const automaticNotStarted = {
  ...notStarted,
  prerequisites: { xueqiu_configured: false, xueqiu_auto_session: true, llm_configured: true },
  xueqiu_session: { mode: "automatic", status: "not_started", retry_at: null },
};

test("automatic sessions permit refresh and initial enqueue without a manually supplied Cookie", () => {
  assert.equal(commentsConfigured(automaticNotStarted), true);
  assert.equal(claimCommentsAutoStart({}, qk.xueqiuComments("600519.SS", 1), automaticNotStarted), true);
  assert.equal(commentsPrerequisiteNotice(automaticNotStarted, true), null);
  assert.equal(commentsPrerequisiteNotice(automaticNotStarted, false), null);
  assert.match(commentsEmptyText(automaticNotStarted), /自动准备雪球会话/);
  assert.match(commentsSessionText(automaticNotStarted), /无需手动填写/);
  assert.equal(commentsConfigured(notStarted), true, "old backends with a manual Cookie remain compatible");
  assert.equal(commentsConfigured({ ...notStarted, prerequisites: { xueqiu_configured: false, llm_configured: true } }), false);
  assert.equal(commentsConfigured({ ...automaticNotStarted, prerequisites: { ...automaticNotStarted.prerequisites, llm_configured: false } }), false);
});

test("automatic sessions still require a personal model key but never request a Cookie for that error", () => {
  const blocked = {
    ...automaticNotStarted,
    status: "blocked",
    error: "请先配置个人 LLM",
    prerequisites: { ...automaticNotStarted.prerequisites, llm_configured: false },
  };
  const notice = commentsPrerequisiteNotice(blocked, false);
  assert.match(notice.message, /AI 模型/);
  assert.match(notice.description, /API Key/);
  assert.doesNotMatch(notice.description, /Cookie/);
  assert.equal(notice.showSetup, true);
  assert.equal(commentsServerErrorText(blocked), "");
  assert.match(commentsEmptyText(blocked), /个人 AI 模型/);
  assert.equal(claimCommentsAutoStart({}, qk.xueqiuComments("600519.SS", 1), blocked), false);
});

test("only actual login or verification responses request one manual administrator intervention", () => {
  for (const status of ["login_required", "verification_required"]) {
    const blocked = {
      ...automaticNotStarted,
      status: "blocked",
      error: status === "login_required" ? "雪球要求登录" : "雪球要求人工验证",
      xueqiu_session: { mode: "automatic", status, retry_at: null },
    };
    const adminNotice = commentsPrerequisiteNotice(blocked, true);
    const memberNotice = commentsPrerequisiteNotice(blocked, false);
    assert.match(adminNotice.description, /官网完成登录或验证/);
    assert.match(adminNotice.description, /作为回退/);
    assert.equal(adminNotice.showSetup, true);
    assert.match(memberNotice.description, /联系管理员/);
    assert.equal(memberNotice.showSetup, false);
    assert.match(commentsEmptyText(blocked), /管理员处理/);
    assert.equal(commentsServerErrorText(blocked), "", "action guidance replaces a duplicate authentication error");
    assert.equal(commentsServerErrorText({ ...blocked, error: "模型筛选失败" }), "模型筛选失败");
    assert.equal(commentsConfigured(blocked), true, "manual retry remains available after administrator recovery");
  }
});

test("transient automatic session failures show retry timing without requiring manual configuration", () => {
  const unavailable = {
    ...automaticNotStarted,
    status: "error",
    error: "雪球 Cookie 自动获取暂不可用",
    xueqiu_session: { mode: "automatic", status: "unavailable", retry_at: Date.UTC(2026, 8, 24, 1, 5) / 1_000 },
  };
  assert.equal(commentsPrerequisiteNotice(unavailable, true), null);
  assert.equal(commentsServerErrorText(unavailable), unavailable.error);
  assert.match(commentsSessionText(unavailable), /09:05（北京时间）后重试/);
  assert.match(commentsEmptyText(unavailable), /稍后重试/);
  assert.match(commentsSessionText({ ...automaticNotStarted, xueqiu_session: { mode: "automatic", status: "ready" } }), /自动会话可用/);
  assert.match(commentsSessionText({ ...automaticNotStarted, xueqiu_session: { mode: "manual", status: "ready" } }), /管理员提供/);
  assert.equal(commentsSessionText(notStarted), "");
});

test("comment caches and initial jobs are isolated by both account and stock", () => {
  const first = qk.xueqiuComments("600519.SS", 1);
  const otherUser = qk.xueqiuComments("600519.SS", 2);
  const otherStock = qk.xueqiuComments("000001.SZ", 1);
  assert.notDeepEqual(first, otherUser);
  assert.notDeepEqual(first, otherStock);
  const client = {};
  assert.equal(claimCommentsAutoStart(client, first, notStarted), true);
  assert.equal(claimCommentsAutoStart(client, otherUser, notStarted), true);
  assert.equal(claimCommentsAutoStart(client, otherStock, notStarted), true);
});

test("automatic enqueue is claimed before work starts and never loops after a failure or remount", () => {
  const client = {};
  const key = qk.xueqiuComments("600519.SS", 1);
  assert.equal(claimCommentsAutoStart(client, key, notStarted), true);
  // StrictMode repeats the effect; a failed request leaves the same not_started snapshot.
  assert.equal(claimCommentsAutoStart(client, [...key], { ...notStarted }), false);
  assert.equal(claimCommentsAutoStart(client, key, { ...notStarted }), false);
  assert.equal(claimCommentsAutoStart({}, key, notStarted), true);
});

test("manual enqueue prevents an automatic duplicate while missing prerequisites do not spend the attempt", () => {
  const client = {};
  const key = qk.xueqiuComments("600519.SS", 1);
  assert.equal(claimCommentsAutoStart(client, key, {
    ...notStarted, prerequisites: { xueqiu_configured: false, llm_configured: true },
  }), false);
  assert.equal(claimCommentsAutoStart(client, key, {
    ...notStarted, prerequisites: { xueqiu_configured: true, llm_configured: false },
  }), false);
  assert.equal(claimCommentsAutoStart(client, key, { ...notStarted, status: "error" }), false);
  assert.equal(claimCommentsAutoStart(client, key, { ...notStarted, job: { status: "running" } }), false);
  assert.equal(markCommentsAttempt(client, key), true);
  assert.equal(claimCommentsAutoStart(client, key, notStarted), false);
});

test("active jobs poll quickly while idle snapshots without an eligible schedule stop polling", () => {
  for (const status of ["queued", "running"]) {
    assert.equal(commentsPollInterval({ status }), 4_000);
    assert.equal(commentsPollInterval({ status: "ready", job: { status } }), 4_000);
  }
  for (const status of ["not_started", "blocked", "ready", "partial", "empty", "error"]) {
    assert.equal(commentsPollInterval({ status, job: { status: "completed" } }), false);
  }
  assert.equal(commentsJobActive(undefined), false);
});

test("configured scheduled stocks poll each minute to discover daily background updates", () => {
  const scheduled = {
    status: "ready",
    prerequisites: { xueqiu_configured: true, llm_configured: true },
    schedule: { enabled: true, next_run_at: 1789952400 },
  };
  assert.equal(commentsPollInterval(scheduled), 60_000);
  assert.equal(commentsPollInterval({ ...scheduled, status: "queued" }), 4_000);
  assert.equal(commentsPollInterval({ ...scheduled, job: { status: "running" } }), 4_000);
  assert.equal(commentsPollInterval({ ...scheduled, schedule: { enabled: false, next_run_at: 1789952400 } }), false);
  assert.equal(commentsPollInterval({ ...scheduled, schedule: { enabled: true, next_run_at: null } }), false);
  assert.equal(commentsPollInterval({ ...scheduled, prerequisites: { xueqiu_configured: false, llm_configured: true } }), false);
  assert.equal(commentsPollInterval({ ...scheduled, prerequisites: { xueqiu_configured: true, llm_configured: false } }), false);
  assert.equal(commentsPollInterval({ ...scheduled, prerequisites: automaticNotStarted.prerequisites }), 60_000);
});

test("configuration guidance suppresses duplicate alerts without hiding actual source or model failures", () => {
  const missingCookie = {
    status: "blocked", prerequisites: { xueqiu_configured: false, llm_configured: true },
    error: "请先配置雪球 Cookie",
  };
  assert.equal(commentsServerErrorText(missingCookie), "");
  assert.equal(commentsServerErrorText({ ...missingCookie, error: "请先配置雪球 Cookie 和个人 LLM" }), "");
  assert.equal(commentsServerErrorText({ ...missingCookie, error: "雪球登录会话已过期，请更新 Cookie。" }), "雪球登录会话已过期，请更新 Cookie。");
  assert.equal(commentsServerErrorText({ ...missingCookie, status: "error", error: "雪球评论接口请求失败，请稍后重试。" }), "雪球评论接口请求失败，请稍后重试。");
  assert.equal(commentsServerErrorText({ ...missingCookie, status: "error", error: "评论筛选未完成，未生成有效评分结果。" }), "评论筛选未完成，未生成有效评分结果。");
  assert.equal(commentsServerErrorText({ ...missingCookie, prerequisites: { xueqiu_configured: true, llm_configured: true } }), "请先配置雪球 Cookie");
});

test("enqueue snapshots preserve old results and their scope, but completed empty results replace them", () => {
  const previous = {
    symbol: "600519.SS", status: "ready", mode: "previous_day", target_date: "2026-09-20",
    items: [{ id: "1", summary: "旧观点" }], raw_count: 180, analyzed_count: 180,
    selected_count: 1, rejected_count: 179, fetched_at: 123, updated_at: 124,
  };
  const queued = { symbol: previous.symbol, status: "queued", mode: "latest", items: [], job: { status: "queued", mode: "latest", progress: 0, total: 200 } };
  const merged = mergeCommentsSnapshot(previous, queued);
  assert.deepEqual(merged.items, previous.items);
  assert.equal(merged.mode, "previous_day");
  assert.equal(merged.target_date, "2026-09-20");
  assert.equal(merged.updated_at, 124);
  assert.equal(merged.raw_count, 180);
  assert.equal(merged.job.mode, "latest");
  assert.equal(merged.stale, true);
  const empty = { symbol: previous.symbol, status: "empty", mode: "latest", items: [], raw_count: 100 };
  assert.deepEqual(mergeCommentsSnapshot(previous, empty), empty);
  const failed = { symbol: previous.symbol, status: "error", stale: true, items: [], error: "模型接口暂不可用" };
  assert.deepEqual(mergeCommentsSnapshot(previous, failed).items, previous.items);
  const differentStock = { ...queued, symbol: "000001.SZ" };
  assert.deepEqual(mergeCommentsSnapshot(previous, differentStock), differentStock);
});

test("original links allow only canonical HTTPS Xueqiu comment paths", () => {
  assert.equal(safeXueqiuCommentUrl("https://xueqiu.com/123456/987654"), "https://xueqiu.com/123456/987654");
  assert.equal(safeXueqiuCommentUrl("https://xueqiu.com/123456/987654?redirect=https://evil.test#ref"), "https://xueqiu.com/123456/987654");
  for (const value of [
    "http://xueqiu.com/1/2", "https://xueqiu.com.evil.test/1/2", "https://evil.test@xueqiu.com/1/2",
    "https://api.xueqiu.com/1/2", "https://xueqiu.com:444/1/2", "https://xueqiu.com/redirect?url=https://evil.test",
    "https://xueqiu.com/S/SH600519", "https://xueqiu.com/1/2/extra", "javascript:alert(1)",
    "data:text/html,unsafe", "/1/2", null,
  ]) assert.equal(safeXueqiuCommentUrl(value), null, String(value));
});

test("comment excerpts remove markup and scripts while remaining bounded plain strings", () => {
  assert.equal(commentText('<p>营收 &amp; 利润</p><script>alert(1)</script><div>增长 &#x1F4C8;</div>'), "营收 & 利润\n\n增长 📈");
  assert.equal(commentText('<img src="invalid" onerror="alert(1)">正文'), "正文");
  assert.equal(commentText("abcdef", 3), "abc");
  assert.equal(commentText({ html: "untrusted" }), "");
  assert.equal(commentText("&#x110000;&#55296;"), "");
});

test("empty-state messages distinguish first use, filtering, source failure and model failure", () => {
  assert.match(commentsEmptyText(notStarted), /尚未抓取/);
  assert.match(commentsEmptyText({ status: "empty", raw_count: 100 }), /没有符合/);
  assert.match(commentsEmptyText({ status: "empty", raw_count: 0 }), /未抓取到评论/);
  assert.match(commentsEmptyText({ status: "error", error: "雪球 Cookie 已失效" }), /接口暂不可用/);
  assert.match(commentsEmptyText({ status: "blocked", error: "雪球 Cookie 已失效", prerequisites: { xueqiu_configured: true, llm_configured: true } }), /接口暂不可用/);
  assert.match(commentsEmptyText({ status: "error", error: "LLM 筛选失败" }), /AI 筛选未完成/);
  assert.doesNotMatch(commentsEmptyText({ status: "error", error: "Request failed" }), /AI 筛选未完成/);
  assert.match(commentsEmptyText({ status: "partial", raw_count: 20 }), /部分/);
});

test("cached daily scopes retain their actual date and timestamps use Beijing time", () => {
  assert.equal(commentsRange({ mode: "previous_day", target_date: "2026-09-20" }), "2026-09-20（北京时间）");
  assert.match(commentsRange({ mode: "latest" }), /200/);
  assert.match(commentsTime(Date.UTC(2026, 8, 21, 1, 0) / 1_000), /09:00/);
  assert.equal(commentsTime(null), "尚未更新");
});
