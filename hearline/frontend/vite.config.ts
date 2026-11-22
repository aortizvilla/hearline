import { cpSync, existsSync, readFileSync } from "node:fs";
import { extname, join, normalize } from "node:path";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv, type Plugin } from "vite";

// Two apps from one codebase: the product (index.html) and the admin dashboard (admin.html).
// In production CloudFront serves admin.html on admin.<domain>; locally this does the same
// for http://admin.localhost:5173 (browsers resolve *.localhost to this machine).
function adminHost(): Plugin {
  return {
    name: "admin-host",
    configureServer(server) {
      server.middlewares.use((req, _res, next) => {
        const page = req.url && !req.url.includes(".") && !/^\/(api|audio|@|src|node_modules)/.test(req.url);
        if (page && req.headers.host?.startsWith("admin.")) req.url = "/admin.html";
        next();
      });
    },
  };
}

// Static demo (VITE_STATIC_DEMO=1, Cloudflare Pages): the snapshot in demo-data/ (made by the
// backend's `app.cli export-demo`) is served at <base>demo/ in dev and copied into dist/demo.
// It stays out of public/, so normal builds never ship the demo listeners.
const DEMO_DIR = "demo-data";
const TYPES: Record<string, string> = { ".json": "application/json", ".mp3": "audio/mpeg" };
function demoData(enabled: boolean, base: string): Plugin {
  let outDir = "dist";
  return {
    name: "demo-data",
    configResolved(config) {
      outDir = config.build.outDir;
    },
    configureServer(server) {
      if (!enabled) return;
      server.middlewares.use(`${base}demo`, (req, res, next) => {
        const path = normalize(decodeURIComponent((req.url ?? "/").split("?")[0]));
        const file = join(DEMO_DIR, path);
        if (path.startsWith("..") || !existsSync(file)) return next();
        res.setHeader("Content-Type", TYPES[extname(file)] ?? "application/octet-stream");
        res.end(readFileSync(file));
      });
    },
    closeBundle() {
      if (enabled) cpSync(DEMO_DIR, join(outDir, "demo"), { recursive: true });
    },
  };
}

// HEARLINE_API: the backend the dev server forwards /api and /audio to (default :8000), e.g.
// HEARLINE_API=http://localhost:8001 npm run dev -- --port 5174 for the demo store
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "");
  const api = env.HEARLINE_API || "http://localhost:8000";
  const base = env.VITE_BASE || "/";
  return {
  // VITE_BASE: the path the site is served under, if not the root (default "/")
  base,
  plugins: [react(), tailwindcss(), adminHost(), demoData(env.VITE_STATIC_DEMO === "1", base)],
  build: {
    rollupOptions: { input: { main: "index.html", admin: "admin.html" } },
  },
  server: {
    port: 5173,
    proxy: { "/api": api, "/audio": api },
  },
  };
});
