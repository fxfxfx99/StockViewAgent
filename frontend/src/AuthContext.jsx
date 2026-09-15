import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import * as api from "./api.js";

const AuthContext = createContext(null);

/**
 * 本地开放模式：无登录 UI。后端 AUTH_REQUIRED=false 时，无 Token 也可拉取 /me 与业务接口。
 */
export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);

  const refreshMe = useCallback(async () => {
    setLoading(true);
    try {
      await api.getHealth();
      try {
        const u = await api.getMe();
        setUser(u);
      } catch {
        // 开放本地模式：清除过期 Token 后重试，避免股票列表等接口因 user=null 被禁用
        if (api.getAuthToken()) {
          api.setAuthToken("");
          const u = await api.getMe();
          setUser(u);
        } else {
          setUser(null);
        }
      }
    } catch {
      setUser(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refreshMe();
  }, [refreshMe]);

  const value = useMemo(
    () => ({
      user,
      loading,
      isAdmin: user?.role === "admin",
      refreshMe,
      setUser,
      /** @deprecated 开放本地部署已取消登录 */
      login: async () => {
        throw new Error("本产品为本地开放部署，无需登录");
      },
      logout: () => {
        api.setAuthToken("");
        setUser(null);
      },
    }),
    [user, loading, refreshMe]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth outside AuthProvider");
  return ctx;
}
