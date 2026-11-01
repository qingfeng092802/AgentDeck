import { goHandleTask } from "../../lib/studioEngine";
import type { DeliveryBlocker } from "../../lib/live";

/** 交付清单空着的时候，**得说出为什么**（owner 2026-10-29）。
 *
 *  原来那一格只有两个读数：「整单 0 项」和「还没有可交付的文件」。两句都是真的，
 *  却都不构成下一步动作 —— 人读完只知道"没东西"，不知道该去动哪张卡。
 *  现场（`real_demo · 57bf3d03`）是 T04 判了 `ARTIFACT_INVALID`：库里 27 份产物，
 *  卡却停在门禁前，而屏上一个字都没提它。
 *
 *  这一块只做两件事：把**哪张卡、卡在哪一步**写成人话，并给一个出口。
 *  事实由 `deliveryBlockers` 算好（那一层不生产文案），这里只负责句子与按钮。
 *
 *  为什么每种情形都要分支出不同的句子：`files === 0`（一份都没交）与
 *  `files > 0`（交了但没过门槛）要人做的事完全不同 —— 前者去催执行器重跑，
 *  后者去看门槛或换人。把它们压成一句"产物没过校验"，等于把判断又推回给人。
 */

/** 一行的一句话解释。**不出现状态码**：`ARTIFACT_INVALID` 这类名字对屏幕前的人没有意义，
 *  它只对日志有意义（要查日志的人本来就查得到）。
 *
 *  `files === 0` 这一支的措辞是被真机纠正过的（`real_demo · 57bf3d03` 的 T04）：
 *  它名下在库里其实有 27 行产物，而文件总线是"一个文件一行、后写的覆盖先写的"——
 *  那 27 行写的都是别的卡也写过的路径，去重之后一份都不算它的。所以"一份文件都没交上来"
 *  是句假话。现在这句话只描述**总线上的事实**，并把两种成因一起说出来。 */
export function deliveryReason(item: DeliveryBlocker): string {
  if (item.state === "REJECTED") {
    const first = item.failed[0];
    if (!first) return "审查退回：判据没全过，先看审查结论再决定是改还是换人。";
    const rest = item.failed.length > 1 ? `（共 ${item.failed.length} 条没过）` : "";
    return `审查退回：判据「${first}」没过${rest}。`;
  }
  /* 读不到文件总线时只说读得到的那一半：卡确实停在产物上，交了几份不知道。
     把"不知道"说成"没交"，就是 2026-10-29 那次"两页总是空的"同一种假话。 */
  if (item.files === null) return "产物没过门禁：文件总线这次没读到，只知道它停在产物上。";
  if (item.rejected.length) {
    const names = item.rejected.slice(0, 2).join("、");
    const more = item.rejected.length > 2 ? " 等" : "";
    return `产物没过门禁：交上来的 ${item.files} 份里有 ${item.rejected.length} 份没过校验（${names}${more}）。`;
  }
  if (item.files === 0) {
    return "产物没过门禁：文件总线上没有一份算在它名下 —— 要么没交，要么交上去的已被后来的卡覆盖。";
  }
  return `产物没过门禁：名下有 ${item.files} 份文件仍判不达标，多半卡在这张卡的字节数或行数门槛上。`;
}

export function DeliveryBlocked({ blockers }: { blockers: readonly DeliveryBlocker[] }) {
  return (
    <div className="dt-blocked">
      <p className="lead">{`交付清单为空 —— 这一单还有 ${blockers.length} 张卡停在门禁前，交付包出不来：`}</p>
      <ul className="list">
        {blockers.map((item) => (
          <li key={item.task.id}>
            <div className="r">
              <b>{`${item.task.id} · ${item.task.title}`}</b>
              <button
                type="button"
                className="ghost go"
                data-act="go-handle"
                data-task={item.task.id}
                onClick={() => goHandleTask(item.task.id)}
              >
                前往处理
              </button>
            </div>
            <p className="why">{deliveryReason(item)}</p>
          </li>
        ))}
      </ul>
    </div>
  );
}
