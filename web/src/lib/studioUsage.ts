/**
 * 成本与用量：**纯前端估算**，不是账单。
 *
 * 这一版没有真实计费源（后端 `/cost*` 零命中），所以这里不画"花了多少钱"这种
 * 会被当成事实的读数，而是把口径写死在界面上：
 *   - 一个"存档点" = 一张完成的卡（K1…K13 / K5R / F1）；
 *   - token 由产物行数推（`行数 × 系数`），系数是常量，写在这里而不是散在组件里；
 *   - 单价按厂商一张表，单位 ¥/千 token，取的是各厂公开价位的量级，**不是实时价**；
 *   - 四席分档展示，**不跨口径求和** —— 统筹/执行/审查/验收各自一个模型，加在一起没有意义。
 *
 * 界面必须同时出现「估算」二字。判据 `test_the_usage_tab_says_it_is_an_estimate` 钉着这一条。
 */

import type { RoleKey, SettingsState } from "./settingsStore";
import { ROLE_KEYS, ROLE_TITLE, roleSet, roleText } from "./settingsStore";
import { normalizeVendor } from "./modelCatalog";
import type { StudioState } from "./studioEngine";

/** 一张完成的卡 → 一次调用。输入 token 随产物行数走，输出随产物大小走。 */
const TOKENS_IN_PER_LINE = 38;
const TOKENS_OUT_PER_LINE = 12;

/** ¥ / 千 token，[输入, 输出]。厂商上新时只改这张表。 */
const PRICE_PER_K: Readonly<Record<string, readonly [number, number]>> = {
  "深度求索 DeepSeek": [0.001, 0.002],
  阿里云通义千问: [0.002, 0.006],
  字节跳动豆包: [0.0008, 0.002],
  "月之暗面 Kimi": [0.004, 0.012],
  "智谱 AI GLM": [0.001, 0.003],
  "MiniMax 稀宇科技": [0.001, 0.004],
  "OpenAI 兼容": [0.03, 0.09],
};

/** 卡的主人 → 四席里的哪一席。执行席兜住 pm/cto/designer/fe/be/writer/qa/sec/rescue 九个人。 */
export function seatOf(owner: string): RoleKey {
  if (owner === "reviewer") return "reviewer";
  if (owner === "acceptor") return "acceptor";
  if (owner === "orchestrator") return "orchestrator";
  return "executor";
}

export interface UsageSeat {
  readonly key: RoleKey;
  readonly label: string;
  /** 未选模型时是空串 —— 界面留白，不填占位型号。 */
  readonly model: string;
  readonly calls: number;
  readonly tokensIn: number;
  readonly tokensOut: number;
  readonly cost: number;
}

export interface UsageCheckpoint {
  readonly id: string;
  readonly title: string;
  readonly owner: string;
  readonly seat: RoleKey;
  readonly model: string;
  readonly artifact: string;
  readonly at: string;
  readonly tokensIn: number;
  readonly tokensOut: number;
  readonly cost: number;
}

export interface UsageReport {
  readonly seats: readonly UsageSeat[];
  readonly checkpoints: readonly UsageCheckpoint[];
  readonly totalCost: number;
  readonly totalTokens: number;
  readonly budget: number;
  readonly remaining: number;
  /** 有任一席没选模型：此时成本只算得到"已选的那几席"，必须说出来。 */
  readonly incomplete: boolean;
}

function priceOf(vendor: string): readonly [number, number] {
  return PRICE_PER_K[normalizeVendor(vendor)] ?? [0, 0];
}

/** 行数 → token。给一个稳定的抖动，让每张卡看起来不是同一个数（但同一张卡每次都一样）。 */
function jitterOf(id: string): number {
  let hash = 0;
  for (let i = 0; i < id.length; i++) hash = (hash * 31 + id.charCodeAt(i)) % 97;
  return 0.85 + (hash % 31) / 100;
}

export function usageOf(studio: StudioState, settings: SettingsState, budget: number): UsageReport {
  const done = studio.tasks.filter((task) => !task.hidden && task.status === "done");

  const checkpoints: UsageCheckpoint[] = done.map((task) => {
    const record = studio.artifacts.find((item) => item.name === task.artifact);
    const lines = Math.max(12, record?.lines ?? 40);
    const factor = jitterOf(task.id);
    const tokensIn = Math.round(lines * TOKENS_IN_PER_LINE * factor);
    const tokensOut = Math.round(lines * TOKENS_OUT_PER_LINE * factor);
    const seat = seatOf(task.owner);
    const selection = settings[seat];
    const model = roleText(selection);
    const [priceIn, priceOut] = priceOf(selection.vendor);
    const cost = (tokensIn / 1000) * priceIn + (tokensOut / 1000) * priceOut;
    return {
      id: task.id,
      title: task.title,
      owner: task.owner,
      seat,
      model,
      artifact: task.artifact,
      at: record?.at ?? "",
      tokensIn,
      tokensOut,
      cost,
    };
  });

  const seats: UsageSeat[] = ROLE_KEYS.map((key) => {
    const rows = checkpoints.filter((item) => item.seat === key);
    return {
      key,
      label: ROLE_TITLE[key],
      model: roleText(settings[key]),
      calls: rows.length,
      tokensIn: rows.reduce((sum, item) => sum + item.tokensIn, 0),
      tokensOut: rows.reduce((sum, item) => sum + item.tokensOut, 0),
      cost: rows.reduce((sum, item) => sum + item.cost, 0),
    };
  });

  const totalCost = seats.reduce((sum, seat) => sum + seat.cost, 0);
  const totalTokens = seats.reduce((sum, seat) => sum + seat.tokensIn + seat.tokensOut, 0);

  return {
    seats,
    checkpoints: checkpoints.slice().reverse(),
    totalCost,
    totalTokens,
    budget,
    remaining: budget - totalCost,
    incomplete: ROLE_KEYS.some((key) => !roleSet(settings[key])),
  };
}

/** 预算滑杆的档位。不做自由输入：一个能被拖到 9999 的输入框不是预算，是玩具。 */
export const BUDGET_STEPS: readonly number[] = [3, 6, 12, 24, 48];
