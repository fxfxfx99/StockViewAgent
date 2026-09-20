import { createContext, useContext, useEffect, useMemo, useState } from "react";
import { App as AntApp, ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { appDarkTheme, appLightTheme, THEME_STORAGE_KEY } from "./appTheme.js";

const ThemeModeContext = createContext(null);

function readStoredMode() {
  try {
    const v = localStorage.getItem(THEME_STORAGE_KEY);
    return v === "light" ? "light" : "dark";
  } catch {
    return "dark";
  }
}

export function ThemeModeProvider({ children }) {
  const [mode, setMode] = useState(readStoredMode);

  useEffect(() => {
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
