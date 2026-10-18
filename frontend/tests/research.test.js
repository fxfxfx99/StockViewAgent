import assert from "node:assert/strict";
import test from "node:test";
import { displayNumber, displayPercent, finiteNumber, gateLabel, parseJsonObject, researchQueryKey, safeSourceUrl, statusInfo } from "../src/researchViewModel.js";

test("missing financial evidence is never rendered as zero", () => {
  for (const value of [undefined, null, "", NaN, Infinity, "unavailable"]) {
    assert.equal(finiteNumber(value), null);
    assert.equal(displayNumber(value), "—");
    assert.equal(displayPercent(value), "—");
  }
  assert.equal(displayPercent(0), "0.00%");
  assert.equal(displayPercent(-1.25), "-1.25%");
});
test("job completion and shadow observation do not claim promotion", () => {
  assert.equal(statusInfo("completed")[0], "已完成");
  assert.equal(statusInfo("shadow_started")[0], "开始模拟观察");
  assert.equal(statusInfo("promoted")[0], "已晋升");
  assert.notEqual(gateLabel("positive_net_return"), "扣费后收益未达要求");
});
test("research cache keys isolate account and detail requests", () => {
  assert.notDeepEqual(researchQueryKey(1), researchQueryKey(2));
  assert.notDeepEqual(researchQueryKey(1, "strategy", "abc"), researchQueryKey(1, "experiment", "abc"));
});
test("source links accept only navigable web protocols", () => {
  assert.equal(safeSourceUrl("javascript:alert(1)"), null);
  assert.equal(safeSourceUrl("data:text/html,unsafe"), null);
  assert.equal(safeSourceUrl("file:///private/file"), null);
  assert.equal(safeSourceUrl("https://github.com/microsoft/qlib"), "https://github.com/microsoft/qlib");
});
test("JSON editor rejects nonobjects and preserves strategy content", () => {
  for (const input of ["null", "[]", "true", "3", '{"bad":}']) assert.throws(() => parseJsonObject(input));
  assert.deepEqual(parseJsonObject('{"name":"候选","entry":{"op":"gt"}}'), { name: "候选", entry: { op: "gt" } });
});
