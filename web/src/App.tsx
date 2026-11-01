import { useEffect, useState } from "react";
import { ToastHost } from "./components/ToastHost";
import { AppShell } from "./shell/AppShell";

/** 主题的两档。键与 `index.html` 的内联脚本共用（脚本不能 import，所以那个字面量在两处各写一次，
 *  判据按跨文件对读钉住；漂开的表现是"刷新后主题回到默认"，而两边各自都自洽）。 */
type Theme = "dark" | "light";
const THEME_KEY = "acp-theme";

/** 初值从 FOUC 脚本已经写好的属性读回来：写死 `"dark"` 会把浅色用户的选择在 React 挂载时覆盖掉。 */
function initialTheme(): Theme {
  if (!("document" in globalThis)) return "dark";
  return document.documentElement.dataset.theme === "light" ? "light" : "dark";
}

/** 壳只做一件事：持有本机偏好（主题）。
 *
 *  2026-10-27 起它不再持有身份、视图与快照：
 *  - 身份（principal）原本只服务审批与运行时保存，治理台整页退役后没有写操作可写；
 *  - 视图只剩一档，`#studio` 是唯一合法 hash，不需要一个 state 去记它；
 *  - 快照（`useSnapshot`）是后端读面，而这一版工作室是纯前端推演，读它只会读到空。
 *  三样东西各自的注释留在被删的位置说明原因，避免下一个人以为是漏了。
 */
export function App() {
  const [theme, setTheme] = useState<Theme>(initialTheme);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    // 内联 colorScheme 是首帧的兜底；切换后必须同步，否则它会把 CSS 里的 color-scheme 盖住。
    document.documentElement.style.colorScheme = theme;
    try {
      localStorage.setItem(THEME_KEY, theme);
    } catch {
      // 隐私模式下写不进去：主题仍在这一个会话里生效，只是刷新后回到默认。
    }
  }, [theme]);

  return (
    <>
      <AppShell theme={theme} onThemeToggle={() => setTheme(theme === "light" ? "dark" : "light")} />
      <ToastHost />
    </>
  );
}
