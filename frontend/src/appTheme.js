import { theme } from "antd";

export const THEME_STORAGE_KEY = "sva_theme_mode";

const shared = {
  token: {
    colorPrimary: "#007AFF",
    colorSuccess: "#34C759",
    colorInfo: "#5AC8FA",
    colorWarning: "#FF9500",
    colorError: "#FF3B30",
    borderRadius: 12,
    borderRadiusLG: 16,
    borderRadiusSM: 8,
    fontFamily:
      '"SF Pro Display", "SF Pro Text", "PingFang SC", system-ui, -apple-system, "Segoe UI", sans-serif',
    fontSize: 13,
    lineHeight: 1.47,
    controlHeight: 34,
    motionDurationMid: "0.2s",
  },
  components: {
    Layout: { headerHeight: 52, headerPadding: "0 22px", bodyBg: "transparent" },
    Card: { headerFontSize: 14, paddingLG: 16 },
    Tabs: {
      itemActiveColor: "#007AFF",
      itemSelectedColor: "#007AFF",
      inkBarColor: "#007AFF",
      titleFontSize: 14,
    },
    Table: { borderColor: "rgba(0, 0, 0, 0.06)" },
    Input: { borderRadius: 10 },
    Select: { borderRadius: 10 },
    Button: { primaryShadow: "none", borderRadius: 10, fontWeight: 500 },
    Spin: { colorPrimary: "#007AFF" },
    Collapse: { headerBg: "transparent", contentBg: "transparent" },
    Alert: { borderRadiusLG: 12 },
    Tag: { borderRadiusSM: 6 },
    Descriptions: { borderRadiusLG: 12 },
  },
};

export const appDarkTheme = {
  algorithm: theme.darkAlgorithm,
  ...shared,
  token: {
    ...shared.token,
    colorPrimary: "#0A84FF",
    colorBgBase: "#000000",
    colorBgLayout: "#000000",
    colorBgContainer: "rgba(28, 28, 30, 0.92)",
    colorBgElevated: "rgba(44, 44, 46, 0.96)",
    colorBorder: "rgba(255, 255, 255, 0.1)",
    colorBorderSecondary: "rgba(255, 255, 255, 0.06)",
    colorText: "rgba(255, 255, 255, 0.92)",
    colorTextSecondary: "rgba(235, 235, 245, 0.6)",
    colorTextTertiary: "rgba(235, 235, 245, 0.45)",
    colorTextQuaternary: "rgba(235, 235, 245, 0.35)",
  },
  components: {
    ...shared.components,
    Layout: {
      ...shared.components.Layout,
      headerBg: "rgba(28, 28, 30, 0.72)",
    },
    Tabs: {
      ...shared.components.Tabs,
      itemColor: "rgba(235, 235, 245, 0.55)",
      itemHoverColor: "rgba(255, 255, 255, 0.88)",
      itemActiveColor: "#0A84FF",
      itemSelectedColor: "#0A84FF",
      inkBarColor: "#0A84FF",
    },
    Table: {
      ...shared.components.Table,
      headerBg: "rgba(44, 44, 46, 0.6)",
      headerColor: "rgba(255, 255, 255, 0.85)",
      rowHoverBg: "rgba(10, 132, 255, 0.08)",
      borderColor: "rgba(255, 255, 255, 0.06)",
      colorBgContainer: "transparent",
    },
    Modal: {
      contentBg: "rgba(44, 44, 46, 0.98)",
      headerBg: "rgba(44, 44, 46, 0.98)",
      titleColor: "rgba(255, 255, 255, 0.95)",
    },
    Input: {
      ...shared.components.Input,
      colorBgContainer: "rgba(118, 118, 128, 0.24)",
      activeBorderColor: "#0A84FF",
      hoverBorderColor: "rgba(255, 255, 255, 0.18)",
    },
    Select: {
      ...shared.components.Select,
      colorBgContainer: "rgba(118, 118, 128, 0.24)",
    },
    Spin: { colorPrimary: "#0A84FF" },
    Drawer: { colorBgElevated: "rgba(44, 44, 46, 0.98)" },
    Card: {
      ...shared.components.Card,
      colorBgContainer: "rgba(28, 28, 30, 0.92)",
      colorBorderSecondary: "rgba(255, 255, 255, 0.06)",
    },
  },
};

export const appLightTheme = {
  algorithm: theme.defaultAlgorithm,
  ...shared,
  token: {
    ...shared.token,
    colorBgBase: "#f5f5f7",
    colorBgLayout: "#f5f5f7",
    colorBgContainer: "#ffffff",
    colorBgElevated: "#ffffff",
    colorBorder: "rgba(0, 0, 0, 0.08)",
    colorBorderSecondary: "rgba(0, 0, 0, 0.04)",
    colorText: "rgba(0, 0, 0, 0.88)",
    colorTextSecondary: "rgba(60, 60, 67, 0.6)",
    colorTextTertiary: "rgba(60, 60, 67, 0.45)",
    colorTextQuaternary: "rgba(60, 60, 67, 0.35)",
  },
  components: {
    ...shared.components,
    Layout: {
      ...shared.components.Layout,
      headerBg: "rgba(255, 255, 255, 0.82)",
    },
    Tabs: {
      ...shared.components.Tabs,
      itemColor: "rgba(60, 60, 67, 0.55)",
      itemHoverColor: "rgba(0, 0, 0, 0.85)",
    },
    Table: {
      ...shared.components.Table,
      headerBg: "rgba(242, 242, 247, 0.9)",
      headerColor: "rgba(0, 0, 0, 0.75)",
      rowHoverBg: "rgba(0, 122, 255, 0.06)",
      borderColor: "rgba(0, 0, 0, 0.06)",
    },
    Modal: {
      contentBg: "#ffffff",
      headerBg: "#ffffff",
      titleColor: "rgba(0, 0, 0, 0.88)",
    },
    Input: {
      ...shared.components.Input,
      colorBgContainer: "rgba(118, 118, 128, 0.12)",
      activeBorderColor: "#007AFF",
      hoverBorderColor: "rgba(0, 0, 0, 0.15)",
    },
    Select: {
      ...shared.components.Select,
      colorBgContainer: "rgba(118, 118, 128, 0.12)",
    },
    Drawer: { colorBgElevated: "#ffffff" },
    Card: {
      ...shared.components.Card,
      colorBgContainer: "#ffffff",
      colorBorderSecondary: "rgba(0, 0, 0, 0.06)",
    },
    Alert: {
      ...shared.components.Alert,
      colorWarningBg: "rgba(255, 149, 0, 0.1)",
      colorWarningBorder: "rgba(255, 149, 0, 0.35)",
      colorInfoBg: "rgba(0, 122, 255, 0.08)",
      colorInfoBorder: "rgba(0, 122, 255, 0.22)",
      colorErrorBg: "rgba(255, 59, 48, 0.08)",
      colorErrorBorder: "rgba(255, 59, 48, 0.28)",
    },
    Collapse: {
      ...shared.components.Collapse,
      headerBg: "transparent",
      contentBg: "transparent",
      colorBorder: "rgba(0, 0, 0, 0.06)",
    },
  },
};
