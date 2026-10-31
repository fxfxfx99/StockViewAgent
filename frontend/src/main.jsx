import React, { lazy, Suspense } from "react";
import { Spin } from "antd";
import ReactDOM from "react-dom/client";
import { QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Routes, Route } from "react-router-dom";
import App from "./App.jsx";
import { SetupPage } from "./ConfigConsole.jsx";
import { AuthProvider } from "./AuthContext.jsx";
import AuthGate from "./AuthGate.jsx";
import { queryClient } from "./queryClient.js";
import { ThemeModeProvider } from "./ThemeModeProvider.jsx";
import { ErrorBoundary } from "./ErrorBoundary.jsx";
import "./index.css";
import "./macos.css";

const ResearchWorkspace = lazy(() => import("./ResearchWorkspace.jsx"));
const ResearchLab = lazy(() => import("./ResearchLab.jsx"));
const EconomicCalendar = lazy(() => import("./EconomicCalendar.jsx"));
const DataCenter = lazy(() => import("./DataCenter.jsx"));
const TodayPage = lazy(() => import("./TodayPage.jsx"));
const OverseasNews = lazy(() => import("./OverseasNews.jsx"));
const MacroWorkspace = lazy(() => import("./MacroWorkspace.jsx"));
const MonitoringWorkspace = lazy(() => import("./MonitoringWorkspace.jsx"));
const KlineReplay = lazy(() => import("./KlineReplay.jsx"));
const MerrillClock = lazy(() => import("./MerrillClock.jsx"));

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <ThemeModeProvider>
          <AuthProvider>
            <ErrorBoundary>
              <AuthGate>
                <Routes>
                  <Route path="/today" element={<Suspense fallback={<div className="sva-boot"><Spin size="large" aria-label="加载今日变化" /></div>}><TodayPage /></Suspense>} />
                  <Route path="/research" element={<Suspense fallback={<div className="sva-boot"><Spin size="large" aria-label="加载研究工作区" /></div>}><ResearchWorkspace /></Suspense>} />
                  <Route path="/lab" element={<Suspense fallback={<div className="sva-boot"><Spin size="large" aria-label="加载量化研究实验室" /></div>}><ResearchLab /></Suspense>} />
                  <Route path="/research-lab" element={<Suspense fallback={<Spin />}><ResearchLab /></Suspense>} />
                  <Route path="/calendar" element={<Suspense fallback={<div className="sva-boot"><Spin size="large" aria-label="加载全球经济日历" /></div>}><EconomicCalendar /></Suspense>} />
                  <Route path="/news" element={<Suspense fallback={<div className="sva-boot"><Spin size="large" aria-label="加载海外快讯" /></div>}><OverseasNews /></Suspense>} />
                  <Route path="/macro" element={<Suspense fallback={<div className="sva-boot"><Spin size="large" aria-label="加载市场研判" /></div>}><MacroWorkspace /></Suspense>} />
                  <Route path="/merrill-clock" element={<Suspense fallback={<div className="sva-boot"><Spin size="large" aria-label="加载美林时钟" /></div>}><MerrillClock /></Suspense>} />
                  <Route path="/monitor" element={<Suspense fallback={<div className="sva-boot"><Spin size="large" aria-label="加载观察与复盘" /></div>}><MonitoringWorkspace /></Suspense>} />
                  <Route path="/replay" element={<Suspense fallback={<div className="sva-boot"><Spin size="large" aria-label="加载 K 线复盘" /></div>}><KlineReplay /></Suspense>} />
                  <Route path="/data" element={<Suspense fallback={<div className="sva-boot"><Spin size="large" aria-label="加载本地数据中心" /></div>}><DataCenter /></Suspense>} />
                  <Route path="/setup" element={<SetupPage />} />
                  <Route path="/*" element={<App />} />
                </Routes>
              </AuthGate>
            </ErrorBoundary>
          </AuthProvider>
        </ThemeModeProvider>
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>
);
