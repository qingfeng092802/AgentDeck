import { useSyncExternalStore } from "react";
import { getStudio, subscribeStudio, studioVersion, type StudioState } from "./studioEngine";

/** 引擎 → React 的唯一桥。
 *
 *  快照取的是**版本号**而不是状态对象本身：状态对象是就地改的（推进逻辑照原型一行没动），
 *  把同一个引用交给 `useSyncExternalStore` 会让它判定"没变"而跳过重渲染。
 *  版本号每次 `emit()` 自增，React 才能看见变化。 */
export function useStudio(): StudioState {
  useSyncExternalStore(subscribeStudio, studioVersion, studioVersion);
  return getStudio();
}
