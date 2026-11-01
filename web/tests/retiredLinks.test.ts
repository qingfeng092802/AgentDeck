import { describe as suite, expect, it } from "vitest";
import { explainRetiredLinks, type RetiredNotice } from "../src/lib/retiredLinks";
import { dismissToast, pushToast } from "../src/lib/toastStore";

/** 退役直链那句解释（2026-10-30 owner：「每次刷新都会出现影响观感」）。
 *
 *  这一屏原来有两个各读一遍 `location.search` 的 effect，各发一条提示，而**判据是书签里那几个
 *  永远不会变的参数** ⇒ 每次 F5 都重新解释一遍，两类参数都在时还上下叠两张同款卡。
 *  下面这几发分别钉：只发一条、发完把参数摘掉、摘完之后第二次进来说不出话（= 刷新不再出现）、
 *  以及"同一条提示不许叠两张"。
 */

function collect(search: string): { notices: RetiredNotice[]; next: string | null } {
  const notices: RetiredNotice[] = [];
  const next = explainRetiredLinks(search, (notice) => notices.push(notice));
  return { notices, next };
}

suite("退役直链 —— 一句话，说完就把地址栏收干净", () => {
  it("干净入口（书签里没有退役参数）：不说话，也不改写地址栏", () => {
    for (const search of ["", "?", "?project=demo", "?prj=1&tab=feed"]) {
      const { notices, next } = collect(search);
      expect(notices, search).toHaveLength(0);
      expect(next, search).toBeNull();
    }
  });

  it("?live 整页：一句「旧入口已合并」，返回的 search 是空串（不是 null）", () => {
    const { notices, next } = collect("?live=1");
    expect(notices.map((item) => item.title)).toEqual(["旧入口已合并"]);
    expect(next).toBe("");
  });

  it("演示直链：一句「演示直链已停用」，而且不替任何人启动推演（本模块不发指令，只发话）", () => {
    const { notices, next } = collect("?studio=demo&ex=2&dispatch=1");
    expect(notices.map((item) => item.title)).toEqual(["演示直链已停用"]);
    expect(next).toBe("");
  });

  it("两类旧链接叠在同一条 URL 上 ⇒ 只发一张卡，不是上下两张", () => {
    const { notices } = collect("?live=1&studio=demo&ex=1&dispatch=1");
    expect(notices).toHaveLength(1);
    expect(notices[0]?.title).toBe("旧链接已停用");
  });

  it("刷新不再出现：把返回值喂回去 ⇒ 第二次既没话可说，也没有第二次改写", () => {
    const first = collect("?live=1&studio=demo&speed=4&dispatch=1&lang=zh");
    expect(first.notices).toHaveLength(1);
    expect(first.next).toBe("?lang=zh");
    const second = collect(first.next ?? "");
    expect(second.notices).toHaveLength(0);
    expect(second.next).toBeNull();
  });

  it("退役之外的参数原样留着（不能为了消横幅把人家的筛选条件一起抹了）", () => {
    expect(collect("?studio=demo&lang=zh&tab=feed").next).toBe("?lang=zh&tab=feed");
  });

  it("只挂 ex / speed 这种演示残参也说一句：它们是同一条旧链接的碎片", () => {
    expect(collect("?speed=4").notices.map((item) => item.title)).toEqual(["演示直链已停用"]);
  });
});

suite("提示层 —— 同一句话不许叠两张", () => {
  const A = { tone: "info" as const, title: "判据甲", detail: "同一条第二次不许再多一张卡" };
  const B = { tone: "warn" as const, title: "判据乙", detail: "换了内容就得是另一条" };

  it("同 tone 同 title 同 detail ⇒ 第二次拿回同一个 id（屏上仍是那一张）", () => {
    const first = pushToast(A);
    expect(pushToast({ ...A })).toBe(first);
    dismissToast(first);
  });

  it("detail 换了 ⇒ 是另一条，另发一个 id", () => {
    const first = pushToast(A);
    const other = pushToast({ ...A, detail: "这句话和上面那条不一样" });
    expect(other).not.toBe(first);
    expect(pushToast(B)).not.toBe(first);
    dismissToast(first);
    dismissToast(other);
  });

  it("关掉之后可以再说一次（去重认的是「屏上还挂着」，不是「这辈子说过」）", () => {
    const first = pushToast(A);
    dismissToast(first);
    expect(pushToast({ ...A })).not.toBe(first);
  });
});
