import { Component, StrictMode, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import "./index.css";

/** 渲染期出错时必须**在页面上说一句话**。

只 `console.error` 的兜底是静默失败的温床：本机看板经常开在别的窗口里，
页面变空白而控制台在另一个面板，使用者只会得到"它坏了但没人告诉我为什么"。
*/
class Boundary extends Component<{ children: ReactNode }, { message: string }> {
  override state = { message: "" };

  static getDerivedStateFromError(error: unknown): { message: string } {
    return { message: error instanceof Error ? `${error.name}: ${error.message}` : String(error) };
  }

  override componentDidCatch(error: unknown): void {
    // 控制台仍然留一份：它是排障时的第二现场，但不是唯一现场。
    console.error("board render failed", error);
  }

  override render(): ReactNode {
    if (this.state.message) {
      return (
        <div className="measure w-full p-6">
          <div className="glass p-6">
            <h1 className="text-lg font-semibold text-danger break-words">页面渲染失败</h1>
            <p className="mt-2 break-words text-sm text-ink-muted">
              {this.state.message}
              （刷新可以重试；持续出现请把这句发给维护者）
            </p>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}

const mount = document.getElementById("root");
if (!mount) {
  // 挂载点找不到是构建产物与 index.html 不同步，不是"用户操作能修好的错" ⇒ 明说，不静默返回。
  throw new Error("找不到挂载点 #root：index.html 与构建产物不同步");
}

createRoot(mount).render(
  <StrictMode>
    <Boundary>
      <App />
    </Boundary>
  </StrictMode>,
);
