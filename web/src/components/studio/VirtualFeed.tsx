"use client";

/** 活动流的窗口化渲染（P2-11）。
 *
 *  为什么手写而不是装一个库：这一屏的输入是**定长的追加流**（`pushLiveEvent` 尾部追加、超 240 条
 *  从头部丢），行高只随文字换行变化，不需要 `react-virtual` 那一整套（动态测量、双向无限滚动、
 *  粘性头）。为 240 行引一个依赖，收益不抵它的升级面。
 *
 *  三个刻意的选择：
 *  ① **少的时候不虚拟化**（≤ `WINDOW_THRESHOLD`）：一屏能装下的列表没必要走窗口逻辑，
 *     而且"渲染全部"这条路径是测试与 SSR 都能跑的那条，留作对照。
 *  ② **滚动容器不是自己**：右栏的滚动盒是 `.side-body`（`overflow-y:auto`），再套一个滚动盒会
 *     出现两条滚动条。这里向上找那个容器、只听它的 `scroll`，自己只做"渲染哪一段"。
 *  ③ **行高先估后测**：第一次渲染用 `ROW_ESTIMATE`，`useLayoutEffect` 里量到真值再回填。
 *     估算值只在第一帧用到，误差靠 `overscan` 吸收；测量只改"我们自己的"数组，不碰 DOM 布局。
 */

import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";

/** 超过这个行数才启用窗口化。低于它时全量渲染 —— 一屏装得下，窗口逻辑只会多一层间接。 */
export const WINDOW_THRESHOLD = 80;

/** 首帧用的行高估值（px）。`--fs-body` 12 + 行高 1.6 + 上下 padding 6 ≈ 34。 */
export const ROW_ESTIMATE = 34;

/** 视口外多渲染几行，滚动时不至于露出空白。 */
export const OVERSCAN = 6;

export interface WindowRange {
  /** 要渲染的第一行（含）。 */
  readonly start: number;
  /** 要渲染的最后一行的**下一行**（不含）。 */
  readonly end: number;
  /** 上方留白的高度：把"没渲染的那些行"占的位置补出来，滚动条长度才不变。 */
  readonly top: number;
  /** 下方留白。 */
  readonly bottom: number;
}

/** 给定每行高度与滚动位置，算出该渲染哪一段、上下各留多少白。
 *
 *  纯函数（不读 DOM、不写参数）：判据在 `web/tests/feed.test.ts` 里逐条钉住 ——
 *  这类"算区间"的逻辑最容易在边界上错一格（最后一行看不见 / 留白算成负数），
 *  而错了以后表现只是"偶尔闪一下"，靠肉眼很难发现。
 */
export function windowOf(
  heights: readonly number[],
  scrollTop: number,
  viewport: number,
  overscan: number = OVERSCAN,
): WindowRange {
  const count = heights.length;
  if (!count) return { start: 0, end: 0, top: 0, bottom: 0 };

  const tops: number[] = new Array(count);
  let total = 0;
  for (let index = 0; index < count; index += 1) {
    tops[index] = total;
    total += heights[index];
  }

  const at = Math.max(0, scrollTop);
  const edge = at + Math.max(0, viewport);

  let first = 0;
  while (first < count && tops[first] + heights[first] <= at) first += 1;
  let last = first;
  while (last < count && tops[last] < edge) last += 1;

  const start = Math.max(0, first - overscan);
  // `last` 可能等于 `first`（视口高度为 0 的那一帧），至少渲染一行，否则屏上会是空气。
  const stop = Math.min(count, Math.max(last, first + 1) + overscan);
  const topPad = tops[Math.min(start, count - 1)] ?? 0;
  const bottomPad = Math.max(0, total - (stop >= count ? total : tops[stop]));
  return { start, end: stop, top: topPad, bottom: bottomPad };
}

export interface VirtualFeedProps<T> {
  readonly items: readonly T[];
  readonly renderItem: (item: T, index: number) => ReactNode;
  /** 行 key：默认用下标（调用方给了更稳的 key 就传）。 */
  readonly keyOf?: (item: T, index: number) => string;
}

export function VirtualFeed<T>({ items, renderItem, keyOf }: VirtualFeedProps<T>) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [viewport, setViewport] = useState(0);
  const [heights, setHeights] = useState<number[]>(() => items.map(() => ROW_ESTIMATE));

  /* 跟随 `.side-body`：它才是滚动盒（见文件头 ②）。找不到就退化成"视口 = 0"，
     那种情况下 `windowOf` 只渲染首帧那一小段 —— 比渲染 240 行好，也比什么都不渲染好。 */
  useEffect(() => {
    const host = hostRef.current;
    const scroller = host?.closest(".side-body") as HTMLElement | null;
    if (!host || !scroller) return;
    const sync = () => {
      setViewport(scroller.clientHeight);
      // 右栏里活动流是 `.side-body` 的第一块内容，`scrollTop` 直接就是它的偏移。
      // （差的是一个 padding，`over`scan 远大于它。）
      setScrollTop(scroller.scrollTop);
    };
    sync();
    scroller.addEventListener("scroll", sync, { passive: true });
    const observer = new ResizeObserver(sync);
    observer.observe(scroller);
    return () => {
      scroller.removeEventListener("scroll", sync);
      observer.disconnect();
    };
  }, []);

  /* 行数变了就把高度表补齐/截断，别让上一批的行高错位到这 240 条上。 */
  useEffect(() => {
    setHeights((previous) => {
      if (previous.length === items.length) return previous;
      const next = items.map((_, index) => previous[index] ?? ROW_ESTIMATE);
      return next;
    });
  }, [items.length]);

  /* 量已渲染的行高。只在真有变化时 setState —— 否则每次 commit 都写一次 state，
     就是自己给自己造一个无限渲染循环。 */
  useLayoutEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const nodes = Array.from(host.querySelectorAll<HTMLElement>("[data-vrow]"));
    if (!nodes.length) return;
    const measured: number[] = [];
    let dirty = false;
    for (const node of nodes) {
      const index = Number(node.dataset.vi);
      if (!Number.isFinite(index)) continue;
      measured[index] = node.offsetHeight;
      if (Math.abs((heights[index] ?? ROW_ESTIMATE) - measured[index]) > 1) dirty = true;
    }
    if (!dirty) return;
    setHeights((previous) => {
      const next = previous.slice();
      for (let index = 0; index < measured.length; index += 1) {
        if (typeof measured[index] === "number") next[index] = measured[index];
      }
      return next;
    });
  });

  const range = windowOf(heights, scrollTop, viewport);

  return (
    <div className="vfeed" ref={hostRef}>
      <div className="vpad" style={{ height: range.top }} aria-hidden="true" />
      {items.slice(range.start, range.end).map((item, offset) => {
        const index = range.start + offset;
        return (
          <div className="vrow" data-vrow="1" data-vi={index} key={keyOf ? keyOf(item, index) : index}>
            {renderItem(item, index)}
          </div>
        );
      })}
      <div className="vpad" style={{ height: range.bottom }} aria-hidden="true" />
    </div>
  );
}
