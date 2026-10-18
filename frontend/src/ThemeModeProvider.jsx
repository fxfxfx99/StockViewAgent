import { createContext, useContext, useLayoutEffect, useMemo, useState } from "react";
import { App as AntApp, ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { appDarkTheme, appLightTheme, THEME_STORAGE_KEY } from "./appTheme.js";

const ThemeModeContext = createContext(null);

function readStoredMode() {
  try {
    const v = localStorage.getItem(THEME_STORAGE_KEY);
    if (v === "light" || v === "dark") return v;
  } catch {
    /* Private browsing may disable storage; system appearance still works. */
  }
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function ThemeModeProvider({ children }) {
  const [mode, setMode] = useState(readStoredMode);

  useLayoutEffect(() => {
    document.documentElement.setAttribute("data-theme", mode);
    try {
      localStorage.setItem(THEME_STORAGE_KEY, mode);
    } catch {
      /* ignore */
    }
  }, [mode]);

  const antTheme = mode === "light" ? appLightTheme : appDarkTheme;
  const ctx = useMemo(
    () => ({
      mode,
      isLight: mode === "light",
      setMode,
      toggle: () => setMode((m) => (m === "dark" ? "light" : "dark")),
    }),
    [mode]
  );

  return (
    <ThemeModeContext.Provider value={ctx}>
      <ConfigProvider locale={zhCN} theme={antTheme}>
        <AntApp component={false}>{children}</AntApp>
      </ConfigProvider>
    </ThemeModeContext.Provider>
  );
}

export function useThemeMode() {
  const ctx = useContext(ThemeModeContext);
  if (!ctx) throw new Error("useThemeMode must be used within ThemeModeProvider");
  return ctx;
}
