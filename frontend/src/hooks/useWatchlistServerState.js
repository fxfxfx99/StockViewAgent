import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { App as AntApp } from "antd";
import * as api from "../api.js";
import { qk, watchlistProfileKey } from "./queryKeys.js";

/**
 * 股票列表相关服务端状态（按登录用户隔离）。
 * @param {number|undefined} userId 未登录时不要传或传 undefined，查询将禁用。
 */
export function useWatchlistServerState(userId) {
  const { message } = AntApp.useApp();
  const qc = useQueryClient();
  const enabled = typeof userId === "number" && userId > 0;
  const wk = qk.watchlist(userId);

  const wlQuery = useQuery({
    queryKey: wk,
    queryFn: async () => {
      const data = await api.getWatchlist();
      return data.symbols || [];
    },
    enabled,
  });

  const symbols = wlQuery.data ?? [];
  const symKey = watchlistProfileKey(symbols);

  const profilesQuery = useQuery({
    queryKey: qk.watchlistProfiles(symKey, userId),
    queryFn: async () => {
      const p = await api.getWatchlistProfiles();
      return p.profiles || {};
    },
    enabled: enabled && symbols.length > 0 && !wlQuery.isPending,
  });

  const priceQuery = useQuery({
    queryKey: qk.priceContext(symKey, userId),
    queryFn: async () => {
      const d = await api.getWatchlistPriceContext();
      return d.context || {};
    },
    enabled: enabled && symbols.length > 0,
  });

  const watchProfiles = symbols.length === 0 ? {} : (profilesQuery.data ?? {});
  const priceContext = symbols.length === 0 ? {} : (priceQuery.data ?? {});

  const saveMutation = useMutation({
    // 串行保存，并在执行时读取最新列表，避免连续添加/删除互相覆盖。
    scope: { id: `watchlist-${userId}` },
    mutationFn: async (nextSymbols) => {
      const previous = qc.getQueryData(wk) || [];
      const next = typeof nextSymbols === "function" ? nextSymbols(previous) : nextSymbols;
      const data = await api.saveWatchlist(next);
      return { ...data, added: (data.symbols || []).filter((s) => !previous.includes(s)) };
    },
    onSuccess: (data) => {
      qc.setQueryData(wk, data.symbols || []);
      qc.invalidateQueries({ queryKey: ["watchlist", "price-context"] });
      qc.invalidateQueries({ queryKey: ["news"] });
    },
  });

  const persistSymbols = async (next, { setProfilesRefreshing, setError, onSaved } = {}) => {
    setError?.("");
    try {
      const data = await saveMutation.mutateAsync(next);
      const saved = data.symbols || [];
      const added = data.added;
      onSaved?.(saved);
      if (added.length) {
        void qc.invalidateQueries({ queryKey: ["watchlist", "stock-detail"] });
        setProfilesRefreshing?.(true);
        try {
          const d = await api.quickRefreshWatchlistProfiles(added);
          qc.setQueryData(qk.watchlistProfiles(watchlistProfileKey(saved), userId), d.profiles || {});
          if (Object.keys(d.errors || {}).length) {
            message.warning(
              "部分公司资料未加载，可点「刷新公司资料」重试"
            );
          } else {
            message.success(`已自动拉取 ${added.length} 个新标的的公司资料`);
          }
        } catch {
          try {
            const p = await api.getWatchlistProfiles();
            qc.setQueryData(qk.watchlistProfiles(watchlistProfileKey(saved), userId), p.profiles || {});
          } catch {
            // 保留已加载资料，下一次刷新可重试。
          }
          message.warning("新标的资料拉取失败，请稍后点「刷新公司资料」重试");
        } finally {
          setProfilesRefreshing?.(false);
        }
      } else {
        await qc.invalidateQueries({ queryKey: ["watchlist", "profiles"] });
      }
      return saved;
    } catch (e) {
      setError?.(api.getApiErrorMessage(e));
      return null;
    }
  };

  const refetchWatchlist = async () => {
    const result = await wlQuery.refetch();
    await Promise.all([
      qc.invalidateQueries({ queryKey: ["watchlist", "profiles"] }),
      qc.invalidateQueries({ queryKey: ["watchlist", "price-context"] }),
      qc.invalidateQueries({ queryKey: ["news"] }),
    ]);
    return result;
  };

  const applyProfilesUpdate = (fn) => {
    const key = watchlistProfileKey(symbols);
    qc.setQueryData(qk.watchlistProfiles(key, userId), (prev) => fn(prev || {}));
  };

  return {
    symbols,
    watchProfiles,
    priceContext,
    loadingList: enabled && wlQuery.isPending,
    savingList: saveMutation.isPending,
    profilesLoading: symbols.length > 0 && profilesQuery.isPending,
    priceContextLoading: symbols.length > 0 && priceQuery.isFetching,
    watchlistError: wlQuery.error ? api.getApiErrorMessage(wlQuery.error) : "",
    persistSymbols,
    refetchWatchlist,
    applyProfilesUpdate,
    invalidatePriceContext: () => qc.invalidateQueries({ queryKey: ["watchlist", "price-context"] }),
  };
}
