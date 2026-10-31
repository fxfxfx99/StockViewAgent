import { useMutation, useQueryClient } from "@tanstack/react-query";
import * as api from "../api.js";
import { invalidateNewsData } from "./newsInvalidate.js";

export function useNewsActions() {
  const client = useQueryClient();
  const updated = () => invalidateNewsData(client);
  const sync = useMutation({
    mutationFn: (symbols) => api.syncArchiveFeeds(symbols),
    retry: false, // Submission queues work; never retry a POST automatically.
    onSuccess: updated,
  });
  const analyze = useMutation({
    mutationFn: (symbol) => api.analyzeNewsForSymbol(symbol, api.NEWS_ANALYSIS_BATCH_SIZE),
    onSuccess: updated,
  });
  return { sync, analyze };
}
