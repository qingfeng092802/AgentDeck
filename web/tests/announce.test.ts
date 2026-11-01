import { describe, expect, it } from "vitest";
import { diffTaskStates, type TaskStateSnapshot } from "../src/lib/announce";
import type { StudioTask, TaskStatus } from "../src/lib/studioEngine";

/** 播报只认四个转折点。把"每帧都在变"的引擎状态压成"值得念一句"的事件。 */

function task(id: string, status: TaskStatus, title = `${id} 的卡`): StudioTask {
  return {
    id,
    title,
    owner: "fe",
    phase: 2,
    dur: 0,
    artifact: "app.js",
    input: "",
    act: "",
    out: "",
    status,
    progress: 0,
    round: 1,
    deps: [],
    hidden: false,
  };
}

function snapshot(status: Record<string, TaskStatus>, stale: Record<string, string> = {}): TaskStateSnapshot {
  return { status: new Map(Object.entries(status)), stale: new Map(Object.entries(stale)) };
}

describe("diffTaskStates：四个转折点，别的一句不播", () => {
  it("首次看见的卡不算「开始」——刷新接回一单时它本来就在跑", () => {
    expect(diffTaskStates(snapshot({}), [task("T01", "run")], snapshot({ T01: "run" }))).toEqual([]);
  });

  it("开始 / 完成 / 停下各一句，带卡号与标题", () => {
    const prev = snapshot({ T01: "wait", T02: "run", T03: "run" });
    const tasks = [task("T01", "run", "写 PRD"), task("T02", "done", "搭骨架"), task("T03", "fail", "交互脚本")];
    const said = diffTaskStates(prev, tasks, snapshot({ T01: "run", T02: "done", T03: "fail" }));
    expect(said).toEqual(["T01 开始 · 写 PRD", "T02 完成 · 搭骨架", "T03 停下 · 交互脚本"]);
  });

  it("ready→run 只播一次开始，不播中间的 ready", () => {
    const said = diffTaskStates(snapshot({ T01: "ready" }), [task("T01", "run")], snapshot({ T01: "run" }));
    expect(said).toEqual(["T01 开始 · T01 的卡"]);
  });

  it("失效只在新出现时播一次", () => {
    const tasks = [task("T04", "done")];
    const withStale = snapshot({ T04: "done" }, { T04: "产物不在总线上" });
    expect(diffTaskStates(snapshot({ T04: "done" }), tasks, withStale)).toEqual([
      "T04 过期 · 产物不在总线上",
    ]);
    expect(diffTaskStates(withStale, tasks, withStale)).toEqual([]);
  });

  it("隐藏卡不参与播报", () => {
    const hidden = { ...task("T09", "done"), hidden: true };
    expect(diffTaskStates(snapshot({ T09: "run" }), [hidden], snapshot({ T09: "done" }))).toEqual([]);
  });
});
