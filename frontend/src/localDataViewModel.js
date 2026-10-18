export const localDataQueryKey = (userId) => ["settings", "local-data", userId];
export const RETENTION_LABELS = { durable: "长期保留", rebuildable: "可重建", runtime: "运行状态", backup: "备份" };

export function localDataCount(value) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}

export function formatDataSize(value) {
  const bytes = localDataCount(value);
  if (bytes === null) return "—";
  if (bytes < 1024) return `${Math.round(bytes)} B`;
  const units = ["KiB", "MiB", "GiB", "TiB"];
  const level = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length);
  return `${(bytes / 1024 ** level).toLocaleString("zh-CN", { maximumFractionDigits: 1 })} ${units[level - 1]}`;
}

export function formatDataFiles(value) {
  const files = localDataCount(value);
  return files === null ? "—" : `${files.toLocaleString("zh-CN", { maximumFractionDigits: 0 })} 个文件`;
}

export function localDataSummary(data) {
  const totals = data?.directory_totals;
  const categories = (Array.isArray(data?.categories) ? data.categories : [])
    .filter((category) => category && typeof category.id === "string" && typeof category.label === "string")
    .map((category) => ({ ...category, retentionLabel: RETENTION_LABELS[category.retention] || null }));
  const timestamp = data?.index?.updated_at ? new Date(data.index.updated_at) : null;
  return {
    path: typeof data?.data_root === "string" ? data.data_root : null,
    files: localDataCount(totals?.files), size: localDataCount(totals?.size_bytes), categories,
    indexPath: typeof data?.index?.path === "string" ? data.index.path : null,
    indexGuide: typeof data?.index?.guide === "string" ? data.index.guide : null,
    indexTime: timestamp && !Number.isNaN(timestamp.getTime()) ? timestamp.toLocaleString("zh-CN", { hour12: false }) : null,
    commands: typeof data?.migration?.commands === "string" ? data.migration.commands : null,
    notes: (Array.isArray(data?.migration?.notes) ? data.migration.notes : []).filter((note) => typeof note === "string"),
  };
}
