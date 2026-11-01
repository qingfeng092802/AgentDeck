import { useState } from "react";
import { SettingsModal } from "../components/SettingsModal";
import { StudioView } from "../views/StudioView";
import { TopBar } from "./TopBar";

export interface AppShellProps {
  readonly theme: "dark" | "light";
  readonly onThemeToggle: () => void;
}

/** 应用壳：顶栏 + 工作室 + 设置弹窗。
 *
 *  根节点挂 `.studio-root` —— 这一版整套视觉语法来自 `styles/studio.css`（从原型样式表搬来的），
 *  而那份样式表的每一条选择器都以 `.studio-root` 打头。挂在壳上而不是视图上，是因为顶栏
 *  也要用同一套 `.top` / `.chip` / `.phase-pill`：原型里顶栏和左右两栏本来就是一张屏。
 *
 *  这里不再有"哪个视图在 DOM 里"这条分支：只剩一档，非当前视图不构造这句话变成了
 *  "工作室的两段（工作室 / 执行看板）二选一"，那条判断留在 `StudioView` 里。
 */
export function AppShell({ theme, onThemeToggle }: AppShellProps) {
  const [settingsOpen, setSettingsOpen] = useState(false);

  return (
    <div className="studio-root">
      <TopBar theme={theme} onThemeToggle={onThemeToggle} onOpenSettings={() => setSettingsOpen(true)} />
      <StudioView />
      <SettingsModal open={settingsOpen} onClose={() => setSettingsOpen(false)} />
    </div>
  );
}
