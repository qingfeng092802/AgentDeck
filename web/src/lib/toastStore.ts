import { useSyncExternalStore } from "react";

/** 一条全局提示。它只由**真实结果**触发：受理回体、审批回体、评审回体、交付留痕。 */
export type ToastTone = "info" | "ok" | "warn" | "bad";

export interface ToastItem {
  readonly id: number;
  readonly tone: ToastTone;
  readonly title: string;
  readonly detail: string;
}

let items: readonly ToastItem[] = [];
let sequence = 0;
const listeners = new Set<() => void>();

function emit(): void {
  listeners.forEach((listener) => listener());
}

export function pushToast(input: { tone: ToastTone; title: string; detail?: string }): number {
  const detail = input.detail ?? "";
  /* 同一句话不许在屏上叠两张。
   *
   *  屏上已经有一条一模一样的还在读秒，第二张不会带来任何新信息，只会把下面那张卡的读数顶走。
   *  重复的来源是实的：开发档 StrictMode 会把同一次挂载的 effect 跑两遍，退役直链那两条旧链接
   *  各自发一条时也是同一种卡（2026-10-30 owner 截图：两张叠在一起）。
   *  认的是**去重而不是引用**：`{…}` 每次都是新对象，按 id/引用判永远判不出来。 */
  const onScreen = items.find(
    (item) => item.tone === input.tone && item.title === input.title && item.detail === detail,
  );
  if (onScreen) return onScreen.id;
  sequence += 1;
  const item: ToastItem = {
    id: sequence,
    tone: input.tone,
    title: input.title,
    detail,
  };
  items = [...items.slice(-3), item];
  emit();
  return item.id;
}

export function dismissToast(id: number): void {
  const next = items.filter((item) => item.id !== id);
  if (next.length === items.length) return;
  items = next;
  emit();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useToasts(): readonly ToastItem[] {
  return useSyncExternalStore(subscribe, () => items, () => items);
}
