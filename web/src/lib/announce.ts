import { useEffect, useRef, useState } from "react";
import { staleOf } from "./live";
import { studioVersion, type StudioState, type StudioTask, type TaskStatus } from "./studioEngine";

/** 一次状态快照：六档 status + 正交的"失效"原因。
 *  用快照而不是直接比 `studio.tasks`：引擎是**就地改**任务的，数组引用不变，
 *  React 的依赖比较看不见变化（理由同 `useStudio` 取版本号）。 */
export interface TaskStateSnapshot {
  readonly status: ReadonlyMap<string, TaskStatus>;
  readonly stale: ReadonlyMap<string, string>;
}

function clipTitle(title: string): string {
  return title.length > 12 ? `${title.slice(0, 12)}…` : title;
}

export function snapshotOf(studio: StudioState): TaskStateSnapshot {
  const status = new Map<string, TaskStatus>();
  const stale = new Map<string, string>();
  for (const task of studio.tasks) {
    if (task.hidden) continue;
    status.set(task.id, task.status);
    const reading = staleOf(task, studio.artifacts, studio.live.evidenceStale);
    if (reading) stale.set(task.id, reading.reason);
  }
  return { status, stale };
}

/** 只认四个转折点：开始 / 完成 / 停下 / 过期。首次看见的卡不算"开始" ——
 *  刷新页面接回一单时，屏上本来就在跑，播一句"开始了"是假话。 */
export function diffTaskStates(
  prev: TaskStateSnapshot,
  tasks: readonly StudioTask[],
  next: TaskStateSnapshot,
): string[] {
  const said: string[] = [];
  for (const task of tasks) {
    if (task.hidden) continue;
    const before = prev.status.get(task.id);
    if (before === undefined) continue;
    if (before !== task.status) {
      if (task.status === "run") said.push(`${task.id} 开始 · ${clipTitle(task.title)}`);
      else if (task.status === "done") said.push(`${task.id} 完成 · ${clipTitle(task.title)}`);
      else if (task.status === "fail") said.push(`${task.id} 停下 · ${clipTitle(task.title)}`);
    }
    const staleReason = next.stale.get(task.id);
    if (staleReason && !prev.stale.has(task.id)) {
      said.push(`${task.id} 过期 · ${staleReason}`);
    }
  }
  return said;
}

/** 2s 尾沿合并：窗口内只保留**最后一次**事件，既不刷屏，也不丢最后一条。 */
export function useAnnouncement(studio: StudioState): string {
  const [message, setMessage] = useState("");
  const version = studioVersion();
  const prev = useRef<TaskStateSnapshot | null>(null);
  const prevStage = useRef<string | null>(null);
  const pending = useRef<string | null>(null);
  const timer = useRef<number | null>(null);

  useEffect(() => {
    const next = snapshotOf(studio);
    if (!prev.current) {
      prev.current = next;
      prevStage.current = studio.live.stage;
      return;
    }
    const events = diffTaskStates(prev.current, studio.tasks, next);
    if (studio.live.stage !== prevStage.current) {
      if (studio.live.stage === "stalled") events.push("这一单停住了");
      else if (studio.live.stage === "needs_human") events.push("这一单在等人");
      prevStage.current = studio.live.stage;
    }
    prev.current = next;

    const latest = events[events.length - 1];
    if (!latest) return;
    pending.current = latest;
    if (timer.current !== null) return;
    timer.current = window.setTimeout(() => {
      timer.current = null;
      const text = pending.current;
      pending.current = null;
      if (text) setMessage(text);
    }, 2000);
  }, [version, studio]);

  useEffect(
    () => () => {
      if (timer.current !== null) window.clearTimeout(timer.current);
    },
    [],
  );

  return message;
}
