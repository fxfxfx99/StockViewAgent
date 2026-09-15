import { useQuery } from "@tanstack/react-query";
import * as api from "../api";

export function useSetupStatus() {
  return useQuery({
    queryKey: ["settings", "setup-status"],
    queryFn: () => api.getSetupStatus(),
    staleTime: 30_000,
    refetchOnWindowFocus: true,
  });
}
