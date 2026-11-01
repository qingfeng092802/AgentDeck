import { resolve } from "node:path";
import { describe as suite, expect, it } from "vitest";
import { BANNED_TERMS, findBannedTerms } from "./support/bannedTerms";
import { collectFromSource, collectVisibleCopy } from "./support/visibleCopy";

/** 设计 §7.2 的禁用词表是**产品界面的**约束：注释、类名、接口路径不在扫描面上。
 *  这一份判据自己也要有反例，否则"扫描器什么都没扫到"与"界面真的干净"会长得一模一样。
 *
 *  2026-10-27：词表从 `src/lib/copy.ts`（随治理台退役）搬到 `tests/support/bannedTerms.ts`，
 *  扫描对象与判据口径不变；只删掉"`copy.ts` 的文案常量自身也干净"那一条 —— 被它保护的文件已经不存在。
 */
const SRC = resolve(process.cwd(), "src");

const SAMPLES: Record<string, string> = {
  GET: "请用 GET 读这一行",
  POST: "提交用了 POST",
  "EN-01 ~ EN-18": "这一项归 EN-08 管",
  events: "events 被截断了",
  帧: "最后一帧到了",
  DOM: "DOM 里没有这一块",
  全息版: "这是全息版",
  sha256: "sha256:1b88",
  cost_record: "cost_record 没有读面",
  SSE: "SSE 断了",
  payload: "payload 没有 pending_item",
  undefined: "显示成 undefined",
  null: "后端回了 null",
  "422": "空着提交会收到 422",
  这一页: "这一页没有动作",
  本页: "本页收到 3 条",
};

suite("禁用词表本身", () => {
  it("逐字等于设计 §7.2 的十六个词", () => {
    expect(BANNED_TERMS.map((item) => item.term)).toEqual([
      "GET",
      "POST",
      "EN-01 ~ EN-18",
      "events",
      "帧",
      "DOM",
      "全息版",
      "sha256",
      "cost_record",
      "SSE",
      "payload",
      "undefined",
      "null",
      "422",
      "这一页",
      "本页",
    ]);
  });

  it("每个词都能在自己的样本里被认出来", () => {
    for (const item of BANNED_TERMS) {
      expect(findBannedTerms(SAMPLES[item.term]), item.term).toContain(item.term);
    }
  });

  it("英文词按整词命中，不误伤更长的标识符", () => {
    expect(findBannedTerms("configuration")).toEqual([]);
    expect(findBannedTerms("nullable")).toEqual([]);
    expect(findBannedTerms("payloads")).toEqual([]);
    expect(findBannedTerms("GET /tasks")).toContain("GET");
  });
});

suite("可见文案扫描器", () => {
  it("扫 JSX 文本、可见属性与普通字符串，跳过注释 / 类名 / 属性名", () => {
    const source = [
      "// GET 在注释里不算",
      'const key = "本页";',
      "export function Demo() {",
      '  return <p className="本页" title="GET">帧</p>;',
      "}",
    ].join("\n");
    const entries = collectFromSource("src/demo.tsx", source);
    const texts = entries.map((entry) => entry.text);
    expect(texts).toContain("本页");
    expect(texts).toContain("GET");
    expect(texts).toContain("帧");
    expect(texts).not.toContain("// GET 在注释里不算");
  });
});

suite("界面全文无禁用词", () => {
  it("src/**/*.{ts,tsx} 的可见串为零命中", () => {
    const entries = collectVisibleCopy(SRC);
    expect(entries.length).toBeGreaterThan(50);
    const violations = entries.flatMap((entry) => {
      const terms = findBannedTerms(entry.text);
      return terms.length > 0 ? [{ ...entry, terms }] : [];
    });
    expect(
      violations.map((item) => `${item.file}:${item.line} 「${item.terms.join("、")}」 ${item.text}`),
    ).toEqual([]);
  });
});
