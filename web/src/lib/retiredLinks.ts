import type { ToastTone } from "./toastStore";

/** 退役直链的解释：一句话，一次。
 *
 *  `?live` 整页与 `?studio=demo&ex=1&dispatch=1` 演示直链都在 2026-10-29 停了，但旧书签还在人手里。
 *  原来这件事由两个各读一遍 `location.search` 的 effect 分别发一条提示 —— 结果 owner 2026-10-30 报
 *  「每次刷新都会出现影响观感」：**书签里的参数不会自己变，所以每次刷新都重新解释一遍**，
 *  一句话通知就变成了常驻横幅；两个参数都在时还会上下叠两张长得一样的卡片。
 *
 *  这一份把三件事收在一起：只发**一条**提示、发完把已经没有读者的参数从地址栏里摘掉
 *  （下一次刷新就没有可解释的东西了），并且做成纯函数 —— 只进出字符串，不碰 `window`，
 *  所以它能直接在判据里被跑，不需要 DOM。
 */

/** 地址栏里已经没有任何读者的参数。 */
const RETIRED_PARAMS: readonly string[] = ["live", "studio", "ex", "dispatch", "speed"];

export interface RetiredNotice {
  readonly tone: ToastTone;
  readonly title: string;
  readonly detail: string;
}

const LIVE_NOTICE: RetiredNotice = {
  tone: "info",
  title: "旧入口已合并",
  detail: "?live 实时整页已删除；真链路现在从受理台填项目键进入。",
};

const DEMO_NOTICE: RetiredNotice = {
  tone: "info",
  title: "演示直链已停用",
  detail: "不再自动播放固定脚本；填好需求直接立项，会走后端真模型会议。",
};

/** 两类旧链接同时出现在一条 URL 上时，说一句话而不是上下叠两张卡。 */
const BOTH_NOTICE: RetiredNotice = {
  tone: "info",
  title: "旧链接已停用",
  detail: "?live 整页已并入项目工作室，演示不再自动播放；填好需求直接立项即可。",
};

/**
 * 读一遍 `search`，需要解释就调一次 `say`。
 *
 * 返回值 = 该换成什么地址栏：`null` 表示这条 URL 里没有退役参数（什么都不做，也不改写历史），
 *  字符串表示**把退役参数摘掉之后的 search**（可能是空串 = 地址栏只剩路径）。
 */
export function explainRetiredLinks(search: string, say: (notice: RetiredNotice) => void): string | null {
  const query = new URLSearchParams(search);
  const retired = RETIRED_PARAMS.filter((name) => query.has(name));
  if (!retired.length) return null;
  const onLive = query.has("live");
  const onDemo = query.has("studio") || query.has("dispatch") || query.has("ex");
  say(onLive ? (onDemo ? BOTH_NOTICE : LIVE_NOTICE) : DEMO_NOTICE);
  const kept = [...query.entries()].filter(([name]) => !RETIRED_PARAMS.includes(name));
  return kept.length ? `?${new URLSearchParams(kept).toString()}` : "";
}
