import { defineConfig } from "vitest/config";

/** 纯函数测试的跑法（owner 2026-10-21 第 A 笔：`web/tests/` 用 vitest）。
 *
 * 刻意**不挂** `vite.config.ts` 里的 React/Tailwind 插件：这一档只跑 `.ts` 纯函数，
 * 挂上插件会把一次 1 秒的单测变成一次构建（而且 Tailwind 的扫描面与这里无关）。
 * 反过来，构建面仍归 `tsconfig.json`（它只 include `src`），测试文件不进 `tsc --noEmit`。
 */
export default defineConfig({
  test: {
    include: ["tests/**/*.test.ts"],
    environment: "node",
  },
});
