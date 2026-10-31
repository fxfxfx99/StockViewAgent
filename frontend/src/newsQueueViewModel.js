export function newsSyncNotice(result) {
  const jobs = result?.jobs || [];
  if (result?.status === "cooldown" || (jobs.length && jobs.every((job) => job.status === "cooldown"))) return "新闻来源冷却中，请稍后重试。";
  if (jobs.length && jobs.every((job) => job.deduplicated || ["running", "queued"].includes(job.status)) && jobs.every((job) => job.deduplicated || job.status === "running")) return "采集任务已在队列或运行中。";
  return "采集任务已提交，完成后可读取归档。";
}
