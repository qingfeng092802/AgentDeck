import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe as suite, expect, it } from "vitest";
import { DeliveryBlocked, deliveryReason } from "../src/components/studio/DeliveryBlocked";
import type { DeliveryBlocker } from "../src/lib/live";
import { adoptTasks, getStudio, goHandleTask, type StudioTask } from "../src/lib/studioEngine";

/** 交付清单为空时那一块**渲染出来的是什么**（owner 2026-10-29）。
 *
 *  这一组判据盯的是"屏上有没有说出具体原因"，不是"函数返回了什么"——
 *  `deliveryBlockers` 已经把事实算好了（`live.test.ts` 那一组盯它），
 *  但事实算对而句子没写上去，人看到的仍然是「整单 0 项」。所以这里渲染真的组件，
 *  在**可见文本**上断言：卡号、卡名、原因、出口按钮。
 */

function task(over: Partial<StudioTask> = {}): StudioTask {
  return {
    id: "T04",
    title: "手冲奶茶静态官网 · 联系我们",
    owner: "qoder",
    phase: 2,
    dur: 0,
    artifact: "contact.html",
    input: "",
    act: "",
    out: "",
    status: "fail",
    progress: 0,
    round: 1,
    deps: [],
    hidden: false,
    raw: "ARTIFACT_INVALID",
    ...over,
  };
}

function blocker(over: Partial<DeliveryBlocker> = {}): DeliveryBlocker {
  return {
    task: task(),
    state: "ARTIFACT_INVALID",
    files: 0,
    rejected: [],
    failed: [],
    ...over,
  };
}

function html(blockers: readonly DeliveryBlocker[]): string {
  return renderToStaticMarkup(createElement(DeliveryBlocked, { blockers }));
}

function text(blockers: readonly DeliveryBlocker[]): string {
  return html(blockers).replace(/<[^>]*>/g, "");
}

suite("交付清单为空时，屏上必须说清「因为哪张卡」", () => {
  it("渲染出卡号、卡名与原因 —— 不是一个冷冰冰的 0", () => {
    const shown = text([blocker()]);
    expect(shown).toContain("T04");
    expect(shown).toContain("联系我们");
    expect(shown).toContain("产物没过门禁");
    // 读数是"还有几张卡"，不是"0 项"：那个数字本身不构成下一步动作
    expect(shown).toContain("还有 1 张卡");
  });

  it("产物没过校验时点得出文件名 —— 格式不合法的现场就是这么被看见的", () => {
    /* 2026-10-28 的现场：执行器交 `submit-contact.php`，总线整件拒收。
       只说"产物没过校验"等于把判断推回给人；点名文件才是可核对的事实。 */
    const shown = text([
      blocker({ files: 3, rejected: ["submit-contact.php"] }),
    ]);
    expect(shown).toContain("submit-contact.php");
    expect(shown).toContain("3 份里有 1 份没过校验");
  });

  it("审查退回时写上是哪条判据没过，而不是笼统一句「被拒」", () => {
    const shown = text([
      blocker({
        state: "REJECTED",
        task: task({ id: "T05", title: "交互脚本", raw: "REJECTED" }),
        failed: ["页面在 375 宽下不横向滚动"],
      }),
    ]);
    expect(shown).toContain("审查退回");
    expect(shown).toContain("页面在 375 宽下不横向滚动");
  });

  it("读不到文件总线时只说读得到的那一半 —— 不许把「不知道」说成「没有」", () => {
    /* 与 2026-10-29「右栏两页总是空的」同一个病根：屏上说的每一句都得是真的。
       文件数不知道时，"一份都没交上来"就是假话（库里可能明明有 27 份）。 */
    const shown = text([blocker({ files: null })]);
    expect(shown).not.toContain("没有一份算在它名下");
    expect(shown).toContain("没读到");
  });

  it("文件数为 0 时不许断言「没交」—— 总线上 0 份还有另一种成因", () => {
    /* 真机纠正过这一句（`real_demo · 57bf3d03` 的 T04）：它名下在库里其实有 27 行产物，
       而文件总线"一个文件一行、后写的覆盖先写的"，那 27 行写的都是别的卡也写过的路径
       ⇒ 去重之后一份都不算它的。说"一份文件都没交上来"是句假话。 */
    const shown = deliveryReason(blocker({ files: 0 }));
    expect(shown).not.toContain("都没交上来");
    expect(shown).toContain("要么没交");
    expect(shown).toContain("覆盖");
  });

  it("每张卡旁边都有一个「前往处理」，且带上是哪一张（点了才知道去哪）", () => {
    const markup = html([
      blocker(),
      blocker({ task: task({ id: "T05", title: "交互脚本" }), state: "ARTIFACT_INVALID" }),
    ]);
    expect((markup.match(/前往处理/g) ?? []).length).toBe(2);
    expect(markup).toContain('data-task="T04"');
    expect(markup).toContain('data-task="T05"');
  });

  it("一张卡都没有时不渲染这一块（空数组不该在屏上留一段空白）", () => {
    expect(html([])).toContain("交付清单为空");
    expect(html([])).not.toContain("前往处理");
  });

  it("句子本身按情形分叉 —— 四种情形给出四句不同的话", () => {
    /* 为什么每条都要不同：`files === 0` 该去催执行器重跑，`files > 0` 该去看门槛或换人，
       压成一句"产物没过校验"等于把判断又推回给人。 */
    const zero = deliveryReason(blocker({ files: 0 }));
    const some = deliveryReason(blocker({ files: 4 }));
    const bad = deliveryReason(blocker({ files: 4, rejected: ["a.php"] }));
    const unknown = deliveryReason(blocker({ files: null }));
    expect(new Set([zero, some, bad, unknown]).size).toBe(4);
    expect(zero).toContain("算在它名下");
    expect(some).toContain("门槛");
    expect(bad).toContain("a.php");
  });
});

suite("「前往处理」真的把人送到那张卡上", () => {
  it("选中那张卡并停在任务详情", () => {
    /* 组件里的 onClick 只是转调这一个函数，所以判它等于判那颗按钮的去向。
       让人从交付清单点出去、落到详情页却发现左栏选中的是另一张卡，比没有按钮更糟。 */
    adoptTasks([task({ id: "T02", raw: "DELIVERED", status: "done" }), task({ id: "T04" })], 2, null);
    goHandleTask("T04");
    expect(getStudio().selected).toBe("T04");
    expect(getStudio().tab).toBe("detail");
    expect(getStudio().screen).toBe("board");
  });
});
