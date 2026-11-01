import { describe, expect, it } from "vitest";
import { detailSay, staleOf, type DeliveryBlocker } from "../src/lib/live";
import { NODE_STATUS_WORDS, stopWordOf, type ArtifactRecord, type StudioTask } from "../src/lib/studioEngine";

/** 把"卡停了"翻译成"词 + 下一步"的判据。
 *  这些词会直接进 DAG 顶行（37 用户单位的槽），所以长度是几何约束，不是文案偏好。 */

function task(over: Partial<StudioTask> = {}): StudioTask {
  return {
    id: "T04",
    title: "交互脚本（购物车 +1）",
    owner: "fe",
    phase: 2,
    dur: 0,
    artifact: "app.js",
    input: "",
    act: "",
    out: "",
    status: "fail",
    progress: 0,
    round: 1,
    deps: [],
    hidden: false,
    ...over,
  };
}

function blocker(over: Partial<DeliveryBlocker> = {}): DeliveryBlocker {
  return {
    task: task({ raw: "ARTIFACT_INVALID" }),
    state: "ARTIFACT_INVALID",
    files: 0,
    rejected: [],
    failed: [],
    ...over,
  };
}

describe("stopWordOf：四种停各有自己的下一步", () => {
  it("四个后端原态分别给词、动宾与形状通道", () => {
    expect(stopWordOf(task({ raw: "ARTIFACT_INVALID" }))).toEqual({
      word: "不合格",
      action: "补文件 · 或重跑",
      signal: "block",
    });
    expect(stopWordOf(task({ raw: "REJECTED" }))).toEqual({
      word: "退回",
      action: "看判据 · 或换人",
      signal: "block",
    });
    expect(stopWordOf(task({ raw: "CHANGES_REQUESTED" }))).toMatchObject({ word: "待改", signal: null });
    expect(stopWordOf(task({ raw: "ESCALATED" }))).toMatchObject({ word: "升级", signal: null });
  });

  it("超时只认正在跑的那张卡，已完成不因整单停了就变超时", () => {
    expect(stopWordOf(task({ status: "run" }), { stalled: true })).toMatchObject({
      word: "超时",
      signal: "timeout",
    });
    expect(stopWordOf(task({ status: "done" }), { stalled: true }).word).toBe("已完成");
  });

  it("失效是正交属性：状态词仍是六档，第二行才说原因", () => {
    expect(stopWordOf(task({ status: "done", raw: "DELIVERED" }), { stale: "产物不在总线上" })).toMatchObject({
      word: "已完成",
      action: "产物不在总线上",
      signal: "stale",
    });
  });

  it("raw 缺席或不认识时退回六档，不猜", () => {
    expect(stopWordOf(task({ status: "fail", raw: "SOMETHING_NEW" })).word).toBe("失败");
    expect(stopWordOf(task({ status: "ready" })).action).toBe("就绪待起跑");
  });

  it("词表 11 项、全部 ≤3 汉字、不含「过期」", () => {
    const words = Object.values(NODE_STATUS_WORDS);
    expect(words).toHaveLength(11);
    expect(words.every((word) => word.length <= 3)).toBe(true);
    expect(words).not.toContain("过期");
  });
});

describe("staleOf：只在能指到别的卡时才说覆盖", () => {
  const artifact = (over: Partial<ArtifactRecord> = {}): ArtifactRecord => ({
    name: "app.js",
    owner: "fe",
    kind: "js",
    lines: 10,
    at: "2026-10-29T10:00:00",
    card: "T09",
    ...over,
  });

  it("同路径最后写入者是别的卡 → 被覆盖", () => {
    expect(staleOf(task({ id: "T04", status: "done", artifact: "app.js" }), [artifact()], false)).toEqual({
      reason: "被后面的卡覆盖",
      by: "T09",
    });
  });

  it("多文件串无法归因，不判覆盖", () => {
    expect(
      staleOf(task({ status: "done", artifact: "index.html / products.html" }), [artifact({ name: "index.html" })], false),
    ).toBeNull();
  });

  it("本卡名下有产物 → 正常", () => {
    expect(staleOf(task({ id: "T09", status: "done", artifact: "app.js" }), [artifact({ card: "T09" })], false)).toBeNull();
  });

  it("证据面读不到 → 不判", () => {
    expect(staleOf(task({ status: "done", artifact: "app.js" }), [artifact()], true)).toBeNull();
  });
});

describe("detailSay：首句 ≤40 字，动作与按钮一一对应", () => {
  it("产物不合格：先说事实，再给文件总线出口", () => {
    const say = detailSay({ task: task({ raw: "ARTIFACT_INVALID" }), blocker: blocker() });
    expect(say?.line.length).toBeLessThanOrEqual(40);
    expect(say).toMatchObject({ action: "bus", actionLabel: "查看文件总线", tone: "block" });
  });

  it("审查退回：带判据原文（裁切）但不带状态码", () => {
    const say = detailSay({
      task: task({ raw: "REJECTED" }),
      blocker: blocker({
        state: "REJECTED",
        files: 1,
        failed: ["表单必填项为空时拦截提交并给出就地提示，不刷新页面"],
      }),
    });
    expect(say?.line).toContain("审查退回");
    expect(say?.line).not.toContain("REJECTED");
    expect(say?.line.length).toBeLessThanOrEqual(40);
    expect(say?.action).toBe("review");
  });

  it("超时：给分钟数，给留痕出口", () => {
    const say = detailSay({ task: task({ status: "run" }), stalled: true, elapsedMin: 12 });
    expect(say).toMatchObject({ action: "feed", tone: "timeout" });
    expect(say?.line).toContain("12 分钟");
  });

  it("文件总线读不到时，不把「不知道」说成「没交」", () => {
    const say = detailSay({ task: task({ raw: "ARTIFACT_INVALID" }), blocker: blocker({ files: null }) });
    expect(say?.line).toContain("没读到");
    expect(say?.line).not.toContain("名下没有产物");
  });

  it("正常卡不生成一句话，避免详情首屏多一块噪音", () => {
    expect(detailSay({ task: task({ status: "done" }) })).toBeNull();
  });
});
