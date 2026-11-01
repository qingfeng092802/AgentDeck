import { useEffect } from "react";
import { dismissToast, useToasts, type ToastItem } from "../lib/toastStore";

const TONE_CLASS: Record<ToastItem["tone"], { box: string; title: string }> = {
  info: { box: "border-edge", title: "text-ink" },
  ok: { box: "border-ok", title: "text-ok" },
  warn: { box: "border-warn", title: "text-warn" },
  bad: { box: "border-danger", title: "text-danger" },
};

function ToastCard({ toast }: { toast: ToastItem }) {
  useEffect(() => {
    const handle = window.setTimeout(() => dismissToast(toast.id), 7_000);
    return () => window.clearTimeout(handle);
  }, [toast.id]);

  const tone = TONE_CLASS[toast.tone];
  return (
    <div
      role="status"
      className={`pointer-events-auto flex w-full max-w-xl items-start gap-3 rounded-[2px] border bg-panel px-4 py-3 shadow-lg ${tone.box}`}
    >
      <div className="min-w-0 flex-1">
        <p className={`text-sm font-semibold break-words ${tone.title}`}>{toast.title}</p>
        {toast.detail ? <p className="mt-1 text-xs break-words text-ink-muted">{toast.detail}</p> : null}
      </div>
      <button
        type="button"
        aria-label="关闭提示"
        onClick={() => dismissToast(toast.id)}
        className="shrink-0 whitespace-nowrap rounded-[2px] border border-edge px-2 py-2 text-xs text-ink-muted hover:border-accent hover:text-accent"
      >
        关闭
      </button>
    </div>
  );
}

/** 全局提示层：只显示 store 里由真实回体/真实留痕推进来的那几条。 */
export function ToastHost() {
  const toasts = useToasts();
  if (toasts.length === 0) return null;
  return (
    <div
      className="pointer-events-none fixed inset-x-0 bottom-6 z-50 flex flex-col items-center gap-2 px-4"
      aria-live="polite"
      aria-atomic="false"
    >
      {toasts.map((toast) => (
        <ToastCard key={toast.id} toast={toast} />
      ))}
    </div>
  );
}
