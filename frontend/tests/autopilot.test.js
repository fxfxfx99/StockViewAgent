import assert from "node:assert/strict";
import test from "node:test";
import { autopilotPollInterval, autopilotPresentation, autopilotQueryKey, inferLlmProvider, llmPatchFromForm, normalizeLlmProviders } from "../src/autopilotViewModel.js";

const providers = normalizeLlmProviders([
  { id: "deepseek", label: "DeepSeek", api_base: "https://api.deepseek.com/v1", model: "deepseek-chat" },
  { id: "dashscope", label: "Qwen", api_base: "https://dashscope.aliyuncs.com/compatible-mode/v1", model: "qwen-plus" },
]);

test("provider selection uses backend presets and preserves custom saved models", () => {
  assert.equal(inferLlmProvider({ api_base: "https://api.deepseek.com/v1/", model: "deepseek-chat" }, providers), "deepseek");
  assert.equal(inferLlmProvider({ api_base: "https://api.deepseek.com/v1", model: "my-custom-model" }, providers), "custom");
  assert.deepEqual(normalizeLlmProviders(null).map((entry) => entry.value), ["custom"]);
  assert.deepEqual(normalizeLlmProviders([{ id: "bad" }]).map((entry) => entry.value), ["custom"]);
});

test("preset plus key is enough; market credentials are never required", () => {
  assert.deepEqual(llmPatchFromForm({ llm_provider: "deepseek", llm_api_key: " example-key " }, {}, providers), {
    api_base: "https://api.deepseek.com/v1", model: "deepseek-chat", api_key: "example-key",
  });
});

test("saved key stays private and cannot silently move to a different provider", () => {
  const existing = { key_configured: true, api_base: "https://api.deepseek.com/v1" };
  const patch = llmPatchFromForm({ llm_provider: "deepseek" }, existing, providers);
  assert.equal(Object.hasOwn(patch, "api_key"), false);
  assert.throws(() => llmPatchFromForm({ llm_provider: "dashscope" }, existing, providers), /更换服务地址/);
  assert.throws(() => llmPatchFromForm({ llm_provider: "deepseek" }, {}, providers), /API Key/);
});

test("custom endpoint validation refuses credentials and missing model", () => {
  const custom = { llm_provider: "custom", llm_api_key: "example-key", llm_model: "model-id" };
  assert.throws(() => llmPatchFromForm({ ...custom, llm_api_base: "https://service.test/v1?api_key=secret" }), /不能包含密钥/);
  assert.throws(() => llmPatchFromForm({ ...custom, llm_api_base: "https://user:pass@service.test/v1" }), /不能包含密钥/);
  assert.throws(() => llmPatchFromForm({ ...custom, llm_api_base: "https://service.test/v1", llm_model: "" }), /模型名称/);
  assert.equal(llmPatchFromForm({ ...custom, llm_api_base: "http://127.0.0.1:11434/v1" }).model, "model-id");
});

test("status stays truthful when setup is saved but worker is offline", () => {
  const status = { llm_configured: true, phase: "worker_offline", watchlist: { count: 5 }, market_data_configured: false };
  const view = autopilotPresentation(status);
  assert.equal(view.label, "需关注");
  assert.match(view.description, /后台任务尚未连接/);
  assert.equal(autopilotPresentation({ ...status, phase: "collecting" }).label, "正在准备");
  assert.equal(autopilotPresentation({ ...status, phase: "researching" }).label, "运行中");
  assert.equal(autopilotPresentation({ ...status, phase: "observing" }).label, "运行中");
  assert.equal(autopilotPresentation({ ...status, phase: "waiting_llm" }).label, "待配置");
});

test("explicitly cleared lists and unknown phases never appear as running", () => {
  assert.equal(autopilotPresentation({ phase: "waiting_watchlist", watchlist: { explicitly_empty: true } }).label, "需关注");
  assert.equal(autopilotPresentation({ phase: "new_unknown_phase" }).label, "需关注");
  assert.equal(autopilotPollInterval({ phase: "collecting" }), 5000);
  assert.equal(autopilotPollInterval({ phase: "observing" }), 15000);
  assert.notDeepEqual(autopilotQueryKey(1), autopilotQueryKey(2));
});
