/** 自定义模型池（路线 C，2026-10-31）：用户自带的模型 + 兼容端点 + key。
 *
 *  **key 只在本次会话内存里**：落 localStorage 的只有名称、模型 ID 与 Base URL。
 *  与 `settingsStore` 同一口径（上一版修过"密钥写进 localStorage"那条），池子不破例。
 */

export interface PoolModel {
  id: string;
  /** 展示名（也作为后端路由的 vendor 标签；自定义厂商必须给 baseUrl）。 */
  name: string;
  /** 模型 ID，例如 `qwen-max` / `deepseek-flash`。 */
  model: string;
  /** OpenAI 兼容端点，例如 `https://dashscope.aliyuncs.com/compatible-mode/v1`。 */
  baseUrl: string;
}

export const POOL_KEY = "acp-model-pool";

let sessionKeys: Map<string, string> | null = null;

function keys(): Map<string, string> {
  if (sessionKeys === null) sessionKeys = new Map();
  return sessionKeys;
}

export function loadPool(): PoolModel[] {
  try {
    const raw = globalThis.localStorage?.getItem(POOL_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter((item): item is Record<string, unknown> => !!item && typeof item === "object")
      .map((item) => ({
        id: String(item.id ?? ""),
        name: String(item.name ?? ""),
        model: String(item.model ?? ""),
        baseUrl: String(item.baseUrl ?? ""),
      }))
      .filter((item) => Boolean(item.id && item.name && item.model));
  } catch {
    return [];
  }
}

export function savePool(pool: PoolModel[]): void {
  try {
    globalThis.localStorage?.setItem(POOL_KEY, JSON.stringify(pool));
  } catch {
    // 存不进去就只作用于本会话。
  }
}

export function rememberKey(id: string, apiKey: string): void {
  if (apiKey) keys().set(id, apiKey);
  else keys().delete(id);
}

export function keyOf(id: string): string {
  return keys().get(id) ?? "";
}

export function forgetKeys(): void {
  keys().clear();
}

export function newPoolId(): string {
  return `m${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
}
