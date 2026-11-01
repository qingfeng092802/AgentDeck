/** 看板 → 后端的唯一一条线（2026-10-27 接线版）。
 *
 *  上一代 `api/client.ts` 已经在 2026-10-27 的治理台退役里停用，但它对契约的读法是对的：
 *  路径全是**裸根路径**（本仓 router 无 URL 前缀），字段名照抄后端 Pydantic 模型，不做第二层重命名。
 *  本文件只保留当前界面真正用到的那几只读/写面，泛型与错误形状与那份一致 ——
 *  判据 `tests/guards/test_board_frontend.py` 节一量的是"前端路径 ⊆ 公开 OpenAPI 面"，
 *  它扫的是 `src` 下所有 TS 里的裸路径字面量，因此这个文件同样在射程内。
 */

/** 与 `src/acp/api/approvals.py::PRINCIPAL_HEADER` 同一个字面量。只有写面需要它。 */
export const PRINCIPAL_HEADER = "x-acp-principal";

/** 退役键：顶栏那格「身份」输入框 2026-10-26 拆掉了，但它写进 localStorage 的值留了下来。 */
const RETIRED_PRINCIPAL_KEY = "acp-principal";

const PROJECT_KEY_STORAGE = "acp-project-key";

/** 本机单用户看板的身份。**常量，不读盘。**
 *
 *  这里原来读 `localStorage["acp-principal"]`，读不到才落回这个值。那套写法在顶栏还有身份
 *  输入框的时候是对的；框退役之后它就变成一枚**看不见的开关**：
 *  页面一切正常，点「批准」却稳定 403 `principal_not_allowed`，而错误里并不说本机发的是谁。
 *
 *  2026-10-28 实测踩到：服务端明明是 `--approver owner-1`，浏览器盘上却留着旧值。
 *  排查花了三轮才排除"后端配置错"这个方向 —— 一个没有任何界面能看见、能改的输入，
 *  不该继续决定"谁在批单"。
 *
 *  现在只发这一个常量：要换身份就改服务端的 `--approver`，两边都是显式的。 */
export const DEFAULT_PRINCIPAL = "owner-1";

/** 清掉退役键。留着没有读者，却会在下一次有人把读盘那条写回来时悄悄生效。 */
export function forgetStalePrincipal(): void {
  try {
    (globalThis as { localStorage?: Storage }).localStorage?.removeItem(RETIRED_PRINCIPAL_KEY);
  } catch {
    // 隐私模式 / 无痕：本来就没有盘可清
  }
}

/** 读盘值 → 实际要用的身份。**只有一个答案**，保留签名是为了让调用点不必关心身份从哪来。 */
export function principalOrDefault(): string {
  return DEFAULT_PRINCIPAL;
}

/* 模块加载时清一次退役键：现在没有读者了，但留着它，下一次有人把读盘那条写回来就会悄悄生效。 */
forgetStalePrincipal();

export function readProjectKey(): string {
  if (!("localStorage" in globalThis)) return "";
  return localStorage.getItem(PROJECT_KEY_STORAGE) ?? "";
}

export function writeProjectKey(value: string): void {
  if (!("localStorage" in globalThis)) return;
  const trimmed = value.trim();
  if (trimmed) localStorage.setItem(PROJECT_KEY_STORAGE, trimmed);
}

/** 服务端的拒因原样带上来显示：翻译一遍就会出现"后端改了口径而前端还在说旧话"。
 *
 *  `detail` 有两种形状：多数端点发**串**，`POST /approvals/{id}/resolve` 发**映射**
 *  （`{ticket_id, applied, state, code}`）。只按串处理的话，那一次拒会显示成 `[object Object]`。
 */
export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;
  readonly code: string | null;

  constructor(status: number, detail: string, code: string | null = null) {
    super(`${status} ${detail}`);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.code = code;
  }
}

function describeDetail(value: unknown): { text: string; code: string | null } {
  if (typeof value === "string") return { text: value, code: null };
  if (value && typeof value === "object") {
    const record = value as Record<string, unknown>;
    const code = typeof record.code === "string" ? record.code : null;
    const rest = Object.entries(record)
      .filter(([key]) => key !== "code")
      .map(([key, item]) => `${key}=${String(item)}`)
      .join(" ");
    return { text: `${code ?? "拒"} ${rest}`.trim(), code };
  }
  return { text: String(value), code: null };
}

/** 405 的人话。**不照抄 `Method Not Allowed`** —— 那句话对着屏幕读不出任何可执行的动作。
 *
 *  这个 app 里 405 几乎只有一个来源：`app.py` 把静态面挂在 `/` 兜底（`StaticFiles` 对非
 *  GET/HEAD 一律回 405）。也就是说"路由表里没有这个接口，请求落到了静态面上"——
 *  而前端 dist 是随仓库走的、后端进程是长期跑着的，**两边版本对不齐时就是这个形状**：
 *  按钮是新加的、接口是旧的没有。
 *
 *  2026-10-29 现场：「追加并重开会」下面只有一行 `Method Not Allowed`，
 *  看不出"重启一下后端"才是解法。 */
const STALE_BACKEND_DETAIL =
  "这个接口在后端不存在（405）：多半是后端进程还在跑旧版本，或者页面不是从这个后端打开的。重启一次 acp.serve，再从它打印的地址打开页面再试。";

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, init);
  if (!response.ok) {
    const text = await response.text();
    let parsed: unknown = text;
    try {
      parsed = JSON.parse(text);
      if (parsed && typeof parsed === "object" && "detail" in parsed) {
        parsed = (parsed as { detail: unknown }).detail;
      }
    } catch {
      // 非 JSON（网关 HTML 页之类）就按原文显示，不许在这里吞掉。
    }
    if (response.status === 405) {
      throw new ApiError(405, STALE_BACKEND_DETAIL);
    }
    const { text: detail, code } = describeDetail(parsed);
    throw new ApiError(response.status, detail, code);
  }
  return (await response.json()) as T;
}

function writeHeaders(): HeadersInit {
  return {
    "content-type": "application/json",
    [PRINCIPAL_HEADER]: principalOrDefault(),
  };
}

// ---------------------------------------------------------------- 任务读面

export interface TaskArtifactRef {
  task_id: string;
  writer_agent_id: string;
  rel_path: string;
  format: string;
  bytes: number;
  lines: number;
}

/** 与 `api/tasks.py::TaskFields` 逐列同形（列表与详情共用那一组列）。 */
export interface TaskFields {
  id: string;
  feature_request_id: string;
  project_key: string;
  request_status: string;
  request_created_at: string;
  /** 这批卡是怎么来的（`llm` / `prd` / `fallback_stub` / `stub`；`null` = 本列上线前的历史行）。
   *  `fallback_stub` / `stub` = 没接真模型 —— 看板据此打"兜底拆单"标识。
   *  **可选**：本列上线前起的那发后端不会回这一格，页面要能照常跑（滚动升级时的形态）。 */
  split_source?: string | null;
  title: string;
  status: string;
  depends_on: string[];
  executor_agent_id: string | null;
  reviewer_agent_id: string | null;
  acceptor_agent_id: string | null;
  difficulty: number;
  skill_tags: string[];
  handoff_count: number;
  acceptance_criteria: string[];
  /** 这张卡属于流水线哪一段：`doc` / `build` / `gate` / `summary`（`reporting/cards.py::family_of`）。
   *  执行看板按它分行 —— 后端算好的事实，前端不重算。 */
  family: string;
}

export interface TaskSummary extends TaskFields {
  artifacts: TaskArtifactRef[];
  /** 这张卡的验收是机器代签的吗（`review/runner.py` 的低风险自动验收）。
   *  流水线节点上的 `Auto-Approved` 徽章只读这一格 —— 它是"这不是人签的字"的唯一可见证据。 */
  auto_approved: boolean;
  /** 机器审查给过的建议结论。非空而 `auto_approved=false` ⇒ "机器审过了，但这份得人签"，
   *  与"机器根本没审"是两件事，界面上不该长得一样。 */
  auto_review: string | null;
}

export interface TaskArtifactSource extends TaskArtifactRef {
  content: string | null;
  content_error: string | null;
}

export interface TaskManifestEntry {
  rel_path: string;
  producer_agent_id: string;
  task_card_id: string;
  bytes: number;
  lines: number;
  format: string;
}

export interface TaskReviewRound {
  round_no: number;
  verdict: string;
  findings: number;
  actor_agent_id: string;
  created_at: string;
  card_status_after: string | null;
  verdict_id: string | null;
  item_findings: Array<Record<string, unknown>>;
}

export interface TaskDetail extends TaskFields {
  artifact_floor: { min_bytes: number; min_lines: number; format: string };
  token_budget: string | null;
  deadline_minutes: number | null;
  review_round: number;
  rework_count: number;
  snapshot_id: string;
  artifacts: TaskArtifactSource[];
  manifest: TaskManifestEntry[];
  review_history: TaskReviewRound[];
}

/** 返回数组的**次序本身就是拓扑序**（服务端 `_topo_order()` 排的）：
 *  前端只按数组摆位、按 `depends_on` 连线，不许自己再求一次拓扑。 */
export const listTasks = (filter: { projectKey?: string; featureRequestId?: string; status?: string } = {}): Promise<TaskSummary[]> => {
  const params = new URLSearchParams();
  if (filter.projectKey) params.set("project_key", filter.projectKey);
  if (filter.featureRequestId) params.set("feature_request_id", filter.featureRequestId);
  if (filter.status) params.set("status", filter.status);
  const query = params.toString();
  return request<TaskSummary[]>(query ? `/tasks?${query}` : "/tasks");
};

// ---------------------------------------------------------------- 活动流

export const STREAM_EVENT = "acp-activity";

export interface ActivityRow {
  id: number;
  entity: string;
  entity_id: string;
  kind: string;
  actor_agent_id: string;
  payload: Record<string, unknown>;
  created_at: string;
}

/** 浏览器 `EventSource` 只按 `addEventListener(名字)` 收数据帧，名字一漂就是"连接活着但页面不动"。 */
export function openActivityStream(
  onFrame: (row: ActivityRow) => void,
  onState: (state: "open" | "retry" | "closed") => void,
): EventSource {
  const source = new EventSource("/events/stream");
  source.addEventListener("open", () => onState("open"));
  source.addEventListener("error", () =>
    onState(source.readyState === EventSource.CLOSED ? "closed" : "retry"),
  );
  source.addEventListener(STREAM_EVENT, (event: MessageEvent<string>) => {
    try {
      onFrame(JSON.parse(event.data) as ActivityRow);
    } catch {
      // 单帧坏数据不该让整条流停摆：丢掉这一帧，下一帧照常。
    }
  });
  return source;
}

// ---------------------------------------------------------------- 需求受理

export interface IntakeResponse {
  request_id: string;
  project_key: string;
  status: string;
  summary_digest: Record<string, string[]>;
  clarification_waived: boolean;
  completer_id: string;
  intake_scheduled: boolean;
}

/** `completer_id` 必须显示：`local-passthrough` 是本机旁路（什么都没补），
 *  把它读成"总管已经把需求补好了"是这一屏最大的一句假话。
 *
 *  `modelRouting`（2026-10-31 路线 B）非空时随请求体带上；`null/undefined` = 不带这一格，
 *  后端保持 NULL（执行者自动排序、把关席从 EN-04 池里填）—— 老路径的报文一字不改。 */
export const submitRequest = (
  projectKey: string,
  rawInput: string,
  modelRouting?: unknown | null,
): Promise<IntakeResponse> =>
  request<IntakeResponse>("/requests", {
    method: "POST",
    headers: writeHeaders(),
    body: JSON.stringify({
      project_key: projectKey,
      raw_input: rawInput,
      clarification_answers: [],
      ...(modelRouting ? { model_routing: modelRouting } : {}),
    }),
  });

/** 设置页四席直跑：总管规划 → 执行写文件 → 审查 → 验收。密钥只在请求体里，不落库。 */
export interface SettingsSeatInput {
  vendor: string;
  model: string;
  /** 设置页那一列「思考强度」，四档之一。以前它只活在 localStorage，现在跟着请求走。 */
  thinking: string;
}

export interface SettingsRunReport {
  run_id: string;
  project_key: string;
  requirement: string;
  output_dir: string;
  models: Record<string, { vendor: string; model: string; base_url: string; thinking: string }>;
  plan: { title: string; acceptance: string[]; files: { name: string; purpose: string }[] };
  files: { name: string; bytes: number; lines: number; purpose: string }[];
  review: { verdict: string; findings: { criterion: string; met: boolean; evidence: string }[] };
  repair_attempts: number;
  repair_error: string | null;
  /** 每一发实际翻了几次倍（键 = 阶段名，执行席是 `execute:<文件名>`）。>0 说明配置里的预算本来不够
   *  （推理吃满了正文）。`review` / `repair` 恒在，其余阶段只在真翻过倍时才进这张表。 */
  budget_doublings: Record<string, number>;
  /** 哪些阶段是靠传输层重打救回来的（键 = 阶段名，值 = 第几发才连上）。正常那一发不进这张表。 */
  transport_retries: Record<string, number>;
  acceptance: { decision: string; reason: string };
  timings_s: Record<string, number>;
  total_s: number;
  created_at: string;
}

export type SettingsRunJobStatus = "running" | "cancelling" | "done" | "error" | "cancelled";

export interface SettingsRunJob {
  run_id: string;
  status: SettingsRunJobStatus;
  stage: string;
  detail: string;
  report: SettingsRunReport | null;
  error: string | null;
  started_at: string;
  finished_at: string | null;
}

export interface SettingsRunStart {
  run_id: string;
  status: "running";
}

const settingsRunBody = (body: {
  project_key: string;
  requirement: string;
  seats: Record<"orchestrator" | "executor" | "reviewer" | "acceptor", SettingsSeatInput>;
  api_keys: Record<string, string>;
  openai_compat_base_url: string;
}): string => JSON.stringify(body);

export const startSettingsRun = (body: {
  project_key: string;
  requirement: string;
  seats: Record<"orchestrator" | "executor" | "reviewer" | "acceptor", SettingsSeatInput>;
  api_keys: Record<string, string>;
  openai_compat_base_url: string;
}): Promise<SettingsRunStart> =>
  request<SettingsRunStart>("/requests/settings-run", {
    method: "POST",
    headers: writeHeaders(),
    body: settingsRunBody(body),
  });

export const getSettingsRun = (runId: string): Promise<SettingsRunJob> =>
  request<SettingsRunJob>(`/requests/settings-run/${encodeURIComponent(runId)}`);

export const cancelSettingsRun = (runId: string): Promise<SettingsRunJob> =>
  request<SettingsRunJob>(`/requests/settings-run/${encodeURIComponent(runId)}/cancel`, {
    method: "POST",
    headers: writeHeaders(),
  });

/** 一条已落库需求的读面（`api/requests.py::RequestView`）。
 *  会议台词就在 `summary_digest["会议"]` 里，形如 `role｜text` —— 后端不再另开一张会议表。 */
export interface RequestView {
  request_id: string;
  project_key: string;
  raw_input: string;
  status: string;
  /** 这批卡是怎么来的；老后端没有这一格，所以可选。`fallback_stub` / `stub` = 没接真模型。 */
  split_source?: string | null;
  summary_digest: Record<string, string[]>;
  clarification_waived: boolean;
  created_at: string;
  /** 受理失败的原因（U6）。`null` = 没失败过。老后端没有这两个字段，所以可选。 */
  last_error?: string | null;
  last_error_code?: string | null;
}

export const fetchRequest = (requestId: string): Promise<RequestView> =>
  request<RequestView>(`/requests/${encodeURIComponent(requestId)}`);

/** 追加需求的回执（`api/requests.py::ExtendResponse`）。
 *
 *  `cards` 是**这一单已经有的任务卡数**，不是"这一次拆出来几张" —— 追加需求不重拆卡
 *  （`serve_intake._run` 里"有卡就不重拆"那条分叉），所以回体里那一格恒等于旧值。
 *  前端拿它说清"卡不会动"，而不是拿来当进度。 */
export interface ExtendResponse {
  request_id: string;
  status: string;
  extend_scheduled: boolean;
  cards: number;
}

/** 在**这一单上**追加一条需求：四岗位带着上一版 PRD 重开一次会，只重出 PRD，不动已有卡。
 *
 *  与 `submitRequest` 的区别是"新单 / 同一单"：那个会新开一条 `feature_request`，
 *  这个只在原单的 `summary_digest["追加"]` 上再加一行。
 *  `202` = 已排队，不是已跑完 —— 会议还要 1–3 分钟，屏上要接着盯（`live.extendLive` 负责）。 */
export const extendRequest = (requestId: string, text: string): Promise<ExtendResponse> =>
  request<ExtendResponse>(`/requests/${encodeURIComponent(requestId)}/extend`, {
    method: "POST",
    headers: writeHeaders(),
    body: JSON.stringify({ text }),
  });

/** 右栏「文件总线」与「审查」两页的读面（`api/requests.py::EvidenceView`）。
 *
 *  为什么它不是 `GET /requests/{id}` 的一部分：那一格在受理期间每 1.5 秒被读一次，
 *  而这两张表只在卡落库之后才有内容 —— 塞进去等于让"开会"那三分钟每次都多查两张表。
 *  也正因为它是**另一次**请求，它允许失败：读不到时两页保持原样，不许因此把整屏判成错误。 */
export interface EvidenceArtifact {
  /** 项目目录里的相对路径。**一个文件一行**（同一个文件被几张卡先后写过，后端已取最后一次）。 */
  rel_path: string;
  task_card_id: string;
  writer_agent_id: string;
  format: string;
  bytes: number;
  lines: number;
  /** `valid` / `invalid` / `pending`（校验节点还没跑）。**原样透出**，前端不猜。 */
  state: string;
}

export interface EvidenceFinding {
  criterion: string;
  met: boolean;
  evidence: string;
}

export interface EvidenceVerdict {
  task_card_id: string;
  reviewer_agent_id: string;
  verdict: string;
  findings: EvidenceFinding[];
}

export interface EvidenceView {
  request_id: string;
  artifacts: EvidenceArtifact[];
  verdicts: EvidenceVerdict[];
}

export const fetchEvidence = (requestId: string): Promise<EvidenceView> =>
  request<EvidenceView>(`/requests/${encodeURIComponent(requestId)}/evidence`);

/** EN-17 交付包索引（只读）。`manifest` 是文件清单，正文留在各 workspace。 */
export interface DeliveryManifestEntry {
  rel_path: string;
  producer: string;
  task_card_id: string;
  bytes: number;
  lines: number;
  format: string;
}

export interface DeliveryPackage {
  deliverable_id: string;
  feature_request_id: string;
  entry_path: string;
  manifest: DeliveryManifestEntry[];
  open_issues: Record<string, string>[];
  assembled_by_agent_id: string;
  created_at: string;
}

export const fetchDelivery = (requestId: string): Promise<DeliveryPackage> =>
  request<DeliveryPackage>(`/requests/${encodeURIComponent(requestId)}/delivery`);

/** 把这一单里**还没跑完的卡**接着跑（2026-10-28）。
 *
 *  为什么需要它：续跑此前只由「批准」那一下触发，而批单只有一次 —— 工单 `APPROVED` 之后就从
 *  `?state=PENDING` 里消失，待办里没有它，屏上也就没有任何能点的东西。中途一张卡失败之后，
 *  人就彻底卡住了（现场：`real_demo` 那单的 T05 写了 `.php` 被总线拒）。
 *  三种 409 的 detail 分别说明：还有待批的单 / 状态不是 SPLIT·RUNNING / 没注续跑者。 */
export const continueRequest = (requestId: string): Promise<{ request_id: string; status: string; resume_scheduled: boolean }> =>
  request<{ request_id: string; status: string; resume_scheduled: boolean }>(
    `/requests/${encodeURIComponent(requestId)}/continue`,
    { method: "POST", headers: writeHeaders() },
  );

// ---------------------------------------------------------------- 审批（读面）

/** 与 `api/approvals.py::TicketSummary` 逐列同形。 */
export interface TicketSummary {
  ticket_id: string;
  kind: string;
  state: string;
  version: number;
  priority: number;
  task_card_id: string | null;
  feature_request_id: string | null;
  risk_exempt_eligible: boolean;
  timeout_at: string | null;
  /** P1-4：建单时刻（ISO）。无超时的单（A/C/D 的 `timeout_at` 是 null）靠它算"挂了多久"。 */
  created_at: string;
}

export interface TicketDetail extends TicketSummary {
  payload: Record<string, unknown>;
  decided_by_principal: string | null;
  decided_at: string | null;
  event_count: number;
}

/** `state` 只接受 `approval_state` 域里的五个值（后端 422 会点名）。
 *
 *  **这一组（`listApprovals` / `resolveTicket` / `batchResolveTickets` / `submitReview` /
 *  `resolveEscalation` / `retryArtifact` / `continueRequest` / `readAutoApproval` /
 *  `writeAutoApproval`）现在没有界面调用点了**（owner 2026-10-28「去除所有待办清单」）。
 *  留着而不是删掉，是因为端点都还在、契约也还在 —— 它们是这一层对后端的完整描述，
 *  哪天要从 CLI 之外再长一个界面，不该从零再写一遍。**但它们不再是产品的一部分**：
 *  审批动作归后端规则（`ACP_REVIEW_MODE` / `ACP_APPROVAL_MODE`），界面只读。
 */
export const listApprovals = (state = "PENDING"): Promise<TicketSummary[]> =>
  request<TicketSummary[]>(`/approvals?state=${encodeURIComponent(state)}`);

// ---------------------------------------------------------------- 审批（写面）

export const DASHBOARD_CHANNEL = "DASHBOARD";

export interface ResolveResponse {
  ticket_id: string;
  state: string;
  applied: boolean;
  version: number;
  logged_as: string | null;
  resumed_from: string | null;
}

export const resolveTicket = (
  ticketId: string,
  verdict: string,
  version: number,
  reasonOrAsk: string,
): Promise<ResolveResponse> =>
  request<ResolveResponse>(`/approvals/${encodeURIComponent(ticketId)}/resolve`, {
    method: "POST",
    headers: writeHeaders(),
    body: JSON.stringify({ verdict, reason_or_ask: reasonOrAsk, version, channel: DASHBOARD_CHANNEL }),
  });

// ---------------------------------------------------------------- 评审（写面）

/** 批量批准/驳回（2026-10-28，待办头部的 `[✅ 批准此批次全部任务]`）。
 *
 *  **不是第二条审批路径**：后端逐单走同一个 `repository.resolve`（同一份 CAS 与或签），
 *  身份与审批者校验也复用同一套。逐单一个事务 ⇒ 部分失败不回滚已经成立的批准，
 *  所以返回的是"哪几张成了、哪几张没成、为什么"，而不是一个布尔。
 */
export interface BatchItemResult {
  ticket_id: string;
  ok: boolean;
  state: string | null;
  applied: boolean | null;
  code: string | null;
  status_code: number;
  resumed_from: string | null;
}

export const batchResolveTickets = (
  tickets: { ticket_id: string; version: number }[],
  verdict: string,
  reasonOrAsk = "",
): Promise<{ results: BatchItemResult[]; applied: number; failed: number }> =>
  request<{ results: BatchItemResult[]; applied: number; failed: number }>("/approvals/batch", {
    method: "POST",
    headers: writeHeaders(),
    body: JSON.stringify({
      tickets: tickets.map((item) => ({ ticket_id: item.ticket_id, version: item.version })),
      verdict,
      reason_or_ask: reasonOrAsk,
      channel: DASHBOARD_CHANNEL,
    }),
  });

/** 低风险自动验收开关的当前态（与 `api/autonomy.py::AutoApprovalView` 逐列同形）。
 *
 *  `source` 是承重的：它说清这个"开"是谁开的 —— `runtime` 是面板按的、`env` 是部署期变量、
 *  `default` 是都没设。少了它，按钮亮着而人以为自己没按过，或者按了却被环境变量盖掉。
 */
export interface AutoApprovalView {
  enabled: boolean;
  source: "runtime" | "env" | "default" | string;
  low_risk_formats: string[];
  reviewer_ready: boolean;
  env_flag: string;
  /** 当前审查模式。`auto` 下门禁自己签，这个开关不参与那条判定 —— 界面据此说明原因。 */
  review_mode: "manual" | "deepseek" | "auto" | string;
}

export const readAutoApproval = (): Promise<AutoApprovalView> =>
  request<AutoApprovalView>("/autonomy/auto-approval");

export const writeAutoApproval = (enabled: boolean | null): Promise<AutoApprovalView> =>
  request<AutoApprovalView>("/autonomy/auto-approval", {
    method: "PATCH",
    headers: writeHeaders(),
    body: JSON.stringify({ enabled }),
  });

export interface EscalationDecision {
  feature_request_id: string;
  /** 只有两个合法出口（状态机那一行的右侧）：`READY` 放回待跑 / `REJECTED` 驳回。 */
  decision: "READY" | "REJECTED";
  /** 做这个决定的人。后端拒 `orchestrator` —— 系统把自己签成人工出口就是那条死路的假通过。 */
  principal: string;
  reason?: string;
}

export interface EscalationResponse {
  task_id: string;
  feature_request_id: string;
  decision: string;
  status_after: string;
  principal: string;
  executor_agent_id: string | null;
  handoff_count: number;
  rework_count: number;
}

/** `ESCALATED ──人工──▶ READY / REJECTED`。卡跑挂之后落在 `ESCALATED`，人从这里把它放回待跑。 */
export const resolveEscalation = (taskId: string, body: EscalationDecision): Promise<EscalationResponse> =>
  request<EscalationResponse>(`/tasks/${encodeURIComponent(taskId)}/escalation`, {
    method: "POST",
    headers: writeHeaders(),
    body: JSON.stringify({ ...body, reason: body.reason ?? "" }),
  });

export interface ArtifactRetryBody {
  feature_request_id: string;
  /** 按这一下的人。后端拒 `orchestrator` —— 重跑是人工决定。 */
  principal: string;
  reason?: string;
}

export interface ArtifactRetryResponse {
  task_id: string;
  feature_request_id: string;
  prior_status: string;
  status_after: string;
  principal: string;
  executor_agent_id: string | null;
  handoff_count: number;
  rework_count: number;
}

/** `ARTIFACT_INVALID ──▶ HANDED_OFF`：门禁判产物不达标之后，把卡放回**能被派发**的那一格。
 *
 *  这一格此前没有出边（`dispatch.DISPATCHABLE` 里没有 `ARTIFACT_INVALID`），于是「继续执行」推不动它，
 *  人只能手改库。目标态由后端从状态机原文解析，这里不传 —— 只有一个合法下家，让界面选是假的选择。
 */
export const retryArtifact = (taskId: string, body: ArtifactRetryBody): Promise<ArtifactRetryResponse> =>
  request<ArtifactRetryResponse>(`/tasks/${encodeURIComponent(taskId)}/artifact-retry`, {
    method: "POST",
    headers: writeHeaders(),
    body: JSON.stringify({ ...body, reason: body.reason ?? "" }),
  });

export interface ReviewSubmitResponse {
  task_id: string;
  feature_request_id: string;
  project_key: string;
  verdict: string;
  card_status: string;
  round_no: number;
  findings: number;
  accepted: boolean;
  acceptance_proxy: string | null;
  accept_error: string | null;
  delivery: {
    deliverable_id: string;
    entry_path: string;
    manifest_count: number;
    cards_delivered: number;
    package_path: string;
    package_bytes: number;
  } | null;
  delivery_skipped: string | null;
  delivery_error: string | null;
  next: string;
}

export const submitReview = (
  taskId: string,
  body: {
    feature_request_id: string;
    verdict: string;
    findings?: { criterion: string; met: boolean; evidence: string }[];
    evidence?: string;
    entry_name?: string;
  },
): Promise<ReviewSubmitResponse> =>
  request<ReviewSubmitResponse>(`/tasks/${encodeURIComponent(taskId)}/review`, {
    method: "POST",
    headers: writeHeaders(),
    body: JSON.stringify({ evidence: "", ...body }),
  });
