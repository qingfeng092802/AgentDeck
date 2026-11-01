import {
  agentMono,
  edgeClass,
  edgePath,
  NODE_H,
  NODE_RADIUS,
  NODE_W,
  nodeClass,
  noticeSelection,
  openTaskDetail,
  selectTask,
  stopWordOf,
  type StopReading,
  type StudioState,
  type StudioTask,
  toneVar,
} from "../../lib/studioEngine";
import { staleOf, type StaleReading } from "../../lib/live";
import { SidePanel } from "./SidePanel";

/** 执行看板：阶段条 / 纵向 DAG / now 条 / 右栏页签。
 *
 *  中栏是整屏唯一的「主角面板」：四角折角只给它一家（`.board-main::before`）。
 *  折角是 8 条 1px 渐变，不加节点、不吃布局、不接点击。
 *
 *  这里**没有**"未立项空态"：那张空态（`PROJECT STUDIO` / 十二个工位都在待命 / 三步说明 / 一句
 *  "原型不连后端"）在 2026-10-27 之前是不可达的 —— 进看板唯一的门是工作室屏那颗按钮，
 *  而它 `plan` 存在时才渲染。150+ 字营销文案 + 约 60 行 CSS 从来没上过屏。
 *  现在"未立项到不了看板"写在 `StudioView` 的条件里（`screen === "board" && plan`），
 *  从"没有按钮"升级成"结构上不可能"，所以这个组件不需要再为空态留分支。
 */

const STAGE_LABEL: Record<number, string> = { 1: "立项", 2: "执行", 3: "门禁", 4: "交付" };

/** 节点图元的两档字号（SVG 用户单位，**不是**排版 px）。
 *
 *  为什么它不并进 `--fs-*` 那五档：整张 DAG 套在一个 viewBox 里等比缩放，`fontSize={NODE_META}`
 *  落到屏上是 10 × 缩放比 —— 和 `--fs-meta: 10.5px` 不在同一个坐标系。硬接令牌会让
 *  "图上的字"随窗口宽度在五档之间跳，而图元该做的是跟着图一起缩。
 *
 *  所以图元自己只留两档：标注（序号 / 状态 / K-id / 负责人 / 产物名）与标题。
 *  收敛前这里是 9.5 / 10 / 10.5 / 12 四个值 —— 0.5 的差在缩放之后连一个像素都不到，
 *  等于写了四个数、只发出两个信号。
 *
 *  判据 = tests/guards/test_studio_frontend.py 的 test_the_node_figure_has_two_type_sizes。 */
const NODE_META = 10;
const NODE_TITLE = 12;

function StageBar({ studio }: { studio: StudioState }) {
  return (
    <div className="stage-bar" id="stageBar">
      {[1, 2, 3, 4].map((phase) => {
        const scoped = studio.tasks.filter((task) => !task.hidden && task.phase === phase);
        const done = scoped.filter((task) => task.status === "done").length;
        const percent = scoped.length ? Math.round((done / scoped.length) * 100) : 0;
        return (
          <div
            key={phase}
            className={`stage-item${studio.phase === phase ? " on" : ""}`}
            data-k={phase}
            aria-current={studio.phase === phase ? "step" : "false"}
          >
            <div className="st-h">
              <span className="i">{String(phase).padStart(2, "0")}</span>
              <b>{STAGE_LABEL[phase]}</b>
              <span className="c">{`${done}/${scoped.length}`}</span>
            </div>
            {/* 进度是**唯一**表达"这一阶段完成多少"的东西，但它原来只是一个 div 里一条被撑开的 i ——
                读屏完全读不到。补 progressbar 之后，`3/3` 与 `0/1` 这种差别才有第二个通道。 */}
            <div
              className="bar"
              role="progressbar"
              aria-label={`${STAGE_LABEL[phase]}进度`}
              aria-valuenow={percent}
              aria-valuemin={0}
              aria-valuemax={100}
            >
              <i style={{ transform: `scaleX(${percent / 100})` }} />
            </div>
          </div>
        );
      })}
    </div>
  );
}

/** 节点可访问名：四段固定顺序（T 号 · 状态词 · 正交属性 · 可做的动作）。
 *  竖条、折角、点线都读不出来，所以「阻断 / 超时 / 产物不在总线上」必须出现在名字里。 */
function nodeAria(task: StudioTask, reading: StopReading, stale: StaleReading | null): string {
  const ortho = [stale?.reason ?? "", task.autoApproved ? "机器代签" : ""].filter(Boolean);
  const action = (stale ? "可重跑再签" : reading.action).replace(/ · /g, "，");
  return [task.id, reading.word, ...ortho, action].filter(Boolean).join(" · ");
}

function DagNode({
  task,
  index,
  selectedId,
  stalled,
  stale,
  halo,
  onActivate,
}: {
  task: StudioTask;
  index: number;
  selectedId: string | null;
  stalled: boolean;
  stale: StaleReading | null;
  halo: number;
  onActivate: (id: string) => void;
}) {
  /* 标题截断放宽到 14 字：12px × 14 = 168，加左内边距 8 仍落在 200 里，不会溢出。
     再短就只剩"交互脚本（购物车 +1…"这种读不出是哪张卡的半句。 */
  const title = task.title.length > 14 ? `${task.title.slice(0, 14)}…` : task.title;
  const reading = stopWordOf(task, { stalled, stale: stale?.reason ?? null });
  const cls = `${nodeClass(task)}${task.id === selectedId ? " sel" : ""}${stale ? " st-stale" : ""}`;
  return (
    <g
      className={cls}
      data-id={task.id}
      data-status={task.status}
      data-signal={reading.signal ?? undefined}
      transform={`translate(${task.gx ?? 0},${task.gy ?? 0})`}
      tabIndex={0}
      role="button"
      aria-label={nodeAria(task, reading, stale)}
      onClick={() => onActivate(task.id)}
      onKeyDown={(event) => {
        if (event.key === "Enter") onActivate(task.id);
      }}
    >
      <rect className="hit" x={-4} y={-4} width={NODE_W + 8} height={NODE_H + 8} />
      {/* `n-plate` 是弹性缩放的唯一受力层：坐标在父级 `<g>` 上，这一层只包图形。
          文字留在外面，缩放时不会被亚像素重栅格化糊掉。 */}
      <g className="n-plate">
        {halo > 0 && !reading.signal ? (
          <g className="halo" aria-hidden="true">
            <rect className="ring r1" x={-3} y={-3} width={NODE_W + 6} height={NODE_H + 6} rx={NODE_RADIUS + 3} />
            {halo >= 3 ? <rect className="ring r2" x={-7} y={-7} width={NODE_W + 14} height={NODE_H + 14} rx={NODE_RADIUS + 7} /> : null}
            {halo >= 3 ? <rect className="ring r3" x={-11} y={-11} width={NODE_W + 22} height={NODE_H + 22} rx={NODE_RADIUS + 11} /> : null}
          </g>
        ) : null}
        <rect className="box" width={NODE_W} height={NODE_H} rx={NODE_RADIUS} />
        {reading.signal === "block" ? (
          <rect className="sig sig-block" x={2} y={12} width={3} height={40} rx={0} />
        ) : null}
        {reading.signal === "timeout" ? (
          <rect className="sig sig-timeout" x={NODE_W - 5} y={12} width={3} height={40} rx={0} />
        ) : null}
        {stale ? <line className="fold" x1={184} y1={0} x2={202} y2={18} clipPath="url(#node-clip)" /> : null}
      </g>
      <text className="idx" x={8} y={18} fontSize={NODE_META}>
        {String(index + 1).padStart(2, "0")}
      </text>
      <circle className="dot tone" cx={28} cy={14.5} r={1.6} />
      <text className="word tone" x={33} y={18} fontSize={NODE_META}>
        {reading.word}
      </text>
      {/* 负责人固定在 x=70：序号 8 + 状态点 28 + 三字状态 ≈ 63，留 7 单位呼吸；
          右边 AUTO 徽章从 126 起，四个拉丁字母最多到 94，碰不上。 */}
      <text className="agent" x={70} y={18} fontSize={NODE_META}>
        {agentMono(task.owner)}
      </text>
      <text className="tid" x={NODE_W - 8} y={18} textAnchor="end" fontSize={NODE_META}>
        {task.id}
      </text>
      {/* 机器代签徽章：语义已经并进 `aria-label`，这里 `aria-hidden`，否则读屏读两遍。 */}
      {task.autoApproved ? (
        <g className="auto-badge" aria-hidden="true">
          <rect x={NODE_W - 74} y={6.5} width={40} height={15} rx={2} />
          <text x={NODE_W - 54} y={17.5} textAnchor="middle" fontSize={NODE_META}>
            AUTO
          </text>
        </g>
      ) : null}
      <text className="title" x={8} y={37} fontSize={NODE_TITLE} fontWeight={600}>
        {title}
      </text>
      <text className="sub" x={8} y={52} fontSize={NODE_META}>
        {reading.action}
      </text>
      <rect className="pbar" x={8} y={NODE_H - 7} width={NODE_W - 16} height={3} rx={1.5} />
      <rect className="prog tone" x={8} y={NODE_H - 7} width={(NODE_W - 16) * task.progress} height={3} rx={1.5} />
    </g>
  );
}


function Dag({ studio }: { studio: StudioState }) {
  /* P2-10：布局在 `adoptTasks` / `tick` 里就算好了，render 只读。
     以前这里直接调 `dagGeometry()`，而它会往任务对象里写 `gr/row/gx/gy` ——
     那是一次渲染期副作用（同一份 props 渲染两次会得到两份不同的对象状态）。 */
  const geo = studio.dag;
  const byId = new Map(geo.visible.map((task) => [task.id, task]));
  const stalled = studio.live.stage === "stalled";
  /* 光环总数封顶 3：一张卡跑时给三层；多张卡同时跑时每张 1 环、最多 3 张。
     超时的卡不呼吸 —— 它已经停了，再呼吸就是在说"还在跑"。 */
  const running = geo.visible.filter((task) => task.status === "run" && !stalled).slice(0, 3);
  const haloDepth = running.length === 1 ? 3 : 1;
  /* 审查/交付是"阅读位置"最贵的两页：点节点只选中 + 顶栏提示，不把页签拽走。
     其余页签（详情/活动流/文件总线/用量）点节点仍然直接落到详情 —— 那里没有"读到一半"的上下文。 */
  const protectTab = studio.tab === "review" || studio.tab === "deliver";
  const activateTask = (id: string): void => {
    if (protectTab) {
      selectTask(id);
      noticeSelection(id);
      return;
    }
    openTaskDetail(id);
  };
  return (
    <div className="dag-wrap" id="dagWrap">
      <svg width={geo.width} height={geo.height} viewBox={`0 0 ${geo.width} ${geo.height}`}>
        <defs>
          <marker id="arw" viewBox="0 0 10 10" refX={8} refY={5} markerWidth={5} markerHeight={5} orient="auto-start-reverse">
            <path className="arw" d="M 0 0 L 10 5 L 0 10 z" />
          </marker>
          {/* 折角的终点落在 rx=10 的弧外，必须裁；13 个节点共用这一份，避免重复 id 与多余栅格化。 */}
          <clipPath id="node-clip">
            <rect width={NODE_W} height={NODE_H} rx={NODE_RADIUS} />
          </clipPath>
        </defs>
        {geo.visible.flatMap((task) =>
          task.deps.map((dep) => {
            const parent = byId.get(dep);
            if (!parent) return null;
            return (
              <path
                key={`${dep}>${task.id}`}
                className={edgeClass(task, dep)}
                data-e={`${dep}>${task.id}`}
                d={edgePath(parent, task)}
                markerEnd="url(#arw)"
              />
            );
          }),
        )}
        {geo.visible.map((task, index) => (
          <DagNode
            key={task.id}
            task={task}
            index={index}
            selectedId={studio.selected}
            stalled={stalled}
            stale={staleOf(task, studio.artifacts, studio.live.evidenceStale)}
            halo={running.some((item) => item.id === task.id) ? haloDepth : 0}
            onActivate={activateTask}
          />
        ))}
      </svg>
    </div>
  );
}

function NowStrip({ studio }: { studio: StudioState }) {
  const running = studio.tasks.filter((task) => task.status === "run");
  const demo = studio.source !== "live";
  if (!running.length) {
    const allDone = studio.tasks.length > 0 && studio.tasks.filter((task) => !task.hidden).every((task) => task.status === "done");
    return (
      <div className="now-strip" id="nowStrip">
        <span className="lbl">now</span>
        <span className="idle">{`${demo ? "固定脚本演示 · " : ""}${allDone ? "全部完成 · 交付包已生成" : "等待依赖就绪…"}`}</span>
      </div>
    );
  }
  return (
    <div className="now-strip" id="nowStrip">
      <span className="lbl">now</span>
      <div className="items">
        {running.map((task) => (
          <span key={task.id} className="now-it" style={{ ["--tone" as string]: toneVar(task.owner) }}>
            <span className="mono-chip">{agentMono(task.owner)}</span>
            {task.title}
            <span className="p">
              <i style={{ width: `${Math.round(task.progress * 100)}%` }} />
            </span>
            <span className="pc">{`${Math.round(task.progress * 100)}%`}</span>
          </span>
        ))}
      </div>
    </div>
  );
}

export function BoardScreen({ studio }: { studio: StudioState }) {
  return (
    <div className="board">
      <div className="board-main">
        <StageBar studio={studio} />
        <Dag studio={studio} />
        <NowStrip studio={studio} />
      </div>
      <SidePanel studio={studio} />
    </div>
  );
}
