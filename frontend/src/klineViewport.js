/** Keep a user's time window stable when ECharts is rebuilt for indicators/theme. */
export function captureKlineViewport(event, candles) {
  if (!candles.length) return null;
  const zoom = event?.batch?.[0] || event;
  if (!Number.isFinite(zoom?.start) || !Number.isFinite(zoom?.end)) return null;
  const last = candles.length - 1;
  const start = Math.max(0, Math.min(last, Math.round(zoom.start / 100 * last)));
  const end = Math.max(start, Math.min(last, Math.round(zoom.end / 100 * last)));
  return { startTime: candles[start].t, endTime: candles[end].t,
    followLatest: end === last, barCount: end - start + 1, all: zoom.start <= 0 && zoom.end >= 100 };
}

export function resolveKlineViewport(saved, candles) {
  if (!saved || !candles.length) return null;
  const last = candles.length - 1;
  let start, end;
  if (saved.all) { start = 0; end = last; }
  else if (saved.followLatest || saved.recent) {
    end = last;
    start = Math.max(0, end - (saved.recent || saved.barCount) + 1);
  } else {
    start = candles.findIndex((bar) => bar.t >= saved.startTime);
    if (start < 0) start = last;
    end = candles.findLastIndex((bar) => bar.t <= saved.endTime);
    end = Math.max(start, end);
  }
  return { start: last ? start / last * 100 : 0, end: last ? end / last * 100 : 100 };
}

export function applyKlineViewport(option, saved, candles) {
  const zoom = resolveKlineViewport(saved, candles);
  if (!option || !zoom) return option;
  return { ...option, dataZoom: option.dataZoom.map(({ startValue, endValue, ...item }) => ({ ...item, ...zoom })) };
}
