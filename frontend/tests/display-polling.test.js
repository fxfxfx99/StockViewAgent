import assert from "node:assert/strict";
import test from "node:test";
import { displayPollingChoice, displayPollingInterval, displayPollingKey } from "../src/displayPolling.js";
import { newsSyncNotice } from "../src/newsQueueViewModel.js";
test("display preferences are scoped by account and page; invalid storage cannot set an excessive frequency", () => {
  assert.notEqual(displayPollingKey(1,"news"),displayPollingKey(2,"news"));
  assert.notEqual(displayPollingKey(1,"news"),displayPollingKey(1,"data"));
  assert.equal(displayPollingChoice("0"),"30");
  assert.equal(displayPollingChoice(null,"15"),"15");
  assert.equal(displayPollingInterval("15"),15000);
  assert.equal(displayPollingInterval("off"),false);
  assert.equal(displayPollingInterval("15",false),false);
  assert.equal(displayPollingInterval("15",true,false),false);
});
test("news queue notices do not claim successful ingestion before the worker completes", () => {
  assert.match(newsSyncNotice({status:"queued",jobs:[{status:"queued"}]}),/已提交/);
  assert.match(newsSyncNotice({status:"queued",jobs:[{status:"queued",deduplicated:true}]}),/已在队列/);
  assert.match(newsSyncNotice({status:"cooldown",jobs:[]}),/冷却中/);
  assert.doesNotMatch(newsSyncNotice({upserted:0}),/新闻已更新|已同步/);
});
