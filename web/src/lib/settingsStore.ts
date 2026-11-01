import { useSyncExternalStore } from "react";
import { normalizeVendor, vendorEntry, type VendorEntry } from "./modelCatalog";
import { DEFAULT_THINKING, isThinkingLevel, type ThinkingLevel } from "./thinking";

/** 一个角色（执行/审查/验收）的级联选择结果。思考强度随角色走，不跟厂商走。 */
export interface RoleSelection {
  vendor: string;
  model: string;
  thinking: ThinkingLevel;
}

/** 全局设置。为什么先落 localStorage 而不是请求体：受理接口的请求体里没有模型字段
 *  （`IntakeRequest` 只有 project_key 与需求原话），塞进去要么被 422 要么被静默丢弃 ——
 *  两种都比"明说还没接"糟。这里是前端全局注入点：后端加上模型字段的那一天，
 *  受理调用方从 `getSettings()` 取值随单发送即可，UI 不用再动。
 *
 *  `apiKeys` 按厂商 id 分开存：同一厂商被两个角色选中时共用一把密钥，
 *  切换角色厂商不会把别家的密钥顶掉。OpenAI 兼容另需一个共享接入地址。
 *
 *  **密钥只在本会话内存里**：`saveSettings()` 落 localStorage 前会把 `apiKeys` 清空，
 *  `load()` 读到旧版本留下的密钥也会顺手抹掉。当前受理请求不携带模型/密钥字段，
 *  这层输入只是占位；没有消费路径的凭据不应该长期留在浏览器里。 */
export interface SettingsState {
  /** 统筹 = 主持立项会、派单、兜底裁决的那一席（owner 2026-10-27 把它补成第四席）。 */
  orchestrator: RoleSelection;
  executor: RoleSelection;
  reviewer: RoleSelection;
  acceptor: RoleSelection;
  openaiCompat: { baseUrl: string };
  apiKeys: Record<string, string>;
  maxConcurrency: number;
  reviewRoundLimit: number;
}

/** 四席的键名与顺序：统筹 → 执行 → 审查 → 验收。派单顺序与展示顺序都读它，不再各写一份。 */
export const ROLE_KEYS = ["orchestrator", "executor", "reviewer", "acceptor"] as const;
export type RoleKey = (typeof ROLE_KEYS)[number];

export const ROLE_TITLE: Record<RoleKey, string> = {
  orchestrator: "统筹",
  executor: "执行",
  reviewer: "审查",
  acceptor: "验收",
};

/** 「未选择」不是缺省值，是一个**合法状态**：owner 要求未选模型时那一格留空，
 *  而不是替他填一个看起来完整的默认型号。空 vendor + 空 model 就表示这一席没人坐。 */
export const UNSET_ROLE: RoleSelection = { vendor: "", model: "", thinking: DEFAULT_THINKING };

export const DEFAULT_SETTINGS: SettingsState = {
  orchestrator: { vendor: "深度求索 DeepSeek", model: "deepseek-v4-pro", thinking: DEFAULT_THINKING },
  executor: { vendor: "深度求索 DeepSeek", model: "deepseek-v4-pro", thinking: DEFAULT_THINKING },
  // 审查席刻意不给 DeepSeek：出厂配置必须**通过**互斥判据，否则"三岗重合"这条红色警示
  // 从第一次打开就在闪，真重合的时候反而没人看。换一家是最省事的自证。
  reviewer: { vendor: "月之暗面 Kimi", model: "kimi-k3", thinking: DEFAULT_THINKING },
  acceptor: { vendor: "深度求索 DeepSeek", model: "deepseek-flash", thinking: DEFAULT_THINKING },
  openaiCompat: { baseUrl: "" },
  apiKeys: {},
  maxConcurrency: 2,
  reviewRoundLimit: 3,
};

/** 这一席是否坐上了人。空串表示"未选择"，与"选了但厂商不认识"是两件事。 */
export function roleSet(role: RoleSelection): boolean {
  return role.vendor.trim().length > 0 && role.model.trim().length > 0;
}

/** 一席的展示串：`厂商 · 型号`。未选时返回空串 —— 调用方负责留白，不许编一个占位型号。 */
export function roleText(role: RoleSelection): string {
  return roleSet(role) ? `${normalizeVendor(role.vendor)} · ${role.model}` : "";
}

// v2 -> v3 换过厂商/密钥结构；v3 起角色多一列 thinking；
// v4 起多一席「统筹」，并允许某一席为「未选择」（空 vendor + 空 model）
const STORAGE_KEY = "acp-settings-v4";
const memory = new Map<string, string>();

interface StorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

function storage(): StorageLike {
  // 隐私模式 / 无痕环境下 localStorage 可能访问失败，退化为仅本会话生效的内存实现
  const adapter: StorageLike = {
    getItem: (key) => memory.get(key) ?? null,
    setItem: (key, value) => {
      memory.set(key, value);
    },
  };
  try {
    const store = (globalThis as { localStorage?: Storage }).localStorage;
    return store ?? adapter;
  } catch {
    return adapter;
  }
}

/** 深拷贝一份设置，避免外部改动穿透到 store 内部状态。 */
export function cloneSettings(source: SettingsState): SettingsState {
  return {
    orchestrator: { ...source.orchestrator },
    executor: { ...source.executor },
    reviewer: { ...source.reviewer },
    acceptor: { ...source.acceptor },
    openaiCompat: { ...source.openaiCompat },
    apiKeys: { ...source.apiKeys },
    maxConcurrency: source.maxConcurrency,
    reviewRoundLimit: source.reviewRoundLimit,
  };
}

/** 逐字段校验一个角色：空 vendor + 空 model 是合法的「未选择」；非空则必须在目录里、
 *  且型号属于该厂商。思考强度必须是四档之一。 */
function parseRole(raw: unknown, fallback: RoleSelection): RoleSelection {
  if (typeof raw !== "object" || !raw) return { ...fallback };
  const candidate = raw as Partial<RoleSelection>;
  const vendor = normalizeVendor(typeof candidate.vendor === "string" ? candidate.vendor.trim() : "");
  const model = typeof candidate.model === "string" ? candidate.model.trim() : "";
  const thinking = isThinkingLevel(candidate.thinking) ? candidate.thinking : DEFAULT_THINKING;
  // 未选择：两格都空。只空一格是脏数据，回落 fallback 而不是猜。
  if (!vendor && !model) return { ...UNSET_ROLE, thinking };
  const entry = vendorEntry(vendor);
  if (!entry) return { ...fallback };
  if (!entry.models.includes(model)) return { ...fallback };
  return { vendor, model, thinking };
}

function load(): SettingsState {
  try {
    const raw = storage().getItem(STORAGE_KEY);
    if (!raw) return cloneSettings(DEFAULT_SETTINGS);
    const parsed = JSON.parse(raw) as Partial<SettingsState>;
    const rawCompat = parsed.openaiCompat;
    const rawKeys = parsed.apiKeys;
    const hadPersistedKeys =
      !!rawKeys && typeof rawKeys === "object" && Object.keys(rawKeys as Record<string, unknown>).length > 0;
    const next: SettingsState = {
      orchestrator: parseRole(parsed.orchestrator, DEFAULT_SETTINGS.orchestrator),
      executor: parseRole(parsed.executor, DEFAULT_SETTINGS.executor),
      reviewer: parseRole(parsed.reviewer, DEFAULT_SETTINGS.reviewer),
      acceptor: parseRole(parsed.acceptor, DEFAULT_SETTINGS.acceptor),
      openaiCompat: {
        baseUrl: typeof rawCompat?.baseUrl === "string" ? rawCompat.baseUrl : "",
      },
      // 旧版本把密钥写在 localStorage 里；读到就丢弃，绝不带回内存。
      apiKeys: {},
      maxConcurrency:
        typeof parsed.maxConcurrency === "number" && parsed.maxConcurrency >= 1
          ? Math.floor(parsed.maxConcurrency)
          : DEFAULT_SETTINGS.maxConcurrency,
      reviewRoundLimit:
        typeof parsed.reviewRoundLimit === "number" && parsed.reviewRoundLimit >= 1
          ? Math.floor(parsed.reviewRoundLimit)
          : DEFAULT_SETTINGS.reviewRoundLimit,
    };
    if (hadPersistedKeys) {
      // 把旧密钥从盘上抹掉；磁盘清理失败不影响本次会话（内存里本来就没带回来）。
      try {
        storage().setItem(STORAGE_KEY, JSON.stringify({ ...next, apiKeys: {} }));
      } catch {
        // 隐私模式 / 无痕环境：本来就没有可写的位置。
      }
    }
    return next;
  } catch {
    return cloneSettings(DEFAULT_SETTINGS);
  }
}

const listeners = new Set<() => void>();
let state: SettingsState = load();

function emit(): void {
  listeners.forEach((listener) => listener());
}

export function getSettings(): SettingsState {
  return state;
}

export function saveSettings(next: SettingsState): void {
  state = cloneSettings(next);
  try {
    // 密钥不落盘：只把非凭据字段写进 localStorage，`apiKeys` 留在本次会话内存里。
    storage().setItem(STORAGE_KEY, JSON.stringify({ ...state, apiKeys: {} }));
  } catch {
    // 隐私模式：仅本会话生效
  }
  emit();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function useSettings(): SettingsState {
  return useSyncExternalStore(subscribe, getSettings, getSettings);
}

/**
 * 依当前四席，列出需要密钥的厂商（按角色顺序去重）。
 * 同一厂商被多席选中只出现一次；目录里 `needsKey=false` 的厂商会被跳过；
 * 「未选择」的那一席不产生任何密钥位 —— 空着不是"缺一把钥匙"，是"这一席没人坐"。
 */
export function requiredKeyEntries(s: SettingsState): VendorEntry[] {
  const seen = new Set<string>();
  const entries: VendorEntry[] = [];
  for (const key of ROLE_KEYS) {
    const role = s[key];
    if (!roleSet(role)) continue;
    const entry = vendorEntry(role.vendor);
    if (!entry?.needsKey || seen.has(entry.id)) continue;
    seen.add(entry.id);
    entries.push(entry);
  }
  return entries;
}

/** 所有必填密钥 trim 后非空才算就绪，防止只敲空格绕过防呆。 */
export function keysReady(s: SettingsState): boolean {
  return requiredKeyEntries(s).every((entry) => (s.apiKeys[entry.id] ?? "").trim().length > 0);
}

/** 四席都坐上了人。`keysReady` 会跳过未选席位，不能单独拿来当“可运行”的判据。 */
export function allSeatsSet(s: SettingsState): boolean {
  return ROLE_KEYS.every((key) => roleSet(s[key]));
}
