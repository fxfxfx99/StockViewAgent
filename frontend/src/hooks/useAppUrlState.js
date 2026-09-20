import { useCallback } from "react";
import { useSearchParams } from "react-router-dom";
import { normalizeAsOf, normalizeChartSymbol } from "./urlState.js";

/**
 * URL 深链：chart=股票代码，as_of=YYYY-MM-DD（时间节点，空=最新）
 */
export function useAppUrlState() {
  const [searchParams, setSearchParams] = useSearchParams();

  const chartSymbol = normalizeChartSymbol(searchParams.get("chart"));
  const asOf = normalizeAsOf(searchParams.get("as_of"));

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
      const s = normalizeChartSymbol(sym);
      if (s) {
        patch((p) => p.set("chart", s));
      } else {
        patch((p) => p.delete("chart"));
      }
    },
    [patch]
  );

  const setAsOf = useCallback(
    (day) => {
      const d = normalizeAsOf(day);
      if (d) {
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
    asOf,
    setAsOf,
  };
}
