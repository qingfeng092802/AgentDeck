/** 思考强度：每个模型角色独立一档。`off` 是明确关闭，不是"没选"——
 *  默认落在 `medium`，旧设置缺这一列时也回到 `medium`。 */
export const THINKING_LEVELS = [
  { value: "off", label: "关" },
  { value: "low", label: "低" },
  { value: "medium", label: "中" },
  { value: "high", label: "高" },
] as const;

export type ThinkingLevel = (typeof THINKING_LEVELS)[number]["value"];

export const DEFAULT_THINKING: ThinkingLevel = "medium";

export const THINKING_LABEL: Record<ThinkingLevel, string> = {
  off: "关",
  low: "低",
  medium: "中",
  high: "高",
};

export function isThinkingLevel(value: unknown): value is ThinkingLevel {
  return THINKING_LEVELS.some((item) => item.value === value);
}
