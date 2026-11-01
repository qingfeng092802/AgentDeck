import { useStudio } from "../lib/useStudio";

/** 全局唯一的顶栏。
 *
 *  2026-10-27（owner）：右上角只剩**浅色/深色切换**与**设置**。
 *  原型那四颗（倍速 / 暂停 / 注入故障 / 重置）全部退役 —— 它们是"给看演示的人"的控制键，
 *  不是产品能力。重置留在受理卡里（"重新立项"），因为它属于那一张卡的动作。
 *
 *  顶栏读的是引擎状态（`useStudio`），不再吃 `snapshot`：这一版是纯前端推演，
 *  顶栏的四个读数如果还从后端来，就会出现"会议已经开到第 5 轮，顶栏还写着待立项"。
 */

const PHASE: Readonly<Record<number, { label: string; key: number }>> = {
  0: { label: "待立项", key: 0 },
  1: { label: "立项会议", key: 1 },
  2: { label: "执行中", key: 2 },
  3: { label: "门禁中", key: 3 },
  4: { label: "已交付", key: 4 },
};

export interface TopBarProps {
  readonly theme: "dark" | "light";
  readonly onThemeToggle: () => void;
  readonly onOpenSettings: () => void;
}

export function TopBar({ theme, onThemeToggle, onOpenSettings }: TopBarProps) {
  const studio = useStudio();
  const phase = PHASE[studio.phase] ?? PHASE[0];
  const plan = studio.plan;
  const onBoard = studio.screen === "board";

  return (
    <header className="top">
      <div className="brand">
        <span className="mark">◆</span>
        <span>AgentDeck</span>
        <span className="chip">指挥台</span>
      </div>

      {/* 面包屑只写"你在哪"，不写"现在什么状态"。
          原来未立项时叶子是「待立项」，可右上那颗阶段胶囊写的就是「待立项」——
          同一行里同一个词出现两次。状态归胶囊（它是状态机的一部分，会随流程变），
          位置归面包屑（它只随 `screen` 变）。没有单号时面包屑就只剩屏名，连斜杠都不渲染 ——
          「项目工作室 /」末尾挂一个没下文的斜杠，读起来像加载失败。 */}
      <div className="crumb">
        <b>{onBoard ? "执行看板" : "项目工作室"}</b>
        {plan ? (
          <>
            <span className="sep">/</span>
            <span className="prj">{plan.prj}</span>
            {onBoard ? (
              <>
                <span className="sep">·</span>
                <span>{plan.title}</span>
              </>
            ) : null}
          </>
        ) : null}
      </div>

      {/* 提示条挂在**顶栏里**，不是浮在画布上。
          原来那条 toast 是底部居中的浮层，正好压在执行看板底部的「现在 / 42%」进度条上 ——
          派单、打回、兜底这些提示恰恰发生在你正盯着那条进度的时候。
          顶栏中段（面包屑与右侧两颗键之间）本来就是空的：放这里谁都不挡，
          而且位置固定，不用为了"躲开内容"去猜用户在看哪一块。 */}
      {studio.toast ? (
        <div
          className="top-notice"
          data-k={studio.toast.kind}
          role="status"
          title={`${studio.toast.title} · ${studio.toast.body}`}
        >
          <i className="dot" />
          <b>{studio.toast.title}</b>
          <span>{studio.toast.body}</span>
        </div>
      ) : null}

      <div className="top-right">
        <div className="phase-pill" data-p={phase.key}>
          <span className="dot" />
          <b>{phase.label}</b>
        </div>
        <button
          type="button"
          className="ibtn"
          aria-pressed={theme === "light"}
          title={theme === "light" ? "当前浅色，点击切到深色" : "当前深色，点击切到浅色"}
          onClick={onThemeToggle}
        >
          {theme === "light" ? "深色" : "浅色"}
        </button>
        <button type="button" className="ibtn" aria-haspopup="dialog" title="模型与凭据设置" onClick={onOpenSettings}>
          设置
        </button>
      </div>
    </header>
  );
}
