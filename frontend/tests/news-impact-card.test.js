import assert from "node:assert/strict";
import { after, test } from "node:test";
import { fileURLToPath } from "node:url";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

// Render the real card; the unrelated composer only mounts after its own action.
const server = await createServer({
  root: fileURLToPath(new URL("..", import.meta.url)),
  configFile: false,
  appType: "custom",
  logLevel: "silent",
  esbuild: { jsx: "automatic" },
  optimizeDeps: { noDiscovery: true },
  server: { middlewareMode: true, hmr: false, watch: null },
  plugins: [{
    name: "isolate-news-card-composer",
    enforce: "pre",
    resolveId(source, importer) {
      if (source === "../TrackingComponents.jsx" && importer?.endsWith("/NewsImpactCard.jsx")) return "\0test-news-composer";
    },
    load(id) {
      if (id === "\0test-news-composer") return "export const ThesisComposer = () => null;";
    },
  }],
});
after(() => server.close());
const { default: NewsImpactCard } = await server.ssrLoadModule("/src/components/NewsImpactCard.jsx");

const saved = { status: "analyzed", sentiment: "positive", impact_direction: "bullish", summary: "此前保存的影响判断", confidence: 65 };
const render = (patch = {}) => renderToStaticMarkup(React.createElement(NewsImpactCard, { item: { id: 1, title: "公司发布经营公告", analysis_status: "pending", analysis: saved, ...patch } }));

test("prompt or knowledge changes explain the pending review while preserving the old conclusion", () => {
  const html = render({ feature_recheck_required: true, analysis: { ...saved, analysis_logic_changed: true, feature_context_changed: false } });
  assert.match(html, /新闻相关性知识库或提示词已更新，旧结论待复核/);
  assert.match(html, /旧版解读 · 待复核/);
  assert.match(html, /此前保存的影响判断/);
  assert.doesNotMatch(html, /个股特征库已更新|新闻原文已更新|待解读/);
});

test("top-level logic review flags do not mislabel the legacy feature umbrella flag", () => {
  const html = render({ logic_recheck_required: true, feature_recheck_required: true });
  assert.match(html, /新闻相关性知识库或提示词已更新/);
  assert.doesNotMatch(html, /个股特征库已更新/);
});

test("feature changes and original-news changes are named separately", () => {
  const feature = render({ analysis: { ...saved, feature_context_changed: true } });
  assert.match(feature, /个股特征库已更新，旧结论待复核/);
  assert.doesNotMatch(feature, /新闻相关性知识库或提示词已更新|新闻原文已更新/);
  const original = render({ content_changed: true });
  assert.match(original, /新闻原文已更新，旧结论待复核/);
  assert.match(original, /此前保存的影响判断/);
  assert.doesNotMatch(original, /个股特征库已更新|新闻相关性知识库或提示词已更新/);
});

test("concurrent evidence changes retain every review cause and the archived interpretation", () => {
  const html = render({ content_changed: true, analysis: { ...saved, analysis_logic_changed: true, feature_context_changed: true } });
  assert.match(html, /新闻相关性知识库或提示词、个股特征库、新闻原文已更新/);
  assert.match(html, /此前保存的影响判断/);
});

test("previous relevance rejections are visible and explicitly awaiting another review", () => {
  const html = render({ analysis: { status: "irrelevant", analysis_logic_changed: true, relevance_reason: "旧版关联不足", relevance_score: 20 } });
  assert.match(html, /此前判定为“相关性不足，已跳过”/);
  assert.match(html, /旧版关联依据/);
  assert.doesNotMatch(html, /待解读/);
});

test("a running review is distinguished from an updated conclusion", () => {
  const html = render({ analysis_status: "analyzing", analysis: { ...saved, analysis_logic_changed: true } });
  assert.match(html, /正在重新解读，完成后将显示新结论/);
  assert.match(html, /此前保存的影响判断/);
});

test("current conclusions and first-time pending news do not receive stale warnings", () => {
  const current = render({ analysis_status: "analyzed", analysis: { ...saved, analysis_logic_changed: false, feature_context_changed: false, content_changed: false } });
  assert.match(current, /此前保存的影响判断/);
  assert.doesNotMatch(current, /待复核|旧版解读|已更新/);
  const pending = render({ analysis: null });
  assert.match(pending, /待解读/);
  assert.doesNotMatch(pending, /待复核|旧版解读/);
});

test("analysis provenance exposes saved IDs and hashes in a closed section without prompt bodies", () => {
  const html = render({ analysis: { ...saved,
    prompt_versions: {
      system_prompt: { revision_id: "saved-system-revision", content_hash: "saved-system-hash", content: "PROMPT_BODY_MUST_NOT_RENDER" },
      news_relevance: "legacy-relevance-revision",
      stock_impact_analysis: { revision_id: "saved-impact-revision", content_hash: "saved-impact-hash" },
    },
    news_relevance_version: "saved-knowledge-revision", news_relevance_hash: "saved-knowledge-hash",
    analysis_logic_signature: "saved-analysis-signature",
  } });
  assert.match(html, /分析使用的知识版本/);
  for (const value of ["saved-system-revision", "saved-system-hash", "legacy-relevance-revision", "saved-impact-revision", "saved-impact-hash", "saved-knowledge-revision", "saved-knowledge-hash", "saved-analysis-signature"]) assert(html.includes(value));
  const details = html.match(/<details[^>]*class="analysis-list analysis-knowledge-versions"[^>]*>/)?.[0];
  assert(details);
  assert.doesNotMatch(details, /\bopen(?:[=\s>])/);
  assert.doesNotMatch(html, /PROMPT_BODY_MUST_NOT_RENDER/);
  assert.doesNotMatch(render(), /分析使用的知识版本/);
});
