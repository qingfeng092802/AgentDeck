import { useAnnouncement } from "../../lib/announce";
import type { StudioState } from "../../lib/studioEngine";

/** 全屏唯一的常驻播报口。
 *
 *  为什么挂在壳上而不是节点上：SVG 里的 `aria-live` 在多数读屏器上不可靠，
 *  而"这一单停住了"这类消息本来就不属于某一张卡。一个区域、一个节奏，
 *  避免 13 张卡各播各的。 */
export function Announcer({ studio }: { studio: StudioState }) {
  const message = useAnnouncement(studio);
  return (
    <div className="sr-only" role="status" aria-live="polite" aria-atomic="true">
      {message}
    </div>
  );
}
