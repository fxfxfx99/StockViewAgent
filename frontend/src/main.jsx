import React from "react";
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

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <ThemeModeProvider>
          <AuthProvider>
            <ErrorBoundary>
              <AuthGate>
                <Routes>
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
