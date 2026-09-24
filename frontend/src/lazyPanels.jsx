import { lazy } from "react";

/** 单页主流程懒加载 chunk */
export const StockKlinePanelLazy = lazy(() => import("./StockKlinePanel.jsx"));
export const TransactionAgentPanelLazy = lazy(() => import("./TransactionAgentPanel.jsx"));
export const NewsInterpretationPanelLazy = lazy(() => import("./NewsInterpretationPanel.jsx"));
export const XueqiuCommentsPanelLazy = lazy(() => import("./XueqiuCommentsPanel.jsx"));
