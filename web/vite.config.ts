import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// `tt ui` serves the built client from src/toolbelt/ui/static/dist (see
// app.py); `pnpm dev` runs this dev server instead, proxying the API to a
// running `tt ui` (TT_UI_PORT, default 8765).
const api = `http://127.0.0.1:${process.env.TT_UI_PORT ?? "8765"}`;

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../src/toolbelt/ui/static/dist",
    emptyOutDir: true,
    sourcemap: true,
  },
  server: {
    proxy: { "/api": api },
  },
  test: {
    environment: "jsdom",
    // Days, weeks and weekends are the viewer's time zone's: tests that do not
    // pass one see UTC (the ones about zones pass theirs explicitly).
    env: { TZ: "UTC" },
    setupFiles: ["./src/testing/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
