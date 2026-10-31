export const DISPLAY_POLL_OPTIONS = [
  { value: "15", label: "每15秒" }, { value: "30", label: "每30秒" },
  { value: "60", label: "每60秒" }, { value: "off", label: "暂停自动刷新" },
];
export const displayPollingKey = (userId, page) => `sva_display_poll:${userId ?? "guest"}:${page}`;
export function displayPollingChoice(value, fallback = "30") { return DISPLAY_POLL_OPTIONS.some((item) => item.value === String(value)) ? String(value) : fallback; }
export function displayPollingInterval(choice, visible = true, active = true) { return !visible || !active || choice === "off" ? false : Number(displayPollingChoice(choice)) * 1000; }
