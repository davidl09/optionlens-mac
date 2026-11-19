import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: { "/api": { target: "http://127.0.0.1:8473", changeOrigin: false } },
  },
  build: {
    outDir: "dist",
    chunkSizeWarningLimit: 1200,
    rollupOptions: {
      output: { manualChunks: { echarts: ["echarts/core", "echarts/charts", "echarts/components", "echarts/renderers"] } },
    },
  },
  preview: { port: 4173, strictPort: true },
});
