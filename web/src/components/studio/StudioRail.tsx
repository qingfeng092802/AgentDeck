import { useState } from "react";
import {
  agentView,
  STUDIO_SEATS,
  STUDIO_AGENTS,
  setScreen,
  openAgentDrawer,
  type SeatView,
  type StudioState,
} from "../../lib/studioEngine";

/** 左栏：一扇门 + 12 张工位 + 四行读数。
 *
 *  门与名册是两件事：
 *    · **门**是"进工作室"的入口，写的是这间屋子的名字（项目工作室）；
 *    · **名册**是"这间屋子里有谁"，第一行就是工作室总管。
 *  2026-10-27 之前总管只以门的形式存在、名册里没有它，于是门上写「12 工位」而下面只数得出 11 行 ——
 *  owner 一眼看出来了。把门当成椅子是错的，把总管从名册里删掉也是错的。
 *
 *  每张椅子的灯由 `agentView()` 从任务推导，这里不写死任何一句话 ——
 *  "卡完成了但灯还亮着"这种假话在结构上就不会出现。总管的读数在 `agentView` 里单独一条
 *  （它不持有执行卡，靠任务状态推导永远点不亮它）。
 *
 *  **一行**（owner 2026-10-29 第十轮）：主行 = 专家职级（`AgentDef.title`），副行整条删掉。
 *  2026-10-28 那一版是"专家职级 + 核心职责"双行、主行 700 恒亮白；owner 用了半天之后的反馈是
 *  「十二个岗位的名称改回原来的颜色，现在的颜色太刺眼了，把副标题也删去」。
 *  所以这里回到 2026-10-27 的字色口径：**空闲/休眠的行压成 `--ink-muted`、字重 500**，
 *  只有真的在动的那几行是亮的 —— 一列导航十二行全亮白加粗，读起来是一片糊。
 *  `agent.note`（核心职责）没丢：它进这一行的 `title` 提示，连同读数一起读。
 */
const SEAT_WORD: Readonly<Record<SeatView["s"], string>> = {
  work: "运行中",
  think: "思考中",
  review: "待你处理",
  take: "接管中",
  done: "已完成",
  idle: "空闲",
  sleep: "休眠",
};

export function StudioRail({ studio }: { studio: StudioState }) {
  const [onlyActive, setOnlyActive] = useState(false);
  const doorOn = studio.screen === "studio";
  const running = studio.tasks.filter((task) => task.status === "run").length;
  const done = studio.tasks.filter((task) => !task.hidden && task.status === "done").length;
  const total = studio.tasks.filter((task) => !task.hidden).length;
  const rework = studio.tasks.filter((task) => task.kind === "rework" && !task.hidden).length;
  const plan = studio.plan;
  /* 先把 12 行连同读数算一次：摘要、过滤、逐行 aria-label 都读这一份，
     避免在 JSX 里对同一个工位调三次 `agentView()`。 */
  const rows = STUDIO_SEATS.map((agent) => ({ agent, view: agentView(agent.id) }));
  const moving = rows.filter((row) => row.view.s === "work" || row.view.s === "think" || row.view.s === "take").length;
  const waiting = rows.filter((row) => row.view.s === "review").length;
  const summary = moving + waiting === 0 ? "全在待命" : `在动 ${moving} · 待你 ${waiting}`;
  const shown = onlyActive
    ? rows.filter((row) => row.view.s === "work" || row.view.s === "think" || row.view.s === "take" || row.view.s === "review")
    : rows;

  return (
    <aside className="rail">
      <button
        type="button"
        className={`door${doorOn ? " on" : ""}`}
        data-act="studio"
        onClick={() => setScreen("studio")}
      >
        <span className="door-mark">▣</span>
        <span className="door-txt">
          <b>项目工作室</b>
          <i>{plan ? `${plan.prj} · ${plan.title}` : `编排 · ${STUDIO_HEADCOUNT} 工位 · 待立项`}</i>
        </span>
        <span
          className="lamp"
          style={doorOn ? { background: "var(--accent)", boxShadow: "0 0 8px var(--accent)" } : undefined}
        />
      </button>

      <div className="rail-sec">
        <h3>编制</h3>
        <span className="line" />
        <button
          type="button"
          className="rail-sum"
          aria-pressed={onlyActive}
          aria-label={onlyActive ? `${summary}，已过滤，点击显示全部工位` : `${summary}，点击只看在动与待你的工位`}
          onClick={() => setOnlyActive((value) => !value)}
        >
          {summary}
        </button>
      </div>

      {shown.map(({ agent, view }) => {
        const reading = view.note.replace(/^待处理 · /, "") || agent.note;
        return (
          <button
            key={agent.id}
            type="button"
            className="seat"
            data-s={view.s}
            data-agent={agent.id}
            style={{ ["--tone" as string]: `var(${agent.tone})` }}
            /* 职级常显，职责与读数只在提示里 —— 一列十二行，正文只留名字。 */
            title={view.note ? `${agent.title} · ${agent.note}｜${view.note}` : `${agent.title} · ${agent.note}`}
            aria-label={`${agent.title} · ${SEAT_WORD[view.s]} · ${reading}`}
            onClick={() => openAgentDrawer(agent.id)}
          >
            <span className="mono-chip">{agent.mono}</span>
            <span className="seat-txt">
              <b>{agent.title}</b>
            </span>
            <span className="lamp" />
          </button>
        );
      })}

      {shown.length === 0 ? <p className="rail-empty">没有在动或待你的工位</p> : null}

      <div className="rail-foot">
        <div className="row">
          <span>并发</span>
          <b>{`${running}/${studio.conc}`}</b>
        </div>
        <div className="row">
          <span>完成</span>
          <b className={done ? "ok" : undefined}>{`${done}/${total || 13}`}</b>
        </div>
        <div className="row">
          <span>返工</span>
          <b className={rework ? "dg" : undefined}>{rework}</b>
        </div>
        <div className="row">
          <span>兜底</span>
          <b className={studio.failUsed ? "dg" : undefined}>{studio.failUsed ? 1 : 0}</b>
        </div>
      </div>
    </aside>
  );
}

/** 编制总数（含门，也含总管那一行）。门上的「N 工位」读它，不写死 ——
 *  判据按它核对"12 个位"这条，而不是在测试里再数一遍数组。 */
export const STUDIO_HEADCOUNT = STUDIO_AGENTS.length;
