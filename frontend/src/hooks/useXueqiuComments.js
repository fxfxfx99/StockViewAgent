import { useCallback, useEffect } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as api from "../api.js";
import { qk } from "./queryKeys.js";
import {
  claimCommentsAutoStart,
  commentsPollInterval,
  markCommentsAttempt,
  mergeCommentsSnapshot,
} from "./xueqiuCommentsState.js";

export function useXueqiuComments(symbol, userId) {
  const client = useQueryClient();
  const sym = String(symbol || "").trim().toUpperCase();
  const enabled = Boolean(sym) && userId != null;
  const query = useQuery({
    queryKey: qk.xueqiuComments(sym, userId),
    queryFn: async ({ signal, queryKey }) => {
      const next = await api.getXueqiuComments(sym, { signal });
      if (next.symbol !== sym) throw new Error("评论响应的股票代码不匹配，请重试。");
      return mergeCommentsSnapshot(client.getQueryData(queryKey), next);
    },
    enabled,
    staleTime: 20_000,
    refetchOnWindowFocus: true,
    refetchInterval: (activeQuery) => commentsPollInterval(activeQuery.state.data),
    refetchIntervalInBackground: false,
    retry: 1,
  });
  const { mutate, error: mutationError, isPending, variables } = useMutation({
    mutationKey: ["xueqiu", "comments-refresh", userId, sym],
    mutationFn: async ({ requestedSymbol, mode, token }) => {
      if (token !== api.getAuthToken()) throw new Error("登录状态已变化，请重试。");
      const next = await api.refreshXueqiuComments(requestedSymbol, mode);
      if (next.symbol !== requestedSymbol) throw new Error("评论响应的股票代码不匹配，请重试。");
      return next;
    },
    onMutate: ({ queryKey }) => client.cancelQueries({ queryKey, exact: true }),
    onSuccess: (next, request) => {
      // 退出后尚未返回的排队请求不能重新写回另一个账号的缓存。
      if (request.token !== api.getAuthToken()) return;
      client.setQueryData(request.queryKey, (previous) => mergeCommentsSnapshot(previous, next));
      void client.invalidateQueries({ queryKey: request.queryKey, exact: true });
    },
    retry: false,
  });

  const refresh = useCallback((mode = "latest") => {
    if (!enabled) return;
    const queryKey = qk.xueqiuComments(sym, userId);
    markCommentsAttempt(client, queryKey);
    mutate({ requestedSymbol: sym, mode, queryKey, token: api.getAuthToken() });
  }, [client, enabled, mutate, sym, userId]);

  useEffect(() => {
    if (!enabled || query.isFetching || query.isError || !claimCommentsAutoStart(client, qk.xueqiuComments(sym, userId), query.data)) return;
    refresh("latest");
  }, [client, enabled, query.data, query.isFetching, query.isError, refresh, sym, userId]);

  const sameScope = variables?.requestedSymbol === sym && variables?.queryKey?.[2] === userId;
  return {
    ...query,
    refresh,
    refreshError: sameScope ? mutationError : null,
    isEnqueueing: sameScope && isPending,
    requestedMode: sameScope ? variables.mode : null,
  };
}
