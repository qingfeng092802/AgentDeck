/** 界面禁用词表（设计 §7.2 的十六个词）与判定器。
 *
 *  2026-10-27：它原来住在 `src/lib/copy.ts` 里 —— 那份模块随治理台一起退役，
 *  但这张表约束的是**当前界面**的文案（实时档也会出现状态码、协议名），不该跟着死。
 *  所以搬进 tests/support：扫描对象仍是 `src/**`，判据仍是 `tests/copy.test.ts`。
 *
 *  表里的词是"产品界面不得出现"的实现术语，不是代码禁令 —— 注释、接口字段、
 *  协议常量照旧使用它们（扫描器只收 JSX 文本、可见属性与字符串字面量）。
 */

export interface BannedTerm {
  readonly term: string;
  readonly pattern: RegExp;
}

export const BANNED_TERMS: readonly BannedTerm[] = [
  { term: "GET", pattern: /\bGET\b/ },
  { term: "POST", pattern: /\bPOST\b/ },
  { term: "EN-01 ~ EN-18", pattern: /\bEN-\d{2}\b/ },
  { term: "events", pattern: /\bevents\b/ },
  { term: "帧", pattern: /帧/ },
  { term: "DOM", pattern: /\bDOM\b/ },
  { term: "全息版", pattern: /全息版/ },
  { term: "sha256", pattern: /\bsha256\b/i },
  { term: "cost_record", pattern: /\bcost_record\b/i },
  { term: "SSE", pattern: /\bSSE\b/ },
  { term: "payload", pattern: /\bpayload\b/i },
  { term: "undefined", pattern: /\bundefined\b/i },
  { term: "null", pattern: /\bnull\b/i },
  { term: "422", pattern: /422/ },
  { term: "这一页", pattern: /这一页/ },
  { term: "本页", pattern: /本页/ },
];

/** 返回命中词（按表里的顺序），空数组 = 这句话可以上屏。 */
export function findBannedTerms(text: string): string[] {
  return BANNED_TERMS.filter((item) => item.pattern.test(text)).map((item) => item.term);
}
