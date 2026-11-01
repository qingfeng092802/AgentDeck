"""T086 前置（owner 2026-10-14 裁待裁 6）：`ESCALATED` 之后的人工出口。

**这条边今天缺的是"人怎么接手"，不是"卡怎么进去"**：Phase 6 把**进入** `ESCALATED` 的迁移做完了
（`handoff.py:_escalate`，两条上限任一超限即升级），而 `data-model.md:303` 给它写的下一跳是
`ESCALATED ──人工──▶ READY / REJECTED` —— 那条"人工"边至今没有一个落点，卡一旦升上来就**永久停住**，
这是全流程唯一的死路（owner 因此把它排进 Phase 7 第一条）。

**为什么出口只有这两个态**：取值不写在代码里当常量自证，而是从状态机原文取 ——
`EXIT_TARGETS` 逐字是 `data-model.md:303` 那一行箭头右侧的两项，用例把它与文档原文对齐
（文档加第三个出口而这里没跟 ⇒ 用例红；这里私自加 ⇒ 也红）。为什么不是 `NEED_INFO` / `REWORK`：
那两个各有自己的触发方（澄清、审查意见），从升级态过去等于绕过它们各自的判定。

**端点是独立的一条路由**（owner 2026-10-14 待裁 ⑫ 的裁定，原话"escalation.py 已做，与 PATCH 的语义不同"）：
公开面在 `api/tasks.py::resolve_escalation`（`POST /tasks/{task_id}/escalation`，契约
`contracts/agent-protocol.md §1.4`）。为什么单独一条而不往 PATCH 加字段，那三条不同写在契约那一节；
本模块只交**决定本体**（取合法出口、改状态、留痕），状态码映射在端点那一层 ⇒ 出口判定仍只有一处，
不会出现"端点判一遍、节点再判一遍"的两份口径（原则 I）。
CLI 那条通道（`python -m acp.approve`）走的是卡点决议，不是这条边 —— 两件事别混：卡点 A / D 是"批不批这张单"，
这里是"停在升级态的卡交回哪个态"。

**不动执行者**：`_escalate` 那一段（`handoff.py:122-127`）刻意保留原执行者，理由是"别替使用者做他没做的决定"；
本模块同一条理由**同样不改执行者** ⇒ 人选 `READY` 之后仍然可以由 `PATCH /tasks/{id}/worker`（T071）改派，
那是既有的、比这里更严格的一条路。`handoff_count` 也不动：升级不是换人，计一次就把 FR-027 的上限用光了。
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from acp.persistence import events
from acp.persistence.models_core import TaskCard
from acp.persistence.states import data_model_default
from acp.persistence.enums import values_of

NODE = "escalation"
ESCALATED = "ESCALATED"

#: 人工出口的合法目标态。运行时以 `data-model.md` 那一行为准（见 `exit_targets_in_doc()`），
#: 这份字面量只是"文档读不到时不静默放行"的兜底形状，判据不许拿它比自己。
EXIT_TARGETS = ("READY", "REJECTED")

#: `ESCALATED` 这一行的锚点：迁移图里带"人工"二字的那一条边。用**包含**匹配而不是整行相等，
#: 因为态名与箭头两侧的空白在文档里会随排版变。
EXIT_LINE_MARK = "人工"


class EscalationMisuse(ValueError):
    """人工出口的用法错。`code` 是给端点与日志用的稳定键名，消息里只带状态名与人，不带产物内容。"""

    code = "escalation_misuse"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        if code is not None:
            self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class EscalationOutcome:
    """一次人工处置的结果。`prior_status` 恒为 `ESCALATED`（不是 ESCALATED 就抛错，不会走到这里）。"""

    card_id: str
    decision: str
    status_after: str
    executor_agent_id: str | None
    principal: str
    reason: str


def exit_line_in_doc(path: Path | None = None) -> str:
    """取状态机里那条"人工"边的原文整行（判据用它比对，不在用例里再抄一份）。"""
    text = (path or data_model_default()).read_text(encoding="utf-8")
    hits = [line for line in text.split("\n") if ESCALATED in line and EXIT_LINE_MARK in line]
    if len(hits) != 1:
        raise EscalationMisuse(
            f"状态机原文里带「{EXIT_LINE_MARK}」的 {ESCALATED} 边读到 {len(hits)} 条（应为 1）"
            "⇒ 文档形态变了，先改这里再改判据",
            code="doc_anchor_ambiguous",
        )
    return hits[0]


def exit_targets_in_doc(path: Path | None = None) -> tuple[str, ...]:
    """从那条边的**箭头右侧**解析出目标态名列表（按出现顺序）。

    解析而不是"拿 `EXIT_TARGETS` 比 `EXIT_TARGETS`"：后者是恒等式，文档删掉一个出口它也不会红。
    """
    line = exit_line_in_doc(path)
    # **从最后一个箭头切**：那一行原文是 `…└─超限─▶ ESCALATED ──人工──▶ READY/REJECTED`，
    # 一条线上有两个 `▶`（前者是"卡怎么进来"，后者才是"人怎么出去"）。用 `partition` 会从第一个切，
    # 把 `ESCALATED` 自己也当成出口读出来 —— 现跑第一次就是这么红的，改在这里而不是改文档。
    _left, _mark, right = line.rpartition("▶")
    targets = tuple(
        token for token in re.split(r"[^A-Z_]+", right.upper()) if token in set(values_of("task_card_state"))
    )
    if not targets:
        raise EscalationMisuse(
            f"从原文行里解析不出任何目标态：{line!r} ⇒ 锚点匹配到了别的行",
            code="doc_anchor_unparsed",
        )
    return targets


def resolve_escalation(
    session: Session,
    *,
    feature_request_id: uuid.UUID,
    task_id: str,
    decision: str,
    principal: str,
    created_at: dt.datetime,
    reason: str = "",
) -> EscalationOutcome:
    """把一张停在 `ESCALATED` 的卡交回人工决定的两个出口之一，并留一条可复核的痕。

    `principal` 是**做这个决定的人**（不是自动主体）：这条边的全部意义就是"这里有人做过判断"，
    留痕的主语写成 `orchestrator` 就等于把人工决定伪装成系统行为 —— 与 `handoff` / `dispatch` 那两处
    用 `AUTOMATED_ACTOR` 的口径正好相反，因为那两处的主语确实是机器。
    """
    if not principal or not principal.strip():
        raise EscalationMisuse(
            "缺 `principal` ⇒ 这条人工出口留下的痕没人认领",
            code="principal_required",
        )
    if decision not in exit_targets_in_doc():
        raise EscalationMisuse(
            f"人工出口的决策 {decision!r} 不在状态机允许的出口里（原文那一行的右侧只有这些）",
            code="exit_not_allowed",
        )
    card = (
        session.query(TaskCard)
        .filter(TaskCard.feature_request_id == feature_request_id, TaskCard.id == task_id)
        .one_or_none()
    )
    if card is None:
        raise EscalationMisuse(
            f"任务卡 {task_id} 在需求单 {feature_request_id} 下不存在 ⇒ 没有可处置的卡",
            code="card_not_found",
        )
    if str(card.status) != ESCALATED:
        raise EscalationMisuse(
            f"卡 {task_id} 现在是 {card.status}，不在 {ESCALATED} ⇒ 人工出口只服务停在升级态的卡，"
            "别的迁移各回各的触发点（状态机每条边只有一个来源态，从这里绕过去就没人判过合法性）",
            code="state_not_escalated",
        )

    before = str(card.status)
    card.status = decision
    events.record(
        session,
        entity="task_card",
        entity_id=card.id,
        kind="TASK",
        actor_agent_id=principal.strip(),
        payload={
            "node": NODE,
            "action": "human_exit",
            "decision": decision,
            "reason": reason,
            "prior_status": before,
            "handoff_count": card.handoff_count,
            "executor_agent_id": card.executor_agent_id,
            "card_status_after": card.status,
        },
        created_at=created_at,
    )
    session.flush()
    return EscalationOutcome(
        card_id=card.id,
        decision=decision,
        status_after=str(card.status),
        executor_agent_id=card.executor_agent_id,
        principal=principal.strip(),
        reason=reason,
    )
