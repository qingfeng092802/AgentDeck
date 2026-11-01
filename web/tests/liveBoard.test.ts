import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { TaskSummary } from "../src/lib/acp";
import { mapTask, planOf } from "../src/lib/live";
import { adoptTasks, getStudio, selectTask, setLive, setSource } from "../src/lib/studioEngine";
import { BoardScreen } from "../src/components/studio/BoardScreen";

/** 渲染级判据：映射层单独绿了不代表屏幕上画得出来。
 *  这一条喂两张真形状的卡给看板，断"标题上屏、状态上屏、右栏不炸"三件事。 */

function row(over: Partial<TaskSummary>): TaskSummary {
  return {
    id: "T01",
    feature_request_id: "11111111-2222-3333-4444-555555555555",
    project_key: "real_demo",
    request_status: "SPLIT",
    request_created_at: "2026-10-27T10:00:00",
    title: "搭首页骨架",
    status: "RUNNING",
    depends_on: [],
    executor_agent_id: "qoder",
    reviewer_agent_id: "deepseek-reviewer",
    acceptor_agent_id: "workbuddy",
    difficulty: 2,
    skill_tags: ["fe"],
    handoff_count: 0,
    acceptance_criteria: ["首页含导航与页脚"],
    family: "build",
    artifacts: [],
    ...over,
  };
}

describe("看板渲染真任务", () => {
  it("两张卡能画出 DAG、标题与状态词，右栏不抛", () => {
    setSource("live");
    const rows = [
      row({ id: "T01", title: "搭首页骨架", status: "DELIVERED" }),
      row({ id: "T02", title: "写响应式样式", status: "RUNNING", depends_on: ["T01"] }),
    ];
    const tasks = rows.map(mapTask);
    adoptTasks(tasks, 2, planOf(rows, "real_demo", rows[0].feature_request_id));
    setLive({ status: "live", updatedAt: "10:32:05" });
    selectTask("T02");

    const html = renderToStaticMarkup(createElement(BoardScreen, { studio: getStudio() }));

    expect(html).toContain("搭首页骨架");
    expect(html).toContain("写响应式样式");
    expect(html).toContain("执行中");
    // 依赖边真的连了：T01 → T02
    expect(html).toContain('data-e="T01&gt;T02"');
  });

  it("没有任务时不画节点，也不伪造一张空卡", () => {
    setSource("live");
    adoptTasks([], 0, null);
    const html = renderToStaticMarkup(createElement(BoardScreen, { studio: getStudio() }));
    expect(html).not.toContain("class=\"node");
  });
});
