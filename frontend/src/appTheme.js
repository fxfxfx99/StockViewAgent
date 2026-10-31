import { theme } from "antd";

export const THEME_STORAGE_KEY = "sva_theme_mode";

const shared = {
  token: {
    colorPrimary: "#007aff", colorSuccess: "#20854b", colorInfo: "#007aff",
    colorWarning: "#b77813", colorError: "#d94b48",
    borderRadius: 8, borderRadiusLG: 12, borderRadiusSM: 6,
    fontFamily: '-apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC", "Segoe UI", sans-serif',
    fontSize: 14, fontSizeSM: 12, fontSizeLG: 16, lineHeight: 1.65,
    fontSizeHeading1: 28, fontSizeHeading2: 24, fontSizeHeading3: 20,
    fontSizeHeading4: 18, fontSizeHeading5: 16,
    controlHeight: 36, controlHeightSM: 32, controlHeightLG: 40,
    motionDurationMid: "0.18s",
  },
  components: {
    Layout: { headerHeight: 60, headerPadding: "0 24px", bodyBg: "transparent" },
    Card: { headerFontSize: 16, headerFontSizeSM: 16, paddingLG: 20, paddingSM: 16, headerHeight: 56, headerHeightSM: 52 },
    Tabs: { titleFontSize: 14, titleFontSizeSM: 13, horizontalItemGutter: 24 },
    Input: { activeShadow: "0 0 0 3px rgba(0, 122, 255, 0.12)" },
    Select: { optionSelectedFontWeight: 500 },
    DatePicker: { cellWidth: 36, cellHeight: 32 },
    Button: { primaryShadow: "0 1px 2px rgba(0, 91, 195, 0.12)", fontWeight: 500 },
    Segmented: { trackPadding: 3, itemSelectedBg: "#ffffff" },
    Collapse: { headerBg: "transparent", contentBg: "transparent" },
    Alert: { borderRadiusLG: 8 },
    Tag: { borderRadiusSM: 5 },
    Descriptions: { borderRadiusLG: 8 },
    Form: { itemMarginBottom: 20, verticalLabelPadding: "0 0 8px", labelFontSize: 14 },
    Table: { cellFontSize: 13, cellFontSizeMD: 13, cellFontSizeSM: 13, cellPaddingBlock: 14, cellPaddingBlockMD: 12, cellPaddingBlockSM: 10, cellPaddingInlineSM: 12 },
  },
};

export const appLightTheme = {
  algorithm: theme.defaultAlgorithm,
  ...shared,
  token: {
    ...shared.token,
    colorBgBase: "#ffffff", colorBgLayout: "#f5f6f8", colorBgContainer: "#ffffff", colorBgElevated: "#ffffff",
    colorBorder: "#d4d9e1", colorBorderSecondary: "#e4e7ec",
    colorText: "#1d2027", colorTextSecondary: "#626c7e", colorTextTertiary: "#7a8496", colorTextQuaternary: "#939baa",
    colorFillAlter: "#f7f8fa", colorFillTertiary: "#f0f2f5",
    boxShadow: "0 4px 24px rgba(27, 39, 60, 0.10)",
    boxShadowSecondary: "0 12px 40px rgba(27, 39, 60, 0.13)",
  },
  components: {
    ...shared.components,
    Layout: { ...shared.components.Layout, headerBg: "#f8f9fb" },
    Tabs: { ...shared.components.Tabs, itemColor: "#626c7e", itemHoverColor: "#1d2027" },
    Table: { ...shared.components.Table, headerBg: "#f7f8fa", headerColor: "#626c7e", headerSplitColor: "transparent", rowHoverBg: "#f5f8fd", borderColor: "#e4e7ec" },
    Input: { ...shared.components.Input, colorBgContainer: "#ffffff", hoverBorderColor: "#b5c2d4" },
    Select: { ...shared.components.Select, colorBgContainer: "#ffffff", optionSelectedBg: "#e4eeff" },
    Segmented: { ...shared.components.Segmented, trackBg: "#f0f2f5", itemColor: "#626c7e", itemSelectedColor: "#1d2027" },
    Drawer: { colorBgElevated: "#f7f8fa" },
    Card: { ...shared.components.Card, colorBgContainer: "#ffffff", colorBorderSecondary: "#e4e7ec" },
    Alert: { ...shared.components.Alert, colorWarningBg: "#fff9ec", colorWarningBorder: "#eed9ae", colorInfoBg: "#f0f6ff", colorInfoBorder: "#d2e3fd", colorErrorBg: "#fff3f2", colorErrorBorder: "#f2d3d0" },
  },
};

export const appDarkTheme = {
  algorithm: theme.darkAlgorithm,
  ...shared,
  token: {
    ...shared.token,
    colorPrimary: "#419cff", colorSuccess: "#57ca85", colorInfo: "#419cff", colorWarning: "#e7b96b", colorError: "#ff6b67",
    colorBgBase: "#202226", colorBgLayout: "#202226", colorBgContainer: "#282a2f", colorBgElevated: "#33363c",
    colorBorder: "#484c55", colorBorderSecondary: "#393c43",
    colorText: "#eef0f5", colorTextSecondary: "#b0b6c3", colorTextTertiary: "#929aa9", colorTextQuaternary: "#737c8a",
    colorFillAlter: "#2d3036", colorFillTertiary: "#34373e",
    boxShadow: "0 4px 24px rgba(0, 0, 0, 0.26)",
    boxShadowSecondary: "0 12px 40px rgba(0, 0, 0, 0.32)",
  },
  components: {
    ...shared.components,
    Layout: { ...shared.components.Layout, headerBg: "#282a2f" },
    Tabs: { ...shared.components.Tabs, itemColor: "#b0b6c3", itemHoverColor: "#eef0f5", itemSelectedColor: "#419cff", inkBarColor: "#419cff" },
    Table: { ...shared.components.Table, headerBg: "#2d3036", headerColor: "#b0b6c3", headerSplitColor: "transparent", rowHoverBg: "#303945", borderColor: "#393c43", colorBgContainer: "transparent" },
    Input: { ...shared.components.Input, colorBgContainer: "#2d3036", activeBorderColor: "#419cff", hoverBorderColor: "#626b7a" },
    Select: { ...shared.components.Select, colorBgContainer: "#2d3036", optionSelectedBg: "#31445c" },
    Segmented: { ...shared.components.Segmented, trackBg: "#202226", itemSelectedBg: "#4a4e57", itemColor: "#b0b6c3", itemSelectedColor: "#ffffff" },
    Modal: { contentBg: "#30333a", headerBg: "#30333a" },
    Drawer: { colorBgElevated: "#282a2f" },
    Card: { ...shared.components.Card, colorBgContainer: "#282a2f", colorBorderSecondary: "#393c43" },
  },
};
