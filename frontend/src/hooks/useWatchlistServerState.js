import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { message } from "antd";
import * as api from "../api.js";
import { clearWatchlistStockDetailCache } from "./useWatchlistStockDetail.js";
import { qk, watchlistProfileKey } from "./queryKeys.js";

/**
 * 股票列表相关服务端状态（按登录用户隔离）。
 * @param {number|undefined} userId 未登录时不要传或传 undefined，查询将禁用。
 */
export function useWatchlistServerState(userId) {
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
    queryKey: qk.watchlistProfiles(symKey),
    queryFn: async () => {
      const p = await api.getWatchlistProfiles();
      return p.profiles || {};
    },
    enabled: enabled && symbols.length > 0 && !wlQuery.isPending,
  });

  const priceQuery = useQuery({
    queryKey: qk.priceContext(symKey),
    queryFn: async () => {
      const d = await api.getWatchlistPriceContext();
      return d.context || {};
    },
    enabled: enabled && symbols.length > 0,
  });

  const watchProfiles = symbols.length === 0 ? {} : (profilesQuery.data ?? {});
  const priceContext = symbols.length === 0 ? {} : (priceQuery.data ?? {});

  const saveMutation = useMutation({
    mutationFn: async (nextSymbols) => {
      const data = await api.saveWatchlist(nextSymbols);
      return data;
    },
    onSuccess: (data) => {
      qc.setQueryData(wk, data.symbols || []);
      qc.invalidateQueries({ queryKey: ["watchlist", "price-context"] });
    },
  });

  const persistSymbols = async (next, { setProfilesRefreshing, setError } = {}) => {
    setError?.("");
    const prev = new Set(qc.getQueryData(wk) || []);
    try {
      const data = await saveMutation.mutateAsync(next);
      const saved = data.symbols || [];
      const added = saved.filter((s) => !prev.has(s));
      if (added.length) {
        clearWatchlistStockDetailCache();
        setProfilesRefreshing?.(true);
        try {
          const d = await api.quickRefreshWatchlistProfiles(added);
          qc.setQueryData(qk.watchlistProfiles(watchlistProfileKey(saved)), d.profiles || {});
          if (Object.keys(d.errors || {}).length) {
            message.warning(
              "部分标的 F10 未拉全，可点「刷新公司信息与要闻」做完整同步（含向量化）"
            );
          } else {
            message.success(`已自动拉取 ${added.length} 个新标的的公司资料`);
          }
        } catch {
          try {
            const p = await api.getWatchlistProfiles();
            qc.setQueryData(qk.watchlistProfiles(watchlistProfileKey(saved)), p.profiles || {});
          } catch {
            qc.setQueryData(qk.watchlistProfiles(watchlistProfileKey(saved)), {});
          }
          message.warning("新标的资料拉取失败，请稍后点「刷新公司信息与要闻」重试");
        } finally {
          setProfilesRefreshing?.(false);
        }
      } else {
        await qc.invalidateQueries({ queryKey: ["watchlist", "profiles"] });
      }
    } catch (e) {
      setError?.(e?.message || "保存失败");
    }
  };

  const reloadProfiles = async () => {
    await qc.invalidateQueries({ queryKey: ["watchlist", "profiles"] });
  };

  const refetchWatchlist = () => wlQuery.refetch();

  const applyProfilesUpdate = (fn) => {
    const key = watchlistProfileKey(symbols);
    qc.setQueryData(qk.watchlistProfiles(key), (prev) => fn(prev || {}));
  };

  return {
    symbols,
    watchProfiles,
    priceContext,
    loadingList: wlQuery.isPending,
    profilesLoading: symbols.length > 0 && profilesQuery.isPending,
    priceContextLoading: symbols.length > 0 && priceQuery.isFetching,
    watchlistError: wlQuery.error?.message || "",
    persistSymbols,
    reloadProfiles,
    refetchWatchlist,
    applyProfilesUpdate,
    invalidatePriceContext: () => qc.invalidateQueries({ queryKey: ["watchlist", "price-context"] }),
  };
}
