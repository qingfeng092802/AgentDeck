/** P2-10：render 必须是纯的 —— 它只读 state，不写任何东西。
 *
 *  `dagGeometry()` 之前被 `BoardScreen` 在 render 体里直接调用，而它会把 `gr/row/gx/gy`
 *  写进任务对象。那是一条渲染期副作用：同一份 props 渲染两次会得到两份不同的对象状态，
 *  并发渲染下甚至可能画出上一次的坐标。
 *
 *  修法不是"用 `useMemo` 包起来" —— `useMemo` 只是让它**少跑几次**，跑的时候照样写。
 *  真正的分界是"算"与"写"两件事分开：纯的那半（`computeDagLayout`）render 随便调，
 *  写回的那半（`dagGeometry`）只在任务集合变了之后调。
 */

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import type { TaskSummary } from "../src/lib/acp";
import { mapTask } from "../src/lib/live";
import { adoptTasks, computeDagLayout, getStudio, type StudioTask } from "../src/lib/studioEngine";

const SRC = resolve(__dirname, "../src");

/** 剥掉注释再扫：**解释为什么不用 `dagGeometry()` 的那段注释里当然会提到这个名字**，
 *  文本直搜会因为"注释里提了一句"而红 —— 那是假红，会逼着人把解释删掉。
 *  这里只留真正的代码字符（`/* … */` 与 `//…` 都剥掉）。 */
function codeOf(path: string): string {
  const raw = readFileSync(path, "utf8");
  return raw.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/[^\n]*/g, "");
}

/** 与 `live.test.ts` 那个同形的最小后端行。**只填 `mapTask()` 真正读的字段**，
 *  其余留默认 —— 少填一个字段在映射层是静默的（会变成 undefined），而这组判据要的
 *  恰恰是"对象里到底有没有被写东西"。 */
function task(over: Partial<TaskSummary> = {}): TaskSummary {
  return {
    id: "T01",
    feature_request_id: "11111111-2222-3333-4444-555555555555",
    project_key: "real_demo",
    request_status: "SPLIT",
    request_created_at: "2026-10-27T10:00:00",
    title: "搭首页骨架",
    status: "READY",
    depends_on: [],
    executor_agent_id: null,
    reviewer_agent_id: null,
    acceptor_agent_id: null,
    difficulty: 2,
    skill_tags: ["fe"],
    handoff_count: 0,
    acceptance_criteria: ["AC-1"],
    family: "build",
    artifacts: [],
    ...over,
  } as TaskSummary;
}

function studioTasks(): StudioTask[] {
  return [
    mapTask(task({ id: "T01" })),
    mapTask(task({ id: "T02", depends_on: ["T01"], family: "gate" })),
  ];
}

describe("computeDagLayout：只算不写", () => {
  it("调用前后入参一个字段都没变（纯函数的唯一判据）", () => {
    const tasks = studioTasks();
    const before = JSON.stringify(tasks);
    const layout = computeDagLayout(tasks);
    expect(JSON.stringify(tasks)).toBe(before);
    expect(layout.positions.size).toBe(2);
    expect(layout.width).toBeGreaterThan(0);
    expect(layout.height).toBeGreaterThan(0);
  });

  it("位置只落在返回的那张表里，不落在任务对象上", () => {
    const tasks = studioTasks();
    const layout = computeDagLayout(tasks);
    expect(tasks.every((task) => task.gx === undefined && task.gy === undefined)).toBe(true);
    for (const [id, spot] of layout.positions) {
      expect(Number.isFinite(spot.gx)).toBe(true);
      expect(Number.isFinite(spot.gy)).toBe(true);
      expect(id).toBeTruthy();
    }
  });

  it("依赖层号仍然算出先后：T01 在 T02 之前", () => {
    const layout = computeDagLayout(studioTasks());
    expect(layout.positions.get("T01")?.gr).toBe(0);
    expect(layout.positions.get("T02")?.gr).toBe(1);
    expect(layout.maxRank).toBe(1);
  });
});

describe("dagGeometry：写回那一半只在 state 变更后调", () => {
  it("adoptTasks 之后任务对象上就有位置，state.dag 也有了", () => {
    const tasks = studioTasks();
    adoptTasks(tasks, 2, null);
    expect(tasks.every((task) => Number.isFinite(task.gx ?? Number.NaN))).toBe(true);
    expect(getStudio().dag.maxRank).toBe(1);
    expect(getStudio().dag.visible.length).toBe(2);
  });

  it("render 读到的那份就是已经算好的，不需要再算一次", () => {
    const studio = getStudio();
    expect(JSON.stringify(studio.dag)).toBe(JSON.stringify(studio.dag)); // 引用即可，不触发写入
    expect(studio.dag.visible[0]?.gx).toBe(studio.dag.visible[0]?.gx);
  });
});

describe("组件源码：render 里不许再出现 dagGeometry(", () => {
  for (const name of ["BoardScreen.tsx", "StudioScreen.tsx", "SidePanel.tsx", "VirtualFeed.tsx"]) {
    it(`${name} 不再调用 dagGeometry()`, () => {
      expect(codeOf(resolve(SRC, "components/studio", name))).not.toContain("dagGeometry(");
    });
  }

  it("写回的那半只在引擎里被调用（引擎是唯一允许写 state 的地方）", () => {
    const engine = codeOf(resolve(SRC, "lib/studioEngine.ts"));
    expect(engine).toContain("export function dagGeometry()");
    // 四个写入点：adoptTasks / setSource / 推演起跑 / tick
    const calls = engine.match(/^\s*dagGeometry\(\);$/gm) ?? [];
    expect(calls.length).toBeGreaterThanOrEqual(3);
  });
});
