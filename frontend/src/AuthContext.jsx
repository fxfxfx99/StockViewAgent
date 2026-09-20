import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import * as api from "./api.js";

const AuthContext = createContext(null);

/**
 * 本地开放模式：无登录 UI。后端 AUTH_REQUIRED=false 时，无 Token 也可拉取 /me 与业务接口。
 */
export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const refreshMe = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      await api.getHealth();
      try {
        const u = await api.getMe();
        setUser(u);
      } catch (e) {
        // 开放本地模式：清除过期 Token 后重试，避免股票列表等接口因 user=null 被禁用
        if (api.getAuthToken() && e?.response?.status === 401) {
          api.setAuthToken("");
          const u = await api.getMe();
          setUser(u);
        } else {
          throw e;
        }
      }
    } catch (e) {
      setUser(null);
      setError(api.getApiErrorMessage(e));
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
      error,
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
    [user, loading, error, refreshMe]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth outside AuthProvider");
  return ctx;
}
