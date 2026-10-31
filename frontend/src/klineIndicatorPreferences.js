import { SUB_INDICATORS } from "./klineIndicators.js";
import { normalizeIndicatorSettings, indicatorSettingsKey } from "./klineIndicatorSettings.js";

const LEGACY_KEY = "sva_kline_sub_indicator";
const LEGACY_OWNER_KEY = `${LEGACY_KEY}_migrated_to`;

function browserStorage() {
  try { return typeof window === "undefined" ? null : window.localStorage; }
  catch { return null; }
}

/** Browser-only preferences: anonymous sessions never read another account's settings. */
export function readKlineIndicatorPreferences(userId, scope, storage = browserStorage()) {
  const fallback = () => normalizeIndicatorSettings();
  const key = indicatorSettingsKey(userId, scope);
  if (!key || !storage) return fallback();
  try {
    const saved = storage.getItem(key);
    if (saved != null) {
      const value = JSON.parse(saved);
      return value && value.version === 1 ? normalizeIndicatorSettings(value) : fallback();
    }
    // The old setting had no owner. Consume it once, only for a signed-in stock view.
    if (scope === "stock" && !storage.getItem(LEGACY_OWNER_KEY)) {
      const legacy = storage.getItem(LEGACY_KEY);
      if (SUB_INDICATORS.some((item) => item.id === legacy)) {
        const migrated = normalizeIndicatorSettings({ subIndicators: [legacy] });
        storage.setItem(LEGACY_OWNER_KEY, key);
        storage.setItem(key, JSON.stringify(migrated));
        storage.removeItem(LEGACY_KEY);
        return migrated;
      }
    }
  } catch { /* Invalid or unavailable browser storage must not prevent rendering. */ }
  return fallback();
}

export function writeKlineIndicatorPreferences(userId, scope, settings, storage = browserStorage()) {
  const key = indicatorSettingsKey(userId, scope);
  if (!key || !storage) return false;
  try {
    storage.setItem(key, JSON.stringify(normalizeIndicatorSettings(settings)));
    return true;
  } catch { return false; }
}

/** Keep editable comma-separated periods as text until the user applies the form. */
export function indicatorSettingsToDraft(settings) {
  const normalized = normalizeIndicatorSettings(settings);
  return {
    ...normalized,
    params: {
      ...normalized.params,
      ma: { periods: normalized.params.ma.periods.join(", ") },
      rsi: { periods: normalized.params.rsi.periods.join(", ") },
    },
  };
}

function parsePeriods(value) {
  return String(value).trim().split(/[,，\s]+/).map((token) => token === "" ? NaN : Number(token));
}

export function indicatorDraftToSettings(draft) {
  return {
    ...draft,
    params: {
      ma: { periods: parsePeriods(draft.params.ma.periods) },
      boll: { period: Number(draft.params.boll.period), multiplier: Number(draft.params.boll.multiplier) },
      macd: { fast: Number(draft.params.macd.fast), slow: Number(draft.params.macd.slow), signal: Number(draft.params.macd.signal) },
      kdj: { period: Number(draft.params.kdj.period), k: Number(draft.params.kdj.k), d: Number(draft.params.kdj.d) },
      rsi: { periods: parsePeriods(draft.params.rsi.periods) },
    },
  };
}

/** Restore only editable periods; keep the pending chart selections and display draft. */
export function resetIndicatorDraftParameters(draft) {
  return { ...draft, params: indicatorSettingsToDraft().params };
}
