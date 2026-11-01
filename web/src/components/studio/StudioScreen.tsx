import { useCallback, useEffect, useRef, useState } from "react";
import {
  agentTitle,
  dispatch,
  editRequirement,
  reopenFromHistory,
  resetStudio,
  setDraft,
  setLive,
  setOutputTab,
  setScreen,
  setSource,
  startProject,
  STUDIO_EXAMPLES,
  type OutputTab,
  type StudioState,
  type Turn,
} from "../../lib/studioEngine";
import { allSeatsSet, keysReady, roleText, useSettings } from "../../lib/settingsStore";
import {
  ApiError,
  cancelSettingsRun,
  getSettingsRun,
  startSettingsRun,
  type SettingsRunJob,
} from "../../lib/acp";
import { DEGRADED_SPLIT_SOURCES, disconnect, extendLive, forgetSession, isParked, MEETING_SEATS, meetingTurnsOf, speakerAt, startLive } from "../../lib/live";
import { runReadings } from "../../lib/runReadings";
import { pushToast } from "../../lib/toastStore";

const RUN_STAGE_LABEL: Record<string, string> = {
  queued: "排队",
  preflight: "检查模型",
  plan: "总管规划",
  execute: "执行写文件",
  review: "审查",
  accept: "验收",
  done: "完成",
  cancelled: "已取消",
  error: "失败",
};

const sleep = (ms: number): Promise<void> => new Promise((resolve) => window.setTimeout(resolve, ms));

/** 受理台留空项目键时的兜底目录名。带秒与 4 位随机尾巴，同一秒连点两次也不会撞车。 */
function autoProjectKey(): string {
  const now = new Date();
  const pad = (value: number): string => String(value).padStart(2, "0");
  const stamp = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
  return `prj-${stamp}-${Math.random().toString(36).slice(2, 6)}`;
}

/** 项目工作室屏：受理台 / 会议桌 / 产出 / 派单台，四张纸叠成两列。
 *
 *  为什么是"纸"不是"仪表"：这四张卡本来就是可以叠起来、可以回看的东西，
 *  四个折角排在一页里会读成四台仪表挤在一起，主角感反而没了（`index.css` 的 `studio-card` 注释同此说）。
 */

/* 会议桌的座位表（`MEETING_SEATS`）现在归 `live.ts`：它同时决定"这一轮谁在说"，
   而那件事带着后端换序规则（第二轮起总管最后说），不是渲染细节。
   分母也不是 4 —— 是 `轮数 × 4`（`meetingTurnsOf`），轮数从 digest 的「会议轮数」读。 */

/** 会议桌此刻的形状 —— 纯函数，推演档与真链路共用。
 *
 *  三态（已说完 / 正在说 / 还没轮到）全部从**已有的台词条数**推，不从计时器推。
 *  真链路尤其如此：后端每轮说完就落一次库（`serve_intake._meet`），所以
 *  "第 N 条台词到了"就等于"第 N+1 个岗位正在调模型" —— 这是可核对的事实，不是编出来的进度条。
 *
 *  `over`（会议这一段结束了）有三个来源，缺一个就会骗人：
 *  1. 台词够了（`轮数 × 4` 条）；
 *  2. **文档到了** —— 文档节点排在会议之后，它一到就说明会议走完了。这一条盖住"第 2 轮就断了"
 *     那种单：`stageOf` 只按台词条数判，那种单会一直显示"会议中"，直到观察窗到期；
 *  3. 观察窗到期（`stalled` / `needs_human`）：屏上如实说没产出，不让静止的一屏继续自称"进行中"。
 */
interface MeetingShape {
  readonly spoken: ReadonlySet<string>;
  readonly speaking: string | null;
  readonly done: boolean;
  readonly over: boolean;
}

function meetingShape(studio: StudioState): MeetingShape {
  const spoken = new Set(studio.turns.map((turn) => turn.who));
  if (studio.source === "live") {
    /* 分母是**这一单真的开了几轮**（`live.rounds`），不是写死的 4：
       写死 4 的话，2 轮的会说到第 4 条就被判"已锁定方案"，第 2 轮的讨论反而看不见。 */
    const total = meetingTurnsOf(studio.live.rounds);
    const over = studio.turns.length >= total || !!studio.live.docs || isParked(studio.live.stage);
    return {
      spoken,
      /* 谁在说不能取 `SEATS[n]`：第二轮起后端把总管挪到最后（`speaking_order`），
         取下标会把"产品在说"显示成"总管在说"。`speakerAt` 带着那条换序规则。 */
      speaking: over ? null : speakerAt(studio.turns.length),
      done: studio.turns.length >= total,
      over,
    };
  }
  const meeting = studio.meeting;
  const speaking = meeting && !meeting.done ? meeting.script[meeting.i]?.who ?? null : null;
  return { spoken, speaking, done: !!meeting?.done, over: !!meeting?.done };
}

function MeetStatus({ studio, shape }: { studio: StudioState; shape: MeetingShape }) {
  if (!studio.plan) return <>待启动</>;
  if (studio.source === "live") {
    if (shape.done) return <>已锁定方案</>;
    /* 断在中间时说的是"说到第几句就断了"，不是笼统的"没有产出" ——
       前几轮的台词还在屏上，笼统一句话会和屏上的内容互相打脸。
       **数的是"句"不是"轮"**：2 轮的会是 8 句，说"说到第 5 轮"会把条数说成轮数。 */
    if (shape.over) {
      return <>{studio.turns.length ? `说到第 ${studio.turns.length} 句就断了` : "会议无产出"}</>;
    }
    return <>{`已说 ${studio.turns.length} / ${meetingTurnsOf(studio.live.rounds)} 句`}</>;
  }
  if (!studio.meeting) return <>已结束</>;
  if (studio.meeting.done) return <>已锁定方案</>;
  return <>{`第 ${studio.meeting.i + 1} / ${studio.meeting.script.length} 轮`}</>;
}

/** 打字机：把**已经拿到的整段文字**逐字揭开，只为可读性。
 *
 *  它不假装模型还在吐字 —— 文本是整段到的（后端一次 `chat()` 拿回一段，见
 *  `serve_intake._meet` 的 docstring），真正"正在生成"的是**下一个岗位**，
 *  那一格由 typing 气泡表示，两者在同一屏上分得很清楚。
 *
 *  三个细节：
 *  · 步长按字数摊到约 1.4 秒，长句不会拖成三秒；
 *  · 依赖只有 `text` 与 `enabled`：父组件每秒重渲染（计时器）不会让字倒退；
 *  · `enabled=false` 时**初始值就是全文**。这一点是承重的：`enabled` 只在"这一条刚
 *    到达"时为真，而首屏（刷新页面接回一单）与静态渲染里不该有任何一条被揭开 ——
 *    刷新一次把已经说完的四段重打一遍，是把"重新加载"演成"重新开会"。 */
function useTypewriter(text: string, enabled: boolean): string {
  const [shown, setShown] = useState(enabled ? 0 : text.length);
  useEffect(() => {
    if (!enabled) return;
    setShown(0);
    if (!text) return;
    const step = Math.max(1, Math.ceil(text.length / 45));
    let at = 0;
    const timer = window.setInterval(() => {
      at = Math.min(text.length, at + step);
      setShown(at);
      if (at >= text.length) window.clearInterval(timer);
    }, 32);
    return () => window.clearInterval(timer);
  }, [text, enabled]);
  return enabled ? text.slice(0, shown) : text;
}

/** 从"开始等"到现在的读数。`since` 为空就是 `0:00`，不拿挂载时刻冒充。 */
function useElapsed(since: string | null): string {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!since) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [since]);
  if (!since) return "0:00";
  const secs = Math.max(0, Math.round((now - Date.parse(since)) / 1000));
  return `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, "0")}`;
}

/** 只到分钟的读数，给读屏**按需**读：它不进 live region，所以每分钟变一次也不会播报。
 *  视觉秒表仍然每秒跳（`useElapsed`），但它已经 `aria-hidden` —— 两者分工明确。 */
function useElapsedMinutes(since: string | null): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!since) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 60_000);
    return () => window.clearInterval(timer);
  }, [since]);
  if (!since) return 0;
  return Math.max(0, Math.floor((now - Date.parse(since)) / 60_000));
}

/* ---------------------------- 历史 ---------------------------- */
/** 顶栏那颗「历史」，点开是一张浮在整屏之上的清单。
 *
 *  为什么是**浮层**而不是第三个屏：历史不是"去哪"，是"拿回来"。它会直接改变这一屏
 *  受理台里的内容（点一条 = 用那条原文重新立项），所以它必须和受理台同屏可见 ——
 *  跳到一个单独的历史页，点完之后你还得自己走回来，而那一步没有任何信息量。
 *
 *  为什么不用 `.card` 里的下拉：这一屏的祖先 `.studio` 是 `overflow:hidden` 的
 *  （四张卡各自滚，整屏不滚），下拉到第三四行就会被裁掉。所以走 `position:fixed`，
 *  它不受任何祖先的裁剪影响（`.studio-root` 一路到 `#root` 都没有 transform，
 *  不会给 fixed 造一个新的包含块）。
 *
 *  为什么不恢复"存档"：点一条是**用原文重跑**，不是恢复当时的进度 —— 见
 *  `HistoryEntry` 的注释。恢复进度会造出一个自相矛盾的中间态（任务完成一半、
 *  定时器停在中途），而那不是一个"历史记录"该有的行为。 */
function HistoryMenu({ studio }: { studio: StudioState }) {
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const closer = useRef<HTMLButtonElement>(null);
  const count = studio.history.length;

  /* 关闭要**同时**把焦点还给那颗触发键 —— 否则关掉之后焦点落在 body 上，
     键盘用户得从页面开头重新 Tab 一遍。这是这一屏的"键盘合同"（同抽屉那条 P2-N）。 */
  const close = useCallback(() => {
    setOpen(false);
    trigger.current?.focus();
  }, []);

  /* 打开就把焦点送进面板（落在 `✕` 上）：`aria-modal="true"` 说的是"背景不可交互"，
     焦点还留在外面就是名不副实。
     Esc 挂在 window 上而不是面板上：面板里没有可聚焦元素时按键根本到不了它，
     而"按 Esc 关掉浮层"是所有人的肌肉记忆，做不到就会被读成"卡住了"。 */
  useEffect(() => {
    if (!open) return;
    closer.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") close();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, close]);

  return (
    <>
      <button
        ref={trigger}
        type="button"
        className="ghost"
        data-act="history"
        aria-expanded={open}
        aria-haspopup="dialog"
        onClick={() => setOpen((value) => !value)}
      >
        {count ? `历史 · ${count}` : "历史"}
      </button>
      {open ? (
        <>
          <div className="hist-mask" onClick={close} />
          <div className="hist-modal" role="dialog" aria-modal="true" aria-label="立项历史">
            <div className="hist-h">
              <b>立项历史</b>
              <span className="hint">点一条 = 用原文重新立项</span>
              <button ref={closer} type="button" className="ghost hist-x" onClick={close} aria-label="关闭">
                ✕
              </button>
            </div>
            {count === 0 ? (
              <div className="meet-empty" style={{ padding: "30px 12px" }}>
                <b>还没有历史</b>
                第一次立项之后，这里会记下每一条需求 —— 刷新页面也还在。
              </div>
            ) : (
              <div className="hist-list">
                {studio.history.map((entry) => (
                  <button
                    key={`${entry.prj}-${entry.date}-${entry.at}-${entry.raw.slice(0, 12)}`}
                    type="button"
                    className="hist-row"
                    onClick={() => {
                      /* 先关再立项：`reopenFromHistory` 会把整份状态换成新的一单，
                         浮层如果还挂着，它读的就是已经被替换掉的那个 `studio`。 */
                      close();
                      reopenFromHistory(entry);
                    }}
                  >
                    <span className="hist-top">
                      <span className="chip hot">{entry.prj}</span>
                      <span className="tag off">固定脚本</span>
                      <span className="hist-tm">{`${entry.date} ${entry.at}`}</span>
                    </span>
                    <span className="hist-txt">{entry.raw}</span>
                    <span className="hist-foot">{`${entry.pages} 页 · ${entry.cards} 张执行卡`}</span>
                  </button>
                ))}
              </div>
            )}
            <div className="hist-note">
              只留最近 20 条固定脚本演示；真链路单在受理台立项后直接开会。
            </div>
          </div>
        </>
      ) : null}
    </>
  );
}

/** P1-4：A 类卡点按宪法**无超时**，所以一张没人认的死单会永远停着、而且不留任何异常读数。
 *
 *  这一条是**只读告警**：它只说"挂了多久、有几张"，不提供批准/驳回按钮。
 *  理由不是懒：这一屏不带任何凭据（`writeHeaders()` 里的身份是看板自己声明的），
 *  在这块浮层上加一颗能改审批结论的按钮，等于把一个写端点摊给任何能打开页面的人。
 *  真要批，走 `python -m acp.approve` 或带 `X-ACP-Principal` 的调用方。
 */
function SuspendedBanner({ studio }: { studio: StudioState }) {
  const alert = studio.live.suspended;
  /* 演示档没有真实审批面 ⇒ 不显示。`!alert` 时整块不渲染（不是渲染成空壳）：
     一条"已挂起 0 天"的横幅比没有横幅更糟 —— 它会让人以为读数正常。 */
  if (!alert || studio.source !== "live") return null;
  const line = alert.count > 1 ? `共 ${alert.count} 张未决单在等人` : "一张未决单在等人";
  return (
    <div className="suspend-alert" role="status" title={`最久的一张：${alert.oldestKind}`}>
      <i aria-hidden="true" />
      <b>已挂起 {alert.oldestDays} 天</b>
      <span className="note">{line} · 超过 24 小时未处理</span>
    </div>
  );
}

function StudioHead({ studio }: { studio: StudioState }) {
  /* 这一行是**状态读数**，不是流程说明 —— 它说"谁在等你、现在到哪一步"。   *
   * 中间有一版把它从"未立项"里删掉了，理由是"它是流程说明的第 1 遍"（流程由四张卡按顺序
   * 出现来表达）。那个理由对"召开智囊团会议 · 派单"那种句子成立，但对这一行不成立：
   * 它在已立项态一直保留着，删的只是未立项那一次 —— 结果同一屏的抬头在两种状态下存在 / 不存在。
   *
   * 两个具体的代价：
   * ① **页面没有 h1 了**。`<h1 class="sr-only">` 就在这一行里，未立项时整页一个标题都没有。
   * ② 受理卡的顶边比已立项时高 43px（head 高度 + 间距），立项那一刻卡片要往下落一格。
   *
   * 所以未立项也渲染。读数按 owner 给的图恢复成原型的写法（"受理需求 · 召开智囊团会议 · 派单"）——
   * 四张卡同时在的时候，这一行确实是唯一一处把"从左到右、从上到下"的顺序讲清楚的地方。 */
  /* 整单都交付了没有 —— 只用来决定抬头第二句该说什么。
   *
   * 原来那一句在真链路里恒为「批单在右下待办」，于是**已交付的单**上它还在说"去右下批单"。
   * 2026-10-28 那块待办浮层被整块删掉之后，这句话连"东西在不在"都不成立了 ——
   * 现在只说这一单走到哪了，不再指任何一块面板。 */
  const settled = studio.tasks.length > 0 && studio.tasks.filter((task) => !task.hidden).every((task) => task.status === "done");
  const sub = !studio.plan
    ? "编排主持 · 受理需求 · 召开智囊团会议 · 派单"
    : studio.source === "live"
      ? /* 真链路原来这一行**永远**是"会议进行中" —— 判据读的是 `studio.meeting`，
           而那一格在 live 档恒为 null（它是推演剧本）。会议早就说完了，抬头还在说"进行中"。
           现在按 `pipelineStep` 说当前那一步，三段之外补一句"卡出来了"的下场。 */
        pipelineStep(studio) === "done"
        ? settled
          ? "编排主持 · 任务卡已出 · 已交付，留痕在右栏"
          : "编排主持 · 任务卡已出 · 执行中，进度见执行看板"
        : `编排主持 · ${STEP_SAID[pipelineStep(studio) as Exclude<Step, "done">]}`
      : studio.meeting && studio.meeting.done
        ? "编排主持 · 固定脚本演示 · 方案已锁定 · 可回看会议记录"
        : "编排主持 · 固定脚本演示 · 会议进行中 · 先拆验收标准";
  return (
    <div className="studio-head">
      {/* 屏名在顶栏面包屑里已经写了一次，左栏那扇门上又写了一次 —— 这是第三处。
          视觉上删掉，语义保留：读屏仍然能读到这一屏的标题（页面里不能没有 h1）。
          这一屏的垂直空间本来就紧（四张卡 + 会议记录），顶上少一行 14px 的重复是净收益。 */}
      <h1 className="sr-only">项目工作室</h1>
      <span className="sub">{sub}</span>
      {/* 兜底拆单标识（2026-10-31）：`.env` 里那把模型 key 失效时，拆单**静默**回落成通用清单，
          屏上此前与"真模型拆的"长得一模一样。只在这一格是降级时渲染；`llm` / `prd` 不出标记，
          `null`（历史行）也不出 —— 不许把"不知道"画成"模型拆的"。 */}
      {studio.source === "live" && studio.live.splitSource !== null && DEGRADED_SPLIT_SOURCES.includes(studio.live.splitSource) ? (
        <div className="split-fallback" role="alert" id="fallbackSplitBadge" data-split-source={studio.live.splitSource}>
          <i aria-hidden="true" />
          <b>兜底拆单 · 未接入真模型</b>
          <span className="note">这批任务卡来自通用清单，不是模型按需求拆的（{studio.live.splitSource}）。</span>
        </div>
      ) : null}
      {/* 两颗按钮的位置是固定的：历史在最左、看板在最右。
          「执行看板 →」未立项时不出现（看板里什么都没有，点了只看到一张空图）；
          「历史」两种状态都在 —— 未立项恰恰是最需要它的时候（回来重新开一单）。 */}
      <div className="right">
        <HistoryMenu studio={studio} />
        {studio.plan ? (
          <button type="button" className="ghost" data-act="board" onClick={() => setScreen("board")}>
            执行看板 →
          </button>
        ) : null}
      </div>
    </div>
  );
}

/* 离开这一单：**先断连接，再清屏**。
 *
 *  只清屏不断连的话，5 秒后那次轮询会把刚清掉的单原样搬回来 —— 而且原文是被抹成空串的
 *  （`refresh()` 手上只有任务卡，没有需求原文）。表现是"点了重新立项，屏幕空了两秒，
 *  又自己长回上一单，只是受理台那句客户原话不见了"。
 *
 *  同时把落盘的会话也清掉：不清的话刷新一次它又会自己接回来。 */
function leaveProject(action: () => void): void {
  disconnect();
  forgetSession();
  action();
}

/* ---------------------------- ① 受理台 ---------------------------- */

/** 受理卡那一行读数：**这一单走到哪一步、这一步等了多久**。
 *
 *  原来写的是"会议与拆单进行中（通常 3–7 分钟）"：一句话管三个阶段，读者只能靠猜；
 *  而且那个 3–7 分钟是**没关深度思考时**的读数 —— 关掉之后整条链实测 38 秒，
 *  继续挂着这个数是在把等待说长，人还没开始等就先烦了。
 *
 *  这里只说两件可核对的事：后端落到哪一步（`pipelineStep`）、从进这一步起过了多久。 */
function IntakeNote({ studio, cards }: { studio: StudioState; cards: number }) {
  const step = pipelineStep(studio);
  const elapsed = useElapsed(studio.live.stageSince ?? null);
  if (cards) return <>{`需求单已落库 · ${cards} 张任务卡`}</>;
  if (step === "done") return <>需求单已落库 · 正在读任务卡</>;
  /* `needs_human` 不报"已等"：这一刻没有任何东西在往这边来，一个还在走的秒表
     会让"等人"读成"快好了"。要等的是人，不是模型。 */
  if (step === "needs_human") return <>需求单已落库 · 待人工干预 · 拆单没过契约</>;
  if (step === "failed") {
    return <>{`需求单已落库 · 失败 · ${studio.live.lastError || "受理链已停，查看留痕或重新立项"}`}</>;
  }
  return <>{`需求单已落库 · ${STEP_SAID[step]} · 已等 ${elapsed}`}</>;
}

function IntakeCard({ studio }: { studio: StudioState }) {
  /* 输入框的值放在**引擎状态**里，不是本地 useState —— 见 `StudioState.draft` 的注释：
     「修改需求」要从产出卡把原文填回来，「开启新需求」要把它清空，两个动作都在这个组件外面。 */
  const draft = studio.draft;
  const settings = useSettings();
  const canRunWithSettings = allSeatsSet(settings) && keysReady(settings);
  const plan = studio.plan;
  const live = studio.source === "live";
  const [busy, setBusy] = useState(false);
  const [runBusy, setRunBusy] = useState(false);
  const [runStartedAt, setRunStartedAt] = useState<number | null>(null);
  const [runElapsed, setRunElapsed] = useState(0);
  const [runId, setRunId] = useState<string | null>(null);
  const [runStage, setRunStage] = useState("queued");
  /** 上一发四席直跑的代价读数（预算翻倍 / 传输重打）。空串 = 还没跑完过一发。 */
  const [runReadingsText, setRunReadingsText] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    if (runStartedAt === null) return undefined;
    const timer = window.setInterval(() => {
      setRunElapsed(Math.max(0, Math.floor((Date.now() - runStartedAt) / 1000)));
    }, 1000);
    return () => window.clearInterval(timer);
  }, [runStartedAt]);

  /* 默认路径只有一条：**立项 = 真链路**。项目键留空时自动生成一个目录名，不再退到
   * 本地固定剧本 —— 那条剧本与需求无关，四个岗位的台词是模板套参数，看起来像彩排。
   * 想离线看版式，右上角「固定演示」是显式入口，按钮本身写明了它不调用模型。 */
  const begin = (): void => {
    const value = draft.trim();
    if (!value || busy) return;
    let key = studio.live.projectKey.trim();
    if (!key) {
      key = autoProjectKey();
      setLive({ projectKey: key });
      pushToast({
        tone: "info",
        title: "已自动生成项目键",
        detail: `${key} · 产物目录按它命名；想自己定就在左栏先填。`,
      });
    }
    setBusy(true);
    setError("");
    void startLive(value)
      .catch((err: unknown) => {
        setError(err instanceof ApiError ? `${err.status} ${err.detail}` : err instanceof Error ? err.message : String(err));
      })
      .finally(() => setBusy(false));
  };

  /* 固定脚本演示：只给“看版式/断网演示”用。它是本地推演，不调用任何模型，
   * 所以内容与真实会议无关 —— 按钮文案与 toast 都必须把这一点说出来。 */
  const runDemo = (): void => {
    const value = draft.trim();
    if (!value) {
      pushToast({ tone: "warn", title: "先写一句需求", detail: "演示也要一句客户原话，否则没有可推演的对象。" });
      return;
    }
    disconnect();
    setLive({ projectKey: "", requestId: null, delivery: null, stage: "idle", docs: null });
    setSource("demo");
    startProject(value);
    pushToast({
      tone: "info",
      title: "固定脚本演示",
      detail: "本地推演，不调用模型；台词按关键词套模板，每次相同。",
    });
  };

  const runWithSettings = (requirement?: string): void => {
    const value = (requirement ?? draft).trim();
    const key = studio.live.projectKey.trim();
    if (!value || !key || runBusy) return;
    if (!allSeatsSet(settings)) {
      pushToast({
        tone: "warn",
        title: "四席还没坐满",
        detail: "先到设置里给统筹 / 执行 / 审查 / 验收各选一个厂商和型号。",
      });
      return;
    }
    if (!keysReady(settings)) {
      pushToast({
        tone: "warn",
        title: "先补齐四席 API Key",
        detail: "打开设置，为当前四个席位用到的厂商填写 API Key，再点运行。",
      });
      return;
    }
    setRunBusy(true);
    setRunStartedAt(Date.now());
    setRunElapsed(0);
    setRunId(null);
    setRunReadingsText("");
    setRunStage("queued");
    pushToast({
      tone: "info",
      title: "四席运行中",
      detail: "总管规划 → 执行写文件 → 审查 → 验收，复杂需求可能需要 3–8 分钟，请保持此页打开。",
    });
    void (async () => {
      try {
        const started = await startSettingsRun({
          project_key: key,
          requirement: value,
          seats: {
            orchestrator: {
              vendor: settings.orchestrator.vendor,
              model: settings.orchestrator.model,
              thinking: settings.orchestrator.thinking,
            },
            executor: {
              vendor: settings.executor.vendor,
              model: settings.executor.model,
              thinking: settings.executor.thinking,
            },
            reviewer: {
              vendor: settings.reviewer.vendor,
              model: settings.reviewer.model,
              thinking: settings.reviewer.thinking,
            },
            acceptor: {
              vendor: settings.acceptor.vendor,
              model: settings.acceptor.model,
              thinking: settings.acceptor.thinking,
            },
          },
          api_keys: settings.apiKeys,
          openai_compat_base_url: settings.openaiCompat.baseUrl,
        });
        setRunId(started.run_id);
        let job: SettingsRunJob | null = null;
        const deadline = Date.now() + 20 * 60 * 1000;
        while (Date.now() < deadline) {
          await sleep(1500);
          job = await getSettingsRun(started.run_id);
          setRunStage(job.stage || job.status);
          if (job.status !== "running" && job.status !== "cancelling") break;
        }
        if (job?.status === "done" && job.report) {
          /* 代价读数落屏（2026-10-31）：`budget_doublings` / `transport_retries` 以前只在盘上的
             报告里 —— 而"这一发是不是靠翻倍救回来的"恰恰是判断模型/预算配置的唯一现场。
             括号里的数字来自报告本身，前端不重算。 */
          const readings = runReadings(job.report);
          setRunReadingsText(readings);
          pushToast({
            tone: "ok",
            title: `四席运行完成：${job.report.files.length} 个文件`,
            detail: `${job.report.review.verdict} · ${job.report.acceptance.decision} · ${readings} · ${job.report.output_dir}`,
          });
        } else if (job?.status === "cancelled") {
          pushToast({ tone: "warn", title: "四席运行已取消", detail: "已生成的文件留在 results/<项目>/settings-run-*。" });
        } else if (job?.status === "error") {
          pushToast({ tone: "bad", title: "四席运行失败", detail: job.error ?? "未知错误" });
        } else {
          pushToast({
            tone: "warn",
            title: "四席仍在后台运行",
            detail: "页面前端等待超时；任务可能还在跑，稍后看 results/<项目>/settings-run-*。",
          });
        }
      } catch (err: unknown) {
        pushToast({
          tone: "bad",
          title: "四席运行失败",
          detail: err instanceof ApiError ? `${err.status} ${err.detail}` : err instanceof Error ? err.message : String(err),
        });
      } finally {
        setRunBusy(false);
        setRunStartedAt(null);
        setRunElapsed(0);
        setRunStage("queued");
        setRunId(null);
      }
    })();
  };

  const cancelRun = (): void => {
    if (!runId) return;
    void cancelSettingsRun(runId)
      .then(() => setRunStage("cancelling"))
      .catch((err: unknown) => {
        pushToast({
          tone: "bad",
          title: "取消失败",
          detail: err instanceof ApiError ? `${err.status} ${err.detail}` : err instanceof Error ? err.message : String(err),
        });
      });
  };

  /* 已立项后也保留同一组直跑控件：直跑不经过会议/拆单，重跑不该逼人“重新立项”再敲门一次。
     同一时刻只渲染一处，`id` 不会撞。 */
  const runControls = (requirement: string) => (
    <>
      <button
        type="button"
        className="ghost"
        id="btnSettingsRun"
        disabled={busy || runBusy || !canRunWithSettings}
        title={
          !allSeatsSet(settings)
            ? "先到设置里把四席都选上"
            : !keysReady(settings)
              ? "先到设置里补齐四席厂商的 API Key"
              : "按设置页四席模型直跑"
        }
        onClick={() => runWithSettings(requirement)}
      >
        {runBusy ? `四席运行中… ${RUN_STAGE_LABEL[runStage] ?? runStage} ${runElapsed}s` : "按设置四席运行"}
      </button>
      {runBusy ? (
        <button type="button" className="ghost" disabled={!runId} onClick={cancelRun}>
          取消
        </button>
      ) : null}
      {runReadingsText ? (
        <div className="run-readings" id="runReadings" role="status">
          {runReadingsText}
        </div>
      ) : null}
    </>
  );
  if (plan) {
    const executor = roleText(settings.executor);
    const cards = studio.tasks.filter((task) => !task.hidden && task.phase >= 2).length;
    return (
      <div className="card intake">
        <div className="card-h">
          <h2>受理需求</h2>
          <span className="hint">已立项</span>
          <div className="right">
            <span className="chip hot">{plan.prj}</span>
          </div>
        </div>
        <div className="card-b">
          <div className="pre" style={{ maxHeight: 96, overflow: "auto" }}>
            {plan.raw}
          </div>
          <div className="tags" style={{ marginTop: 8 }}>
            {!live ? <span className="tag off">固定脚本演示</span> : null}
            {live ? (
              <span className="tag hot">真链路</span>
            ) : (
              <>
                <span className="tag hot">{plan.typeLabel}</span>
                {plan.feat.responsive ? <span className="tag">响应式</span> : null}
                {plan.feat.cart ? <span className="tag">购物车</span> : null}
                {plan.feat.form ? <span className="tag">表单</span> : null}
                {plan.feat.data ? <span className="tag">图表</span> : null}
                {plan.feat.auth ? <span className="tag">登录</span> : null}
                {!plan.feat.api ? <span className="tag off">无后端</span> : null}
              </>
            )}
          </div>
          {/* R-26 卡点：拆单两次没过契约。这一格要说清"不是失败、下一步干什么、去哪儿干" ——
              只写"待人工干预"是四个字加一个死胡同，人到了屏前仍然不知道手该放哪。
              「修改需求」走 `editRequirement`：它把原文填回**左侧受理台**（`重新立项` 是清空重写），
              所以这里必须点它，不能让人自己再敲一遍。 */}
          {studio.live.stage === "needs_human" ? (
            <div className="intake-hold" role="status">
              <b>待人工干预 · 拆单没过契约</b>
              <span>
                模型两次都没拆出合规任务卡，停在 R-26 卡点。
                不是失败 —— 一张卡都没建，但会议台词与三份文档都还在，不用重跑；改一下需求再立项就行。
              </span>
              <button type="button" className="ghost" data-act="needs-human-edit" onClick={() => leaveProject(editRequirement)}>
                改需求再立项
              </button>
            </div>
          ) : null}
          <div className="intake-foot">
            {/* 真链路没有"解析出 N 页"这回事：页数与判据都在真拆单的卡里，
                在这里印一个 0 会被读成"什么都没拆出来"。只说后端真的落了什么。 */}
            <span className="note">
              {live ? (
                <IntakeNote studio={studio} cards={cards} />
              ) : (
                `解析出 ${plan.pages.length} 页 · ${plan.ac.length} 条验收标准 · ${cards} 张执行卡`
              )}
            </span>
            <div className="sp" />
            <button type="button" className="ghost" data-act="reset" onClick={() => leaveProject(resetStudio)}>
              重新立项
            </button>
          </div>
          {/* 受理这一格要说清"谁在接单"：模型是 owner 在设置里选的，未选就留白，
              不替他填一个看起来完整的默认型号。 */}
          <div className="intake-foot">
            <span className="note">
              {live
                ? "受理链 · 服务端模型"
                : executor
                  ? `四席运行席 · ${executor}（演示未调用）`
                  : "执行席 · 未选模型"}
            </span>
            <div className="sp" />
            {studio.live.projectKey.trim() ? runControls(plan.raw) : null}
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="card intake">
      <div className="card-h">
        <h2>受理需求</h2>
        <span className="hint">客户原话，越口语越好</span>
        <div className="right">
          <button
            type="button"
            className="ghost"
            id="btnDemo"
            title="本地固定脚本，不调用任何模型；只用于预览界面或断网演示"
            onClick={runDemo}
          >
            固定演示
          </button>
          <span className="chip">自然语言</span>
        </div>
      </div>
      <div className="card-b">
        <textarea
          id="reqInput"
          value={draft}
          placeholder="例如：做一个卖手冲咖啡的静态官网，要包含首页、商品展示、联系我们三个页面。要有购物车按钮（仅 UI 交互），并且能在手机上正常看。"
          onChange={(event) => setDraft(event.target.value)}
        />
        <div className="chips">
          {STUDIO_EXAMPLES.map((example) => (
            <button key={example.label} type="button" className="chip-btn" onClick={() => setDraft(example.text)}>
              {example.label}
            </button>
          ))}
        </div>
        {/* 这两句在 2026-10-27 早些时候被删过，理由是"替界面解释界面"。现在按 owner 给的图
            恢复 —— 恢复的理由不是文案好看，是**这一屏的形态变了**：
            当时未立项只有一张卡，删掉说明还剩"写需求 / 立项"两个明确的东西；
            现在四张卡同时在，会议桌 / 产出 / 派单都是空的，没有这两句，
            右上那四个页签（PRD / 架构 / 令牌 / 任务卡）就只是四个词，看不出和这条需求的关系。 */}
        {/* 项目键决定产物目录，但不再是“真/假”的岔口：留空由 `begin()` 自动生成一个，
            默认永远走真模型。固定脚本演示是右上角那颗显式按钮，跟项目键无关。 */}
        <div className="intake-key">
          <label htmlFor="prjKey">项目键</label>
          <input
            id="prjKey"
            value={studio.live.projectKey}
            placeholder="留空 = 自动生成，走真模型"
            spellCheck={false}
            autoComplete="off"
            onChange={(event) => setLive({ projectKey: event.target.value })}
          />
          <span className="note">{studio.live.projectKey.trim() ? "真链路" : "自动键 · 真链路"}</span>
        </div>
        <div className="intake-foot">
          <button type="button" className="primary" id="btnStart" disabled={busy} onClick={begin}>
            {busy ? "立项中…" : "立项 · 召开智囊团会议"}
          </button>
          {studio.live.projectKey.trim() ? runControls(draft) : null}
          <span className="note">四岗逐轮讨论 · 真拆单 · 数分钟后出任务卡</span>
        </div>
        {error ? (
          <p className="la-err" role="alert">
            {/* 第一段是后端原话（`原样显示，不翻译`）；第二段才是我们该说的话 ——
                只说一句 "503" 等于把人留在原地，而这一格是立项唯一的失败出口。 */}
            <b>{error}</b>
            <span>
              检查服务端是否在跑（python -m acp.serve，默认 127.0.0.1:8765）；只想先看版式的话，点右上角「固定演示」。
            </span>
          </p>
        ) : null}
      </div>
    </div>
  );
}

/* ---------------------------- ② 会议桌 ---------------------------- */

/** 一条真台词。`animate` 只对**最新到的那一条**为真，其余直接给全文。 */
function LiveTurn({ turn, animate }: { turn: Turn; animate: boolean }) {
  /* 切的是 `raw`（未转义原文）而不是 `text`（已转义）：在 `&amp;` 中间下刀会切出裸的 `&am`。
     切完当纯文本渲染 —— 模型台词里没有标记，不需要 `dangerouslySetInnerHTML`。 */
  const full = turn.raw ?? turn.text;
  const revealed = useTypewriter(full, animate);
  const typing = animate && revealed.length < full.length;
  return (
    <div className="turn" style={{ ["--tone" as string]: `var(${toneOf(turn.who)})` }}>
      <span className="mono-chip">{monoOf(turn.who)}</span>
      <div className="bub">
        <div className="bub-name">
          <b>{agentTitle(turn.who)}</b>
          <span className="tm">{turn.t}</span>
        </div>
        <div className={`bub-txt${typing ? " typing-on" : ""}`}>
          {typing ? revealed : full}
          {typing ? <i className="caret" aria-hidden="true" /> : null}
        </div>
      </div>
    </div>
  );
}

/** 一条推演台词。它走 `innerHTML`：剧本里的 `<b>` / `<code>` 是真的标记，不是数据。 */
function DemoTurn({ turn }: { turn: Turn }) {
  return (
    <div className={`turn${turn.kind === "arb" ? " arb" : ""}`} style={{ ["--tone" as string]: `var(${toneOf(turn.who)})` }}>
      <span className="mono-chip">{monoOf(turn.who)}</span>
      <div className="bub">
        <div className="bub-name">
          <b>{agentTitle(turn.who)}</b>
          <span className="tm">{turn.t}</span>
        </div>
        {/* 推演台词由引擎生成，动态值只有页面标签 / AC 文案 / 调色板名，全部出自
            本文件的常量表 —— 其中的 `<b>` 是刻意的标记，所以这一格走 innerHTML。
            真链路的台词**在适配层就已经过 `esc()`**（`lib/live.ts` 的 `adoptMeetingTurns`），
            而且那一路改走上面的 `LiveTurn`，根本不进这一格。 */}
        <div className="bub-txt" dangerouslySetInnerHTML={{ __html: turn.text }} />
      </div>
    </div>
  );
}

/** "这个人正在说" —— 一个只有三个点、没有正文的气泡。
 *
 *  它出现的位置是**算出来的**：后端四轮固定顺序、逐轮落库，所以"已有 N 条"就意味着
 *  第 N+1 个座位正在被调用。四轮齐了、文档到了、或者观察窗到期，它就不该再出现。
 *
 *  `waiting` 只影响那行小字：一条台词都没有时说"四轮模型调用，通常 1–3 分钟"，
 *  已经有台词时说"下一轮"。前者是第一次等的人唯一需要的那句话。 */
function SpeakingBubble({ who, waiting }: { who: string; waiting: boolean }) {
  return (
    <div className="turn" style={{ ["--tone" as string]: `var(${toneOf(who)})` }}>
      <span className="mono-chip">{monoOf(who)}</span>
      <div className="bub">
        <div className="bub-name">
          <b>{agentTitle(who)}</b>
          <span className="tm">{waiting ? "模型调用中" : "正在说"}</span>
        </div>
        <div className="bub-txt typing-on">
          <span className="typing">
            <i />
            <i />
            <i />
          </span>
        </div>
      </div>
    </div>
  );
}

/** 会议进行中的读数条：第几轮 / 谁在说 / 已等多久。
 *
 *  为什么"已等"必须有：四轮串行的模型调用实测 1–3 分钟，而屏上只有三个跳动的点。
 *  一个没有刻度的等待会被读成"卡住了"，然后人去刷新页面 —— 刷新不会更快，
 *  只会把这一屏的读数清掉。读数条给的是"它一直在动"的证据，且这个数字可核对
 *  （从开始盯这一单算起，见 `LiveState.meetingSince`）。 */
/* ---------------------------- 受理链的三段 ---------------------------- */

/** 受理链现在走到哪一段。**只按可核对的读数判**，不看 `stage` 的死活。
 *
 *  `stage` 是轮询算出来的：文档到了之后要等下一轮才变成 `split`，那一秒里
 *  "正在落文档"和"已经拆卡"会同时成立，屏上就会闪一下。这里按事实排优先级：
 *  卡真的有了就是 `done`，文档真的有了就是 `split`，其余才回去看台词条数。
 *
 *  为什么是这三段而不是一条百分比：四轮会议有分母（4），文档与拆卡**没有** ——
 *  一次模型调用要多久，我们这边根本不知道。所以这里只做**状态机**（第几步），
 *  不做进度条（百分之几）。编一个 67% 出来，是在给读者一个查不了的数。 */
type Step = "meeting" | "docs" | "split" | "done" | "stalled" | "needs_human" | "failed";

const STEP_ORDER = ["meeting", "docs", "split"] as const;

function pipelineStep(studio: StudioState): Step {
  if (studio.source !== "live") return studio.meeting?.done ? "done" : "meeting";
  /* `meeting` / `docs` 两档排在"卡已经有了"**之前**：追加需求之后这一趟会退回到这两档，
     而卡是上一版 PRD 的结论。先判卡的话，进度条会在整趟重出期间说"已完成" ——
     那时真正在发生的是四岗位重开了一场会。正常受理时卡还是 0，这两条不改变任何行为。
     `needs_human` 也在卡之前：它说的是"这一单等人"，与卡数无关（那一档本来就没有卡）。 */
  if (studio.live.stage === "needs_human") return "needs_human";
  if (studio.live.stage === "meeting") return "meeting";
  if (studio.live.stage === "docs") return "docs";
  /* P1-5：失败态排在"卡已经有了"之前 —— 受理链已经停了，不能因为上一版还有卡就说"已完成"。 */
  if (studio.live.stage === "failed") return "failed";
  /* 卡在了就算走完了 —— 这一条必须排在 `stalled` **之前**：观察窗到期是一条会过期的读数，
     而"库里有卡"是既成事实。反过来排会让一张交付完的单在观察窗到期后重新自称"停住了"。 */
  if (studio.tasks.length > 0) return "done";
  if (studio.live.stage === "stalled") return "stalled";
  if (studio.live.docs) return "split";
  if (studio.turns.length >= meetingTurnsOf(studio.live.rounds)) return "docs";
  return "meeting";
}

/** 每一步屏上该说的一句话。**主语的动词都是"正在"，只有 `stalled` 与 `needs_human` 不是。**
 *
 *  `needs_human` 那句为什么这么写（R-26，2026-10-28 方案二）：它必须同时说清三件事 ——
 *  ① 这不是模型挂了，是**拆单没过契约**；② 下一步该干什么（改需求再立项）；
 *  ③ 去哪儿改（「修改需求」会把原文带回左侧受理台）。
 *  只写"待人工干预"是四个字加一个死胡同：人到了屏前仍然不知道手该放哪。 */
const STEP_SAID: Readonly<Record<Exclude<Step, "done">, string>> = {
  meeting: "会议进行中",
  docs: "会议已锁定 · 正在落 PRD / 架构 / 令牌",
  split: "文档已落 · 正在拆任务卡",
  stalled: "停住了 · 观察窗到期还没拿到该拿的东西",
  needs_human: "待人工干预 · 拆单没过契约，改需求后重新立项",
  failed: "失败 · 受理链已停",
};

/** 受理链读数条：**第几步 / 这一步在干什么 / 已等多久**。
 *
 *  它接的是用户那句"讨论完就没反应了"：会议桌四段台词说完之后，屏上会**静下来 1–3 分钟**
 *  （文档节点一次调用 + 拆单一次调用），而那段时间原来一个字都没有。
 *
 *  三个刻意的选择：
 *  1. **三段等宽，不按时间分**。文档那一段可能比会议还长，但屏幕上不给它更多格子 ——
 *     格子宽窄一旦按时间画，就是在暗示"还剩多少"，而那个数我们不知道。
 *  2. **已过、当前、未到三种状态**，当前那格呼吸。`lamp-breathe` 是这支样式表里
 *     唯二允许的关键帧之一（见 `test_the_only_keyframes_…`），不为它新开一支。
 *  3. **`已等` 用 `stageSince`**，不是整单的 `meetingSince`。会议说完之后屏上换成了
 *     "正在落文档"，这时候把整单的 2 分钟算进"落文档已等 2 分钟"是错的。 */
function PipelineBar({ studio }: { studio: StudioState }) {
  const step = pipelineStep(studio);
  const since = studio.live.stageSince ?? studio.live.meetingSince;
  const elapsed = useElapsed(since);
  const elapsedMin = useElapsedMinutes(since);
  if (step === "done") return null;

  const total = meetingTurnsOf(studio.live.rounds);
  const n = studio.turns.length;
  const who = meetingShape(studio).speaking;
  /* 停下来（`stalled` / `needs_human`）时三段条没有"当前格"：`-1` 让三格全灭。
     给 `needs_human` 留一格"现在"会读成"它还在推进"，而它已经停了。 */
  const parked = step === "stalled" || step === "needs_human" || step === "failed";
  const at = parked ? -1 : STEP_ORDER.indexOf(step as "meeting" | "docs" | "split");
  /* 停下来那两档都说原因，但主语不同（U6）：
     - `stalled`：说**为什么**停（"停住了 · docs_timeout…"），用户据此去查留痕或重跑；
     - `needs_human`：说**下一步做什么**，归因放进 `title`（悬停可读）与左侧那张卡里 ——
       进度条这一行是 `nowrap` 的窄条，把"拆单 2 次都过不了契约（最后一次 split_extra_field：…）"
       塞进来会把后面那句最要紧的话挤掉。 */
  const stalledSaid = studio.live.lastError
    ? `停住了 · ${studio.live.lastError.length > 46 ? `${studio.live.lastError.slice(0, 45)}…` : studio.live.lastError}`
    : STEP_SAID.stalled;
  const said =
    step === "meeting"
      ? `第 ${Math.min(n + 1, total)} / ${total} 句${who ? ` · ${agentTitle(who)}正在发言` : ""}`
      : step === "stalled"
        ? stalledSaid
        : step === "failed"
          ? `失败 · ${studio.live.lastError || STEP_SAID.failed}`
          : STEP_SAID[step];
  const causeTitle = (step === "needs_human" || step === "failed") && studio.live.lastError ? studio.live.lastError : undefined;
  /* `needs_human` 不报"已等"：这一格等的是人，不是模型 ——
     一个还在走的秒表会让"等人"读成"快好了"，而它不会自己好。`failed` 同理：链已经停了。 */
  const tail = step === "needs_human" || step === "failed" ? "" : ` · 已等 ${elapsed}`;
  /* 读屏只播"阶段"，不播每一句会议发言：`said` 里带第 N 句/发言人，会每轮变一次；
     `stageSaid` 只在 step 变化时变，播报频率才与"转折点"对齐。 */
  const stageSaid =
    step === "meeting"
      ? "会议进行中"
      : step === "stalled"
        ? stalledSaid
        : step === "failed"
          ? `失败 · ${studio.live.lastError || STEP_SAID.failed}`
          : STEP_SAID[step];
  /* 两档停下的**颜色**不同（2026-10-28 方案二）：`stuck` = 红（这单失败了），
     `hold` = 琥珀（这单在等人）。同一个色会让"该动手改需求"与"该去查日志"看起来是一件事。 */
  const tone = step === "stalled" || step === "failed" ? " stuck" : step === "needs_human" ? " hold" : "";

  return (
    <div className={`meet-prog${tone}`} title={causeTitle}>
      <span className="typing" aria-hidden="true">
        <i />
        <i />
        <i />
      </span>
      <div className="steps" aria-hidden="true">
        {STEP_ORDER.map((name, index) => (
          <i key={name} data-s={index < at ? "done" : index === at ? "now" : "todo"} />
        ))}
      </div>
      <span className="note" aria-hidden="true">{`${said}${tail}`}</span>
      {/* 播报只跟阶段走；秒表进 aria-live="off" 的独立节点，读屏可读、不自动念。 */}
      <span className="sr-only" role="status" aria-live="polite">{stageSaid}</span>
      {step !== "needs_human" ? <span className="sr-only" aria-live="off">{`已等 ${elapsedMin} 分`}</span> : null}
    </div>
  );
}

function MeetingCard({ studio }: { studio: StudioState }) {
  const shape = meetingShape(studio);
  const live = studio.source === "live";
  /* 推演档的剧本是四段固定的；真链路的分母是这一单真的开了几轮。 */
  const total = live ? meetingTurnsOf(studio.live.rounds) : MEETING_SEATS.length;
  /* 哪一条是"刚到的" —— 只在**这一屏停留期间新增**的那一条上放打字机。
   *
   *  为什么不是"最后一条"：刷新页面接回一单时，四条台词早就都在库里了；
   *  把最后一条也揭开一遍，等于把"重新加载"演成"正在开会"。
   *  `useRef` 的初值就是挂载那一刻的条数，所以挂载时 `fresh` 恒为 -1（谁都不揭）。 */
  const mountedAt = useRef(studio.turns.length);
  const fresh = studio.turns.length > mountedAt.current ? studio.turns.length - 1 : -1;
  const cutOff = shape.over && studio.turns.length > 0 && studio.turns.length < total;
  return (
    <div className="card meet">
      <div className="meet-h">
        {MEETING_SEATS.map((id) => (
          <span
            key={id}
            className={`who${shape.speaking === id ? " on" : ""}${shape.spoken.has(id) ? " done" : ""}`}
            style={{ ["--tone" as string]: `var(${toneOf(id)})` }}
          >
            <span className="mono-chip">{monoOf(id)}</span>
            {agentTitle(id)}
            {shape.spoken.has(id) ? (
              <span className="seat-tick" aria-label="已发言">
                ✓
              </span>
            ) : null}
          </span>
        ))}
        <div className="right" style={{ marginLeft: "auto" }}>
          <span className="hint">
            <MeetStatus studio={studio} shape={shape} />
          </span>
        </div>
      </div>
      {live ? <PipelineBar studio={studio} /> : null}
      <div className="turns" id="turns">
        {/* 三个空态各说一件事，互不重复：
            「待启动」= 还没有需求（这一步该你写）；
            「没有产出」= 会开过了但一段台词都没读到（这一步不该你操作）；
            有台词 / 有人正在说 = 会议本身。 */}
        {!studio.plan ? (
          <div className="meet-empty">
            <b>会议室待启动</b>
            受理需求后，编排专家会先开一场会：需求分析写 PRD、系统架构定结构、设计定令牌，最后拍板派单。
          </div>
        ) : studio.turns.length === 0 && shape.over ? (
          <div className="meet-empty">
            <b>会议没有产出</b>
            {studio.live.stage === "needs_human"
              ? "这单停在 R-26 卡点：拆单两次都没过契约，等人改需求。没读到台词不影响这个判断 —— 改完重新立项会重开一场会。"
              : studio.live.stage === "stalled"
                ? "等了 5 分钟没读到台词：模型侧慢，或者这一档没接上。拆单不受影响，任务卡仍会落库。"
                : "这一单没有读到任何会议台词（模型没接上，或这一档没开会议）。拆单不受影响，任务卡仍会落库。"}
          </div>
        ) : (
          <>
            {studio.turns.map((turn, index) =>
              live ? (
                <LiveTurn key={`${turn.who}-${index}`} turn={turn} animate={index === fresh} />
              ) : (
                <DemoTurn key={`${turn.t}-${index}`} turn={turn} />
              ),
            )}
            {shape.speaking ? <SpeakingBubble who={shape.speaking} waiting={studio.turns.length === 0} /> : null}
            {cutOff ? (
              <div className="meet-cut">
                {`会说到第 ${studio.turns.length} 句就停了，这一场共 ${total} 句（${total - studio.turns.length} 个发言位没人接）。`}
                拆单不受影响 —— 它会退回只按需求原文拆。
              </div>
            ) : null}
          </>
        )}
      </div>
    </div>
  );
}

const MONO: Readonly<Record<string, string>> = {
  orchestrator: "总管",
  pm: "产品",
  cto: "技术",
  designer: "设计",
  fe: "前端",
  be: "后端",
  qa: "测试",
  sec: "安全",
  reviewer: "审查",
  acceptor: "验收",
  writer: "文档",
  rescue: "全能",
};

const TONE: Readonly<Record<string, string>> = {
  orchestrator: "--a1",
  pm: "--a2",
  cto: "--a3",
  designer: "--a4",
  fe: "--a5",
  be: "--a6",
  qa: "--a3",
  sec: "--a5",
  reviewer: "--a1",
  acceptor: "--a4",
  writer: "--a2",
  rescue: "--a6",
};

function monoOf(id: string): string {
  return MONO[id] ?? "总管";
}

function toneOf(id: string): string {
  return TONE[id] ?? "--a1";
}

/* ---------------------------- ③ 产出 ---------------------------- */

/** 产出卡底部那一条：**修改需求 / 增加需求**，以及"增加需求"就地展开的输入格。
 *
 *  为什么两个出口挨着放：读完 PRD 只有两个念头 —— "这条不对，得改"与"这条少了点东西"。
 *  它们的分岔只在一句"换不换单"，读者要在同一处做这个判断。
 *  - 修改需求 = 离开这一单，把原文带回受理台重开一单（`editRequirement`）；
 *  - 增加需求 = **留在这一单上**加一句（owner 2026-10-28 第 2 条）：四岗位带着上一版 PRD
 *    重开一场会，只重出 PRD，**已有任务卡不动**（后端 `_run` 里"有卡就不重拆"那条分叉）。
 *
 *  为什么输入格是**就地展开**而不是弹窗：弹窗要管焦点陷阱、要挡住下面的会议桌，
 *  而这一格恰恰是"我刚读完 PRD，想再补一句"—— 读者需要一边看着 PRD 一边写。
 *
 *  为什么提交后不自己收起来：`extendLive` 回来的是 202（已排队），会议还要再跑 1–3 分钟。
 *  立刻收起会让人以为"没发生"；留着"已提交"那行字，等新的台词开始到才收 ——
 *  到那一刻，"我这一下真的起作用了"才有证据（见下面那个 `useEffect`）。
 */
function OutFoot({ studio }: { studio: StudioState }) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [sent, setSent] = useState(false);
  const requestId = studio.live.requestId;
  /* 受理正在跑（开会 / 落文档）时不给按：后端那道 409 只看 `status == INTAKE`，
     而**追加后的重开不改 status**（`extend_request` 刻意不推进状态机），
     所以那一道挡不住"重开期间又追加一次"。挡在这里是唯一挡住它的地方 ——
     真按下去的后果不是报错，是两条链同时改同一份 digest，谁覆盖谁全靠运气。 */
  const running = studio.live.stage === "meeting" || studio.live.stage === "docs";

  /* 新的一趟开始说话了（`extendLive` 清过本地台词，所以这是"新台词"）⇒ 收掉那行"已提交"。
     它已经兑现了，留着就是一句过期的承诺。 */
  useEffect(() => {
    if (sent && studio.turns.length > 0) {
      setSent(false);
      setOpen(false);
      setText("");
    }
  }, [sent, studio.turns.length]);

  const fire = async (): Promise<void> => {
    const value = text.trim();
    if (!value || busy) return;
    setBusy(true);
    setErr("");
    try {
      await extendLive(value);
      setSent(true);
    } catch (cause) {
      setErr(cause instanceof ApiError ? cause.detail : String(cause));
    } finally {
      setBusy(false);
    }
  };

  const openBox = (): void => {
    /* 推演档没有后端：这一屏的需求是写死的脚本，加一句进去没有任何东西会读它。
       点下去什么也不发生比弹一句话更糟 —— 那会被读成"按钮坏了"。 */
    if (!requestId) {
      pushToast({
        tone: "info",
        title: "增加需求要走真链路",
        detail: "推演档的需求是固定脚本，加一句没人读。受理台填上项目键、真立一次项之后，这个按钮就在这一单上生效。",
      });
      return;
    }
    setErr("");
    setOpen(true);
  };

  return (
    <>
      {open ? (
        <div className="out-extend" role="group" aria-label="增加需求">
          <textarea
            value={text}
            autoFocus
            placeholder="在这条需求上再加一句，例如：另外要一个后台能改商品价格的页面。"
            onChange={(event) => setText(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Escape") setOpen(false);
              if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) void fire();
            }}
          />
          <div className="out-extend-foot">
            <span className="note" role={err ? "alert" : "status"}>
              {err
                ? err
                : sent
                  ? "已提交 · 四岗位正在重开讨论，PRD 出来时这一格会换成新版"
                  : "四岗位会带着这一版 PRD 重开一场会，只重出 PRD，已有任务卡不动"}
            </span>
            <button type="button" className="ghost" onClick={() => setOpen(false)} disabled={busy}>
              取消
            </button>
            <button
              type="button"
              className="primary"
              data-act="extend-send"
              onClick={() => void fire()}
              disabled={busy || sent || !text.trim()}
            >
              {busy ? "提交中…" : sent ? "已提交" : "追加并重开会"}
            </button>
          </div>
        </div>
      ) : null}
      <div className="out-foot">
        <button type="button" className="ghost" onClick={() => leaveProject(editRequirement)}>
          修改需求
        </button>
        <button
          type="button"
          className="ghost"
          data-act="extend-open"
          disabled={running || open}
          title={running ? "这一趟还在跑（开会 / 落文档），等它跑完再加" : undefined}
          onClick={openBox}
        >
          增加需求
        </button>
      </div>
    </>
  );
}

function OutputsCard({ studio }: { studio: StudioState }) {
  const has = (name: string): boolean => studio.artifacts.some((item) => item.name === name);
  const live = studio.source === "live";
  const docs = studio.live.docs;
  const tabs: { k: OutputTab; label: string; on: boolean }[] = [
    /* 真链路点灯的条件是**文档真的到了**（`live.docs`），不是"这一屏有 plan"。
       给没内容的格子点灯等于承诺"点开有东西"，点开却只有一句"还在写"。 */
    { k: "prd", label: "PRD", on: live ? !!docs?.prd : !!studio.plan },
    { k: "arch", label: "架构", on: live ? !!docs?.arch : has("architecture.md") },
    { k: "token", label: "令牌", on: live ? !!docs?.tokens : has("tokens.json") },
    { k: "cards", label: "任务卡", on: live ? studio.tasks.length > 0 : !!studio.meeting && studio.meeting.done },
  ];
  return (
    <div className="card out-card">
      <div className="out-tabs" role="tablist" aria-label="会议产出">
        {tabs.map((tab) => (
          <button
            key={tab.k}
            type="button"
            role="tab"
            className={`out-tab${studio.otab === tab.k ? " on" : ""}`}
            data-otab={tab.k}
            aria-selected={studio.otab === tab.k}
            aria-controls="outBody"
            onClick={() => setOutputTab(tab.k)}
          >
            {tab.label}
            {tab.on && studio.otab !== tab.k ? <span className="dot" /> : null}
          </button>
        ))}
      </div>
      <div className="out-body" id="outBody" role="tabpanel" aria-label={tabs.find((tab) => tab.k === studio.otab)?.label}>
        <OutputBody studio={studio} />
      </div>
      {/* 读完 PRD 最可能产生的两个念头，出口都放在**这张卡的底部**（见 `OutFoot`）。 */}
      {studio.plan ? <OutFoot studio={studio} /> : null}
    </div>
  );
}

/** 文档还没到时的占位：**在写就画骨架，没有就只说没有**。
 *
 *  为什么要有骨架而不是一行灰字：文档节点实测 16 秒（关思考）到 45 秒（开思考），
 *  那段时间整张产出卡是一片空白加一句话 —— 屏上没有任何东西在动，读起来就是"卡住了"。
 *  骨架条本身不承诺进度（它没有宽度变化、没有百分比），只承诺"这一格会被填上"。
 *
 *  **观察窗一到期就立刻撤掉骨架**：还在呼吸的骨架等于在说"马上就来"，
 *  而那句话已经不成立了（见 `docWait`）。这一条是这段代码里最重要的分支。
 */
function WaitDoc({ file, tip, studio }: { file: string; tip: string; studio: StudioState }) {
  const since = studio.live.stageSince ?? null;
  const elapsed = useElapsed(since);
  const elapsedMin = useElapsedMinutes(since);
  const writing = studio.source === "live" && !isParked(studio.live.stage) && !studio.live.docs;
  if (!writing) {
    return (
      <div className="meet-empty">
        <b className="mono doc-file">{file}</b>
        {tip}
      </div>
    );
  }
  return (
    <div className="doc-skel">
      {/* 视觉层整块 aria-hidden：读屏只听下面三句，不跟着每秒跳的秒表念。 */}
      <div className="doc-skel-h" aria-hidden="true">
        <b className="mono">{file}</b>
        <span className="typing">
          <i />
          <i />
          <i />
        </span>
        <span className="tm">{`已等 ${elapsed}`}</span>
      </div>
      <span className="skel" aria-hidden="true" />
      <span className="skel" aria-hidden="true" />
      <span className="skel" aria-hidden="true" />
      <span className="skel" aria-hidden="true" />
      <p className="doc-skel-tip" aria-hidden="true">{tip}</p>
      <span className="sr-only" role="status" aria-live="polite">{`正在写 ${file}`}</span>
      <span className="sr-only">{tip}</span>
      <span className="sr-only" aria-live="off">{`已等 ${elapsedMin} 分`}</span>
    </div>
  );
}

/** 真链路里文档还没到时的两种说法：**在写** 与 **没有**。
 *
 *  区别不是措辞，是下一步该干什么：前者等，后者去查服务端的 `docs_failed` 留痕
 *  （或者承认这是一张文档节点上线前跑的老单）。一句话糊过去，读者会一直等一个不会来的东西。 */
function docWait(studio: StudioState): string {
  /* 先判 `needs_human`：它比 `stalled` 多一句"已经发生了什么"，那句才是人做决定要的。
     这单的会议与文档其实可能都在（停在拆单那一步），所以不能说"这一单没有文档"，
     更不能说"拆单与后续不受影响" —— 拆单正是停在这里的那一步。 */
  if (studio.live.stage === "needs_human")
    return studio.live.lastError
      ? `这单停在 R-26 卡点：拆单两次都没过契约（${studio.live.lastError}）。会议与文档不用重跑，改需求后重新立项即可。`
      : "这单停在 R-26 卡点：拆单两次都没过契约，等人改需求。会议与文档不用重跑。";
  if (studio.live.stage === "stalled")
    return studio.live.lastError
      ? `这一单停在受理段：${studio.live.lastError}。重试会从同一条单重跑，之前的会议台词不会丢。`
      : "这一单没有文档：要么是文档节点上线前跑的老单（digest 里没有这三段），要么模型跑偏了（活动流里有一条 docs_failed）。拆单与后续不受影响。";
  return "PRD / 架构 / 令牌 是一次调用产出的，所以三格会同时亮。";
}

function OutputBody({ studio }: { studio: StudioState }) {
  const plan = studio.plan;
  const live = studio.source === "live";
  const docs = studio.live.docs;
  /* 未立项时这四个页签都还没有内容，但**卡本身在** —— 它和已立项是同一个位置，
     差别只是里面是空的。空态那句说清"这里会长出什么"，而不是"请先立项"：
     顺序由四张卡的位置表达，这句话只补一个信息 —— 长出来的东西**依次**是这四个。 */
  if (!plan)
    return (
      <div className="meet-empty" style={{ padding: "34px 10px" }}>
        还没有产出。
        <br />
        立项后，这里会依次长出 PRD、架构、令牌与任务卡。
      </div>
    );

  if (studio.otab === "prd") {
    /* 真链路读后端文档（`live.docs.prd`），推演档读本地生成记录（`artifacts`）。
       两条来源不许混：混起来就会出现"文档没到但页面有内容"，而那份内容是猜的。 */
    const ready = live ? plan.pages.length > 0 : studio.artifacts.some((item) => item.name === "prd.md");
    if (!ready) return <WaitDoc studio={studio} file="prd.md" tip={live ? docWait(studio) : "需求分析专家正在写…"} />;
    return (
      <div className="doc">
        <div className="doc-h">
          <span className="fn">prd.md</span>
          <span className="who2">{`需求分析专家 · ${plan.prj}`}</span>
        </div>
        <div className="doc-sec">
          <h4>目标</h4>
          <p>
            {`${plan.title}：${plan.typeLabel}。${plan.feat.responsive ? "手机端是一等公民，不是适配补丁。" : "先跑通主路径。"}`}
          </p>
        </div>
        <div className="doc-sec">
          <h4>页面结构</h4>
          <ul>
            {plan.pages.map((page) => (
              <li key={page.key}>
                <b>{page.label}</b>{" "}
                <span className="mono" style={{ fontSize: "var(--fs-meta)", color: "var(--ink-muted)" }}>
                  {page.file}
                </span>
              </li>
            ))}
          </ul>
        </div>
        <div className="doc-sec">
          <h4>验收标准</h4>
          <ul>
            {plan.ac.map((item) => (
              <li key={item.id}>
                <span className="ac">{item.id}</span>
                {item.text}
              </li>
            ))}
          </ul>
        </div>
        <div className="doc-sec">
          <h4>本期不做</h4>
          <ul>
            {plan.outOfScope.map((item) => (
              <li key={item} style={{ color: "var(--ink-muted)" }}>
                {item}
              </li>
            ))}
          </ul>
        </div>
      </div>
    );
  }

  if (studio.otab === "arch") {
    const ready = live ? plan.stack.length > 0 : studio.artifacts.some((item) => item.name === "architecture.md");
    if (!ready) return <WaitDoc studio={studio} file="architecture.md" tip={live ? docWait(studio) : "系统架构专家正在定结构…"} />;
    const tree = plan.pages.map((page) => `├─ ${page.file}`).join("\n");
    return (
      <div className="doc">
        <div className="doc-h">
          <span className="fn">architecture.md</span>
          <span className="who2">系统架构专家</span>
        </div>
        <dl className="kv">
          <dt>选型</dt>
          <dd>{plan.stack}</dd>
          <dt>理由</dt>
          <dd>{plan.why}</dd>
          {/* 风险是真跑出来的那一句（`docs.arch.risk`）；推演档没有这一格，就不印空行。 */}
          {plan.risk ? (
            <>
              <dt>风险</dt>
              <dd>{plan.risk}</dd>
            </>
          ) : null}
          <dt>并发</dt>
          <dd>3（依赖就绪即起跑）</dd>
        </dl>
        <div className="doc-sec">
          <h4>文件树</h4>
          <div className="pre">{`${tree}
├─ style.css
├─ app.js
├─ serve.py
└─ README.md`}</div>
        </div>
        <div className="doc-sec">
          <h4>拆卡原则</h4>
          <p>一张卡 = 一个可独立验收的产物。产物不能独立验收的，说明还没拆到底。</p>
        </div>
      </div>
    );
  }

  if (studio.otab === "token") {
    const ready = live ? !!docs?.tokens : studio.artifacts.some((item) => item.name === "tokens.json");
    if (!ready) return <WaitDoc studio={studio} file="tokens.json" tip={live ? docWait(studio) : "UI/UX 设计师正在定色与字阶…"} />;
    const palette = plan.palette;
    return (
      <div className="doc">
        <div className="doc-h">
          <span className="fn">tokens.json</span>
          <span className="who2">{`UI/UX 设计师 · ${palette.name}`}</span>
        </div>
        <div className="swatches">
          <div className="sw" style={{ background: palette.accent }}>
            <span>accent</span>
          </div>
          <div className="sw" style={{ background: palette.accent2 }}>
            <span>accent2</span>
          </div>
          <div className="sw" style={{ background: palette.bg }}>
            <span>bg</span>
          </div>
          <div className="sw" style={{ background: palette.surface }}>
            <span>surface</span>
          </div>
          <div className="sw" style={{ background: palette.ink }}>
            <span>ink</span>
          </div>
        </div>
        <div className="pre">{`{
  "color": {
    "bg": "${palette.bg}",
    "surface": "${palette.surface}",
    "ink": "${palette.ink}",
    "muted": "${palette.muted}",
    "accent": "${palette.accent}",
    "accent-2": "${palette.accent2}",
    "line": "${palette.line}"
  },
  "font": { "scale": [12, 14, 20, 32], "line": 1.7 },
  "space": { "unit": 8, "steps": [1, 2, 3, 5, 8] },
  "radius": { "card": 12, "pill": 999 }
}`}</div>
        <div className="doc-sec">
          <h4>约束</h4>
          <p>前端只引变量，不写死色值 —— 否则改一版配色要动 5 个文件。</p>
        </div>
      </div>
    );
  }

  /* 真链路没有"方案锁定"这个前端里程碑：卡是模型拆完直接落库的，
     所以这里的门槛是"库里有卡没有"。等卡的那段时间说清在等什么，别让空态看起来像坏了。 */
  if (live ? studio.tasks.length === 0 : !studio.meeting || !studio.meeting.done) {
    return <WaitDoc studio={studio} file="任务卡" tip={live ? "真拆单：模型读 PRD 拆成可执行的卡，一次调用。" : "等方案锁定后由系统架构专家拆卡…"} />;
  }
  const visible = studio.tasks.filter((task) => !task.hidden && task.phase >= 2);
  const ranks = [...new Set(visible.map((task) => task.gr ?? task.rank ?? 0))].sort((a, b) => a - b);
  return (
    <div className="doc">
      <div className="doc-h">
        <span className="fn">{`tasks · ${visible.length} 张`}</span>
        <span className="who2">系统架构专家派单</span>
      </div>
      {ranks.map((rank) => (
        <div key={rank}>
          <div className="rank-h">{`第 ${rank + 1} 层`}</div>
          {visible
            .filter((task) => (task.gr ?? task.rank ?? 0) === rank)
            .map((task) => (
              <div key={task.id} className="kcard" style={{ ["--tone" as string]: `var(${toneOf(task.owner)})` }}>
                <div className="kc-h">
                  <span className="kc-id">{task.id}</span>
                  <b>{task.title}</b>
                  <span className="mono-chip">{monoOf(task.owner)}</span>
                </div>
                <div className="kc-m">
                  {agentTitle(task.owner)}
                  {task.deps.length ? (
                    <>
                      {" · 依赖 "}
                      {task.deps.map((dep) => (
                        <span key={dep} className="dep">
                          {dep}
                        </span>
                      ))}
                    </>
                  ) : (
                    " · 无依赖，可起跑"
                  )}
                </div>
              </div>
            ))}
        </div>
      ))}
    </div>
  );
}

/* ---------------------------- ④ 派单台 ---------------------------- */

function DispatchCard({ studio }: { studio: StudioState }) {
  /* 真链路这颗主键是**导航键**，不是推进键：它只切屏，一个字节都不写给后端。
   *
   * 为什么原来没有它（2026-10-28 之前）：这一格写的是「门禁在右下「待办」」并且 `disabled` ——
   * 一颗长得像动作、实际点不动的按钮，而流程里再没有第二个"往前走"的入口
   * （抬头那颗「执行看板 →」在最右边，不在四张卡的顺序里）。owner 的原话：
   * 「改为确认派单，进入执行，然后点击就会跳转到执行看板」。
   *
   * 为什么叫"确认派单"却不在这里批单：真链路的派单是后端 A 类审批（`A_SPLIT`），
   * 批单只有一次、而且要留痕（谁批的、第几版）。在页面上再放一个能推进的键，
   * 等于把那道门从界面上绕过去 —— 所以这颗键做的是**把人送到那道门跟前**。
   *
   * 2026-10-28 之后那道门自己开了：`ACP_APPROVAL_MODE=auto` 让拆单确认由规则免确认
   * （`approval/policy.py::SPLIT_AUTO_RULE`），受理跑完就直接续跑。右下那块待办浮层
   * 也随之整块删掉 —— 所以这颗键现在纯粹是"进入执行"的导航，note 那行不再提任何面板。 */
  const live = studio.source === "live";
  const ready = live ? studio.tasks.length > 0 : !!studio.meeting && studio.meeting.done;
  const geo = studio.tasks.length ? studio.dag : null;
  const count = studio.tasks.filter((task) => !task.hidden && task.phase >= 2).length;
  return (
    <div className="card">
      <div className="card-h">
        <h2>派单</h2>
        <span className="hint">依赖图 · 并发 3</span>
      </div>
      <div className="card-b">
        <div className="rail-foot" style={{ margin: "0 0 10px" }}>
          <div className="row">
            <span>执行卡</span>
            <b>{ready ? count : "—"}</b>
          </div>
          <div className="row">
            <span>依赖层数</span>
            <b>{ready && geo ? geo.maxRank + 1 : "—"}</b>
          </div>
          <div className="row">
            <span>关键路径</span>
            <b>{ready && geo ? `${geo.maxRank + 1} 步` : "—"}</b>
          </div>
          <div className="row">
            <span>门禁</span>
            <b>E2E · 安全 · AC 比对 · 验收</b>
          </div>
        </div>
        <button
          type="button"
          className="primary"
          id="btnDispatch"
          style={{ width: "100%" }}
          disabled={!ready}
          onClick={() => {
            /* 两档的 onClick 不同，但**目的相同**：把人送到执行看板。
               推演档的 `dispatch()` 顺手把推演状态推进一格；真链路只切屏 ——
               `dispatch()` 自己在 live 档就 `return`，这里显式分叉是为了让"谁做了什么"读得出来。 */
            if (live) setScreen("board");
            else dispatch();
          }}
        >
          {ready ? (
            /* 两档同一句：从这颗键出发，下一站都是执行看板。 */
            "确认派单，进入执行 →"
          ) : live ? (
            <>
              等拆单
              <span className="typing">
                <i />
                <i />
                <i />
              </span>
            </>
          ) : (
            "方案未锁定"
          )}
        </button>
        <div className="note" style={{ marginTop: 8, fontSize: "var(--fs-meta)", color: "var(--ink-muted)", textAlign: "center" }}>
          {live
            ? "点它切到执行看板；卡的进度在那张图上"
            : "派单后视图切回执行看板，左栏亮灯"}
        </div>
      </div>
    </div>
  );
}

export function StudioScreen({ studio }: { studio: StudioState }) {
  /* 工作室屏**永远**是这四张卡：受理 / 会议桌 / 产出 / 派单。未立项与已立项共用同一个网格，
   * 区别只在卡里是空的还是有内容 —— 所以这里没有分支，只有一份 JSX。
   *
   * 这一天里这一屏改过四版，值得记下来，因为**每一次的判断依据都不一样**：
   *
   *   ① 四张卡 + 五句说明（原型原样）。四张卡等权重、只有左上角能点，于是"顺序"要靠五句话讲
   *      五遍（副标题、按钮旁 note、会议空态、产出空态、派单 note）。判据是规则 1：
   *      删掉那五句就"看不懂了"，说明布局本身有问题。
   *   ② 只留受理卡（重设计）。三张空卡不渲染，"出现的顺序"代替说明书。这一步本身是对的，
   *      但它把"位置"也一起改坏了 —— 见 ③④。
   *   ③ 整屏居中一张 820 的表单。owner：「受理需求也没必要单独做出来一页」。
   *   ④ 主区里垂直居中。位置还是不对，owner 又指了一次：「移到这个位置」。
   *
   * 现在这一版把 ① 的**布局**和 ② 的**判断**放在一起：四张卡都在（位置、占位、网格都对，
   * 立项前后一个像素不动），但三张空卡的空态各说**一件不同的事**，不再是把同一句流程
   * 讲五遍 ——
   *   受理：客户原话越口语越好 / 会后产出什么（这两句在说"你该怎么写"）
   *   会议：会议室待启动（在说"你写完会发生什么"）
   *   产出：还没有产出，这里会依次长出四个东西（在说"这块地方将来装什么"）
   *   派单：四行「—」+ 方案未锁定（在说"为什么按钮是灰的"）
   * 四句话各回答一个不同的问题，所以它们不是同一句的五个副本。
   *
   * 附带的好处：`!plan` 只活在四张卡内部（各自渲染空态），StudioScreen 不再需要判两次。 */
  return (
    <section className="studio">
      <StudioHead studio={studio} />
      <SuspendedBanner studio={studio} />
      <div className="studio-grid">
        <div>
          <IntakeCard studio={studio} />
          <MeetingCard studio={studio} />
        </div>
        <div>
          <OutputsCard studio={studio} />
          <DispatchCard studio={studio} />
        </div>
      </div>
    </section>
  );
}
