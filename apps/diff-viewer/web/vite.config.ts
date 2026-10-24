import { defineConfig } from "vite";

// The dev bridge (`dv-serve`) answers the same calls the Tauri app makes in-process.
export default defineConfig({
  server: { proxy: { "/api": "http://127.0.0.1:5274" } },
});
