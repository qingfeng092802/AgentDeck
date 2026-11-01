import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe as suite, expect, it } from "vitest";
import { SettingsModal } from "../src/components/SettingsModal";
import {
  DEFAULT_SETTINGS,
  getSettings,
  allSeatsSet,
  keysReady,
  requiredKeyEntries,
  roleSet,
  roleText,
  saveSettings,
  UNSET_ROLE,
  type RoleSelection,
  type SettingsState,
} from "../src/lib/settingsStore";
import { MODEL_CATALOG, modelsFor, vendorEntry } from "../src/lib/modelCatalog";
import { THINKING_LEVELS, type ThinkingLevel } from "../src/lib/thinking";

/** 全局设置：厂商/型号级联 + 每角色思考强度 + 按厂商密钥。
 *
 *  渲染走 `renderToStaticMarkup`：effects 不跑，量的是"打开时的形状"；
 *  键盘交互与点外关闭由 Python 守卫钉字面量。
 */

afterEach(() => saveSettings({ ...DEFAULT_SETTINGS }));

const roleOf = (vendor: string, thinking: ThinkingLevel = "medium"): RoleSelection => ({
  vendor,
  model: modelsFor(vendor)[0] ?? "",
  thinking,
});

/** 四席从 2026-10-27 起多了一席「统筹」。下面这一组判据量的是**另外三席**的联动，
 *  所以统筹席显式留空 —— 否则出厂默认（DeepSeek）会偷偷带一把密钥进来，
 *  让"三家厂商 = 三把密钥"变成"四把"，判据红得莫名其妙。 */
const settingsOf = (vendors: [string, string, string], keys: Record<string, string> = {}): SettingsState => ({
  ...DEFAULT_SETTINGS,
  orchestrator: UNSET_ROLE,
  executor: roleOf(vendors[0]),
  reviewer: roleOf(vendors[1]),
  acceptor: roleOf(vendors[2]),
  apiKeys: keys,
  openaiCompat: { baseUrl: "" },
});

suite("厂商模型目录", () => {
  it("七家厂商：国内六家 + OpenAI 兼容端点", () => {
    expect(MODEL_CATALOG).toHaveLength(7);
    const byRegion = (region: string): string[] =>
      MODEL_CATALOG.filter((entry) => entry.region === region).map((entry) => entry.vendor);
    expect(byRegion("国内")).toEqual([
      "深度求索 DeepSeek",
      "阿里云通义千问",
      "字节跳动豆包",
      "月之暗面 Kimi",
      "智谱 AI GLM",
      "MiniMax 稀宇科技",
    ]);
    expect(byRegion("自定义")).toEqual(["OpenAI 兼容"]);
    expect(byRegion("海外")).toEqual([]);
  });

  it("旧厂商名仍能解析到新目录（本地设置不会因改名回落默认）", () => {
    expect(vendorEntry("DeepSeek")?.vendor).toBe("深度求索 DeepSeek");
    expect(vendorEntry("阿里通义千问")?.vendor).toBe("阿里云通义千问");
    expect(vendorEntry("字节豆包")?.vendor).toBe("字节跳动豆包");
    expect(vendorEntry("智谱 GLM")?.vendor).toBe("智谱 AI GLM");
    expect(vendorEntry("MiniMax")?.vendor).toBe("MiniMax 稀宇科技");
  });

  it("型号名逐字对上厂商 /models 的实测返回（有 key 的三家），且没有说明文字", () => {
    // 这一份的权威从"owner 口头清单"换成了"账号里真叫得通的名字"（2026-10-30）：
    // 旧名单里豆包 8 个名字 7 个 404、DeepSeek 6 个里只有 2 个可用。
    // 复跑：`python scripts/check_model_catalog.py`（读环境变量里的 key，只报漂移）。
    expect(modelsFor("深度求索 DeepSeek")).toEqual(["deepseek-flash", "deepseek-v4-pro"]);
    expect(modelsFor("阿里云通义千问")).toEqual([
      "qwen3.8-max",
      "qwen3.8-max-0902",
      "qwen3.8-flash",
      "qwen3.8-27b",
      "qwen3.8-2.4t-a95b",
      "qwen3-coder-flash",
      "qwen-flash",
      "qwen-plus",
      "qwen-max",
      "qwen-long",
      "deepseek-v4.1-flash",
      "deepseek-v4-pro-0813",
    ]);
    expect(modelsFor("字节跳动豆包")).toEqual([
      "doubao-seed-2-1-pro-260915",
      "doubao-seed-2-1-pro-260628",
      "doubao-seed-2-1-turbo-260628",
      "doubao-seed-2-1-lite-260915",
      "doubao-seed-2-0-pro-260215",
      "doubao-seed-2-0-lite-260428",
      "doubao-seed-2-0-lite-260215",
      "doubao-seed-2-0-mini-260428",
      "doubao-seed-2-0-mini-260215",
      "doubao-seed-2-0-code-preview-260215",
    ]);
    expect(modelsFor("月之暗面 Kimi")).toEqual([
      "kimi-k3",
      "kimi-k2.8-preview",
      "kimi-k2.7-code",
      "kimi-k2.7-code-highspeed",
      "kimi-k2.6",
    ]);
    expect(modelsFor("智谱 AI GLM")).toEqual([
      "glm-5.3",
      "glm-5.3-flash",
      "glm-5.3-flashx",
      "glm-5.2",
      "glm-4.7",
      "glm-4.7-flash",
      "glm-4.6",
    ]);
    expect(modelsFor("MiniMax 稀宇科技")).toEqual(["MiniMax-M3.1", "MiniMax-M3.1-Flash", "MiniMax-M3"]);
    expect(modelsFor("OpenAI 兼容")).toEqual([
      "gpt-5.6-sol",
      "gpt-5.6",
      "gpt-5.6-mini",
      "claude-opus-4-6",
      "claude-sonnet-4-6",
      "claude-haiku-4-5",
      "gemini-3.8-flash",
    ]);
    expect(modelsFor("不存在的厂商")).toEqual([]);
    for (const entry of MODEL_CATALOG) {
      expect("description" in entry).toBe(false);
    }
  });

  it("每个厂商各有一把独立密钥；OpenAI 兼容另需自定义接入地址", () => {
    expect(MODEL_CATALOG.every((entry) => entry.needsKey)).toBe(true);
    expect(new Set(MODEL_CATALOG.map((entry) => entry.id)).size).toBe(MODEL_CATALOG.length);
    const compat = MODEL_CATALOG.find((entry) => entry.vendor === "OpenAI 兼容");
    expect(compat?.customEndpoint).toBe(true);
    expect(compat?.models.length).toBeGreaterThan(0);
  });
});

suite("requiredKeyEntries / keysReady —— 密钥联动防呆", () => {
  it("按角色顺序去重：同一厂商被两个角色选中只出现一把密钥", () => {
    expect(requiredKeyEntries(settingsOf(["深度求索 DeepSeek", "OpenAI 兼容", "深度求索 DeepSeek"])).map((entry) => entry.id)).toEqual([
      "deepseek",
      "openai-compat",
    ]);
    expect(requiredKeyEntries(settingsOf(["阿里云通义千问", "月之暗面 Kimi", "MiniMax 稀宇科技"])).map((entry) => entry.id)).toEqual([
      "qwen",
      "kimi",
      "minimax",
    ]);
  });

  it("keysReady：只敲空格不算填了", () => {
    expect(keysReady(settingsOf(["深度求索 DeepSeek", "深度求索 DeepSeek", "深度求索 DeepSeek"], { deepseek: "  " }))).toBe(false);
    expect(keysReady(settingsOf(["深度求索 DeepSeek", "深度求索 DeepSeek", "深度求索 DeepSeek"], { deepseek: "sk-x" }))).toBe(true);
    expect(keysReady(settingsOf(["深度求索 DeepSeek", "OpenAI 兼容", "深度求索 DeepSeek"], { deepseek: "sk-x" }))).toBe(false);
  });

  it("allSeatsSet：四席都选好才算可运行；空席不会被 keysReady 当成通过", () => {
    expect(allSeatsSet(DEFAULT_SETTINGS)).toBe(true);
    expect(allSeatsSet({ ...DEFAULT_SETTINGS, executor: UNSET_ROLE })).toBe(false);
    // 空席不产生 key 要求 ⇒ 在其余厂商的 Key 已填时 keysReady 仍可能为 true，所以运行前必须另查 allSeatsSet
    const restHaveKeys = {
      ...DEFAULT_SETTINGS,
      executor: UNSET_ROLE,
      apiKeys: { deepseek: "sk-deepseek", kimi: "sk-kimi" },
    };
    expect(keysReady(restHaveKeys)).toBe(true);
  });
});

suite("思考强度", () => {
  it("四档：关/低/中/高，默认中", () => {
    expect(THINKING_LEVELS.map((item) => [item.value, item.label])).toEqual([
      ["off", "关"],
      ["low", "低"],
      ["medium", "中"],
      ["high", "高"],
    ]);
    expect(DEFAULT_SETTINGS.executor.thinking).toBe("medium");
    expect(DEFAULT_SETTINGS.reviewer.thinking).toBe("medium");
    expect(DEFAULT_SETTINGS.acceptor.thinking).toBe("medium");
  });

  it("思考强度随角色保存并回读", () => {
    saveSettings({
      ...DEFAULT_SETTINGS,
      orchestrator: { vendor: "智谱 AI GLM", model: "glm-5.3", thinking: "high" },
      executor: { vendor: "阿里云通义千问", model: "qwen3.8-max", thinking: "high" },
      reviewer: { vendor: "月之暗面 Kimi", model: "kimi-k2.7-code", thinking: "low" },
      acceptor: { vendor: "OpenAI 兼容", model: "gpt-5.6-sol", thinking: "off" },
      apiKeys: { zhipu: "sk-glm", qwen: "sk-qwen", kimi: "sk-kimi", "openai-compat": "sk-compat" },
      openaiCompat: { baseUrl: "https://api.example.com/v1" },
    });
    expect(getSettings().orchestrator.thinking).toBe("high");
    expect(getSettings().executor.thinking).toBe("high");
    expect(getSettings().reviewer.thinking).toBe("low");
    expect(getSettings().acceptor.thinking).toBe("off");
    const html = renderToStaticMarkup(createElement(SettingsModal, { open: true, onClose: () => {} }));
    expect(html).toContain('aria-label="统筹模型思考强度"');
    expect(html).toContain('aria-label="执行模型思考强度"');
    expect(html).toContain('aria-label="审查模型思考强度"');
    expect(html).toContain('aria-label="验收模型思考强度"');
    expect(html).toContain("接入地址");
    expect(html).toContain("https://api.example.com/v1");
  });
});

suite("SettingsModal —— 打开形状与防呆", () => {
  it("打开时是 2px 直角 dialog：十二个自绘下拉触发器、只渲染当前坐人的厂商密钥框、无原生 select", () => {
    const html = renderToStaticMarkup(createElement(SettingsModal, { open: true, onClose: () => {} }));
    expect(html).toContain('role="dialog"');
    expect(html).toContain('aria-modal="true"');
    // 四席 × 三（厂商/型号/思考强度）= 12；路线 C 的路由区再四格（执行者、执行模型、审查员、验收员）⇒ 16。
    expect((html.match(/aria-haspopup="listbox"/g) ?? []).length).toBe(16);
    expect((html.match(/role="listbox"/g) ?? []).length).toBe(0);
    expect(html).not.toContain("<select");
    for (const label of [
      "统筹模型",
      "执行模型",
      "审查模型",
      "验收模型",
      "思考强度",
      "API 凭据",
      "最大并发数",
      "审查轮次上限",
    ]) {
      expect(html).toContain(label);
    }
    expect(html).toContain("国内 6 家 · 另有 OpenAI 兼容端点");
    expect(html).not.toContain("海外 4 家");
    expect(html).toContain("深度求索 DeepSeek API Key");
    expect(html).toContain("请输入 深度求索 DeepSeek API Key");
    expect(html).not.toContain("接入地址");
    // 出厂配置坐了两家厂商（DeepSeek ×3 + 月之暗面 Kimi ×1），所以是两把而不是一把
    expect((html.match(/type="password"/g) ?? []).length).toBe(2);
    expect(html).toContain("保存配置");
    expect(html).toContain("取消");
    expect(html).not.toMatch(/rounded-(?:lg|xl|2xl|md)/);
  });

  it("关闭时什么都不渲染", () => {
    const html = renderToStaticMarkup(createElement(SettingsModal, { open: false, onClose: () => {} }));
    expect(html).toBe("");
  });

  it("默认配置：三席 DeepSeek + 一席 Kimi，未填时点名缺哪把但不阻塞保存", () => {
    saveSettings({ ...DEFAULT_SETTINGS });
    const html = renderToStaticMarkup(createElement(SettingsModal, { open: true, onClose: () => {} }));
    expect(html).not.toContain("阿里云通义千问 API Key");
    // 出厂审查席刻意不坐 DeepSeek（见 `DEFAULT_SETTINGS` 上的注释），所以是两把而不是一把
    expect((html.match(/type="password"/g) ?? []).length).toBe(2);
    expect(html).toContain("请输入 深度求索 DeepSeek API Key");
    expect(html).toContain("请输入 月之暗面 Kimi API Key");
    expect(html).toContain("未填写 深度求索 DeepSeek API Key");
    expect(html).not.toContain("disabled");
    expect(html).toContain("保存配置");
  });

  it("密钥只留本次会话：保存时 localStorage 里不出现密钥值", () => {
    const store = new Map<string, string>();
    const previous = (globalThis as { localStorage?: Storage }).localStorage;
    Object.defineProperty(globalThis, "localStorage", {
      configurable: true,
      value: {
        getItem: (key: string) => store.get(key) ?? null,
        setItem: (key: string, value: string) => store.set(key, value),
      },
    });
    try {
      saveSettings({ ...DEFAULT_SETTINGS, apiKeys: { deepseek: "sk-test-secret" } });
      expect(getSettings().apiKeys.deepseek).toBe("sk-test-secret");
      const raw = store.get("acp-settings-v4") ?? "";
      expect(raw).not.toContain("sk-test-secret");
      expect(JSON.parse(raw).apiKeys).toEqual({});
    } finally {
      if (previous) {
        Object.defineProperty(globalThis, "localStorage", { configurable: true, value: previous });
      } else {
        delete (globalThis as { localStorage?: Storage }).localStorage;
      }
    }
  });

  it("四席用四家厂商时，四把密钥各就各位；保存过的型号与密钥要回显", () => {
    saveSettings({
      ...DEFAULT_SETTINGS,
      orchestrator: { vendor: "智谱 AI GLM", model: "glm-5.3", thinking: "medium" },
      executor: { vendor: "阿里云通义千问", model: "qwen3.8-max", thinking: "high" },
      reviewer: { vendor: "月之暗面 Kimi", model: "kimi-k2.7-code", thinking: "low" },
      acceptor: { vendor: "OpenAI 兼容", model: "gpt-5.6-sol", thinking: "medium" },
      openaiCompat: { baseUrl: "https://api.example.com/v1" },
      apiKeys: { zhipu: "sk-glm", qwen: "sk-qwen", kimi: "sk-kimi", "openai-compat": "sk-openai" },
    });
    expect(getSettings().executor.model).toBe("qwen3.8-max");
    const html = renderToStaticMarkup(createElement(SettingsModal, { open: true, onClose: () => {} }));
    expect(html).toContain("glm-5.3");
    expect(html).toContain("qwen3.8-max");
    expect(html).toContain("kimi-k2.7-code");
    expect(html).toContain("gpt-5.6-sol");
    expect(html).toContain('value="sk-glm"');
    expect(html).toContain('value="sk-qwen"');
    expect(html).toContain('value="sk-kimi"');
    expect(html).toContain('value="sk-openai"');
    expect(html).toContain('value="https://api.example.com/v1"');
    expect((html.match(/type="password"/g) ?? []).length).toBe(4);
    expect(html).not.toContain("请先填写");
  });

  it("OpenAI 兼容：接入地址与密钥都空时逐项点名", () => {
    saveSettings({
      ...DEFAULT_SETTINGS,
      orchestrator: UNSET_ROLE,
      executor: { vendor: "OpenAI 兼容", model: "gpt-5.6-sol", thinking: "medium" },
      reviewer: { vendor: "深度求索 DeepSeek", model: "deepseek-v4-pro", thinking: "medium" },
      acceptor: { vendor: "深度求索 DeepSeek", model: "deepseek-flash", thinking: "medium" },
      apiKeys: {},
      openaiCompat: { baseUrl: "" },
    });
    const html = renderToStaticMarkup(createElement(SettingsModal, { open: true, onClose: () => {} }));
    // 同一角色同一厂商的两项缺失合成**一条**待办（接入地址 + 它那把密钥），不再各占一行
    expect(html).toContain("未填写 接入地址、OpenAI 兼容 API Key");
    expect(html).toContain("未填写 深度求索 DeepSeek API Key");
    expect((html.match(/选用了 OpenAI 兼容/g) ?? []).length).toBe(1);
    expect(html).not.toContain("disabled");
  });

  it("混搭组合：Qwen + DeepSeek + 已填接入地址的兼容端点 → 只点名 DeepSeek 密钥", () => {
    saveSettings({
      ...DEFAULT_SETTINGS,
      orchestrator: UNSET_ROLE,
      executor: { vendor: "阿里云通义千问", model: "qwen3.8-max", thinking: "medium" },
      reviewer: { vendor: "深度求索 DeepSeek", model: "deepseek-v4-pro", thinking: "medium" },
      acceptor: { vendor: "OpenAI 兼容", model: "claude-opus-4-6", thinking: "medium" },
      openaiCompat: { baseUrl: "https://api.example.com/v1" },
      apiKeys: { qwen: "sk-qwen", "openai-compat": "sk-compat" },
    });
    const html = renderToStaticMarkup(createElement(SettingsModal, { open: true, onClose: () => {} }));
    expect(html).toContain("未填写 深度求索 DeepSeek API Key");
    expect(html).not.toContain("阿里云通义千问：未填写");
    expect(html).not.toContain("未填写接入地址");
    expect((html.match(/type="password"/g) ?? []).length).toBe(3);
  });
});

suite("四席与「未选择」", () => {
  it("未选择 = 空厂商 + 空型号；展示串留空，不编一个占位型号", () => {
    expect(roleSet(UNSET_ROLE)).toBe(false);
    expect(roleText(UNSET_ROLE)).toBe("");
    expect(roleText({ vendor: "深度求索 DeepSeek", model: "deepseek-v4-pro", thinking: "medium" })).toBe(
      "深度求索 DeepSeek · deepseek-v4-pro",
    );
    // 半清空（有厂商没型号）不算坐上了人 —— 这种脏组合不许被当成合法选择
    expect(roleSet({ vendor: "深度求索 DeepSeek", model: "", thinking: "medium" })).toBe(false);
  });

  it("未选择的那一席不产生密钥位，也不拖累 keysReady", () => {
    const seated = settingsOf(["深度求索 DeepSeek", "深度求索 DeepSeek", "深度求索 DeepSeek"], { deepseek: "sk-x" });
    expect(requiredKeyEntries(seated).map((entry) => entry.id)).toEqual(["deepseek"]);
    expect(keysReady(seated)).toBe(true);
  });

  it("四席同厂商只出现一把密钥；出厂配置按 统筹→执行→审查→验收 的顺序去重", () => {
    // 出厂：统筹/执行/验收 = DeepSeek，审查 = 月之暗面 Kimi
    expect(requiredKeyEntries(DEFAULT_SETTINGS).map((entry) => entry.id)).toEqual(["deepseek", "kimi"]);
    const allDeepSeek: SettingsState = {
      ...DEFAULT_SETTINGS,
      reviewer: { vendor: "深度求索 DeepSeek", model: "deepseek-flash", thinking: "medium" },
    };
    expect(requiredKeyEntries(allDeepSeek).map((entry) => entry.id)).toEqual(["deepseek"]);
  });

  it("未选中的那一格在设置里显示「未选择」，型号格写「先选厂商」而不是空下拉", () => {
    saveSettings({ ...DEFAULT_SETTINGS, orchestrator: UNSET_ROLE, apiKeys: { deepseek: "sk-x", kimi: "sk-k" } });
    const html = renderToStaticMarkup(createElement(SettingsModal, { open: true, onClose: () => {} }));
    expect(html).toContain("未选择");
    expect(html).toContain("先选厂商");
    // 未选席不点名要密钥
    expect(html).not.toContain("统筹模型选用了");
  });

  it("保存四席后原样回读，未选席不会被默认值顶掉", () => {
    saveSettings({
      ...DEFAULT_SETTINGS,
      orchestrator: UNSET_ROLE,
      executor: { vendor: "深度求索 DeepSeek", model: "deepseek-v4-pro", thinking: "medium" },
      reviewer: { vendor: "月之暗面 Kimi", model: "kimi-k3", thinking: "medium" },
      acceptor: { vendor: "深度求索 DeepSeek", model: "deepseek-flash", thinking: "medium" },
    });
    expect(getSettings().orchestrator.vendor).toBe("");
    expect(getSettings().orchestrator.model).toBe("");
    expect(roleText(getSettings().orchestrator)).toBe("");
  });
});
