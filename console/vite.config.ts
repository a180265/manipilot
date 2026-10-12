import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Arm Fleet 控制台使用 5174；WX 业务前端使用 5173。/v1 代理到 Arm Fleet 后端 8788（仅本机）
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5174,
    host: "127.0.0.1",
    proxy: {
      "/v1": { target: "http://127.0.0.1:8788", changeOrigin: true },
    },
  },
});
