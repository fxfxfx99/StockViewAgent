import assert from "node:assert/strict";
import test from "node:test";
import { formatDataFiles, formatDataSize, localDataQueryKey, localDataSummary } from "../src/localDataViewModel.js";

test("local data totals distinguish unavailable from an empty directory", () => {
  for (const missing of [undefined, null, "", "0", -1, NaN, Infinity]) {
    assert.equal(formatDataSize(missing), "—");
    assert.equal(formatDataFiles(missing), "—");
  }
  assert.equal(formatDataSize(0), "0 B");
  assert.equal(formatDataFiles(0), "0 个文件");
  assert.equal(formatDataSize(1536), "1.5 KiB");
  assert.equal(formatDataSize(2 * 1024 ** 3), "2 GiB");
  assert.equal(formatDataFiles(1234), "1,234 个文件");
});

test("local data cache keys are isolated by the authenticated administrator", () => {
  assert.notDeepEqual(localDataQueryKey(1), localDataQueryKey(2));
});

test("a missing data root or directory total is not guessed from the old partial catalog", () => {
  const summary = localDataSummary({ totals: { files: 20, size_bytes: 2048 }, catalog: "_managed/catalog.json" });
  assert.equal(summary.path, null);
  assert.equal(summary.size, null);
  assert.equal(summary.files, null);
  assert.equal(summary.commands, null);
});

test("local data summary preserves server-supplied paths and offline steps without executing them", () => {
  const summary = localDataSummary({
    data_root: "/example/data folder", directory_totals: { files: 25, size_bytes: 3072 },
    categories: [{ id: "research", label: "研究与策略", files: 4, size_bytes: 1024, retention: "durable" }],
    index: { path: "_managed/catalog.json", guide: "INDEX.md", updated_at: "2026-10-18T08:00:00Z" },
    migration: { commands: "# Stop services before migration\n./backend/.venv/bin/python scripts/storage.py migrate --to '/target/data folder'", notes: ["校验复制后保留原目录。"] },
  });
  assert.equal(summary.path, "/example/data folder");
  assert.equal(summary.files, 25);
  assert.equal(summary.size, 3072);
  assert.equal(summary.categories[0].id, "research");
  assert.equal(summary.categories[0].retentionLabel, "长期保留");
  assert.equal(summary.indexPath, "_managed/catalog.json");
  assert.equal(summary.indexGuide, "INDEX.md");
  assert.ok(summary.indexTime);
  assert.match(summary.commands, /\n\.\/backend\/\.venv\/bin\/python scripts\/storage\.py migrate --to '\/target\/data folder'/);
  assert.deepEqual(summary.notes, ["校验复制后保留原目录。"]);
});
