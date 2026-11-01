import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, describe as suite, expect, it, vi } from "vitest";
import { runReadings } from "../src/lib/runReadings";
import {
  ApiError,
  cancelSettingsRun,
  getSettingsRun,
  startSettingsRun,
  type SettingsRunJob,
} from "../src/lib/acp";

/** 设置页四席直跑的三只 HTTP 面：202 启动、轮询读状态、POST 取消。
 *
 *  这一组只钉**发出去的报文**（路径 / 方法 / 头 / 体）与错误透传；
 *  屏上的阶段文案与取消按钮由 Python 守卫和 5180 DOM 实测覆盖。
 */

type Call = { url: string; init?: RequestInit };

function stubFetch(reply: { status?: number; body?: unknown } = {}): Call[] {
  const calls: Call[] = [];
  const status = reply.status ?? 200;
  vi.stubGlobal("fetch", async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    return {
      ok: status >= 200 && status < 300,
      status,
      json: async () => reply.body ?? {},
      text: async () => JSON.stringify(reply.body ?? {}),
    };
  });
  return calls;
}

const SEATS = {
  orchestrator: { vendor: "深度求索 DeepSeek", model: "deepseek-v4-pro", thinking: "off" },
  executor: { vendor: "深度求索 DeepSeek", model: "deepseek-v4-pro", thinking: "low" },
  reviewer: { vendor: "阿里云通义千问", model: "qwen3.8-flash", thinking: "medium" },
  acceptor: { vendor: "字节跳动豆包", model: "doubao-seed-2-1-lite-260915", thinking: "high" },
};

afterEach(() => vi.unstubAllGlobals());

suite("设置页四席直跑的 HTTP 面", () => {
  it("启动：POST 裸路径，四席、密钥与兼容端点原样进 body", async () => {
    const calls = stubFetch({ status: 202, body: { run_id: "run-1", status: "running" } });
    const started = await startSettingsRun({
      project_key: "demo",
      requirement: "做一个页面",
      seats: SEATS,
      api_keys: { deepseek: "sk-test" },
      openai_compat_base_url: "",
    });

    expect(started).toEqual({ run_id: "run-1", status: "running" });
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe("/requests/settings-run");
    expect(calls[0].init?.method).toBe("POST");
    const headers = calls[0].init?.headers as Record<string, string>;
    expect(headers["content-type"]).toBe("application/json");
    const body = JSON.parse(String(calls[0].init?.body));
    expect(body.seats.executor).toEqual(SEATS.executor);
    expect(body.api_keys).toEqual({ deepseek: "sk-test" });
  });

  it("思考强度逐席进 body：四席各带自己那一档，不是一份全局值复制四遍", async () => {
    // 2026-10-30 之前这一列只存在 localStorage：页面写「中」，请求体里一个字段都没有。
    const calls = stubFetch({ status: 202, body: { run_id: "run-1", status: "running" } });
    await startSettingsRun({
      project_key: "demo",
      requirement: "做一个页面",
      seats: SEATS,
      api_keys: { deepseek: "sk-d", qwen: "sk-q", doubao: "ark-k" },
      openai_compat_base_url: "",
    });
    const body = JSON.parse(String(calls[0].init?.body));
    expect(
      (["orchestrator", "executor", "reviewer", "acceptor"] as const).map((name) => body.seats[name].thinking),
    ).toEqual(["off", "low", "medium", "high"]);
    expect(new Set(Object.values(body.seats).map((seat: { thinking: string }) => seat.thinking)).size).toBe(4);
  });

  it("发起那一屏真的把 settings 里的 thinking 交出来（不是只改类型不改调用点）", () => {
    const screen = readFileSync(resolve(__dirname, "../src/components/studio/StudioScreen.tsx"), "utf8");
    for (const role of ["orchestrator", "executor", "reviewer", "acceptor"]) {
      expect(screen, role).toContain(`thinking: settings.${role}.thinking`);
    }
  });

  it("轮询与取消：run_id 做 URL 编码，取消是 POST", async () => {
    const job: SettingsRunJob = {
      run_id: "a/b",
      status: "running",
      stage: "execute",
      detail: "1/2 style.css",
      report: null,
      error: null,
      started_at: "2026-10-29T00:00:00Z",
      finished_at: null,
    };
    const calls = stubFetch({ body: job });

    expect((await getSettingsRun("a/b")).stage).toBe("execute");
    expect(calls[0].url).toBe("/requests/settings-run/a%2Fb");
    expect(calls[0].init?.method).toBeUndefined();

    await cancelSettingsRun("a/b");
    expect(calls[1].url).toBe("/requests/settings-run/a%2Fb/cancel");
    expect(calls[1].init?.method).toBe("POST");
  });

  it("读不到任务：404 与后端 detail 原样交给调用方", async () => {
    stubFetch({ status: 404, body: { detail: "settings_run_not_found：nope" } });
    const caught = await getSettingsRun("nope").catch((err: unknown) => err);
    expect(caught).toBeInstanceOf(ApiError);
    expect((caught as ApiError).status).toBe(404);
    expect((caught as ApiError).detail).toContain("settings_run_not_found");
  });
});

suite("代价读数：预算翻倍与传输重打（2026-10-31 落屏）", () => {
  it("0 与空表都读成 0，不隐藏", () => {
    expect(runReadings({ budget_doublings: { review: 0, repair: 0 }, transport_retries: {} })).toBe(
      "预算翻倍 0 · 传输重打 0",
    );
  });

  it("只列真翻过 / 真重打过的阶段，且带次数", () => {
    expect(
      runReadings({
        budget_doublings: { review: 0, repair: 1, "execute:style.css": 1 },
        transport_retries: { "execute:about.html": 2 },
      }),
    ).toBe("预算翻倍 repair×1 / execute:style.css×1 · 传输重打 execute:about.html=第2发");
  });

  it("缺字段（老后端 / 报告没有这两格）也不炸", () => {
    expect(runReadings({})).toBe("预算翻倍 0 · 传输重打 0");
  });
});
