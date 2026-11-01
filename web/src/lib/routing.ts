/** 主链路路由（路线 C，2026-10-31）：执行/审查/验收三席都可以点名**自定义模型池**里的模型。
 *
 *  三条口径：
 *  1. 执行席：`qoder`（默认，CLI）或池子条目（纯 API 执行者）；审查/验收席：空（跟随默认）或池子条目。
 *  2. 把关席仍是"偏好"语义，但模型形态由通用审查/验收 Runner 真正调用；三岗互斥按**模型实例指纹**
 *     （同模型同 key 两席会被后端拦）。
 *  3. **key 只进请求体**（会话内存），`model_routing` 落库的是 vendor/model/base_url/key_fp。
 */

import { getSettings, type SettingsState } from "./settingsStore";
import { keyOf, loadPool, type PoolModel } from "./modelPool";

export const ROUTING_KEY = "acp-model-routing";

export type ExecutorKind = "qoder" | "api";

export interface RoutingChoice {
  /** `qoder` = CLI 默认；`pool` = 池子条目当纯 API 执行者；`api` = 旧存储值（按 pool 处理）。 */
  executorKind: ExecutorKind | "pool";
  /** 池子条目 id（空串 = 不点名）。 */
  executor: string;
  reviewer: string;
  acceptor: string;
}

export interface ModelRoutingPayload {
  executor: {
    kind: ExecutorKind;
    vendor: string;
    model: string;
    thinking: string;
    base_url?: string;
    api_key?: string;
  };
  reviewer?: {
    agent_id?: string;
    vendor?: string;
    model?: string;
    base_url?: string;
    api_key?: string;
    thinking?: string;
  };
  acceptor?: {
    agent_id?: string;
    vendor?: string;
    model?: string;
    base_url?: string;
    api_key?: string;
    thinking?: string;
  };
}

export const DEFAULT_ROUTING: RoutingChoice = {
  executorKind: "qoder",
  executor: "",
  reviewer: "",
  acceptor: "",
};

export const EXECUTOR_KIND_OPTIONS = [
  { value: "qoder", label: "Qoder CLI（默认）" },
  { value: "pool", label: "自定义模型池" },
];

/** 审查 / 验收的"不点名"哨兵；其余选项由池子动态生成（不再写死 agent）。 */
export const REVIEWER_DEFAULT_LABEL = "跟随审查模式（默认）";
export const ACCEPTOR_DEFAULT_LABEL = "自动（默认）";

export function loadRouting(): RoutingChoice {
  try {
    const raw = globalThis.localStorage?.getItem(ROUTING_KEY);
    if (!raw) return { ...DEFAULT_ROUTING };
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return { ...DEFAULT_ROUTING };
    const record = parsed as Record<string, unknown>;
    const kind = record.executorKind === "pool" || record.executorKind === "api" ? "pool" : "qoder";
    return {
      executorKind: kind,
      executor: typeof record.executor === "string" ? record.executor : "",
      reviewer: typeof record.reviewer === "string" ? record.reviewer : "",
      acceptor: typeof record.acceptor === "string" ? record.acceptor : "",
    };
  } catch {
    return { ...DEFAULT_ROUTING };
  }
}

export function saveRouting(choice: RoutingChoice): void {
  try {
    globalThis.localStorage?.setItem(ROUTING_KEY, JSON.stringify(choice));
  } catch {
    // 存不进去就只作用于本会话。
  }
}

function poolSeat(item: PoolModel, thinking?: string): NonNullable<ModelRoutingPayload["reviewer"]> {
  const seat: NonNullable<ModelRoutingPayload["reviewer"]> = {
    vendor: item.name,
    model: item.model,
    base_url: item.baseUrl,
  };
  const apiKey = keyOf(item.id);
  if (apiKey) seat.api_key = apiKey;
  if (thinking) seat.thinking = thinking;
  return seat;
}

/** 路由 → 请求体字段。**全默认时返回 `null`**：老请求不带这一格，后端保持 NULL。 */
export function routingPayload(
  choice: RoutingChoice,
  seats: SettingsState,
  pool: PoolModel[],
): ModelRoutingPayload | null {
  const byId = new Map(pool.map((item) => [item.id, item]));
  const executorItem = choice.executorKind === "pool" ? byId.get(choice.executor) : undefined;
  if (choice.executorKind === "pool" && !executorItem) {
    throw new Error("路由选了自定义模型池，但执行席还没选池里的模型");
  }
  const reviewerItem = choice.reviewer ? byId.get(choice.reviewer) : undefined;
  const acceptorItem = choice.acceptor ? byId.get(choice.acceptor) : undefined;
  if ((choice.reviewer && !reviewerItem) || (choice.acceptor && !acceptorItem)) {
    throw new Error("路由点名的池子模型已不存在 —— 回设置里重选");
  }
  const apiExecutor = executorItem !== undefined;
  if (!apiExecutor && !reviewerItem && !acceptorItem) return null;
  const executor = apiExecutor
    ? ({ kind: "api", ...poolSeat(executorItem, seats.executor.thinking) } as ModelRoutingPayload["executor"])
    : ({ kind: "qoder", vendor: "", model: "", thinking: seats.executor.thinking } as ModelRoutingPayload["executor"]);
  const payload: ModelRoutingPayload = { executor };
  if (reviewerItem) payload.reviewer = poolSeat(reviewerItem);
  if (acceptorItem) payload.acceptor = poolSeat(acceptorItem);
  return payload;
}

export function routingLabel(choice: RoutingChoice, pool: PoolModel[]): string {
  const byId = new Map(pool.map((item) => [item.id, item]));
  const label = (id: string): string => (id ? (byId.get(id)?.model ?? "已删除") : "默认");
  const executor = choice.executorKind === "pool" ? `池：${label(choice.executor)}` : "Qoder CLI";
  return `执行：${executor} · 审查：${label(choice.reviewer)} · 验收：${label(choice.acceptor)}`;
}

/** 供 `submit()` 用：读本机偏好、四席设置与自定义池，组装成请求体里那一格。 */
export function currentRoutingPayload(): ModelRoutingPayload | null {
  return routingPayload(loadRouting(), getSettings(), loadPool());
}
