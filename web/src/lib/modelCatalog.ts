/**
 * 模型目录：厂商 -> 型号清单的两级级联数据源。
 * 型号名是厂商 API 的代码值，必须逐字一致；这里只存 ID，不存说明文字。
 * 清单按 owner 2026-10-26 给的可选模型表收口，厂商上新时从这里改。
 */
export type VendorRegion = "国内" | "海外" | "自定义";

export interface VendorEntry {
  id: string;                              // 稳定标识：DOM id 与 apiKeys 的键
  vendor: string;                          // 展示名，也是角色选择里存的值
  region: VendorRegion;
  models: readonly string[];
  needsKey: boolean;
  customEndpoint?: boolean;                // 还需填一个自定义接入地址
}

export const MODEL_CATALOG: readonly VendorEntry[] = [
  // 2026-10-30 之前这份目录里的名字是**照产品叙事写的**，不是照厂商返回写的：豆包 8 个名字里
  // 7 个在账号里 404（`doubao-seed-2.1-lite` 的真名是 `doubao-seed-2-1-lite-260915`），
  // DeepSeek 那一档 6 个名字里只有 2 个能叫到。下面三家（DeepSeek / 千问 / 豆包）的名单
  // 逐条来自 owner 三把 key 的 `GET /models` 实测；kimi / 智谱 / MiniMax / OpenAI 兼容
  // **本轮没有 key，未核**，保持原样 —— 未核过不等于错，但也不许顺手写成"已验证"。
  // 复跑：`python scripts/check_model_catalog.py`（读环境变量里的三把 key，只报漂移不打印取值）。
  {
    id: "deepseek",
    vendor: "深度求索 DeepSeek",
    region: "国内",
    needsKey: true,
    models: ["deepseek-flash", "deepseek-v4-pro"],
  },
  {
    id: "qwen",
    vendor: "阿里云通义千问",
    region: "国内",
    needsKey: true,
    models: [
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
      // dashscope 也托管第三方型号：`deepseek-v4.1-flash` 这个名字**只在这条路上存在**，
      // 挂在 api.deepseek.com 下会 404。选它等于选"用千问的 key 走 DeepSeek 的模型"。
      "deepseek-v4.1-flash",
      "deepseek-v4-pro-0813",
    ],
  },
  {
    id: "doubao",
    vendor: "字节跳动豆包",
    region: "国内",
    needsKey: true,
    models: [
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
    ],
  },
  {
    id: "kimi",
    vendor: "月之暗面 Kimi",
    region: "国内",
    needsKey: true,
    models: ["kimi-k3", "kimi-k2.8-preview", "kimi-k2.7-code", "kimi-k2.7-code-highspeed", "kimi-k2.6"],
  },
  {
    id: "zhipu",
    vendor: "智谱 AI GLM",
    region: "国内",
    needsKey: true,
    models: ["glm-5.3", "glm-5.3-flash", "glm-5.3-flashx", "glm-5.2", "glm-4.7", "glm-4.7-flash", "glm-4.6"],
  },
  {
    id: "minimax",
    vendor: "MiniMax 稀宇科技",
    region: "国内",
    needsKey: true,
    models: ["MiniMax-M3.1", "MiniMax-M3.1-Flash", "MiniMax-M3"],
  },
  {
    id: "openai-compat",
    vendor: "OpenAI 兼容",
    region: "自定义",
    needsKey: true,
    customEndpoint: true,
    models: [
      "gpt-5.6-sol",
      "gpt-5.6",
      "gpt-5.6-mini",
      "claude-opus-4-6",
      "claude-sonnet-4-6",
      "claude-haiku-4-5",
      "gemini-3.8-flash",
    ],
  },
];

/** 旧显示名 → 现行显示名。本地设置里存过旧名字时，不把用户的选择判成“脏数据”回落默认。 */
const VENDOR_ALIASES: Readonly<Record<string, string>> = {
  DeepSeek: "深度求索 DeepSeek",
  阿里通义千问: "阿里云通义千问",
  字节豆包: "字节跳动豆包",
  "智谱 GLM": "智谱 AI GLM",
  MiniMax: "MiniMax 稀宇科技",
};

export function normalizeVendor(vendor: string): string {
  return VENDOR_ALIASES[vendor] ?? vendor;
}

/** 按厂商名取目录项；查不到说明数据已过期，调用方需自行回落。 */
export function vendorEntry(vendor: string): VendorEntry | undefined {
  const name = normalizeVendor(vendor);
  return MODEL_CATALOG.find((entry) => entry.vendor === name);
}

/** 取某厂商的型号清单；未知厂商返回空数组而不是抛错，保证 UI 始终可渲染。 */
export function modelsFor(vendor: string): readonly string[] {
  return vendorEntry(vendor)?.models ?? [];
}
