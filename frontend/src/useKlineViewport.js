import { useCallback, useMemo, useRef, useState } from "react";
import { applyKlineViewport, captureKlineViewport } from "./klineViewport.js";

export default function useKlineViewport({ contextKey, candles, visibleBars }) {
  const state = useRef({ contextKey, visibleBars, saved: null });
  const currentCandles = useRef(candles);
  currentCandles.current = candles;
  if (state.current.contextKey !== contextKey || state.current.visibleBars !== visibleBars) {
    state.current = { contextKey, visibleBars, saved: null };
  }
  const [revision, setRevision] = useState(0);
  const applyViewport = useCallback((option) => applyKlineViewport(option, state.current.saved, candles), [candles, contextKey, visibleBars]);
  const onEvents = useMemo(() => ({ datazoom: (event) => {
    const saved = captureKlineViewport(event, currentCandles.current);
    if (saved) state.current.saved = saved;
  } }), []);
  const resetViewport = useCallback(() => {
    state.current.saved = { recent: state.current.visibleBars };
    setRevision((value) => value + 1);
  }, []);
  const showAll = useCallback(() => {
    state.current.saved = { all: true };
    setRevision((value) => value + 1);
  }, []);
  return { applyViewport, onEvents, resetViewport, showAll, revision };
}
