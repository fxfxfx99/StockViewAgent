import { useQuery } from "@tanstack/react-query";
import * as api from "../api";
import { qk } from "./queryKeys.js";

export function useXueqiuCompany(symbol) {
  const sym = String(symbol || "").trim().toUpperCase();
  return useQuery({
    queryKey: qk.xueqiuCompany(sym),
    queryFn: () => api.getXueqiuCompany(sym),
    enabled: Boolean(sym),
    staleTime: 5 * 60 * 1000,
    retry: 1,
  });
}
