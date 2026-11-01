import { useEffect, useRef } from "react";
import { BoardScreen } from "../components/studio/BoardScreen";
import { StudioRail } from "../components/studio/StudioRail";
import { StudioScreen } from "../components/studio/StudioScreen";
import {
  closeDrawer,
  setLive,
  tick,
  type StudioState,
} from "../lib/studioEngine";
import { useStudio } from "../lib/useStudio";
import { setSource } from "../lib/studioEngine";
import { Announcer } from "../components/studio/Announcer";
import { readProjectKey } from "../lib/acp";
import { readSession, resumeSession } from "../lib/live";
import { explainRetiredLinks } from "../lib/retiredLinks";
import { pushToast } from "../lib/toastStore";

/** 项目工作室视图：左栏（一扇门 + 12 张椅子，第一张是工作室总管）+ 主区（工作室屏 / 执行看板屏）。
 *
 *  为什么两段在同一个视图里：工作室与执行看板是**同一条单的一生**
 *  （受理 → 会议 → 产出 → 派单 → 执行），切成两个一级 tab 会把"刚才那场会"丢掉。
 *
 *  皮肤走 `.studio-root` 那套：`web/src/styles/studio.css` 是从原型样式表机械搬运来的，
 *  只做了作用域（挂 `.studio-root` 下）与变量映射（`--canvas` 等从产品 `--color-*` 派生）。
 *  所以这一屏与原型是同一份 CSS，不是"照着截图重画一遍"。
 */

/** 引擎的推进帧。60ms ≈ 16fps，够让进度条走顺；`tick` 内部只在"会议进行中 / 派单后"才发通知，
 *  空闲时一帧都不重渲染 —— 否则一张静止的看板会以 16fps 白白重算整棵 SVG。 */
const TICK_MS = 60;

function Overlays({ studio }: { studio: StudioState }) {
  const drawerRef = useRef<HTMLElement>(null);
  const restoreRef = useRef<HTMLElement | null>(null);

  /* 抽屉的键盘合同：Esc 关、开时焦点进来、关时焦点还回去。
   *
   * 2026-10-27 之前这三条一条都没有：设置弹窗处理了 Esc（`SettingsModal`），抽屉没有 ——
   * 键盘用户打开抽屉后只能 Tab 一圈，找到那个 18×19 的 `×` 才出得去；遮罩是个带 onClick 的 `div`，
   * 没有任何键盘等价物。同一个应用里两个浮层两套行为，本身就是缺陷。
   *
   * 依赖是 `studio.drawer` 而不是整个 `studio`：抽屉对象只在开关时换引用，
   * 而引擎每帧 emit 都会换掉外层 state —— 依赖外层会让这个 effect 每秒重挂几次，
   * 于是"记录焦点 → 抢焦点"跟着抖。 */
  useEffect(() => {
    if (!studio.drawer) {
      restoreRef.current?.focus();
      return;
    }
    restoreRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    drawerRef.current?.focus();
    const onKey = (event: KeyboardEvent): void => {
      if (event.key === "Escape") closeDrawer();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [studio.drawer]);

  return (
    <>
      <div className={`mask${studio.drawer ? " on" : ""}`} onClick={() => closeDrawer()} />
      <aside
        className={`drawer${studio.drawer ? " on" : ""}`}
        ref={drawerRef}
        tabIndex={-1}
        role="dialog"
        aria-modal={studio.drawer ? true : false}
        aria-hidden={studio.drawer ? false : true}
        aria-label={studio.drawer ? studio.drawer.title : ""}
      >
        <div className="drawer-h">
          <b>{studio.drawer ? studio.drawer.title : ""}</b>
          <button type="button" className="x" aria-label="关闭" onClick={() => closeDrawer()}>
            ×
          </button>
        </div>
        <div className="drawer-b" dangerouslySetInnerHTML={{ __html: studio.drawer ? studio.drawer.html : "" }} />
      </aside>
      {/* 提示条不在这里：它挂在顶栏中段（`shell/TopBar.tsx` 的 `.top-notice`）。
          浮层会压住执行看板底部的进度读数，而那些提示正是派单/打回/兜底时发的 —— 挡的恰好是要看的东西。 */}
    </>
  );
}

export function StudioView() {
  const studio = useStudio();

  /* 唯一入口 = 项目工作室（推演）。旧的 `?live` 实时看板已按 owner 指令删除；
     真链路仍在**项目键**那条岔口里（填了项目键，`StudioScreen.begin` 走 `startLive`），
     不再有第二个整页入口。 */
  useEffect(() => {
    setSource("demo");
  }, []);

  /* 旧直链（`?live` 实时整页 / `?studio=demo&ex=N&dispatch=1` 自动演示）都停了，书签还在人手里。
   *
   *  这里只做两件事：说**一句**话，然后把那几个参数从地址栏摘掉。
   *  原来每条退役链接各挂一个 effect、各发一条提示，而判据是 URL 上那几个**永远不会变**的参数
   *  ⇒ 每次刷新都重新解释一遍（两类参数都在时还上下叠两张卡）。owner 2026-10-30「每次刷新都会出现影响观感」。
   *  摘掉参数之后，"这句话"在一台浏览器上只会遇到一次；要看固定脚本，受理台右上那颗「固定演示」自己点。 */
  useEffect(() => {
    const next = explainRetiredLinks(window.location.search, pushToast);
    if (next === null) return;
    window.history.replaceState(window.history.state, "", `${window.location.pathname}${next}${window.location.hash}`);
  }, []);

  /* 把上次用过的项目键填回受理台。
   *
   *  这一步只填**输入框**，不动数据源 —— 打开页面仍然是"未立项的空受理台"，
   *  一个字都不替 owner 填。填进去是因为项目键是这个屏唯一的岔口：
   *  不填回来，每次想真跑一遍都得重新敲一遍目录名。
   *
   *  只读不写：`readProjectKey` 失败（隐私模式）返回空串，那时受理台就是纯推演。 */
  useEffect(() => {
    const stored = readProjectKey();
    if (stored) setLive({ projectKey: stored });
  }, []);

  /* 接回上一单。
   *
   *  一单要跑 3–7 分钟，中途 F5 之后如果回到空受理台，那单就"孤儿"了：它还在库里、
   *  审批还挂着，而屏上连左下的待办条都不渲染 —— 也就是说那张待批的单**从界面上再也够不着**。
   */
  useEffect(() => {
    const session = readSession();
    if (!session) return;
    void resumeSession(session);
  }, []);

  useEffect(() => {
    let last = Date.now();
    const timer = window.setInterval(() => {
      const now = Date.now();
      const dt = Math.min(160, now - last);
      last = now;
      tick(dt);
    }, TICK_MS);
    return () => window.clearInterval(timer);
  }, []);

  return (
    <>
      <Announcer studio={studio} />
      {/* 窄屏提示。显隐**只由 CSS 决定**（`@media (max-width:999px)`），这里不读
          `window.innerWidth`：首帧拿不到真实宽度，用 JS 判会在加载时闪一下。
          文案里的 1000 与 `--app-min-width` 同值，判据逐字比对 —— 改令牌忘了改文案会红。 */}
      <div className="narrow-bar" role="status">
        <b>窗口太窄</b>
        <span>这块屏需要至少 1000px 宽 —— 左右滑动可以看全。</span>
      </div>
      <div className="shell" id="view-studio">
        <StudioRail studio={studio} />
        <main className="main">
          {/* `studio.plan` 这个条件不是防御性编程，是**结构约束**：
              看板屏的每一块（阶段条、DAG、now 条、右栏六个页签）都建立在"这一单已经立项"之上，
              未立项时它没有任何东西可画。原来靠"进看板的按钮只在有 plan 时渲染"来保证，
              那是**可达性**层面的保证，一旦别处多一条 `setScreen("board")` 就破。
              写进条件里之后，"未立项到不了看板"在类型/结构层面成立，`BoardScreen` 也就不必再留空态分支。 */}
          {studio.screen === "board" && studio.plan ? <BoardScreen studio={studio} /> : <StudioScreen studio={studio} />}
        </main>
      </div>
      {/* 待办条**已整块删除**（owner 2026-10-28：「去除所有待办清单」）。
          它原来是右下那块浮层，装四类人工动作：批单 / 评审 / 卡住了 / 继续执行。
          三类的去处在后端而不在界面 —— `ACP_REVIEW_MODE=auto` 让门禁代签评审、
          `ACP_APPROVAL_MODE=auto` 让拆单确认由规则免确认；剩下的"卡住了"归 CLI。
          删掉之后这一屏只表达"这一单到哪一步了"，不再有第二套动作入口。
          详见 `src/acp/approval/policy.py` 与 `src/acp/serve_intake.py::_auto_pass_split`。 */}
      <Overlays studio={studio} />
    </>
  );
}
