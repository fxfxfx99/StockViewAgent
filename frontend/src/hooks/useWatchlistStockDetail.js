import { useQuery } from "@tanstack/react-query";
import * as api from "../api.js";
import { useAuth } from "../AuthContext.jsx";
import { qk } from "./queryKeys.js";

/** 公司详情与其余服务端状态共享 React Query 缓存，刷新后可统一失效。 */
export function useWatchlistStockDetail(symbol) {
  const { user } = useAuth();
  const sym = String(symbol || "").trim().toUpperCase();
  const query = useQuery({
    queryKey: qk.stockDetail(sym, user?.id),
    queryFn: () => api.getWatchlistStockDetail(sym),
    enabled: Boolean(sym && user),
    staleTime: 5 * 60_000,
  });
  return { ...query, loading: query.isFetching };
}
