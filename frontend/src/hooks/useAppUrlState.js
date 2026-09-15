import { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";

/**
 * URL 深链：chart=股票代码，as_of=YYYY-MM-DD（时间节点，空=最新）
 */
export function useAppUrlState() {
  const [searchParams, setSearchParams] = useSearchParams();

  const chartFromUrl = useMemo(() => {
    const c = (searchParams.get("chart") || "").trim().toUpperCase();
    if (/^\d{6}\.(SS|SZ|BJ)$/.test(c)) return c;
    return null;
  }, [searchParams]);

  const asOfFromUrl = useMemo(() => {
    const d = (searchParams.get("as_of") || "").trim();
    if (/^\d{4}-\d{2}-\d{2}$/.test(d)) return d;
    return null;
  }, [searchParams]);

  const [chartPending, setChartPending] = useState(null);
  const chartSymbol = chartPending ?? chartFromUrl;

  useEffect(() => {
    if (chartPending != null && chartFromUrl === chartPending) {
      setChartPending(null);
    }
  }, [chartFromUrl, chartPending]);

  const patch = useCallback(
    (mutator) => {
      setSearchParams(
        (prev) => {
          const p = new URLSearchParams(prev);
          mutator(p);
          return p;
        },
        { replace: true }
      );
    },
    [setSearchParams]
  );

  const setChartSymbol = useCallback(
    (sym) => {
      const s = (sym || "").trim().toUpperCase();
      if (/^\d{6}\.(SS|SZ|BJ)$/.test(s)) {
        setChartPending(s);
        patch((p) => p.set("chart", s));
      } else {
        setChartPending(null);
        patch((p) => p.delete("chart"));
      }
    },
    [patch]
  );

  const setAsOf = useCallback(
    (day) => {
      const d = (day || "").trim();
      if (/^\d{4}-\d{2}-\d{2}$/.test(d)) {
        patch((p) => p.set("as_of", d));
      } else {
        patch((p) => p.delete("as_of"));
      }
    },
    [patch]
  );

  return {
    chartSymbol,
    setChartSymbol,
    asOf: asOfFromUrl,
    setAsOf,
  };
}
