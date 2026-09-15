import { useEffect, useState } from "react";
import * as api from "../api.js";

const stockDetailCache = new Map();

export function clearWatchlistStockDetailCache() {
  stockDetailCache.clear();
}

/** 懒加载 /watchlist/stock-detail，用于公司资料 F10 补全 */
export function useWatchlistStockDetail(symbol) {
  const sym = String(symbol || "").trim().toUpperCase();
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!sym) {
      setData(null);
      setLoading(false);
      return undefined;
    }
    const cached = stockDetailCache.get(sym);
    if (cached) {
      setData(cached);
      setLoading(false);
      return undefined;
    }
    let cancelled = false;
    setLoading(true);
    setData(null);
    (async () => {
      try {
        const d = await api.getWatchlistStockDetail(sym);
        if (cancelled) return;
        stockDetailCache.set(sym, d);
        setData(d);
      } catch {
        if (!cancelled) setData(null);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [sym]);

  return { data, loading };
}
