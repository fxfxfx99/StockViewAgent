import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

/** 开发时：浏览器只访问 Vite 端口；/api 转发到后端（默认 8001，与 scripts/dev.sh 一致）。 */
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const backend = (env.VITE_DEV_BACKEND_URL || "http://127.0.0.1:8001").replace(/\/$/, "");
  const port = Number.parseInt(env.VITE_DEV_PORT || "5175", 10) || 5175;

  return {
    plugins: [react()],
    server: {
      host: "0.0.0.0",
      port,
      strictPort: true,
      proxy: {
        "/api": {
          target: backend,
          changeOrigin: true,
          timeout: 900_000,
          proxyTimeout: 900_000,
        },
      },
    },
  };
});
