import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// 后端的本机端口与 `src/acp/serve.py::DEFAULT_PORT` 是同一个数，不是两份配置。
// 判据 = tests/guards/test_board_frontend.py（从这里正则取 BACKEND，与 Python 侧常量比）。
// 改一边而没改另一边时，dev 档的所有请求都会 404/拒连，而 build 出来的同源页面照样能跑 ——
// 那种"只有开发时坏"的漂移最难查，所以它被钉成一条判据而不是一句注释。
const BACKEND = "http://127.0.0.1:8765";

// dev 服务器端口刻意不用 5173：owner 本机另有前端常驻在 5173/8001（`strictPort` 会直接拒起而不是漂到下一个端口）。
const BOARD_PORT = 5178;

// 公开面上的根路径全集（本仓的 router 全部裸挂、无 URL 前缀）。
// 漏一条的后果是"页面里那个面板 dev 时永远加载不出来，而生产同源时好好的" ——
// 同一判据也数它：`API_ROOTS` 必须覆盖 `/openapi.json` 里所有路径的第一段。
const API_ROOTS = [
  "/autonomy",
  "/approvals",
  "/events",
  "/growth",
  "/requests",
  "/runtime",
  "/tasks",
];

export default defineConfig({
  base: "/",
  plugins: [react(), tailwindcss()],
  server: {
    host: "127.0.0.1",
    port: BOARD_PORT,
    strictPort: true,
    proxy: Object.fromEntries(API_ROOTS.map((root) => [root, { target: BACKEND }])),
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
