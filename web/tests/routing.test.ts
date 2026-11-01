import { afterEach, describe, expect, it, vi } from "vitest";
import {
  DEFAULT_ROUTING,
  loadRouting,
  routingLabel,
  routingPayload,
  saveRouting,
  ROUTING_KEY,
  type RoutingChoice,
} from "../src/lib/routing";
import { DEFAULT_SETTINGS, type SettingsState } from "../src/lib/settingsStore";
import { keyOf, loadPool, rememberKey, savePool, type PoolModel } from "../src/lib/modelPool";

/** 路线 C（2026-10-31）：三席都可以点名自定义池里的模型。
 *
 *  钉三件事：全默认不带请求字段、池子座位的请求体形状（含 base_url 与会话 key）、
 *  **key 不落 localStorage**。 */

const SEATS: SettingsState = {
  ...DEFAULT_SETTINGS,
  executor: { vendor: "深度求索 DeepSeek", model: "deepseek-flash", thinking: "high" },
};

const POOL: PoolModel[] = [
  { id: "m1", name: "千问", model: "qwen-max", baseUrl: "https://dashscope.example/v1" },
  { id: "m2", name: "深索", model: "deepseek-flash", baseUrl: "https://api.deepseek.example/v1" },
  { id: "m3", name: "豆包", model: "doubao-seed", baseUrl: "https://ark.example/v1" },
];

function withStore() {
  const store = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => store.set(key, value),
    removeItem: (key: string) => store.delete(key),
  });
  return store;
}

afterEach(() => vi.unstubAllGlobals());

describe("routingPayload：默认不带字段，池子座位带完整凭据点名", () => {
  it("全默认 ⇒ null（老路径请求体一字不改）", () => {
    expect(routingPayload(DEFAULT_ROUTING, SEATS, POOL)).toBeNull();
  });

  it("执行席来自池子 ⇒ kind=api + 名称/模型/Base URL；key 只在会话里", () => {
    withStore();
    rememberKey("m2", "sk-session-only");
    const choice: RoutingChoice = { executorKind: "pool", executor: "m2", reviewer: "", acceptor: "" };
    const payload = routingPayload(choice, SEATS, POOL);
    expect(payload).toEqual({
      executor: {
        kind: "api",
        vendor: "深索",
        model: "deepseek-flash",
        base_url: "https://api.deepseek.example/v1",
        api_key: "sk-session-only",
        thinking: "high",
      },
    });
  });

  it("三席各点一个池子模型 ⇒ 三个模型实例一起进请求体", () => {
    withStore();
    const choice: RoutingChoice = { executorKind: "pool", executor: "m1", reviewer: "m2", acceptor: "m3" };
    const payload = routingPayload(choice, SEATS, POOL);
    expect(payload?.reviewer).toMatchObject({ vendor: "深索", model: "deepseek-flash" });
    expect(payload?.acceptor).toMatchObject({ vendor: "豆包", model: "doubao-seed" });
    expect(payload?.executor.kind).toBe("api");
  });

  it("执行席仍是 Qoder 但审查席点了池子 ⇒ 只带审查那一格", () => {
    const choice: RoutingChoice = { executorKind: "qoder", executor: "", reviewer: "m1", acceptor: "" };
    const payload = routingPayload(choice, SEATS, POOL);
    expect(payload?.executor.kind).toBe("qoder");
    expect(payload?.reviewer).toMatchObject({ model: "qwen-max" });
    expect(payload?.acceptor).toBeUndefined();
  });

  it("点了池子但执行席没选 ⇒ 明确报错", () => {
    const choice: RoutingChoice = { executorKind: "pool", executor: "", reviewer: "", acceptor: "" };
    expect(() => routingPayload(choice, SEATS, POOL)).toThrow(/执行席还没选池里的模型/);
  });

  it("摘要读得出来", () => {
    const choice: RoutingChoice = { executorKind: "pool", executor: "m1", reviewer: "m2", acceptor: "" };
    expect(routingLabel(choice, POOL)).toBe("执行：池：qwen-max · 审查：deepseek-flash · 验收：默认");
  });
});

describe("本机偏好：坏数据退回默认", () => {
  it("存进去读出来是同一份（旧 api 值归一成 pool）", () => {
    withStore();
    const choice: RoutingChoice = { executorKind: "pool", executor: "m1", reviewer: "m2", acceptor: "" };
    saveRouting(choice);
    expect(loadRouting()).toEqual(choice);
    const store = withStore();
    store.set(ROUTING_KEY, JSON.stringify({ executorKind: "api", executor: "m1", reviewer: "", acceptor: "" }));
    expect(loadRouting().executorKind).toBe("pool");
  });

  it("坏 JSON / 未知字段退回默认", () => {
    const store = withStore();
    store.set(ROUTING_KEY, "{not json");
    expect(loadRouting()).toEqual(DEFAULT_ROUTING);
  });
});

describe("模型池：key 不落 localStorage", () => {
  it("savePool 只写名称/模型/端点，读回来没有 key", () => {
    const store = withStore();
    rememberKey("m1", "sk-secret");
    savePool(POOL);
    const raw = store.get("acp-model-pool") ?? "";
    expect(raw).not.toContain("sk-secret");
    expect(raw).not.toContain("apiKey");
    expect(loadPool()).toHaveLength(3);
    expect(keyOf("m1")).toBe("sk-secret");
  });

  it("坏数据退回空池，不炸设置面板", () => {
    const store = withStore();
    store.set("acp-model-pool", "[[[");
    expect(loadPool()).toEqual([]);
  });
});
