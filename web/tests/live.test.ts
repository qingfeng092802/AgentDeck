import { describe, expect, it, vi } from "vitest";
import type { ActivityRow, EvidenceArtifact, EvidenceVerdict, EvidenceView, TaskSummary, TicketSummary } from "../src/lib/acp";
import { artifactsOfCard, boardPhase, deliveryBlockers, mapEvent, mapEvidence, mapStatus, mapTask, phaseOf, planOf, requestPlan, SseBatcher, suspendedOf } from "../src/lib/live";
import { DEGRADED_SPLIT_SOURCES, splitSourceOf } from "../src/lib/live";
import { forgetSession, NEEDS_HUMAN_CODE, readSession, rememberSession, stageOf } from "../src/lib/live";
import {
  ADD_SECTION,
  hasAdditions,
  LEGACY_ROUNDS,
  MEETING_ROLES,
  meetingTurnsOf,
  ROUNDS_SECTION,
  roundsOf,
  speakerAt,
} from "../src/lib/live";
import { adoptTasks, adoptTurns, dagGeometry, getStudio, setSource, tick } from "../src/lib/studioEngine";
import type { ArtifactRecord, ReviewRecord } from "../src/lib/studioEngine";

/** 适配层的判据：**映射错了不会报错，只会让屏幕说假话**。
 *  这一组钉的是三件事：15 个域值一个不漏、没有来源的字段不被编出来、一张单只上一块看板。 */

function task(over: Partial<TaskSummary> = {}): TaskSummary {
  return {
    id: "T01",
    feature_request_id: "11111111-2222-3333-4444-555555555555",
    project_key: "real_demo",
    request_status: "SPLIT",
    request_created_at: "2026-10-27T10:00:00",
    title: "搭首页骨架",
    status: "READY",
    depends_on: [],
    executor_agent_id: null,
    reviewer_agent_id: null,
    acceptor_agent_id: null,
    difficulty: 2,
    skill_tags: ["fe"],
    handoff_count: 0,
    acceptance_criteria: ["AC-1"],
    family: "build",
    artifacts: [],
    ...over,
  };
}

describe("mapStatus：域值一个不漏", () => {
  it("15 个 EN-02 状态都有明确落点", () => {
    const expected: Record<string, string> = {
      DRAFT: "wait",
      PENDING_APPROVAL: "wait",
      READY: "ready",
      RUNNING: "run",
      REWORK: "run",
      HANDED_OFF: "run",
      NEED_INFO: "run",
      ESCALATED: "fail",
      REJECTED: "fail",
      REVIEWING: "review",
      CHANGES_REQUESTED: "fail",
      ARTIFACT_INVALID: "fail",
      ACCEPTING: "review",
      DELIVERED: "done",
      DELIVERED_WITH_ISSUES: "done",
    };
    for (const [status, tone] of Object.entries(expected)) {
      expect(mapStatus(status), status).toBe(tone);
    }
  });

  it("不认识的域值落「待依赖」，绝不落「已完成」", () => {
    expect(mapStatus("SOMETHING_NEW")).toBe("wait");
  });
});

describe("phaseOf：阶段条四格", () => {
  it("立项/执行/门禁/交付各归其位", () => {
    expect(phaseOf("DRAFT")).toBe(1);
    expect(phaseOf("RUNNING")).toBe(2);
    expect(phaseOf("REVIEWING")).toBe(3);
    expect(phaseOf("DELIVERED")).toBe(4);
  });

  it("看板的当前阶段 = 第一个还有卡没完成的阶段，不是最靠后的那一格", () => {
    /* 真实单里交付阶段的文档卡会先完成；取最大值会让顶栏在执行中就说「已交付」。
       T01 已交付（phase 4）+ T02 运行中（phase 2）⇒ 当前阶段必须是 02。 */
    const tasks = [task({ id: "T01", status: "DELIVERED" }), task({ id: "T02", status: "RUNNING" })].map((row) => mapTask(row));
    expect(boardPhase(tasks)).toBe(2);
  });

  it("全部卡完成才报到交付", () => {
    const tasks = [task({ id: "T01", status: "DELIVERED" }), task({ id: "T02", status: "DELIVERED" })].map((row) => mapTask(row));
    expect(boardPhase(tasks)).toBe(4);
  });
});

describe("mapTask：没有来源的字段留空", () => {
  it("RUNNING 也不编进度（后端没有百分比）", () => {
    const mapped = mapTask(task({ status: "RUNNING" }));
    expect(mapped.status).toBe("run");
    expect(mapped.progress).toBe(0);
  });

  it("DELIVERED 的进度才满格", () => {
    expect(mapTask(task({ status: "DELIVERED" })).progress).toBe(1);
  });

  it("依赖与产物逐字照抄，不重排也不猜", () => {
    const mapped = mapTask(
      task({
        depends_on: ["T01", "T02"],
        artifacts: [{ task_id: "T03", writer_agent_id: "qoder", rel_path: "web/index.html", format: "html", bytes: 10, lines: 2 }],
      }),
    );
    expect(mapped.deps).toEqual(["T01", "T02"]);
    // 实时档**不设** rank：层级由 `dagGeometry()` 按 depends_on 算。
    // 塞数组下标会让四张卡各自成层、排成一条竖线（下标不是层号）。
    expect(mapped.rank).toBeUndefined();
    expect(mapped.artifact).toBe("web/index.html");
  });

  it("三岗全空时 owner 是空串（屏幕显示「—」），不是编一个 unassigned", () => {
    expect(mapTask(task()).owner).toBe("");
  });
});

describe("planOf：只为顶栏提供单号，不编 PRD", () => {
  it("页面/验收/选型一律为空", () => {
    const plan = planOf([task()], "real_demo", "11111111-2222-3333-4444-555555555555");
    expect(plan).not.toBeNull();
    expect(plan?.pages).toEqual([]);
    expect(plan?.ac).toEqual([]);
    expect(plan?.stack).toBe("");
    expect(plan?.prj).toBe("REQ-11111111");
  });

  it("没有任务就没有单号，不造一个", () => {
    expect(planOf([], "real_demo", null)).toBeNull();
  });
});

describe("mapEvent：留痕只写后端给了什么", () => {
  it("状态迁移被带到正文，kind 落在可视档位", () => {
    const row: ActivityRow = {
      id: 1,
      entity: "task_card",
      entity_id: "T02",
      kind: "TASK",
      actor_agent_id: "orchestrator",
      payload: { action: "assign", card_status_after: "READY" },
      created_at: "2026-10-27T10:20:30",
    };
    const mapped = mapEvent(row);
    expect(mapped.kind).toBe("info");
    expect(mapped.text).toContain("TASK");
    expect(mapped.text).toContain("assign");
    expect(mapped.text).toContain("READY");
    expect(mapped.t).toMatch(/^\d{2}:\d{2}:\d{2}$/);
  });

  it("告警类不降级成普通信息", () => {
    const base: ActivityRow = {
      id: 2,
      entity: "task_card",
      entity_id: "T02",
      kind: "GUARD_VIOLATION",
      actor_agent_id: "",
      payload: {},
      created_at: "2026-10-27T10:20:30",
    };
    expect(mapEvent(base).kind).toBe("danger");
    expect(mapEvent({ ...base, kind: "ARTIFACT" }).kind).toBe("art");
  });
});

/* 真链路的受理：单先立起来，卡再慢慢来。
 *
 * 这一组守的都是"时序"上的假话 —— 它们不会让页面报错，只会让页面在某一分钟里
 * 显示成另一件事：
 *   · 拆单要 70 秒，这 70 秒里受理台如果还是输入框，那颗按钮就还能再点一次 ——
 *     再点一次就是第二张需求单；
 *   · 会议台词是四轮跑完一次性落库的，每 1.5 秒读回来都是**完整快照**，
 *     按追加写，第四次读回来桌上就有 10 条重复台词；
 *   · 切到真链路之后还让 `tick()` 推进，页面就会在真数据旁边演一场假会议。 */
describe("真链路：单先立起来，卡再慢慢来", () => {
  const RAW = "做一个卖手冲咖啡的静态官网";
  const RID = "11111111-2222-3333-4444-555555555555";

  it("requestPlan 让受理台立刻翻成「已立项」，原话不会被后续刷新抹掉", () => {
    const early = requestPlan(RAW, RID);
    expect(early.raw).toBe(RAW);
    expect(early.prj).toBe("REQ-11111111");
    // 页面/验收/选型一律为空：后端没有这三个读面，不编
    expect(early.pages).toEqual([]);
    expect(early.ac).toEqual([]);

    // 卡回来之后 refresh() 用 planOf 重算这份 plan —— 原文必须跟着走
    const later = planOf([task({ feature_request_id: RID })], "real_demo", RID, early.raw);
    expect(later?.raw).toBe(RAW);
    expect(later?.cards).toBe(1);
  });

  it("adoptTurns 整段替换，不追加", () => {
    adoptTurns([{ who: "orchestrator", text: "A", t: "10:00:00" }]);
    adoptTurns([
      { who: "orchestrator", text: "A", t: "10:00:01" },
      { who: "pm", text: "B", t: "10:00:01" },
    ]);
    expect(getStudio().turns.map((turn) => turn.text)).toEqual(["A", "B"]);
  });

  it("切到 live 档清掉推演残留，且 tick 不再推进任何东西", () => {
    setSource("demo");
    adoptTasks([mapTask(task())], 2, requestPlan(RAW, RID));
    expect(getStudio().plan).not.toBeNull();

    setSource("live");
    expect(getStudio().plan).toBeNull();
    expect(getStudio().turns).toEqual([]);

    const before = getStudio().phase;
    tick(10000);
    expect(getStudio().phase).toBe(before);
    expect(getStudio().turns).toEqual([]);
  });
});

/* 会话落盘：一单要跑 3–7 分钟，中途 F5 之后必须能接回来。
 * 接不回来不是"少看一屏"——那张待批的单会从界面上彻底够不着（待办条只在 live 档渲染）。 */
describe("会话落盘", () => {
  it("写进去能读回来，清掉之后读不到，没有盘也不抛", () => {
    const hasStorage = "localStorage" in globalThis;
    expect(() => rememberSession("r-1", "demo")).not.toThrow();
    if (hasStorage) expect(readSession()).toEqual({ requestId: "r-1", projectKey: "demo" });
    else expect(readSession()).toBeNull();

    forgetSession();
    expect(readSession()).toBeNull();
  });

  it("形状不对就当没有会话，不让整屏起不来", () => {
    expect(readSession()).toBeNull();
  });
});

/* 受理链的段位是纯函数：它决定屏上说"在写"还是"没有"。
 * 2026-10-28 踩到的那次：单是文档节点上线前跑的，status 早已 SPLIT，
 * 而页面还在说「文档节点正在写」—— 它等的是一个不会发生的事。 */
describe("stageOf：单走完了就别再说「正在写」", () => {
  const docs = {
    prd: { title: "t", pages: [], acceptance: [], out_of_scope: [] },
    arch: null,
    tokens: null,
  };

  it("台词四轮 + 文档齐了 ⇒ 拆卡", () => {
    expect(stageOf(4, docs, "INTAKE")).toBe("split");
  });

  it("受理段已结束却没有文档 ⇒ 没有，不是正在写", () => {
    expect(stageOf(4, null, "SPLIT")).toBe("stalled");
    expect(stageOf(0, null, "SPLIT")).toBe("stalled");
  });

  it("还在受理中就按台词条数分段", () => {
    expect(stageOf(2, null, "INTAKE")).toBe("meeting");
    expect(stageOf(4, null, "INTAKE")).toBe("docs");
  });

  /* R-26 交人工（2026-10-28 方案二）。**带文档**是这一条的要害：
     文档节点排在拆单之前，所以"拆单要人定"的单几乎必然已经有 PRD/架构/令牌。
     若"文档齐了就算在拆卡"先判，这一档永远轮不到 —— 屏上会一直说"正在拆任务卡"，
     直到 5 分钟观察窗到期才翻成"停住了"。那是把"等人"演成"在跑"。 */
  it("归因码是 split_needs_human ⇒ 待人工干预，不是失败，也不是「正在拆卡」", () => {
    expect(stageOf(4, docs, "FAILED", NEEDS_HUMAN_CODE)).toBe("needs_human");
    expect(stageOf(4, null, "FAILED", NEEDS_HUMAN_CODE)).toBe("needs_human");
    expect(stageOf(0, null, "FAILED", NEEDS_HUMAN_CODE)).toBe("needs_human");
  });

  it("别的归因码 ⇒ 失败态，不是「停住了」", () => {
    expect(stageOf(4, null, "FAILED", "split_extra_field")).toBe("failed");
    expect(stageOf(4, null, "FAILED", null)).toBe("failed");
    expect(stageOf(4, null, "FAILED")).toBe("failed");
  });

  it("后端给了 last_error ⇒ 失败态，不能一直说「正在拆单」", () => {
    expect(stageOf(4, docs, "INTAKE", "split_failed", 2, false, "GateMisuse")).toBe("failed");
    expect(stageOf(0, null, "INTAKE", "split_failed", 2, false, "GateMisuse")).toBe("failed");
  });

  it("还没走完受理段就带着这个码 ⇒ 先按段位说，不提前喊停", () => {
    expect(stageOf(2, null, "INTAKE", NEEDS_HUMAN_CODE)).toBe("meeting");
  });

  it("这个码的字面量与后端那个常量逐字一致", () => {
    /* 两边各写一份字面量（前端不 import 后端）。这条钉的是"两份不许漂"：
       漂了的表现是屏上静默退回红色失败，而那正是这次要修的东西。 */
    expect(NEEDS_HUMAN_CODE).toBe("split_needs_human");
  });
});

/* 多轮会议（owner 2026-10-28 第 1 条）：一场会开几轮是**后端**按 `ACP_MEETING_ROUNDS` 定的，
 * 前端读 `summary_digest["会议轮数"]`。这一组钉的是"分母从哪儿来"与"换序后谁在说" ——
 * 两处错了都不报错，只让屏上说假话（"第 2 轮刚开始"被演成"会议已结束"、
 * 或者高亮着产品而实际在调总管）。 */
describe("多轮会议：分母是轮数 × 4，不是写死的 4", () => {
  const docs = {
    prd: { title: "t", pages: [], acceptance: [], out_of_scope: [] },
    arch: null,
    tokens: null,
  };

  it("轮数从 digest 读；读不到 / 读到坏值都按老单口径算 1 轮", () => {
    expect(roundsOf({ [ROUNDS_SECTION]: ["2"] })).toBe(2);
    expect(roundsOf({ [ROUNDS_SECTION]: ["3"] })).toBe(3);
    /* 读不到只出现在两种单上：多轮上线前的老单（真的只开 1 轮）、与受理任务还没起头的新单。
       前者用 1 才对得上它库里那 4 条台词；默认 2 会让老单永远差 4 条，判据卡在 `meeting` 不动。 */
    expect(roundsOf({})).toBe(LEGACY_ROUNDS);
    expect(roundsOf(null)).toBe(LEGACY_ROUNDS);
    expect(roundsOf({ [ROUNDS_SECTION]: ["两轮"] })).toBe(LEGACY_ROUNDS);
    expect(roundsOf({ [ROUNDS_SECTION]: [] })).toBe(LEGACY_ROUNDS);
  });

  it("总条数 = 轮数 × 4；没读到轮数时退成 4 条", () => {
    expect(MEETING_ROLES).toBe(4);
    expect(meetingTurnsOf(1)).toBe(4);
    expect(meetingTurnsOf(2)).toBe(8);
    expect(meetingTurnsOf(0)).toBe(4);
  });

  it("2 轮的第 4 条说完不算开完会 —— 这是这一条需求的核心", () => {
    /* 写死 4 的表现：第 1 轮刚说完，屏上就判"会议已结束"、进度条跳到"正在落文档"，
       而第 2 轮的三次模型调用还在后头 —— 那 1–2 分钟里屏上说的全是假话。 */
    expect(stageOf(4, null, "INTAKE", null, 2)).toBe("meeting");
    expect(stageOf(7, null, "INTAKE", null, 2)).toBe("meeting");
    expect(stageOf(8, null, "INTAKE", null, 2)).toBe("docs");
    // 老单（读不到轮数）仍然是 4 条说完就换段
    expect(stageOf(4, null, "INTAKE")).toBe("docs");
  });

  it("文档到了就是会议完了 —— 不看说到第几条", () => {
    /* 文档节点排在会议**之后**。会议中途断了（第 5 条就抛）而文档照常落的那种单，
       按条数判会一直说"会议进行中"，直到 5 分钟观察窗到期。 */
    expect(stageOf(4, docs, "INTAKE", null, 2)).toBe("split");
    expect(stageOf(5, docs, "INTAKE", null, 2)).toBe("split");
  });

  it("换序之后谁在说：第一轮按名册，第二轮起总管最后说", () => {
    /* 必须与后端 `acp.meeting.service.speaking_order` 一致：
       第二轮如果总管还第一个说，它就没有别人的新话可收敛，"拍板"只是把主持词再念一遍。 */
    expect([0, 1, 2, 3].map(speakerAt)).toEqual(["orchestrator", "pm", "cto", "designer"]);
    expect([4, 5, 6, 7].map(speakerAt)).toEqual(["pm", "cto", "designer", "orchestrator"]);
    expect(speakerAt(-1)).toBeNull();
  });

  it("追加需求之后的这一趟：状态还是上一趟的，不许拿它判「停住了」", () => {
    /* 追加不改 `status`（`extend_request` 刻意不推进状态机），所以这一趟里
       `status` 还是 `DELIVERED`/`SPLIT`。按正常规则判会立刻落到 `stalled`（没文档）
       或 `needs_human`（上一次拆单要人定的归因码还留在留痕里）—— 而它正在跑。 */
    expect(stageOf(0, null, "DELIVERED", "split_needs_human", 2, true)).toBe("meeting");
    expect(stageOf(3, null, "SPLIT", null, 2, true)).toBe("meeting");
    // 台词数到齐、文档还没重出来 ⇒ 在落文档，不是"停住了"
    expect(stageOf(8, null, "DELIVERED", null, 2, true)).toBe("docs");
    // 文档重出来了 ⇒ 拆卡（卡不会重拆，见 `_run` 里"有卡就不重拆"那条分叉）
    expect(stageOf(8, docs, "DELIVERED", null, 2, true)).toBe("split");
  });

  it("「追加」那一格是这单追加过需求的唯一凭据", () => {
    expect(hasAdditions({})).toBe(false);
    expect(hasAdditions(null)).toBe(false);
    expect(hasAdditions({ [ADD_SECTION]: [] })).toBe(false);
    expect(hasAdditions({ [ADD_SECTION]: ["10:24｜再加一个改价页面"] })).toBe(true);
  });

  it("两个分区名与后端的字面量逐字一致", () => {
    /* 与 `NEEDS_HUMAN_CODE` 同一条纪律：前端不 import 后端，能共享的只有字符串本身。
       漂了的表现是"轮数永远读不到"（退回 4 条）与"追加过也看不出来" —— 都不报错。 */
    expect(ROUNDS_SECTION).toBe("会议轮数");
    expect(ADD_SECTION).toBe("追加");
  });
});

/* 右栏两页（owner 2026-10-29）：文件总线与审查在真链路上一直是空的。
 * 根因不在前端 —— 真链路从来没往 `studio.artifacts` / `studio.reviews` 写过东西，
 * 那两格只有推演档会填。补上 `GET /requests/{id}/evidence` 之后，这一组钉的是
 * "屏幕上说的与库里记的是同一件事"，以及三件容易做错的映射：
 *   · 同名文件不许糊成一行（`a/index.html` 与 `b/index.html` 是两个产物）；
 *   · 一条 AC 属于整单，六张卡的审查里都有它 —— 去重后必须"全部卡都判成立"才算过；
 *   · 判据文字来自 PRD、证据文字来自审查侧，都可能带尖括号，必须转义。 */
describe("mapEvidence：右栏两页照抄证据面，不编", () => {
  const art = (over: Partial<EvidenceArtifact> = {}): EvidenceArtifact => ({
    rel_path: "qoder-workspace/index.html",
    task_card_id: "T02",
    writer_agent_id: "qoder",
    format: "html",
    bytes: 14924,
    lines: 379,
    state: "valid",
    ...over,
  });

  const verdict = (over: Partial<EvidenceVerdict> = {}): EvidenceVerdict => ({
    task_card_id: "T01",
    reviewer_agent_id: "doubao",
    verdict: "APPROVE",
    findings: [{ criterion: "含至少一条 @media 断点", met: true, evidence: "门禁代签" }],
    ...over,
  });

  const view = (over: Partial<EvidenceView> = {}): EvidenceView => ({
    request_id: "11111111-2222-3333-4444-555555555555",
    artifacts: [],
    verdicts: [],
    ...over,
  });

  it("空证据面就是空的 —— 三格都不编", () => {
    /* 这一条是这次修复的反面：真链路跑完那两页全空，人看到的第一反应是"坏了"。
       所以"确实没产出"必须表现为空清单，而不是退回推演档那几条写死的样例。 */
    const mapped = mapEvidence(view());
    expect(mapped.artifacts).toEqual([]);
    expect(mapped.reviews).toEqual([]);
    expect(mapped.gate).toEqual([]);
  });

  it("文件名取路径末段，整条路径留在 path 里 —— 同名文件才分得开", () => {
    const mapped = mapEvidence(
      view({
        artifacts: [
          art({ rel_path: "qoder-workspace/index.html" }),
          art({ rel_path: "qoder-workspace/admin/index.html", task_card_id: "T07" }),
        ],
      }),
    );
    expect(mapped.artifacts.map((item) => item.name)).toEqual(["index.html", "index.html"]);
    // 两条都在，且 path 不同 —— SidePanel 的 React key 用的是 `name-path`
    expect(mapped.artifacts.map((item) => item.path)).toEqual([
      "qoder-workspace/index.html",
      "qoder-workspace/admin/index.html",
    ]);
    expect(new Set(mapped.artifacts.map((item) => `${item.name}-${item.path}`)).size).toBe(2);
  });

  it("文件那一行的读数逐字照抄，没有来源的字段留空", () => {
    /* `at` 留空而不是编一个"刚刚"：证据面没有时间戳，编出来就与库里对不上。
       `card` 是卡号 —— 任务详情按它筛产物（见 `artifactsOfCard`），所以它必须一路带过来。
       `state` 是这一份的校验态（`valid` / `invalid` / `pending`）—— 交付清单靠它说清
       "交上来的哪几份没过校验"，压成布尔就分不出"没达标"与"还没校验"。 */
    const [row] = mapEvidence(view({ artifacts: [art()] })).artifacts;
    expect(row).toEqual({
      name: "index.html",
      path: "qoder-workspace/index.html",
      owner: "qoder",
      card: "T02",
      kind: "html",
      lines: 379,
      state: "valid",
      at: "",
    });
  });

  it("APPROVE 落 good，别的落 bad —— 不留第三档", () => {
    const mapped = mapEvidence(
      view({
        verdicts: [
          verdict({ task_card_id: "T01", verdict: "APPROVE" }),
          verdict({ task_card_id: "T02", verdict: "REJECT" }),
          verdict({ task_card_id: "T03", verdict: "CHANGES_REQUESTED" }),
        ],
      }),
    );
    expect(mapped.reviews.map((item) => item.k)).toEqual(["good", "bad", "bad"]);
    expect(mapped.reviews.map((item) => item.who)).toEqual(["doubao", "doubao", "doubao"]);
  });

  it("标题带卡号、结论与判据条数 —— 一行就说得清是哪张卡判的", () => {
    const mapped = mapEvidence(
      view({
        verdicts: [
          verdict({
            task_card_id: "T02",
            findings: [
              { criterion: "a", met: true, evidence: "" },
              { criterion: "b", met: false, evidence: "" },
            ],
          }),
        ],
      }),
    );
    expect(mapped.reviews[0].title).toBe("T02 · APPROVE · 2 条判据");
    // 正文一行一条，✓/✕ 跟着 met 走；没有证据就不拖一个破折号
    expect(mapped.reviews[0].body).toBe("✓ a<br>✕ b");
  });

  it("判据正文里的尖括号被转义 —— 它走 innerHTML", () => {
    const mapped = mapEvidence(
      view({
        verdicts: [
          verdict({
            findings: [{ criterion: "<script>x</script>", met: true, evidence: "a & b" }],
          }),
        ],
      }),
    );
    expect(mapped.reviews[0].body).toContain("&lt;script&gt;");
    expect(mapped.reviews[0].body).not.toContain("<script>");
    expect(mapped.reviews[0].body).toContain("a &amp; b");
  });

  it("一条 AC 出现在六张卡里只留一行，且要全部卡都判成立才算过", () => {
    /* 判据按文字去重（一条 AC 属于整单，不是属于某一张卡）。
       合并口径是"与"：任何一张卡判它不成立，这一格就不该亮。 */
    const ac = "产物文件名必须是 index.html";
    const mapped = mapEvidence(
      view({
        verdicts: [
          verdict({ task_card_id: "T01", findings: [{ criterion: ac, met: true, evidence: "" }] }),
          verdict({ task_card_id: "T02", findings: [{ criterion: ac, met: true, evidence: "" }] }),
          verdict({ task_card_id: "T03", findings: [{ criterion: ac, met: true, evidence: "" }] }),
          verdict({ task_card_id: "T04", findings: [{ criterion: ac, met: false, evidence: "缺页" }] }),
        ],
      }),
    );
    expect(mapped.gate).toHaveLength(1);
    expect(mapped.gate[0]).toEqual({ id: "AC-1", text: ac, on: false });
  });

  it("同一条判据两边都判成立才亮；编号按出现顺序重排", () => {
    const mapped = mapEvidence(
      view({
        verdicts: [
          verdict({
            findings: [
              { criterion: "样式表用 :root 变量", met: true, evidence: "" },
              { criterion: "375px 下不横向溢出", met: true, evidence: "" },
            ],
          }),
          verdict({
            task_card_id: "T02",
            findings: [
              { criterion: "样式表用 :root 变量", met: true, evidence: "" },
              { criterion: "375px 下不横向溢出", met: true, evidence: "" },
            ],
          }),
        ],
      }),
    );
    expect(mapped.gate).toEqual([
      { id: "AC-1", text: "样式表用 :root 变量", on: true },
      { id: "AC-2", text: "375px 下不横向溢出", on: true },
    ]);
  });

  it("空白判据不占一格 —— 免得屏上多出一条无字的空条", () => {
    const mapped = mapEvidence(
      view({
        verdicts: [verdict({ findings: [{ criterion: "   ", met: true, evidence: "" }] })],
      }),
    );
    expect(mapped.gate).toEqual([]);
  });
});

describe("层级：按 depends_on 算，不是按数组下标", () => {
  it("同一层的卡并排，不会各自占一列排成竖线", () => {
    /* 这条钉的是 2026-10-28 修掉的那个形状：`mapTask` 原来收数组下标当 `rank`，
       而 `dagGeometry.rankOf()` 第一行是 `if (task.rank != null) return task.rank`
       ⇒ 四张卡各自成层、排成一条竖线，明明 T02/T03 是同一层。
       下标是"它在数组里第几个"，不是"它在依赖树上第几层"。 */
    const rows = [
      task({ id: "T01", depends_on: [] }),
      task({ id: "T02", depends_on: ["T01"] }),
      task({ id: "T03", depends_on: ["T01"] }),
      task({ id: "T04", depends_on: ["T02", "T03"] }),
    ];
    adoptTasks(rows.map((row) => mapTask(row)), 2, null);
    dagGeometry(); // `gr` 由它回填 —— 看板画之前必然先调一次

    const layers = Object.fromEntries(getStudio().tasks.map((item) => [item.id, item.gr]));
    expect(layers).toEqual({ T01: 0, T02: 1, T03: 1, T04: 2 });
  });

  it("推演档的固定 rank 仍然优先：脚本给的层号不被重算覆盖", () => {
    /* 推演档每一格的位置是脚本排好的，`dagGeometry` 认那个值。
       可选化 `rank` 只是让实时档能空着，不是把推演档的排布交还给自动计算。 */
    adoptTasks(
      [
        { ...mapTask(task({ id: "T01" })), rank: 5 },
        { ...mapTask(task({ id: "T02", depends_on: ["T01"] })), rank: 7 },
      ],
      2,
      null,
    );
    dagGeometry();
    const layers = Object.fromEntries(getStudio().tasks.map((item) => [item.id, item.gr]));
    expect(layers).toEqual({ T01: 5, T02: 7 });
  });

  it("按类别分行：文档 / 执行逐层 / 门禁 / 汇总各占一行", () => {
    /* owner 2026-10-28：「不同任务的卡片竖向排列，同类任务横向排列」。
       真链路的现场是 T01（样式表）与 T90/T91/T92（PRD/架构/令牌）挤在同一行 ——
       它们**依赖层都是 0**，所以只按层号排就一定挤在一起，而一张样式表和一份 PRD 不是同一批活。 */
    const rows = [
      task({ id: "T01", depends_on: [], family: "build" }),
      task({ id: "T02", depends_on: ["T01"], family: "build" }),
      task({ id: "T03", depends_on: ["T01"], family: "build" }),
      task({ id: "T90", depends_on: [], family: "doc" }),
      task({ id: "T91", depends_on: [], family: "doc" }),
      task({ id: "T93", depends_on: [], family: "gate" }),
      task({ id: "T95", depends_on: [], family: "summary" }),
    ];
    adoptTasks(rows.map((row) => mapTask(row)), 2, null);
    dagGeometry();
    const placed = getStudio().tasks;
    const rowOf = Object.fromEntries(placed.map((item) => [item.id, item.row]));

    // 文档一行、执行两层、门禁一行、汇总一行
    expect(rowOf).toEqual({ T90: 0, T91: 0, T01: 1, T02: 2, T03: 2, T93: 3, T95: 4 });
    // 同一行的卡 y 相同、x 递增（"同类任务横向排列"）
    const doc = placed.filter((item) => item.row === 0);
    expect(new Set(doc.map((item) => item.gy)).size).toBe(1);
    expect(doc[0].gx).toBeLessThan(doc[1].gx as number);
    // `gr` 仍是**依赖层号**，不被行号顶掉：文档与执行第一层都是第 0 层，却不在同一行
    const layerOf = Object.fromEntries(placed.map((item) => [item.id, item.gr]));
    expect(layerOf.T90).toBe(0);
    expect(layerOf.T01).toBe(0);
    expect(rowOf.T90).not.toBe(rowOf.T01);
  });
});

describe("任务详情的产物按卡筛，不按执行者筛（owner 2026-10-29「任务完成没有产物」）", () => {
  /* 现场：打开一张已交付的卡（T06 · README），右栏写着「这张卡还没有已验证产物」，
     而库里 `qoder-workspace/README.md` 那一条明明是 `valid`。
     根因是这一格按 `owner` 筛 —— 而 owner 是"谁写的"，一个执行者连着写好几张卡。 */

  const row = (name: string, owner: string, card?: string) => ({
    name,
    owner,
    kind: "file",
    lines: 10,
    at: "",
    ...(card ? { card } : {}),
  });

  it("同一个执行者的两张卡各看各的 —— 不会把别人的文件摊到这张卡上", () => {
    const records = [
      row("style.css", "qoder", "T01"),
      row("index.html", "qoder", "T02"),
      row("products.html", "qoder", "T03"),
      row("README.md", "qoder", "T06"),
    ];
    expect(artifactsOfCard(records, { id: "T06", owner: "qoder" }).map((item) => item.name)).toEqual([
      "README.md",
    ]);
    expect(artifactsOfCard(records, { id: "T02", owner: "qoder" }).map((item) => item.name)).toEqual([
      "index.html",
    ]);
  });

  it("卡号与执行者不一致时按卡号算 —— owner 那一格对不上不该让清单变空", () => {
    /* 真链路里出现过卡上记 `qoder`、产物记模型名的情形；按 owner 筛会一份都筛不出来，
       屏上就是"没有产物"。按卡号筛与那两个名字谁写错无关。 */
    const records = [row("README.md", "deepseek-v4-flash", "T06")];
    expect(artifactsOfCard(records, { id: "T06", owner: "qoder" })).toHaveLength(1);
  });

  it("推演档没有卡号，退回按执行者筛（那里是唯一能用的口径）", () => {
    const records = [row("tokens.json", "ui"), row("prd.md", "pm")];
    expect(artifactsOfCard(records, { id: "K3", owner: "ui" }).map((item) => item.name)).toEqual([
      "tokens.json",
    ]);
  });

  it("两种口径混在一起时，带卡号的按卡号、没卡号的按人 —— 不会互相吞掉", () => {
    const records = [row("README.md", "qoder", "T06"), row("tokens.json", "qoder")];
    const got = artifactsOfCard(records, { id: "T06", owner: "qoder" }).map((item) => item.name);
    // 两条都算这张卡的：一条卡号对上，一条没有卡号但执行者对上。
    expect(got.sort()).toEqual(["README.md", "tokens.json"]);
  });
});

describe("交付清单空着时，说得清是哪张卡挡住的（owner 2026-10-29「只写 0 项」）", () => {
  /* 现场：`real_demo · 57bf3d03` 的 T04 判了 `ARTIFACT_INVALID`，而右栏「交付清单」只有
     「整单 0 项」和「还没有可交付的文件」。两句都对，却都不构成下一步动作 ——
     人读完只知道"没东西"，不知道该去动哪张卡。

     `deliveryBlockers` 就是那一格的"为什么"：只交事实（哪张卡、卡在哪、手上有什么），
     句子留给组件写。三条口径各有一条判据盯着。 */

  const artifact = (name: string, card: string, state: string): ArtifactRecord => ({
    name,
    owner: "qoder",
    kind: "html",
    lines: 40,
    at: "",
    card,
    state,
  });

  const review = (card: string, failed: string[]): ReviewRecord => ({
    k: "bad",
    who: "reviewer",
    title: `${card} · REJECT · ${failed.length} 条判据`,
    t: "",
    body: "",
    card,
    failed,
  });

  it("mapTask 把原态带上来 —— 六档的 `fail` 说不出「为什么停」", () => {
    /* 这一条是整块的地基：没有 `raw`，`ARTIFACT_INVALID` 与 `REJECTED` 在屏上都是 `fail`，
       而这两件事的下一步动作完全不同（补产物 / 看判据）。 */
    expect(mapTask(task({ status: "ARTIFACT_INVALID" })).raw).toBe("ARTIFACT_INVALID");
    expect(mapTask(task({ status: "ARTIFACT_INVALID" })).status).toBe("fail");
    expect(mapTask(task({ status: "REJECTED" })).raw).toBe("REJECTED");
  });

  it("只认这两种卡态：别的失败态不进这一格（裁决 2026-10-29）", () => {
    /* `ESCALATED` / `CHANGES_REQUESTED` 的下一步在治理台，不挡交付包这条路。
       把它们也列进来，交付清单会变成"什么问题都往里塞"的一锅。 */
    const tasks = [
      mapTask(task({ id: "T01", status: "DELIVERED" })),
      mapTask(task({ id: "T02", status: "CHANGES_REQUESTED" })),
      mapTask(task({ id: "T03", status: "ESCALATED" })),
      mapTask(task({ id: "T04", status: "ARTIFACT_INVALID" })),
      mapTask(task({ id: "T05", status: "REJECTED" })),
    ];
    const got = deliveryBlockers(tasks, [], [], false);
    expect(got.map((item) => item.task.id)).toEqual(["T04", "T05"]);
    expect(got.map((item) => item.state)).toEqual(["ARTIFACT_INVALID", "REJECTED"]);
  });

  it("一份都没交：文件数是 0（这是「去催执行器重跑」的那种情形）", () => {
    const tasks = [mapTask(task({ id: "T04", status: "ARTIFACT_INVALID" }))];
    const [blocker] = deliveryBlockers(tasks, [], [], false);
    expect(blocker.files).toBe(0);
    expect(blocker.rejected).toEqual([]);
  });

  it("交了但没过校验：点得出是哪几份没过（这是「去看门槛」的那种情形）", () => {
    const tasks = [mapTask(task({ id: "T04", status: "ARTIFACT_INVALID" }))];
    const records = [
      artifact("contact.html", "T04", "valid"),
      artifact("submit-contact.php", "T04", "invalid"),
      artifact("index.html", "T05", "invalid"),
    ];
    const [blocker] = deliveryBlockers(tasks, records, [], false);
    // 只算这张卡名下的：T05 那份不算进来（按卡筛，不按 owner）
    expect(blocker.files).toBe(2);
    expect(blocker.rejected).toEqual(["submit-contact.php"]);
  });

  it("证据面读不到时文件数记「不知道」，不许说成 0", () => {
    /* 2026-10-29 那次「右栏两页总是空的」同一个病根：把"我读不到"说成"你没有"。
       这里若返回 0，屏上就会写「一份文件都没交上来」—— 而库里可能明明有 27 份。 */
    const tasks = [mapTask(task({ id: "T04", status: "ARTIFACT_INVALID" }))];
    const [blocker] = deliveryBlockers(tasks, [], [], true);
    expect(blocker.files).toBe(null);
    expect(blocker.rejected).toEqual([]);
  });

  it("审查退回：带上是哪几条判据没过", () => {
    const tasks = [mapTask(task({ id: "T05", status: "REJECTED" }))];
    const reviews = [
      review("T05", ["页面在 375 宽下不横向滚动", "首屏 1 秒内可见"]),
      review("T04", ["与主题无关的一条"]),
    ];
    const [blocker] = deliveryBlockers(tasks, [], reviews, false);
    expect(blocker.failed).toEqual(["页面在 375 宽下不横向滚动", "首屏 1 秒内可见"]);
  });

  it("同一张卡审过好几轮时取最后一条结论 —— 旧判据早已作废", () => {
    /* 拿第一轮的判据解释今天为什么停，等于让人去修一个已经不存在的毛病。 */
    const tasks = [mapTask(task({ id: "T05", status: "REJECTED" }))];
    const reviews = [review("T05", ["旧的那条"]), review("T05", ["新的那条"])];
    expect(deliveryBlockers(tasks, [], reviews, false)[0].failed).toEqual(["新的那条"]);
  });

  it("按卡号排序：清单不许因为一次返回顺序就重排", () => {
    const tasks = [
      mapTask(task({ id: "T05", status: "REJECTED" })),
      mapTask(task({ id: "T02", status: "ARTIFACT_INVALID" })),
    ];
    expect(deliveryBlockers(tasks, [], [], false).map((item) => item.task.id)).toEqual(["T02", "T05"]);
  });

  it("推演档没有原态 ⇒ 一条都不挡（它没有后端，也就没有被拒收这回事）", () => {
    const simulated = [
      { id: "K1", title: "写 PRD", owner: "pm", phase: 1 as const, dur: 0, artifact: "prd.md", input: "", act: "", out: "", status: "fail" as const, progress: 0, round: 1, deps: [], hidden: false },
    ];
    expect(deliveryBlockers(simulated, [], [], false)).toEqual([]);
  });
});

describe("SseBatcher：50ms 内的多帧合并成一次 render", () => {
  it("三帧在同一窗口里只 flush 一次，且顺序不丢", () => {
    vi.useFakeTimers();
    try {
      const flushed: ActivityRow[][] = [];
      const batcher = new SseBatcher((rows) => flushed.push(rows), 50);
      batcher.push({ id: 1 } as ActivityRow);
      batcher.push({ id: 2 } as ActivityRow);
      batcher.push({ id: 3 } as ActivityRow);
      expect(flushed).toHaveLength(0);
      vi.advanceTimersByTime(49);
      expect(flushed).toHaveLength(0);
      vi.advanceTimersByTime(1);
      expect(flushed).toHaveLength(1);
      expect(flushed[0].map((row) => row.id)).toEqual([1, 2, 3]);
    } finally {
      vi.useRealTimers();
    }
  });

  it("cancel 之后不再 flush 已缓冲的帧", () => {
    vi.useFakeTimers();
    try {
      const flushed: ActivityRow[][] = [];
      const batcher = new SseBatcher((rows) => flushed.push(rows), 50);
      batcher.push({ id: 1 } as ActivityRow);
      batcher.cancel();
      vi.advanceTimersByTime(100);
      expect(flushed).toHaveLength(0);
    } finally {
      vi.useRealTimers();
    }
  });
});

/* P1-4：A 类卡点无超时 ⇒ 死单会永远停着。这条告警是**唯一**能看见它的地方，
   所以它报错的两种方式都要钉住：漏报（该报不报）与乱报（把新鲜单也说成挂起）。 */
describe("suspendedOf：挂太久的未决单", () => {
  const now = Date.parse("2026-10-30T00:00:00Z");
  const hoursAgo = (h: number) => new Date(now - h * 3600_000).toISOString();
  const ticket = (over: Partial<TicketSummary>): TicketSummary => ({
    ticket_id: "t-1",
    kind: "A_SPLIT",
    state: "PENDING",
    version: 0,
    priority: 1,
    task_card_id: null,
    feature_request_id: null,
    risk_exempt_eligible: false,
    timeout_at: null,
    created_at: hoursAgo(1),
    ...over,
  });

  it("一天之内不报：跨夜与开会都是正常的", () => {
    expect(suspendedOf([ticket({ created_at: hoursAgo(23) })], now)).toBeNull();
  });

  it("刚好 24 小时就报，且最少说 1 天", () => {
    expect(suspendedOf([ticket({ created_at: hoursAgo(24) })], now)).toEqual({
      count: 1,
      oldestDays: 1,
      oldestKind: "A_SPLIT",
    });
  });

  it("按最久那张报天数，多张时报总张数", () => {
    const alert = suspendedOf(
      [
        ticket({ ticket_id: "t-1", created_at: hoursAgo(30) }),
        ticket({ ticket_id: "t-2", created_at: hoursAgo(24 * 4 + 2) }),
      ],
      now,
    );
    expect(alert).toEqual({ count: 2, oldestDays: 4, oldestKind: "A_SPLIT" });
  });

  it("已决单不算挂起：它已经有人处理过了", () => {
    expect(suspendedOf([ticket({ state: "APPROVED", created_at: hoursAgo(72) })], now)).toBeNull();
  });

  it("created_at 读不出来就不计入，不猜一个时间", () => {
    expect(suspendedOf([ticket({ created_at: "not-a-date" })], now)).toBeNull();
    expect(suspendedOf([ticket({ created_at: "" })], now)).toBeNull();
  });
});

describe("splitSourceOf：这批卡是怎么来的（2026-10-31 兜底拆单留痕）", () => {
  it("认识的四个值原样过去；历史行的 null 也原样是 null", () => {
    expect(splitSourceOf({ split_source: "llm" })).toBe("llm");
    expect(splitSourceOf({ split_source: "prd" })).toBe("prd");
    expect(splitSourceOf({ split_source: "fallback_stub" })).toBe("fallback_stub");
    expect(splitSourceOf({ split_source: "stub" })).toBe("stub");
    expect(splitSourceOf({ split_source: null })).toBeNull();
    expect(splitSourceOf({})).toBeNull();
    expect(splitSourceOf(undefined)).toBeNull();
  });

  it("不认识的值一律 null —— 宁可不标，也不把未知画成模型拆的", () => {
    expect(splitSourceOf({ split_source: "LLM" })).toBeNull();
    expect(splitSourceOf({ split_source: "真的模型" })).toBeNull();
  });

  it("降级集合就是那两个值（多了少了都会让横幅在错的单上出现或消失）", () => {
    expect([...DEGRADED_SPLIT_SOURCES]).toEqual(["fallback_stub", "stub"]);
  });
});
