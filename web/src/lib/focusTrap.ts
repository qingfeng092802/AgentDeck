import { useEffect, type RefObject } from "react";

/** 模态焦点陷阱：打开时把焦点收进对话框，Tab/Shift+Tab 在框内循环，关闭时还给打开它的那颗按钮。
 *
 *  为什么必须有：模态只是视觉上"盖住"了背景，键盘焦点并不会自己停下 —— 不设陷阱时，
 *  用户按 Tab 会一路走到背后的画布与队列上，而屏幕看起来还停在弹窗里（读屏更糟：
 *  焦点跑出去之后，屏上那段"看不见的"内容会被念出来）。
 *
 *  焦点先给容器本身（对话框要带 `tabIndex={-1}`）：第一个可聚焦元素常是右上角的"关闭"或遮罩，
 *  直接聚焦它会让读屏先念"关闭对话框"，而不是先念这段是什么。
 */
export function useFocusTrap(ref: RefObject<HTMLElement | null>, active: boolean): void {
  useEffect(() => {
    if (!active) return undefined;
    const node = ref.current;
    if (!node) return undefined;
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    node.focus({ preventScroll: true });

    const selector =
      'a[href],button:not([disabled]),input:not([disabled]),textarea:not([disabled]),select:not([disabled]),[tabindex]:not([tabindex="-1"])';

    const onKey = (event: KeyboardEvent): void => {
      if (event.key !== "Tab") return;
      // `offsetParent === null` 过滤掉 display:none 的分支（详情区里那些折叠块）
      const items = [...node.querySelectorAll<HTMLElement>(selector)].filter(
        (element) => element.offsetParent !== null,
      );
      if (items.length === 0) {
        event.preventDefault();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      const current = document.activeElement;
      if (event.shiftKey && (current === first || current === node)) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && current === last) {
        event.preventDefault();
        first.focus();
      }
    };

    node.addEventListener("keydown", onKey);
    return () => {
      node.removeEventListener("keydown", onKey);
      // 还原焦点要**等这一帧的卸载做完**：React 先摘 DOM 再跑清理，被聚焦的那个节点一摘掉，
      // 浏览器就把焦点丢给 body —— 同步 focus() 会被这一步覆盖掉（实测 activeElement 落在 BODY）。
      window.setTimeout(() => {
        if (opener && document.contains(opener)) opener.focus({ preventScroll: true });
      }, 0);
    };
  }, [ref, active]);
}
