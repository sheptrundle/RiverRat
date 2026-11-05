import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Inside docker compose the API is reachable at http://api:8000; on the host it's localhost.
const apiTarget = process.env.API_PROXY_TARGET ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  // The map page (MapLibre, about 250 kB gzipped) loads on its own, only when opened.
  build: { chunkSizeWarningLimit: 1000 },
  server: {
    proxy: { "/api": apiTarget },
    // File events don't cross Docker Desktop's bind mount reliably on macOS, so poll there.
    watch: process.env.WATCH_POLL ? { usePolling: true, interval: 300 } : undefined,
  },
});
