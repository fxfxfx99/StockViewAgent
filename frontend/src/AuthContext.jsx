import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import * as api from "./api.js";

const AuthContext = createContext(null);

/** 服务端决定是否登录；本地 AUTH_REQUIRED=false 继续使用默认账号。 */
export function AuthProvider({ children }) {
  const queryClient = useQueryClient();
  const [user, setUser] = useState(null);
  const [config, setConfig] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const configRef = useRef(null);
  const refreshVersion = useRef(0);

  const clearSession = useCallback((reason = "") => {
    refreshVersion.current += 1;
    api.setAuthToken("");
    setUser(null);
    setError(reason);
    setLoading(false);
    void queryClient.cancelQueries();
    queryClient.clear();
  }, [queryClient]);

  const refreshMe = useCallback(async () => {
    const version = ++refreshVersion.current;
    setLoading(true);
    setError("");
    try {
      const nextConfig = await api.getAuthConfig();
      if (version !== refreshVersion.current) return;
      configRef.current = nextConfig;
      setConfig(nextConfig);
      if (nextConfig.auth_required && !api.getAuthToken()) {
        setUser(null);
        return;
      }
      let nextUser;
      try {
        nextUser = await api.getMe();
      } catch (e) {
        if (version !== refreshVersion.current) return;
        if (!nextConfig.auth_required && api.getAuthToken() && e?.response?.status === 401) {
          api.setAuthToken("");
          nextUser = await api.getMe();
        } else {
          throw e;
        }
      }
      if (version === refreshVersion.current) setUser(nextUser);
    } catch (e) {
      if (version === refreshVersion.current) {
        setUser(null);
        setError(api.getApiErrorMessage(e));
      }
    } finally {
      if (version === refreshVersion.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    const unsubscribe = api.onAuthExpired(() => {
      if (configRef.current?.auth_required) clearSession("登录已过期，请重新登录。");
    });
    void refreshMe();
    return () => {
      unsubscribe();
      refreshVersion.current += 1;
    };
  }, [clearSession, refreshMe]);

  const authenticate = useCallback(async (credentials, isRegistration) => {
    const result = await (isRegistration ? api.register(credentials) : api.login(credentials));
    refreshVersion.current += 1;
    void queryClient.cancelQueries();
    queryClient.clear();
    api.setAuthToken(result.access_token);
    setUser(result.user);
    setError("");
    setLoading(false);
  }, [queryClient]);

  const login = useCallback((credentials) => authenticate(credentials, false), [authenticate]);
  const register = useCallback((credentials) => authenticate(credentials, true), [authenticate]);
  const logout = useCallback(() => clearSession(), [clearSession]);

  const value = useMemo(() => ({
    user,
    loading,
    error,
    authRequired: config?.auth_required === true,
    registrationEnabled: config?.registration_enabled === true,
    configReady: config !== null,
    isAdmin: user?.role === "admin",
    refreshMe,
    setUser,
    login,
    register,
    logout,
  }), [user, loading, error, config, refreshMe, login, register, logout]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth outside AuthProvider");
  return ctx;
}
