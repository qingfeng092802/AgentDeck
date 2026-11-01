/** 四席直跑报告的「代价读数」（2026-10-31）。
 *
 *  `budget_doublings` / `transport_retries` 一直是 `run-report.json` 里就有的字段，页面上却一个数都
 *  不显示 —— 想看"这一发是不是靠翻倍 / 重打救回来的"只能去盘上翻文件。这里只做一件事：
 *  把两张表折成一行人话。**0 与空表都读成 `0`，不隐藏**：没有翻倍本身就是一条读数
 *  （说明配置的预算够用），把它省掉会让"没跑过"和"没翻过"长得一样。
 */

export interface RunCostReport {
  budget_doublings?: Record<string, number> | null;
  transport_retries?: Record<string, number> | null;
}

export function runReadings(report: RunCostReport): string {
  const doublings = Object.entries(report.budget_doublings ?? {}).filter(([, times]) => Number(times) > 0);
  const retries = Object.entries(report.transport_retries ?? {});
  const doubled = doublings.length
    ? doublings.map(([stage, times]) => `${stage}×${times}`).join(" / ")
    : "0";
  const retried = retries.length ? retries.map(([stage, attempt]) => `${stage}=第${attempt}发`).join(" / ") : "0";
  return `预算翻倍 ${doubled} · 传输重打 ${retried}`;
}
