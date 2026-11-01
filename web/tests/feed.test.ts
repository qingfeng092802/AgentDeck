/** P2-11：活动流窗口化的判据 —— 只钉"算区间"这一个纯函数。
 *
 *  为什么单测这一段：窗口化最容易错的不是"渲染不出来"，而是**边界差一格** ——
 *  最后一行永远露不出来、留白算成负数、滚动条长度变一下。这三种都只表现成
 *  "偶尔闪一下"或"看得见但摸不着"，肉眼看不出来，所以必须有数。
 *
 *  另一个理由：本仓的组件**没有 jsdom 测量**（`clientHeight` / `scrollTop` 在无头环境里
 *  恒为 0），所以把可测的部分做成纯函数、把不可测的部分（听 `scroll`、量 `offsetHeight`）
 *  压到最薄的一层里。
 */

import { describe, expect, it } from "vitest";

import { OVERSCAN, ROW_ESTIMATE, WINDOW_THRESHOLD, windowOf } from "../src/components/studio/VirtualFeed";

/** n 行等高的高度表。等高的好处是"留白 + 已渲染 = 总长"能被精确验算，不等高时只能验不等式。 */
function rows(count: number, height = ROW_ESTIMATE): number[] {
  return Array.from({ length: count }, () => height);
}

describe("windowOf：区间与留白", () => {
  it("装得下就全渲染，且不留白", () => {
    expect(windowOf(rows(10), 0, 400)).toEqual({ start: 0, end: 10, top: 0, bottom: 0 });
  });

  it("滚到中段只渲染那一段 + overscan（边界逐格对得上）", () => {
    /* 100 行起、视口 10 行 ⇒ 可见段是 [100, 110)，再各放 6 行 overscan。 */
    const range = windowOf(rows(240), ROW_ESTIMATE * 100, ROW_ESTIMATE * 10);
    expect(range.start).toBe(100 - OVERSCAN);
    expect(range.end).toBe(110 + OVERSCAN);
    expect(range.end - range.start).toBe(10 + OVERSCAN * 2);
  });

  it("留白 + 已渲染的高度 == 总长（滚动条长度不变）", () => {
    const total = 240 * ROW_ESTIMATE;
    for (const scrollTop of [0, ROW_ESTIMATE * 37, ROW_ESTIMATE * 200, total]) {
      const range = windowOf(rows(240), scrollTop, ROW_ESTIMATE * 10);
      expect(range.top + (range.end - range.start) * ROW_ESTIMATE + range.bottom).toBe(total);
    }
  });

  it("留白永不为负：滚到底也不会算出 -1", () => {
    const range = windowOf(rows(240), 240 * ROW_ESTIMATE * 2, ROW_ESTIMATE * 10);
    expect(range.top).toBeGreaterThanOrEqual(0);
    expect(range.bottom).toBeGreaterThanOrEqual(0);
  });

  it("视口高度为 0 的那一帧至少渲染一行（不能是空气）", () => {
    const range = windowOf(rows(240), 0, 0);
    expect(range.end - range.start).toBeGreaterThanOrEqual(1);
  });

  it("空列表不炸", () => {
    expect(windowOf([], 0, 400)).toEqual({ start: 0, end: 0, top: 0, bottom: 0 });
  });

  it("不等高时也对齐：行高乱序不影响留白 = 上面没渲染的那些行的高度", () => {
    const heights = [20, 60, 35, 100, 24, 24, 24, 90, 30];
    const total = heights.reduce((sum, value) => sum + value, 0);
    const range = windowOf(heights, 150, 100);
    const rendered = heights.slice(range.start, range.end).reduce((sum, value) => sum + value, 0);
    expect(range.top + rendered + range.bottom).toBe(total);
  });
});

describe("窗口化的阈值", () => {
  it("阈值在 1 与容量之间：一屏能装下的列表不该走窗口逻辑", () => {
    expect(WINDOW_THRESHOLD).toBeGreaterThan(0);
    expect(WINDOW_THRESHOLD).toBeLessThan(240);
  });

  it("overscan 是正数：留 0 会露出白条", () => {
    expect(OVERSCAN).toBeGreaterThan(0);
  });
});
