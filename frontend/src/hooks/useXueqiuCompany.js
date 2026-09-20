import { useQuery } from "@tanstack/react-query";
import * as api from "../api.js";
import { qk } from "./queryKeys.js";

export function useXueqiuCompany(symbol) {
  const sym = String(symbol || "").trim().toUpperCase();
  return useQuery({
    queryKey: qk.xueqiuCompany(sym),
    queryFn: () => api.getXueqiuCompany(sym),
    enabled: Boolean(sym),
    // 读取服务端缓存；源站更新频率由后端统一控制，隐藏页面停止轮询。
    staleTime: 60_000,
    refetchInterval: 60_000,
    refetchIntervalInBackground: false,
    retry: 1,
  });
}
