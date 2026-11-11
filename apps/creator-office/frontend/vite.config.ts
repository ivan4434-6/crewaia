import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// In development the API runs on its own port; the browser only ever talks to
// this origin and Vite forwards /api to the backend.
const backend = process.env.VITE_BACKEND_URL ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    port: 5173,
    allowedHosts: true,
    proxy: { "/api": { target: backend, changeOrigin: false } },
  },
  build: { outDir: "dist", sourcemap: false, target: "es2020" },
  test: { environment: "node", include: ["src/**/*.test.ts"] },
});
