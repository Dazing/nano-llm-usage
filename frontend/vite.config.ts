import path from "node:path";
import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

// The image build sets DASHBOARD_STATIC_OUT to a staging directory that the
// runtime stage copies to /opt/static; a source checkout builds in place so the
// serve process finds the bundle next to the API it serves.
const outDir = process.env.DASHBOARD_STATIC_OUT
  ? path.resolve(process.env.DASHBOARD_STATIC_OUT)
  : path.resolve(import.meta.dirname, "../src/jarvis_dashboard/static");

export default defineConfig({
  base: "/",
  plugins: [vue()],
  build: {
    outDir,
    assetsDir: "assets",
    sourcemap: false,
    copyPublicDir: false,
    emptyOutDir: true,
  },
});
