/** 实时数据源：把后端的三条读面（任务列表 / 活动流 / 受理回执）搬进引擎。
 *
 *  三条设计约束，都是"别把不知道说成知道"：
 *
 *  1. **映射是显式的、可读的**。后端的 15 个任务状态压进屏幕上的 6 档，每一步都在
 *     `STATUS_MAP` / `PHASE_MAP` 里成文 —— 不做"看起来差不多就归一类"的模糊匹配，
 *     因为压错一档的表现是"这卡显示完成了，其实它还在复审"。
 *  2. **没有读面的字段留空**。后端不提供进度百分比、不提供会议台词，
 *     所以 `progress` 一律 0、`act` 一律空 —— 由 `actOf()` 决定"没有进度就只说状态"。
 *     在适配层编一个 40% 出来，整屏读数就全废了。
 *  3. **错误原样上屏**。`ApiError.detail` 是后端自己写的拒因（中文、带键名），
 *     翻译一遍就会出现"后端改了口径而前端还在说旧话"。
 */

import {
  ApiError,
  extendRequest,
  fetchDelivery,
  fetchEvidence,
  fetchRequest,
  listApprovals,
  listTasks,
  openActivityStream,
  readProjectKey,
  submitRequest,
  writeProjectKey,
  type ActivityRow,
  type EvidenceView,
  type ExtendResponse,
  type IntakeResponse,
  type RequestView,
  type TaskSummary,
  type TicketSummary,
} from "./acp";
import {
  adoptEvidence,
  adoptTasks,
  adoptTurns,
  blockingOf,
  DEFAULT_PALETTE,
  esc,
  getStudio,
  pushLiveEvent,
  setLive,
  setSource,
  type ArchDoc,
  type ArtifactRecord,
  type BlockingState,
  type Docs,
  type GateItem,
  type LiveState,
  type Plan,
  type PrdDoc,
  type ReviewRecord,
  type StudioEventRecord,
  type StudioTask,
  type TaskStatus,
  type TokensDoc,
  type SuspendedAlert,
} from "./studioEngine";
import { currentRoutingPayload } from "./routing";

/* 文档类型定义在引擎里（`LiveState` 持有它），这里只做转出，调用方不必知道它住哪。 */
export type { ArchDoc, Docs, PrdDoc, TokensDoc };

/** EN-02 的 15 个状态 → 屏幕上那 6 档。**没有默认分支**：域里加了值而这里没跟上，
 *  `mapStatus` 会把它落进 `wait` 并在 `console.warn` 里点名 —— 宁可显示成"待依赖"，
 *  也不能静默显示成"已完成"。
 */
const STATUS_MAP: Readonly<Record<string, TaskStatus>> = {
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

/** 阶段条那四格：1 立项 / 2 执行 / 3 门禁 / 4 交付。 */
const PHASE_MAP: Readonly<Record<string, 1 | 2 | 3 | 4>> = {
  DRAFT: 1,
  PENDING_APPROVAL: 1,
  READY: 2,
  RUNNING: 2,
  REWORK: 2,
  HANDED_OFF: 2,
  NEED_INFO: 2,
  ESCALATED: 2,
  REJECTED: 2,
  REVIEWING: 3,
  CHANGES_REQUESTED: 3,
  ARTIFACT_INVALID: 3,
  ACCEPTING: 3,
  DELIVERED: 4,
  DELIVERED_WITH_ISSUES: 4,
};

export function mapStatus(status: string): TaskStatus {
  const mapped = STATUS_MAP[status];
  if (mapped) return mapped;
  console.warn(`[acp] 未知任务状态 ${status}：已按"待依赖"显示，请补 STATUS_MAP`);
  return "wait";
}

export function phaseOf(status: string): 1 | 2 | 3 | 4 {
  return PHASE_MAP[status] ?? 1;
}

/** 一张后端任务卡 → 屏幕上的一张卡。
 *
 *  **不设 `rank`**：`rank` 是推演档的"固定层号"，而实时档的真实层级应当由 `dagGeometry()`
 *  按 `depends_on` 算。原来这里收一个 `rank` 参数、由 `rows.map(mapTask)` 传数组下标，
 *  结果是 `dagGeometry` 的 `rankOf()` 第一行 `if (task.rank != null) return task.rank` 直接命中，
 *  四张卡各自成层排成一条竖线 —— 而它们的依赖其实是扇形的。下标不是层号。
 *
 *  `deps` 直接取 `depends_on`，前端不再自己求拓扑。 */
export function mapTask(row: TaskSummary): StudioTask {
  const phase = phaseOf(row.status);
  return {
    id: row.id,
    title: row.title,
    /* 三岗都可能空（未派单的卡）：空串表示"还没人坐"，由 `agentMono("")` 显示成「—」。
       写一个 `unassigned` 之类的假 id 会让左栏多出一个不存在的参与者。 */
    owner: row.executor_agent_id ?? row.reviewer_agent_id ?? row.acceptor_agent_id ?? "",
    phase,
    dur: 0,
    artifact: row.artifacts[0]?.rel_path ?? "",
    input: "",
    act: "",
    out: "",
    status: mapStatus(row.status),
    /* 原态原样带上来：`status` 只留六档，交付清单要回答"为什么空"时得看得到
       `ARTIFACT_INVALID` / `REJECTED` 这两个名字（理由见 `StudioTask.raw`）。 */
    raw: row.status,
    progress: row.status === "DELIVERED" || row.status === "DELIVERED_WITH_ISSUES" ? 1 : 0,
    round: 1,
    deps: [...row.depends_on],
    hidden: false,
    /* 代签标记原样带上来，不做判断：这一格是后端读 EN-14 得出的，前端再推一遍就是第二份事实源。 */
    autoApproved: row.auto_approved === true,
    autoReview: row.auto_review ?? null,
    /* 类别也原样带：后端 `family_of()` 读的是**建卡那一刻**写下的标签，
       前端若按卡号段或标题去猜，猜错的方向是把一份 PRD 画进执行行里。 */
    family: (row.family as StudioTask["family"]) ?? "build",
  };
}

/** 屏上的"当前阶段"= **第一个还有卡没完成的阶段**。
 *
 *  不能取最大值：真实单里"交付"阶段的文档卡会在执行开始前就完成，取最大值会让顶栏
 *  在执行 0/5 的时候已经写「已交付」。也不能取第一张卡：同一阶段里卡的先后不代表阶段。
 *  按阶段桶找第一个未完成的桶，才是这一单真正卡在哪一步。 */
export function boardPhase(tasks: readonly StudioTask[]): number {
  const visible = tasks.filter((task) => !task.hidden);
  if (!visible.length) return 0;
  for (const phase of [1, 2, 3, 4]) {
    const scoped = visible.filter((task) => task.phase === phase);
    if (scoped.some((task) => task.status !== "done")) return phase;
  }
  return 4;
}

/* ---------------------------------------------------------------- 文档 */

/** `summary_digest` 的三段 → 结构化文档。缺席或坏掉的那一段记 `null`，不猜。 */
export function parseDocs(digest: Record<string, string[]> | undefined): Docs | null {
  if (!digest) return null;
  const take = <T>(key: string): T | null => {
    const lines = digest[key];
    if (!lines?.length) return null;
    try {
      return JSON.parse(lines[0]) as T;
    } catch {
      // 这一段坏了只影响这一格：另外两份还能用，没必要整组丢掉
      return null;
    }
  };
  const docs: Docs = { prd: take<PrdDoc>("PRD"), arch: take<ArchDoc>("架构"), tokens: take<TokensDoc>("令牌") };
  return docs.prd || docs.arch || docs.tokens ? docs : null;
}

/** 最小 Plan：只为让既有顶栏/右栏有"单号 + 标题"可显示。
 *
 *  文档到了之后，PRD / 架构 / 令牌三格**直接用后端结构**填 —— 不再拿前端的 `analyze()`
 *  去猜一份。那份是按关键词派生的，却长得跟真的一样，读者分不出哪一句是模型说的。
 *
 *  `raw` 由调用方给：任务卡回来之后 `refresh()` 会重算这份 plan，
 *  但受理卡上那句需求原文只存在于"刚才提交的那一单"里 —— 不传进来就会被空串覆盖掉。 */
export function planOf(
  rows: readonly TaskSummary[],
  projectKey: string,
  requestId: string | null,
  raw = "",
  docs: Docs | null = null,
): Plan | null {
  if (!rows.length) return null;
  const first = rows[0];
  const short = (requestId ?? first.feature_request_id).slice(0, 8);
  return {
    ...requestPlan(raw, requestId ?? first.feature_request_id, docs),
    title: `${projectKey || first.project_key} · ${short}`,
    prj: `REQ-${short}`,
    cards: rows.length,
  };
}

/** 刚提交、卡还没拆出来时的那份 plan。
 *
 *  它存在的唯一理由：受理台要靠 `plan` 非空才能从"输入框"翻成"已立项"。
 *  等拆单回来（实测 3–7 分钟）才翻页，中间那段时间页面会一直显示成一个还能再点一次的按钮 ——
 *  再点一次就是第二张需求单。 */
export function requestPlan(raw: string, requestId: string, docs: Docs | null = null): Plan {
  const short = requestId.slice(0, 8);
  const prd = docs?.prd ?? null;
  const arch = docs?.arch ?? null;
  const tokens = docs?.tokens ?? null;
  return {
    raw,
    type: "static",
    typeLabel: `需求单 ${short}`,
    feat: {
      responsive: false,
      cart: false,
      form: false,
      auth: false,
      payment: false,
      data: false,
      realtime: false,
      api: false,
      admin: false,
      seo: false,
      i18n: false,
    },
    /* 文档没到就是空数组，不编 —— 页面清单与判据只可能来自 PRD 那一段。 */
    pages: prd ? prd.pages.map((page) => ({ key: page.file.replace(/\.html$/, ""), label: page.label, file: page.file, keys: [] })) : [],
    palette: tokens
      ? {
          name: tokens.name,
          bg: tokens.bg,
          surface: tokens.surface,
          ink: tokens.ink,
          muted: tokens.muted,
          accent: tokens.accent,
          accent2: tokens.accent2,
          line: tokens.line,
        }
      : DEFAULT_PALETTE,
    stack: arch?.stack ?? "",
    why: arch?.why ?? "",
    risk: arch?.risk,
    ac: prd ? prd.acceptance.map((text, index) => ({ id: `AC-${index + 1}`, text })) : [],
    outOfScope: prd?.out_of_scope ?? [],
    title: prd?.title ?? `需求单 ${short}`,
    prj: `REQ-${short}`,
    cards: 0,
  };
}

const EVENT_KIND: Readonly<Record<string, StudioEventRecord["kind"]>> = {
  TASK: "info",
  APPROVAL: "warn",
  ARTIFACT: "art",
  REVIEW: "warn",
  SNAPSHOT: "sys",
  GUARD_VIOLATION: "danger",
  READONLY_VIOLATION: "danger",
  AUTONOMY_INTERCEPT: "warn",
  SELECTION: "info",
  split_fallback: "warn",
};

/** 拆单来源（`/tasks` 的 `split_source`）。后两者是**降级**：卡来自通用清单，不是模型拆的。 */
export type SplitSource = "llm" | "prd" | "fallback_stub" | "stub";

export const DEGRADED_SPLIT_SOURCES: readonly SplitSource[] = ["fallback_stub", "stub"];

/** 行上的 `split_source` → 引擎那一格。不认识的值一律 `null`：
 *  宁可不标，也不能把 `llm` 读成降级、或把历史行的 `null` 读成"模型拆的"。 */
export function splitSourceOf(row: { split_source?: string | null } | undefined): SplitSource | null {
  const raw = row?.split_source ?? null;
  return raw === "llm" || raw === "prd" || raw === "fallback_stub" || raw === "stub" ? raw : null;
}

/** 一帧留痕 → 一行活动流。正文只写"后端给了什么"，不补形容词。 */
export function mapEvent(row: ActivityRow): StudioEventRecord {
  const payload = row.payload ?? {};
  const status = typeof payload.card_status_after === "string" ? payload.card_status_after : "";
  const file = typeof payload.rel_path === "string" ? payload.rel_path : null;
  const reason = typeof payload.reason === "string" ? payload.reason : "";
  const head = `${row.kind} · ${row.entity}:${row.entity_id}`;
  const action = typeof payload.action === "string" ? payload.action : typeof payload.node === "string" ? payload.node : "";
  const text = [head, action, status ? `→ ${status}` : "", reason].filter(Boolean).join("  ");
  const at = new Date(row.created_at);
  const t = Number.isNaN(at.getTime())
    ? row.created_at
    : [at.getHours(), at.getMinutes(), at.getSeconds()].map((n) => String(n).padStart(2, "0")).join(":");
  return { t, a: row.actor_agent_id || "system", text, kind: EVENT_KIND[row.kind] ?? "info", file };
}

/* ---------------------------------------------------------------- 连接 */

let stream: EventSource | null = null;
let pollTimer: number | null = null;
let frameTimer: number | null = null;
let sseBatcher: SseBatcher | null = null;
let currentKey = "";

function nowHM(): string {
  const d = new Date();
  return [d.getHours(), d.getMinutes(), d.getSeconds()].map((n) => String(n).padStart(2, "0")).join(":");
}

function scheduleRefresh(delay = 600): void {
  if (!("window" in globalThis)) return;
  if (frameTimer !== null) window.clearTimeout(frameTimer);
  frameTimer = window.setTimeout(() => {
    frameTimer = null;
    void refresh();
  }, delay);
}

/** 读一次任务列表并搬进引擎。**这是唯一的"拉"**：SSE 只负责"有事发生"，
 *  数据仍以这条读面为准 —— 推送与读取走两个来源时，页面会长期停在旧形状。 */
export async function refresh(): Promise<void> {
  try {
    const pinned = getStudio().live.requestId;
    const all = await listTasks({
      projectKey: currentKey || undefined,
      featureRequestId: pinned ?? undefined,
    });
    /* **一张单一个看板**：卡号 `T01` 在每张需求单下都存在（EN-02 是复合主键），
       把 28 行全摆上屏会出现四个同 id 的节点，DAG 的 key 与选中都会串。
       取"当前单"：刚提交的那张，或列表里最新的那张（服务端按需求单从新到旧排）。
       已知单号时把过滤下推到服务端（`feature_request_id`）；否则先拉一次全量挑最新的。 */
    const wanted = pinned ?? all[0]?.feature_request_id ?? null;
    const rows = wanted ? all.filter((row) => row.feature_request_id === wanted) : all;
    const tasks = rows.map((row) => mapTask(row));
    /* 拆单来源跟着"当前这一单"走：同一屏只该有一种来源，取这一单的第一行（卡上的字段同值）。 */
    setLive({ splitSource: splitSourceOf(rows[0]) });
    adoptTasks(
      tasks,
      boardPhase(tasks),
      planOf(rows, currentKey, wanted, getStudio().plan?.raw ?? "", getStudio().live.docs),
    );
    if (wanted && wanted !== getStudio().live.requestId) setLive({ requestId: wanted, delivery: null });
    setLive({ status: "live", error: "", updatedAt: nowHM() });
    /* 证据面**另发一次**，而且它失败不影响上面那一步：
       文件总线与审查两页空了整整一轮（2026-10-29 用户报的），根因就是它们从来没人取。
       但它是旁证 —— 读不到就保持原样，不许把"两页没内容"升级成"整屏错误"。 */
    /* P1-4 的告警与"当前这一单"无关：挂起的是**任何**一张未决单，所以不挂在 wanted 上面 ——
       否则切到一张新单就看不见老的死单了。 */
    void loadSuspended();
    if (wanted) {
      void loadEvidence(wanted);
      void loadDelivery(wanted);
    }
  } catch (error) {
    const message = error instanceof ApiError ? `${error.status} ${error.detail}` : error instanceof Error ? error.message : String(error);
    setLive({ status: "error", error: message });
  }
}

/** 任务详情里"**这一张卡**的产物"。
 *
 *  owner 2026-10-29：「任务完成没有产物」—— 打开一张已交付的卡，右栏写着
 *  「这张卡还没有已验证产物」，而库里那一条明明是 `valid`。根因是这一格原来按 `owner` 筛：
 *
 *  - owner 是"谁写的"，一个执行者会连着写好几张卡（真链路里 T01–T06 全是 `qoder`）
 *    ⇒ 按 owner 筛会把同一个人的全部产物摊到每张卡的详情里（T02 的页面上出现 T03 的文件）；
 *  - 而 owner 对不上时（卡上记 `qoder`、产物记的是模型名）一份都筛不出来 ⇒ 屏上就是"没有产物"。
 *
 *  两个方向都错，所以按**卡号**筛。退回 `owner` 只服务推演档：那里没有真卡号（`card` 为空），
 *  按人筛是唯一能用的口径。
 *
 *  为什么提成具名函数：这条口径被一次真机截图判过"错"，得让判据能直接盯住它，
 *  而不是靠渲染一整块 React 组件去间接证明。
 */
export function artifactsOfCard(
  records: readonly ArtifactRecord[],
  task: { readonly id: string; readonly owner: string },
): ArtifactRecord[] {
  return records.filter((item) => (item.card ? item.card === task.id : item.owner === task.owner));
}

/** 交付清单里"为什么是空的"的一行。 */
export interface DeliveryBlocker {
  readonly task: StudioTask;
  readonly state: BlockingState;
  /** 这张卡名下总线上有几份文件。**`null` 不是 0**：证据面读不到时我们不知道有几份，
   *  说成"一份都没交上来"就是假话（2026-10-29 那次"两页总是空的"教训同源）。 */
  readonly files: number | null;
  /** 那几份里没过校验的文件名（`state === "invalid"`）。 */
  readonly rejected: readonly string[];
  /** 审查结论里没通过的判据原文。 */
  readonly failed: readonly string[];
}

/** 交付清单空着的时候，是哪几张卡把它挡住的。
 *
 *  owner 2026-10-29：「交付清单只写『0 项』，不写因为哪张卡没过」—— 那一格原本只报数，
 *  而数字本身不构成下一步动作。这里把"哪张卡、卡在哪一步、手上有什么"算出来，
 *  句子留给组件去写（这一层不生产文案，只交事实）。
 *
 *  三个刻意的口径：
 *
 *  1. **只认 `ARTIFACT_INVALID` / `REJECTED`**（`blockingOf` 上写着为什么不是"所有失败态"）；
 *  2. **产物按卡筛**（`artifactsOfCard`），不按 `owner` —— 同一个执行者连着写好几张卡；
 *  3. **`evidenceStale` 时文件数记 `null`**：读不到与没有是两件事，这一格不许混。
 *
 *  按卡号排序：清单要稳定，不能因为 `/tasks` 的返回顺序变一次就重排一次。
 */
export function deliveryBlockers(
  tasks: readonly StudioTask[],
  artifacts: readonly ArtifactRecord[],
  reviews: readonly ReviewRecord[],
  evidenceStale: boolean,
): DeliveryBlocker[] {
  const out: DeliveryBlocker[] = [];
  for (const task of tasks) {
    const state = blockingOf(task);
    if (!state || task.hidden) continue;
    const mine = artifactsOfCard(artifacts, task);
    /* 一张卡可能审过好几轮：取**最后一条**结论 —— 前面几轮的判据早已作废，
       拿旧结论解释今天为什么停，等于让人去修一个已经不存在的毛病。 */
    const verdict = [...reviews].reverse().find((item) => item.card === task.id);
    out.push({
      task,
      state,
      files: evidenceStale ? null : mine.length,
      rejected: evidenceStale ? [] : mine.filter((item) => item.state === "invalid").map((item) => item.name),
      failed: verdict?.failed ?? [],
    });
  }
  return out.sort((a, b) => a.task.id.localeCompare(b.task.id));
}

/** 一张**已完成**的卡，名下在总线上没有产物 —— 这是正交于六档的"失效"。
 *
 *  三种情形刻意分开：
 *  1. 证据面读不到（`evidenceStale`）：不知道，不判；
 *  2. 本卡名下有产物：正常；
 *  3. 本卡名下 0 份、而**同路径的最后写入者能指到别的卡**：才说"被覆盖"。
 *     指不到就返回 null —— 把"可能被覆盖"说成"被覆盖"和说成"没交"一样，都是假话。
 */
export interface StaleReading {
  readonly reason: string;
  readonly by: string | null;
}

export function staleOf(
  task: StudioTask,
  artifacts: readonly ArtifactRecord[],
  evidenceStale: boolean,
): StaleReading | null {
  if (evidenceStale || task.status !== "done") return null;
  if (artifactsOfCard(artifacts, task).length > 0) return null;
  /* 多文件串（`index.html / products.html / contact.html`）无法归因到单个路径；
     归不了因就不说"被覆盖" —— 这是这条判据最容易说假话的地方。 */
  if (!task.artifact || /[、,]|\s\/\s/.test(task.artifact)) return null;
  const name = task.artifact.split("/").filter(Boolean).pop() ?? "";
  const record = name ? artifacts.find((item) => item.name === name) : undefined;
  if (!record) return null;
  const other = record.card ? record.card !== task.id : record.owner !== task.owner;
  return other ? { reason: "被后面的卡覆盖", by: record.card ?? null } : null;
}

/** 详情页首屏那一句：**为什么停 + 下一步**。首句 ≤40 字，动作与屏上一个按钮一一对应。 */
export interface DetailSay {
  readonly line: string;
  readonly actionLabel: string;
  readonly action: "bus" | "review" | "feed";
  readonly tone: "block" | "timeout" | "stale";
}

function clipText(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max)}…` : text;
}

export function detailSay(input: {
  readonly task: StudioTask;
  readonly blocker?: DeliveryBlocker;
  readonly stale?: StaleReading | null;
  readonly stalled?: boolean;
  readonly elapsedMin?: number;
}): DetailSay | null {
  const { blocker, stale, stalled } = input;
  if (blocker) {
    if (blocker.state === "REJECTED") {
      const first = blocker.failed[0];
      return {
        line: first ? `审查退回：判据「${clipText(first, 18)}」没过。` : "审查退回：判据没全过，先看结论。",
        actionLabel: "查看判据",
        action: "review",
        tone: "block",
      };
    }
    if (blocker.files === null) {
      return {
        line: "文件总线这次没读到，只知道它停在产物上。",
        actionLabel: "查看文件总线",
        action: "bus",
        tone: "block",
      };
    }
    if (blocker.files === 0) {
      return {
        line: "名下没有产物，要么没交，要么已被后来的卡覆盖。",
        actionLabel: "查看文件总线",
        action: "bus",
        tone: "block",
      };
    }
    if (blocker.rejected.length) {
      return {
        line: `交上来的 ${blocker.files} 份里 ${blocker.rejected.length} 份没过校验。`,
        actionLabel: "查看文件总线",
        action: "bus",
        tone: "block",
      };
    }
    return {
      line: "产物还在，但门禁没过（多半卡在字节数或行数）。",
      actionLabel: "查看文件总线",
      action: "bus",
      tone: "block",
    };
  }
  if (stalled) {
    return {
      line: `观察窗到期，这一步 ${input.elapsedMin ?? 0} 分钟没有产出。`,
      actionLabel: "查看留痕",
      action: "feed",
      tone: "timeout",
    };
  }
  if (stale) {
    return {
      line: stale.by
        ? `产物被 ${stale.by} 覆盖，交付包里是别人的版本。`
        : "产物不在总线上，交付包里缺它这一份。",
      actionLabel: "查看文件总线",
      action: "bus",
      tone: "stale",
    };
  }
  return null;
}

/** 证据面 → 右栏那两页的读数。**纯函数**，判据直接量它（`web/tests/live.test.ts`）。
 *
 *  三条映射，每条都对着"屏上会说什么假话"：
 *
 *  1. **文件总线：一个文件一行**。后端已按 `rel_path` 取过最后一次写入 —— 同一份
 *     `about.html` 会被好几张卡先后写过，逐行铺开会出现 5 个同名文件，读起来像"产出了五份"。
 *  2. **审查：一条 verdict 一张卡**。`item_findings` 是逐条判据的 `met` 与证据，
 *     它既是这张卡的结论，也是「门禁比对」那张表的行 —— 两边**同源**，不许各算一份。
 *     `ACP_REVIEW_MODE=auto` 下这些结论由门禁代签（证据那行会写明），但它们是**真的**：
 *     不能因为"不是人签的"就把整页留空。
 *  3. **判据按文字去重**。一条 AC 属于整单，六张卡的审查里都会出现它；不去重会变成六行一样的。
 *     去重后 `on` 取"全部卡都判成立"——有一张没成立，这条就不算过。
 */
export function mapEvidence(evidence: EvidenceView): {
  artifacts: ArtifactRecord[];
  reviews: ReviewRecord[];
  gate: GateItem[];
} {
  const artifacts: ArtifactRecord[] = evidence.artifacts.map((item) => ({
    /* 显示用文件名取路径末段；整条路径留在 `path` 里当 React key 与悬停提示 ——
       不同目录下同名文件（`a/index.html` / `b/index.html`）靠它才分得开。 */
    name: item.rel_path.split("/").filter(Boolean).pop() ?? item.rel_path,
    path: item.rel_path,
    owner: item.writer_agent_id,
    /* 卡号：任务详情靠它筛"这一张卡的产物"。缺了它就只能按 owner 筛，
       而一个执行者连着写好几张卡（T01–T06 全是 `qoder`）⇒ 详情页要么串台要么全空。 */
    card: item.task_card_id,
    kind: item.format,
    lines: item.lines,
    /* 校验态原样带：`invalid`（写进来了却没达标）与 `pending`（校验还没跑）
       对交付清单说的下一句话不一样，压成布尔就分不开了。 */
    state: item.state,
    at: "",
  }));

  const reviews: ReviewRecord[] = evidence.verdicts.map((verdict) => ({
    k: verdict.verdict === "APPROVE" ? "good" : "bad",
    who: verdict.reviewer_agent_id,
    title: `${verdict.task_card_id} · ${verdict.verdict} · ${verdict.findings.length} 条判据`,
    card: verdict.task_card_id,
    failed: verdict.findings.filter((finding) => !finding.met).map((finding) => finding.criterion),
    t: "",
    /* `body` 走 `dangerouslySetInnerHTML`：判据文字来自 PRD、证据文字来自审查侧，
       两者都可能含尖括号，必须转义（与会议台词同一条纪律）。 */
    body: verdict.findings
      .map((finding) => {
        const mark = finding.met ? "✓" : "✕";
        const why = finding.evidence ? ` — ${esc(finding.evidence)}` : "";
        return `${mark} ${esc(finding.criterion)}${why}`;
      })
      .join("<br>"),
  }));

  const merged = new Map<string, boolean>();
  for (const verdict of evidence.verdicts) {
    for (const finding of verdict.findings) {
      const key = finding.criterion.trim();
      if (!key) continue;
      merged.set(key, (merged.get(key) ?? true) && finding.met);
    }
  }
  const gate: GateItem[] = [...merged].map(([text, on], index) => ({
    id: `AC-${index + 1}`,
    text,
    on,
  }));

  return { artifacts, reviews, gate };
}

/** 读一次证据面并搬进引擎。失败只留痕：它撑的是右栏两页的旁证，不是这一屏的主读数。
 *
 *  但**"读不到"与"没有"必须分开说**：`/evidence` 404/405 意味着后端根本没有这个读面
 *  （前端比后端新，或页面不是从这个后端打开的），那时两页显示"还是空的"就是一句假话 ——
 *  库里明明有产物。owner 2026-10-29 看到的正是这一句。所以这一类失败记成 `evidenceStale`，
 *  让两页改口说"读不到"。一次网络抖动（5xx / 断网）不算：那种情况留旧值，不下结论。 */
async function loadEvidence(requestId: string): Promise<void> {
  try {
    adoptEvidence(mapEvidence(await fetchEvidence(requestId)));
    if (getStudio().live.evidenceStale) setLive({ evidenceStale: false });
  } catch (cause) {
    const stale = cause instanceof ApiError && (cause.status === 404 || cause.status === 405);
    if (stale && !getStudio().live.evidenceStale) setLive({ evidenceStale: true });
  }
}

/** P1-4：A 类挂起告警的阈值。**24 小时**是产品口径，不是工程常数 ——
 *  一天之内没人处理是正常的（跨夜、开会），超过一天就是"这张单没人认领"。 */
export const SUSPEND_AFTER_MS = 24 * 60 * 60 * 1000;

/** 从**未决单**里挑出挂了太久的，给屏上一句"已挂起 N 天"；没有就返回 null。
 *
 *  三个刻意的口径：
 *  ① 只看 `state === "PENDING"`：已决单不该再报"挂起"；
 *  ② `created_at` 读不出来（空串/脏值）就**不计入**，不猜一个时间 —— 这条告警要能当证据用；
 *  ③ 天数向下取整且最少 1：刚过 24 小时说"1 天"，说"0 天"等于没说。
 */
export function suspendedOf(
  tickets: readonly TicketSummary[],
  now: number = Date.now(),
): SuspendedAlert | null {
  const stale = tickets.filter((ticket) => {
    if (ticket.state !== "PENDING") return false;
    const born = Date.parse(ticket.created_at);
    if (!Number.isFinite(born)) return false;
    return now - born >= SUSPEND_AFTER_MS;
  });
  if (!stale.length) return null;
  const oldest = stale.reduce((left, right) =>
    Date.parse(left.created_at) <= Date.parse(right.created_at) ? left : right,
  );
  const days = Math.floor((now - Date.parse(oldest.created_at)) / SUSPEND_AFTER_MS);
  return { count: stale.length, oldestDays: Math.max(1, days), oldestKind: oldest.kind };
}

async function loadSuspended(): Promise<void> {
  try {
    setLive({ suspended: suspendedOf(await listApprovals("PENDING")) });
  } catch {
    /* 读不到就保持原样：这是**旁证**，不许把"审批面读不到"升级成整屏错误。
       下一次 refresh 还会再试（SSE 有事件、定时轮询也在跑）。 */
  }
}

/** 读 EN-17 交付包索引。404 = 还没组包（合法状态），不升级成整屏错误。 */
async function loadDelivery(requestId: string): Promise<void> {
  try {
    const pkg = await fetchDelivery(requestId);
    setLive({
      delivery: {
        entryPath: pkg.entry_path,
        manifest: pkg.manifest.map((item) => ({
          relPath: item.rel_path,
          lines: item.lines,
          format: item.format,
        })),
        openIssues: pkg.open_issues.length,
      },
    });
  } catch (cause) {
    if (cause instanceof ApiError && cause.status === 404 && getStudio().live.delivery !== null) {
      setLive({ delivery: null });
    }
  }
}

/** 会议那一段的解析：`role｜text` → 两个字段。没有分隔符的行整段当正文。 */
export function parseMeetingLines(lines: readonly string[]): { role: string; text: string }[] {
  return lines.map((line) => {
    const at = line.indexOf("｜");
    return at === -1 ? { role: "unknown", text: line } : { role: line.slice(0, at), text: line.slice(at + 1) };
  });
}

/** 盯一单的受理链：每 1.5 秒读一次 `/requests/{id}`，把「会议」台词与三份文档搬进读数。
 *
 *  为什么是轮询而不是 SSE：台词与文档都是**整段写完才落库**的，SSE 只在"有事件"时推；
 *  而这里要的是"还没写完时也知道在跑"。一件事一次请求就够，不必为文档再开一条轮询。
 *
 *  窗口 5 分钟（200 × 1.5s）。原来是 120 秒，按"实测 45–60 秒"定的 —— 那个读数只对了一次：
 *  2026-10-28 那场会用了 **2 分 48 秒**（同一台机器、同一模型），探针当场过了窗口，
 *  于是台词明明落库了，屏上却一直"会议进行中"。模型侧延迟不受本仓控制，
 *  这里的窗口就该按"观察到的最慢"再留一倍，而不是按"最好的一次"。
 *
 *  顺手记 `stage`：会议 → 文档 → 拆卡。跑完还没等到的那一段由调用方按 `stalled` 说明，
 *  不许让一屏静止的字继续冒充"正在跑"。 */
const WATCH_POLL_MAX = 200;

export interface WatchSnapshot {
  readonly turns: { role: string; text: string }[];
  readonly docs: Docs | null;
  /** 后端当前给到的段名。排障用：`会议` 有了但 `PRD` 没有，是文档节点挂了而不是没开会。 */
  readonly sections: readonly string[];
}

/** 后端"拆单要人来定"的固定码（`acp.orchestrate.llm_split.NEEDS_HUMAN_CODE`）。
 *
 *  两边各写一份字面量而不是共享一个常量：前端不 import 后端，能共享的只有这个字符串本身。
 *  **它必须与后端那个常量逐字一致** —— 改一边不改另一边，屏上就静默退回红色失败
 *  （`tests/contract/test_split_contract.py` 与 `web/tests/live.test.ts` 各钉住自己那一侧）。
 */
export const NEEDS_HUMAN_CODE = "split_needs_human";

/** 受理链是否已经**停下**（不会再自己往前走）：`stalled` 已失败 / `needs_human` 等人。
 *
 *  五处渲染都要问这一句（撤骨架、撤打字机、结束会议段、进度条置灰、换措辞）。
 *  各写一遍 `stage === "stalled" || stage === "needs_human"` 迟早漏一处 ——
 *  漏掉的那一处表现是"骨架一直呼吸"或"会议一直说进行中"，而它等的是一件不会发生的事。
 *  停下来的两种成因在**说什么**上不同（见 `STEP_SAID`），在**停没停**上相同，所以判据只有这一支。 */
export function isParked(stage: LiveState["stage"]): boolean {
  return stage === "stalled" || stage === "needs_human" || stage === "failed";
}

/** P1-19：把 50ms 内的多条 SSE 帧合并成一次 render。
 *
 *  `openActivityStream` 是逐帧回调；6 个 Agent 同时推事件时，原来的写法每帧都
 *  `pushLiveEvent()` + `setLive()`，render 次数等于帧率。合并不改变数据顺序，
 *  只把"同一窗口内的 N 次 setState"压成 1 次 —— 这是 live 档最容易先崩的那一格。
 *  `cancel()` 只在断开连接时调用，避免定时器在卸载后还写一次状态。 */
export class SseBatcher {
  private buffer: ActivityRow[] = [];
  private timer: ReturnType<typeof setTimeout> | null = null;

  constructor(
    private readonly flush: (rows: ActivityRow[]) => void,
    private readonly delayMs: number = 50,
  ) {}

  push(row: ActivityRow): void {
    this.buffer.push(row);
    if (this.timer !== null) return;
    this.timer = setTimeout(() => {
      this.timer = null;
      const rows = this.buffer;
      this.buffer = [];
      if (rows.length) this.flush(rows);
    }, this.delayMs);
  }

  cancel(): void {
    if (this.timer !== null) {
      clearTimeout(this.timer);
      this.timer = null;
    }
    this.buffer = [];
  }
}

/* ---------------------------- 会议轮数 ---------------------------- */

/** 会议桌的座位，顺序即**第一轮**的发言顺序（与后端 `ROLE_SPECS` 同序）。
 *
 *  第二轮起后端把总管挪到最后做收敛（`acp.meeting.service.speaking_order`），
 *  所以"现在谁在说"不能只看下标 —— 用 `speakerAt()`，它带着那条换序规则。
 *  这一份和 `ROUND_ORDER` 是同一件事的两面（一个是"有谁"，一个是"这一轮什么顺序"），
 *  合成一个数组会让换序规则藏进下标运算里，宁可两行写清楚。 */
export const MEETING_SEATS = ["orchestrator", "pm", "cto", "designer"] as const;

/** 后端一场会几个岗位。**必须与 `ROLE_SPECS` 的条数一致**。 */
export const MEETING_ROLES = MEETING_SEATS.length;

/** digest 里"这一单开了几轮"那一格（`acp.meeting.service.ROUND_SECTION`）。
 *
 *  两边各写一份字面量（前端不 import 后端），**必须逐字一致** ——
 *  漂了的表现是屏上永远按 4 条台词判"会议结束"，第 2 轮刚开始就被判成结束，
 *  而配置看上去是对的。`web/tests/live.test.ts` 与后端的
 *  `tests/contract/test_meeting_service.py` 各钉住自己那一侧。 */
export const ROUNDS_SECTION = "会议轮数";

/** digest 里"客户追加的需求"那一格（`acp.serve_intake.ADD_SECTION`）。同上，逐字一致。 */
export const ADD_SECTION = "追加";

/** 读不到轮数时的口径：**1 轮**。
 *
 *  为什么不是"默认 2 轮"（后端 `DEFAULT_ROUNDS` 的值）：这一格缺只出现在两种单上 ——
 *  多轮上线**之前**跑的老单（它真的只开了 1 轮），与受理任务还没起头的新单（下一秒就会被
 *  `_meet` 写上的真值覆盖）。前者用 1 才对得上它库里那 4 条台词；反过来默认 2 会让老单
 *  永远差 4 条台词，判据卡在 `meeting` 一动不动，直到 5 分钟观察窗到期。 */
export const LEGACY_ROUNDS = 1;

/** 这一单开了几轮。`会议轮数` 那一格是后端每轮落库时一并写的，所以**第 1 条台词一到就有**。 */
export function roundsOf(digest: Record<string, readonly string[]> | null | undefined): number {
  const raw = String((digest?.[ROUNDS_SECTION] ?? [])[0] ?? "").trim();
  const n = Number.parseInt(raw, 10);
  return Number.isFinite(n) && n >= 1 ? n : LEGACY_ROUNDS;
}

/** 一场会总共几条台词。判"会议说完了没有"用的就是这个数，不是写死的 4。 */
export function meetingTurnsOf(rounds: number): number {
  return MEETING_ROLES * Math.max(1, rounds);
}

/** 这一单是不是被追加过需求（`追加` 那一格非空）。
 *
 *  它决定"会议要不要重开一遍"：追加之后后端会清掉 `会议` 段重跑一场，
 *  而**上一版的三份文档还留在 digest 里**（它们正是这一趟的修订底稿）。
 *  见 `stageOf` 的 `reMeeting` 那一段。 */
export function hasAdditions(digest: Record<string, readonly string[]> | null | undefined): boolean {
  return ((digest?.[ADD_SECTION] ?? []) as readonly string[]).length > 0;
}

/** 第 `turn` 条台词（从 0 数）是谁在说。**顺序必须与后端 `speaking_order` 一致**：
 *  第一轮按名册，第二轮起总管挪到最后做收敛。
 *
 *  为什么值得为它写一个函数而不是 `SEATS[turn % 4]`：漂了不报错，只是屏上高亮着产品
 *  而实际在调总管 —— 一个查不出来的假读数。`web/tests/live.test.ts` 钉住换序。 */
export function speakerAt(turn: number): string | null {
  if (!Number.isFinite(turn) || turn < 0) return null;
  const round = Math.floor(turn / MEETING_ROLES);
  const seat = turn % MEETING_ROLES;
  const order = round === 0 ? MEETING_SEATS : [...MEETING_SEATS.slice(1), MEETING_SEATS[0]];
  return order[seat] ?? null;
}

/** 受理链现在走到哪一段 —— 纯函数，判据直接量它。
 *
 *  五条规则，每条都对应一次真实踩坑：
 *  1. **追加需求之后的重开**（`reMeeting`）先判：这一趟 digest 里还躺着**上一版**的三份文档
 *     （它们是这次修订的底稿，后端故意不清），而台词从 0 重数。按下面几条判会立刻落到
 *     `split`（"文档在"）—— 屏上就说"正在拆任务卡"，而真正在发生的是四岗位重开了一场会。
 *     同理不能落到 `stalled`：状态早就是 `SPLIT`/`DELIVERED`，一判就是"停住了"；
 *  2. **R-26 卡点**：后端给的归因码是 `split_needs_human` ⇒ `needs_human`（这单在等人，
 *     不是失败）。为什么必须排在"文档齐了就算在拆卡"之前：文档节点排在拆单**之前**，
 *     所以"拆单要人定"的单几乎必然**有**文档 —— 按下面那条先判，这一档永远轮不到，
 *     屏上会一直说"正在拆任务卡"，直到 5 分钟观察窗到期才翻成"停住了"。
 *     那是把"等人"演成"在跑"，正是要修的东西；
 *  3. **文档到了 ⇒ 拆卡**：文档节点排在会议**之后**，它一到就说明会议已经走完 ——
 *     不管说到第几条。这一条同时盖住"会议说到第 5 条就断了、文档照常落"那种单：
 *     按台词条数判，它会一直显示"会议进行中"，而会议根本不会再往前走了；
 *  4. **单已经走过受理段（status 不再是 INTAKE）却还是没有文档** ⇒ `stalled`：
 *     文档不会再来。老单（文档节点上线前跑的）与 `docs_failed` 都落这一格 ——
 *     继续轮询会让屏上一直说"正在写"，而它等的是一个不会发生的事；
 *  5. 其余按台词条数分「会议」与「文档」两段。分母是 `轮数 × 4`，不是写死的 4。 */
export function stageOf(
  turns: number,
  docs: Docs | null,
  requestStatus: string,
  errorCode?: string | null,
  rounds: number = LEGACY_ROUNDS,
  reMeeting = false,
  lastError?: string | null,
): LiveState["stage"] {
  /* 追加后的这一趟：`status` 还是上一趟的（`DELIVERED` / `SPLIT`），拿它判"停住了"必错 ——
     这一单正在跑，只是它跑的是一趟不推进状态机的重出。所以这一格只按台词与文档分两段。 */
  if (reMeeting) {
    if (docs) return "split";
    return turns >= meetingTurnsOf(rounds) ? "docs" : "meeting";
  }
  if (requestStatus !== "INTAKE" && errorCode === NEEDS_HUMAN_CODE) return "needs_human";
  /* P1-5：失败必须排在"文档到了 ⇒ 正在拆卡"之前。拆单失败时 PRD 往往已经落库，
     先判 docs 会让屏上一直说"正在拆任务卡"，而链其实已经停了。 */
  if (String(lastError ?? "").trim()) return "failed";
  if (requestStatus === "FAILED") return "failed";
  if (docs) return "split";
  if (requestStatus !== "INTAKE") return "stalled";
  return turns >= meetingTurnsOf(rounds) ? "docs" : "meeting";
}

/** 进入某一段：**只在真的变了时**记时刻。
 *
 *  为什么不直接 `setLive({ stage, stageSince: now })`：`stage` 每 1.5 秒被同一句话重设一次，
 *  而 `stageSince` 每次都是新字符串 ⇒ "有变化"恒成立 ⇒ 每秒 emit 一次，且计时永远停在 0:00。
 *  屏上就变成一个不动的时间戳，比没有更糟。 */
function enterStage(stage: LiveState["stage"]): void {
  if (getStudio().live.stage === stage) return;
  setLive({ stage, stageSince: new Date().toISOString() });
}

export function watchRequest(
  requestId: string,
  onUpdate: (snapshot: WatchSnapshot) => void,
  onGiveUp?: () => void,
): () => void {
  let stopped = false;
  let attempts = 0;
  let timer = 0;
  setLive({ stage: "meeting", stageSince: new Date().toISOString(), lastError: "" });

  const poll = async (): Promise<void> => {
    if (stopped) return;
    attempts += 1;
    try {
      const view: RequestView = await fetchRequest(requestId);
      const digest = view.summary_digest ?? {};
      const turns = parseMeetingLines(digest["会议"] ?? []);
      /* 追加过需求的那一单：这一趟是**重开一场会**（后端已清掉 `会议` 与三份文档）。
         判据不能只看"台词还不够" —— 台词数到齐了、文档还没重出来的那半分钟里，
         它也还是这一趟，而那时按正常规则判会落到 `stalled`（状态还是上一趟的 `DELIVERED`）。
         所以两个条件都算：**台词没齐 或 文档没到**。 */
      const rounds = roundsOf(digest);
      const rawDocs = parseDocs(digest);
      const reMeeting = hasAdditions(digest) && (turns.length < meetingTurnsOf(rounds) || !rawDocs);
      const docs = reMeeting ? null : rawDocs;
      onUpdate({ turns, docs, sections: Object.keys(digest) });
      /* 写轮数的意义是**会议桌的分母**：`n / 8` 还是 `n / 4`，取决于这一格。
         `setLive` 自带"没变就不 emit"，所以这里不必先比一次。 */
      setLive({ rounds });
      /* 失败原因只在这一格说得出来（U6）。**只在真的变了时写**：轮询每 1.5 秒读一次，
         每次写一遍会让屏上那行字每 1.5 秒重排一次。 */
      const failure = String(view.last_error ?? "").trim();
      if (failure && failure !== getStudio().live.lastError) setLive({ lastError: failure });
      /* 两段都齐了才收手。会议 + 三份文档是同一条受理链上的两站，
         只按台词收手会让"文档到得比台词晚"的那一瞬间被漏掉。
         走完两段、或者"受理段已结束但文档不会再来"，都交给 `stageOf` 判 —— 规则只写一处。
         归因码一起传：`stalled` 与 `needs_human` 在同一格里分岔，判据是后端给的那个码。 */
      const stage = stageOf(
        turns.length,
        docs,
        view.status,
        view.last_error_code,
        rounds,
        reMeeting,
        getStudio().live.lastError,
      );
      if (stage === "split") {
        enterStage(stage);
        return;
      }
      if (isParked(stage)) {
        enterStage(stage);
        onGiveUp?.();
        return;
      }
      enterStage(stage);
    } catch {
      // 读不到就下一轮再试：这条链路失败不该弹错误盖住受理回执
    }
    if (attempts >= WATCH_POLL_MAX) {
      enterStage("stalled");
      onGiveUp?.();
      return;
    }
    timer = window.setTimeout(() => void poll(), 1500);
  };

  void poll();
  return () => {
    stopped = true;
    if (timer) window.clearTimeout(timer);
  };
}

export function connect(projectKey?: string): void {
  disconnect();
  currentKey = (projectKey ?? readProjectKey()).trim();
  /* **不清 `requestId`**：连接层的职责是"接到哪个项目"，不是"回到哪张单"。
     2026-10-27 修：`submit()` 已经先写好 requestId 再调 `connect()`，
     这里一清，紧接着的 `refresh()` 就把"当前单"退回到列表第一条 ——
     刚提交的新单被一张跑完的旧单顶掉，看板上会突然出现别人的卡。 */
  setLive({ status: "connecting", error: "", projectKey: currentKey, frames: 0, stage: "idle" });
  void refresh();
  if (!("window" in globalThis)) return;
  pollTimer = window.setInterval(() => void refresh(), 5000);
  if (!("EventSource" in globalThis)) {
    setLive({ status: "error", error: "这个浏览器没有 EventSource：活动流不可用，任务仍每 5 秒轮询" });
    return;
  }
  sseBatcher = new SseBatcher((rows) => {
    for (const row of rows) pushLiveEvent(mapEvent(row));
    setLive({ frames: getStudio().live.frames + rows.length });
    if (rows.some((row) => row.kind === "TASK" || row.kind === "APPROVAL" || row.kind === "REVIEW" || row.kind === "ARTIFACT")) {
      scheduleRefresh();
    }
  }, 50);
  stream = openActivityStream(
    (row) => sseBatcher?.push(row),
    (state) => {
      if (state === "open") setLive({ status: "live", error: "" });
      if (state === "closed") setLive({ status: "error", error: "活动流已断开：浏览器不再重连（刷新页面可恢复）" });
      if (state === "retry") setLive({ status: "connecting", error: "" });
    },
  );
}

export function disconnect(): void {
  if (("window" in globalThis)) {
    if (pollTimer !== null) window.clearInterval(pollTimer);
    if (frameTimer !== null) window.clearTimeout(frameTimer);
  }
  pollTimer = null;
  frameTimer = null;
  if (sseBatcher) {
    sseBatcher.cancel();
    sseBatcher = null;
  }
  if (stream) {
    stream.close();
    stream = null;
  }
  if (getStudio().live.status !== "idle") setLive({ status: "idle" });
}

/** 提交一条真实需求。**成功不等于拆完**：`intake_scheduled=false` 时后端连后台拆解都没排，
 *  页面只能说"已接单"，不许说"正在拆解" —— 这句话由调用方按回体决定。 */
export async function submit(raw: string): Promise<IntakeResponse> {
  const key = (getStudio().live.projectKey || readProjectKey()).trim();
  if (!key) throw new ApiError(422, "project_key_required：先填项目键（它会成为产物的目录名）");
  const response = await submitRequest(key, raw, currentRoutingPayload());
  writeProjectKey(key);
  /* 换了项目键就要换筛选口径：`refresh()` 按 `currentKey` 过滤任务，
     这里不同步，新单的卡会被旧项目的过滤条件挡在外面（表现为"提交成功但看板空着"）。 */
  currentKey = key;
  setLive({ requestId: response.request_id, status: "live", error: "" });
  /* 拆解在后台跑：800ms / 2.5s / 6s 各拉一次就够看到第一批卡。
     不用"一直轮询到有卡为止"—— 那张单可能压根没排上（`intake_scheduled=false`），
     无限轮询会把"没有拆解"表现成"还在加载"。 */
  scheduleRefresh(800);
  if (("window" in globalThis)) {
    window.setTimeout(() => void refresh(), 2500);
    window.setTimeout(() => void refresh(), 6000);
  }
  return response;
}

/** 在**这一单上**追加一条需求：四岗位带着上一版 PRD 重开一场会，只重出 PRD，不动已有卡。
 *
 *  owner 2026-10-28 第 2 条要的是"在已产生的 prd 的基础上增加"，不是"另开一单"——
 *  所以这里走 `/requests/{id}/extend`，而不是 `submit()`（那个会新建一条 `feature_request`）。
 *
 *  三件事，缺一件屏上就演错：
 *  1. **清掉会议桌上这一场的台词**：后端已经把 digest 的 `会议` 段清了，
 *     本地不清就是"8 条旧台词 + 新的第 1 条"，读起来像第 9 位在说话；
 *     而且 `meetingShape` 按条数判"会议已结束"，新的讨论反而看不见；
 *  2. **把三份文档从屏上撤下来**：后端已把它们挪进 `上一版`（那是修订底稿），
 *     屏上那三格要说的是"这一版正在写"。不清的话 `WaitDoc` 会认为文档已到，
 *     画不画骨架、说什么话全错；
 *  3. **重新盯这一单**：不重开轮询，屏上会停在"会议进行中"直到原来的观察窗到期。
 */
export async function extendLive(text: string): Promise<ExtendResponse> {
  const requestId = getStudio().live.requestId;
  if (!requestId) throw new ApiError(422, "no_request：这一屏还没有可追加的需求单");
  const response = await extendRequest(requestId, text);
  adoptTurns([]);
  setLive({ docs: null });
  watchInto(requestId);
  /* 这一趟**不重拆卡**（回体的 `cards` 恒等于旧值），所以只要拉一次任务列表确认卡还在，
     不用像 `submit()` 那样排三次。 */
  scheduleRefresh(800);
  return response;
}

/* 正在盯的那场会的取消句柄。换单时先取消旧的 —— 两场会同时往同一张会议桌上写台词，
   表现是"新产品经理说了一句旧需求的话"，而且查不出来是哪来的。 */
let meetingWatch: (() => void) | null = null;

/** 把「会议」段搬进会议桌。**转义是必须的**：会议桌那一格走 `dangerouslySetInnerHTML`
 *  （推演台词的 `<b>` 是真的 HTML 标记），而真台词会复述客户原话、原话是用户敲的。
 *
 *  2026-10-28：顺手把**未转义原文**也存一份（`raw`）。会议桌的打字机要按字符切片，
 *  在 `&amp;` 中间下刀会切出一个裸的 `&am` —— 切原文、每帧再转义就不会。
 */
function adoptMeetingLines(lines: readonly string[]): void {
  adoptMeetingTurns(parseMeetingLines(lines));
}

/** 逐轮到达的台词搬进会议桌。
 *
 *  **时间戳 = 第一次读到这一条的时刻**，之后不再重打。
 *  原先每次都写 `nowHM()`：后端四轮跑完一次性落库，四条同时到达，看不出问题；
 *  改成逐轮落库之后，每 1.5 秒轮询一次就会把**已经说完的**那几条一直刷成"刚刚" ——
 *  那是给读者的假精度（后端根本没存发言时间）。 */
function adoptMeetingTurns(turns: readonly { role: string; text: string }[]): void {
  const seen = new Map(getStudio().turns.map((turn) => [turn.who, turn.t]));
  adoptTurns(
    turns.map((turn) => ({
      who: turn.role,
      text: esc(turn.text),
      raw: turn.text,
      t: seen.get(turn.role) ?? nowHM(),
    })),
  );
}

/** 把三份文档搬进引擎，并**就地补一次 plan**。
 *
 *  为什么不能等下一次 `refresh()`：卡还没拆出来时 `planOf()` 返回 null（它要有 rows 才建 plan），
 *  而 PRD / 架构 / 令牌是在拆卡**之前**就到的。光把 docs 存起来，三格会一直空到卡回来 ——
 *  那正好是这类"文档看起来永远没有"的成因。 */
function applyDocs(docs: Docs): void {
  setLive({ docs });
  const current = getStudio();
  const requestId = current.live.requestId;
  if (!current.plan || !requestId) return;
  /* 卡已经回来过的单不在这里重算：`refresh()` 会用 `planOf` 带着 docs 一起建，
     那份还知道单号与卡数；这里覆盖会把它们抹成 0。 */
  if (current.plan.cards > 0) return;
  adoptTasks(current.tasks, current.phase, requestPlan(current.plan.raw, requestId, docs));
}

/** 盯一单的受理链。`startLive` 与 `resumeSession` 共用这一条。 */
function watchInto(requestId: string): void {
  meetingWatch?.();
  /* 起等时刻在这里落，不在 `startLive`：`resumeSession` 只在"会议还没跑完"时才走到这一行，
     刷新页面之后重新等的那一单，读数是"本次等了多久"，而不是拿提交时间冒充。 */
  setLive({ meetingSince: new Date().toISOString() });
  meetingWatch = watchRequest(requestId, (snapshot) => {
    if (snapshot.turns.length) adoptMeetingTurns(snapshot.turns);
    if (snapshot.docs) applyDocs(snapshot.docs);
  });
}

/* ------------------------------------------------------------ 会话落盘 */

/** 「当前这一单」。刷新页面之后必须能回到它 —— 一单要跑 3–7 分钟，
 *  中途 F5 就丢掉全部读数是这一屏最刺眼的失败：那单还在库里跑着，
 *  而屏上什么都没了，连"这一单到哪一步"都读不出来。 */
const LIVE_SESSION_KEY = "acp-live-session";

export interface LiveSession {
  requestId: string;
  projectKey: string;
}

export function rememberSession(requestId: string, projectKey: string): void {
  try {
    (globalThis as { localStorage?: Storage }).localStorage?.setItem(
      LIVE_SESSION_KEY,
      JSON.stringify({ requestId, projectKey } satisfies LiveSession),
    );
  } catch {
    // 隐私模式：这一单在本会话里仍然正常，只是刷新之后回不来
  }
}

export function forgetSession(): void {
  try {
    (globalThis as { localStorage?: Storage }).localStorage?.removeItem(LIVE_SESSION_KEY);
  } catch {
    // 同上
  }
}

/** 读回上一次的会话。形状不对就当没有 —— 读不出来不该让整屏起不来。 */
export function readSession(): LiveSession | null {
  try {
    const raw = (globalThis as { localStorage?: Storage }).localStorage?.getItem(LIVE_SESSION_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<LiveSession>;
    if (typeof parsed.requestId !== "string" || typeof parsed.projectKey !== "string") return null;
    if (!parsed.requestId.trim() || !parsed.projectKey.trim()) return null;
    return { requestId: parsed.requestId, projectKey: parsed.projectKey };
  } catch {
    return null;
  }
}

/** 刷新之后接回上一单：读回原文与台词，再建连让任务卡自己回来。
 *
 *  顺序和 `startLive` 同理，只是第一步从"提交"换成了"读回"：
 *  `setSource("live")` → 立 plan（**带上原文**，否则受理台是一个空框）→ 台词 →
 *  `connect()`（它内部立刻 `refresh()`，而 `refresh` 会保留已有 plan 的 raw）。 */
export async function resumeSession(session: LiveSession): Promise<void> {
  setSource("live");
  setLive({ projectKey: session.projectKey, requestId: session.requestId, stage: "idle", docs: null });
  try {
    const view: RequestView = await fetchRequest(session.requestId);
    const digest = view.summary_digest ?? {};
    /* 轮数与"追加过没有"都要在**建 plan 之前**读出来：`requestPlan` 拿 `docs` 填三格，
       而追加过的那一单此刻的 docs 是上一版的 —— 与 `watchRequest` 同一条口径。 */
    const rounds = roundsOf(digest);
    const lines = digest["会议"] ?? [];
    const rawDocs = parseDocs(digest);
    const reMeeting = hasAdditions(digest) && (lines.length < meetingTurnsOf(rounds) || !rawDocs);
    const docs = reMeeting ? null : rawDocs;
    setLive({ rounds });
    adoptTasks([], 0, requestPlan(view.raw_input ?? "", session.requestId, docs));
    if (docs) setLive({ docs });
    if (lines.length) adoptMeetingLines(lines);
    /* 刷新之后也要说得出"为什么停"。原先这一格没写：F5 回来那行字就退成
       "观察窗到期还没拿到该拿的东西"，而库里明明存着归因（`last_error`）。
       只在非空时写，空串不清掉旧读数（`setLive` 的合并语义）。 */
    const failure = String(view.last_error ?? "").trim();
    if (failure) setLive({ lastError: failure });
    /* 缺哪一段就继续盯哪一段；但**受理段已经走完**的单不必再盯：
       它的文档要么早在 digest 里（上面的 `docs`），要么永远不会有了。
       `needs_human` 与 `stalled` 同一条处置：再盯也不会变，等人去改需求。 */
    const settled = stageOf(lines.length, docs, view.status, view.last_error_code, rounds, reMeeting);
    if (isParked(settled)) setLive({ stage: settled });
    else if (!docs || lines.length < meetingTurnsOf(rounds)) watchInto(session.requestId);
  } catch {
    /* 单读不到（被删了 / 换了库）：当作没有会话，屏上停在未立项 */
    forgetSession();
    return;
  }
  connect(session.projectKey);
}

/** 受理台走真链路：开会 → 落单 → 拆单 → 建连。
 *
 *  和 `submit()` 的分工：`submit` 只管"把这句话交给后端"，
 *  这里负责"页面因此变成什么样"—— 两条职责分开，是因为实时档（受理台填项目键）
 *  只想发单、不要工作室屏那套状态切换。
 *
 *  顺序不是随便排的：
 *  1. `setSource("live")` 先清推演残留（会议剧本、假任务卡），并让 `tick()` 停手 ——
 *     晚一步，下一帧就会在这张桌上推演出半场假会议；
 *  2. `submit()` 拿到单号；
 *  3. `adoptTasks([], 0, requestPlan(...))` 把受理台立刻翻成"已立项"。
 *     拆单要 70 秒，等卡回来才翻页，那 70 秒里按钮一直是可点的 —— 再点一次就是第二张单；
 *  4. `connect()` 起 SSE + 轮询。放在 3 之后，是因为 `connect` 内部会立刻 `refresh()` 一次。
 */
export async function startLive(raw: string): Promise<IntakeResponse> {
  const key = (getStudio().live.projectKey || readProjectKey()).trim();
  if (!key) throw new ApiError(422, "project_key_required：先填项目键（它会成为产物的目录名）");
  setSource("live");
  const response = await submit(raw);
  adoptTasks([], 0, requestPlan(raw, response.request_id));
  connect(key);
  /* 清掉上一单的文档读数：不清的话新单的三格会先显示旧单的 PRD，几秒后才被替换。 */
  setLive({ docs: null });
  watchInto(response.request_id);
  rememberSession(response.request_id, key);
  return response;
}
