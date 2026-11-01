import { describe, expect, it } from "vitest";
import { analyze, buildTasks, STUDIO_EXAMPLES } from "../src/lib/studioEngine";

/** 开机预置那条需求（受理卡第一颗示例）跑出来的读数。
 *
 *  为什么值得单钉一条：默认档"打开即已立项"之后，屏幕上那句
 *  「解析出 3 页 · 6 条验收标准 · N 张执行卡」就不再是演示者手打的产物，
 *  而是每一次打开都会出现的**承诺**。示例文案一改、`analyze()` 的规则一改，
 *  这句话就会静默变成假的 —— 这里把它钉住。
 */
describe("开机预置需求", () => {
  const plan = analyze(STUDIO_EXAMPLES[0].text);

  it("解析出 3 页：首页 / 商品 / 联系我们", () => {
    expect(plan.pages.map((page) => page.key)).toEqual(["home", "products", "contact"]);
  });

  it("6 条验收标准（响应式与购物车各占一条，外加两条交付底线）", () => {
    expect(plan.ac.map((item) => item.id)).toEqual(["AC-1", "AC-2", "AC-3", "AC-4", "AC-5", "AC-6"]);
  });

  it("执行卡数量与屏幕上的读数一致", () => {
    const tasks = buildTasks(plan).filter((task) => !task.hidden && task.phase >= 2);
    expect(tasks.length).toBe(10);
  });

  it("判定为静态多页站点，不是看板/系统", () => {
    expect(plan.type).toBe("static");
    expect(plan.typeLabel).toBe("静态多页站点");
  });
});
