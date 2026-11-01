"""T081 / T082：`HandOff` 节点 —— 失败自动换可替代工具（FR-026｜R-11）+ 换人上限（FR-027）。

**上限为什么有两道，而且两道都要在应用层查**：
`data-model.md` 的 EN-02 上有一条库面 CHECK（`handoff_count <= 1`，`models_core.py:79`），
所以"写第二个人"这件事**库里也拦**。但库面拦的表现是一个 `IntegrityError` 而不是"升级人工"，
而 FR-027 要的正是后者 —— 超限是一条**业务出口**（`ESCALATED`），不是一次崩溃。
所以这里先按 `handoff_count` 与"涉及工具数"两条各自判一次，判不过就走升级；
库那道 CHECK 留作"应用层被人改坏了"时的最后一道，本文件的用例两面都测。

**"涉及工具总数 ≤ 2"与"换人次数 ≤ 1"不是同一条**（虽然看着等价）：
次数是**动作数**，工具数是**去重后的身份数**。同一支工具被换下来又换回去，次数会到 2 而工具数仍是 1；
反过来一支工具连着接两张卡不算换人。所以两条边界各自成一条判据，
工具数从 EN-14 的 `SELECTION` 历史行里读（`payload["selected"]`），不在库里再造一份历史列。

**"三工具循环"为什么在结构上就不可表示**：候选集合里既排除失败者、又排除已经在场的工具，
于是第三次换人要么找不到候选（升级），要么被次数上限挡住（升级）—— 两条都在**写之前**。
`test_a_third_handoff_is_refused_before_any_candidate_is_ranked` 就是查这个先后：
超限那次连排序都不该发生（排序发生过的表现是"给人看了一个不会被采用的理由"）。

一处文档没写完、这里不替它补的形态（登记在 `evidence/phase-06.md` 的未闭清单）：
T081 要求"换人原因入 `status.md` 与审计"。审计这一半做了（EN-14 一条 `TASK` 行，理由 + from/to + 次数）；
`status.md` 那一半**本轮不接**，两条理由都是可查的：
① `status.md` 的行格式是固定七列（`bus/orchestrator_writer.py::STATUS_HEADER`：卡号 / 状态 / 执行者 /
   审查者 / 验收者 / 产物落点 / 累计用量），**没有"原因"那一列** ⇒ 加一列属 `contracts/file-bus.md §4` 的格式修订；
② 复算 `grep -rn "upsert_status\\|append_event" src/` 的**原始命中 = 11 行**（2026-10-14 实跑），
   逐项排除后才是 0：3 行 `.pyc` 二进制命中 + 6 行在定义模块 `bus/orchestrator_writer.py` 里
   （2 处公开定义、2 处私有 job 定义、2 处类内自调用）+ 1 行 `config/loader.py:20` 的散文引用
   + 1 行就是本句自己 ⇒ **模块外调用点 = 0**（口径按 owner 2026-10-14 裁决 P2-2 更正；原文只写"非定义命中数 = 0"，
   那个"非定义"没排除注释与自调用，别人复算会得到 11，是一条不可复算的判据句）。
   结论不变：公共投影今天在整个生产路径上还没有任何写点。只在 `HandOff` 里接一次，效果是"这张表里只有换过的卡"，
   那比完全不写更误导。⇒ 接线点归状态迁移的公共装配件（Phase 7 的 T086 之后，四类迁移共用一个写点）。
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from acp.guard import triple as gp
from acp.orchestrate.nodes import select_worker as sw
from acp.persistence import events
from acp.persistence.models_core import TaskCard
from acp.persistence.models_registry import EventLog

NODE = "handoff"

#: 触发换人的卡态。`data-model.md` 状态机里只有 `ARTIFACT_INVALID ──▶ HANDED_OFF` 这一支是换人；
#: `CHANGES_REQUESTED ──▶ REWORK ──▶ RUNNING` 是**同一执行者返工**，不是换人 —— 混进来就会把
#: "审查提了修改意见"也计成一次换人，FR-027 的上限会被返工吃光。
TRIGGER_STATES = frozenset({"ARTIFACT_INVALID"})
#: 换人成功后的卡态（状态机原文：`HANDED_OFF ──▶ RUNNING（换人≤1）`）。
LANDED = "HANDED_OFF"
#: 超限的卡态（`HANDED_OFF ──超限──▶ ESCALATED`）。
ESCALATED = "ESCALATED"

#: FR-027 的两条上限。**数值取自库面同一条 CHECK 的口径**，不是这里另定一份：
#: `models_core.py:79` 的 `handoff_count <= 1`；本常量写成"允许的最大次数"，两处同值由用例钉。
MAX_HANDOFF_COUNT = 1
MAX_AGENTS_INVOLVED = 2


class HandoffMisuse(ValueError):
    """换人这件事的形状不成立（卡不存在 / 不是可换人的态 / 没有失败者可换）。"""

    def __init__(self, message: str, *, code: str = "handoff_invalid") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class HandoffOutcome:
    """一次换人的结论。`escalated=True` 时 `to_agent` 必为 None —— 形状上不允许"升级了但换了人"。"""

    card_id: str
    from_agent: str
    to_agent: str | None
    handoff_count_after: int
    agents_involved: tuple[str, ...]
    escalated: bool
    status_after: str
    reason: str

    @property
    def action(self) -> str:
        return "escalated" if self.escalated else "handed_off"


def agent_history(session: Session, *, feature_request_id: uuid.UUID, task_id: str) -> tuple[str, ...]:
    """这张卡涉及过的工具，按时间顺序 —— 唯一载体是 EN-14 的 `SELECTION` 行，库里没有第二份历史列。"""
    rows = session.scalars(
        select(EventLog)
        .where(
            EventLog.entity == "task_card",
            EventLog.entity_id == task_id,
            EventLog.kind == "SELECTION",
        )
        .order_by(EventLog.created_at, EventLog.id)
    ).all()
    seen: list[str] = []
    for row in rows:
        chosen = (row.payload or {}).get("selected")
        if chosen and chosen not in seen:
            seen.append(str(chosen))
    return tuple(seen)


def _card_of(session: Session, feature_request_id: uuid.UUID, task_id: str) -> TaskCard:
    card = (
        session.query(TaskCard)
        .filter(TaskCard.feature_request_id == feature_request_id, TaskCard.id == task_id)
        .one_or_none()
    )
    if card is None:
        raise HandoffMisuse(f"任务卡 {task_id} 在需求单 {feature_request_id} 下不存在 ⇒ 无卡可换人")
    return card


def _escalate(session: Session, card: TaskCard, *, reason: str, created_at: dt.datetime, agents: Sequence[str]) -> HandoffOutcome:
    """升级人工：只改状态 + 留痕，**不动执行者、不动次数**。

    保留原执行者是刻意的：`ESCALATED` 之后的人工出口是"READY / REJECTED"（状态机原文），
    在这里先把人换掉等于替使用者做了一个他没做的决定。
    """
    card.status = ESCALATED
    events.record(
        session,
        entity="task_card",
        entity_id=card.id,
        kind="TASK",
        actor_agent_id=sw.AUTOMATED_ACTOR,
        payload={
            "node": NODE,
            "action": "escalated",
            "reason": reason,
            "handoff_count": card.handoff_count,
            "agents_involved": sorted(set(agents)),
            "card_status_after": card.status,
        },
        created_at=created_at,
    )
    session.flush()
    return HandoffOutcome(
        card_id=card.id,
        from_agent=str(card.executor_agent_id),
        to_agent=None,
        handoff_count_after=card.handoff_count,
        agents_involved=tuple(agents),
        escalated=True,
        status_after=card.status,
        reason=reason,
    )


def handoff(
    session: Session,
    *,
    feature_request_id: uuid.UUID,
    task_id: str,
    reason: str,
    candidates: Sequence[sw.WorkerCandidate],
    created_at: dt.datetime,
    seat_blocked: Sequence[tuple[str, Sequence[str]]] = (),
) -> HandoffOutcome:
    """失败换人：判上限 → 排除在场者重排 → 交回 `SelectWorker` 的唯一写者落库 → 状态机走一格。

    `reason` 是换人原因（FR-026 要求留痕），不是判定输入 —— 判定只看卡上的次数与历史工具数。
    """
    if not reason or not reason.strip():
        raise HandoffMisuse("换人原因为空 ⇒ 留痕里会写出一句没人能复核的'不知道为什么换了'", code="reason_required")
    card = _card_of(session, feature_request_id, task_id)
    if card.status not in TRIGGER_STATES:
        raise HandoffMisuse(
            f"卡 {task_id} 现在是 {card.status}，不在换人触发态 {sorted(TRIGGER_STATES)} 里 ⇒ "
            "返工（`REWORK`）与换人是两条路，别把审查意见计成换人次数",
            code="state_not_handed_off",
        )
    failed = card.executor_agent_id
    if failed is None:
        raise HandoffMisuse(
            f"卡 {task_id} 没有执行者可换（`executor_agent_id` 为空）⇒ 那是首次派发失败，不是换人",
            code="no_executor_to_replace",
        )
    history = agent_history(session, feature_request_id=feature_request_id, task_id=task_id)
    agents = tuple(history) if failed in history else (*history, str(failed))

    # —— 上限一：换人次数。查在**排序之前**，超限那次连"为什么选它"都不该生成。
    if card.handoff_count >= MAX_HANDOFF_COUNT:
        return _escalate(
            session,
            card,
            reason=(
                f"已换人 {card.handoff_count} 次，达到上限 {MAX_HANDOFF_COUNT}（FR-027）；"
                f"本次原因：{reason}"
            ),
            created_at=created_at,
            agents=agents,
        )
    # —— 上限二：涉及工具数。与上限一独立（同一支工具换回来次数会涨而工具数不涨）。
    if len({*agents}) >= MAX_AGENTS_INVOLVED:
        return _escalate(
            session,
            card,
            reason=(
                f"这张卡已经涉及 {len(set(agents))} 支工具（{sorted(set(agents))}），"
                f"到上限 {MAX_AGENTS_INVOLVED}（FR-027）；本次原因：{reason}"
            ),
            created_at=created_at,
            agents=agents,
        )

    excluded_ids = {str(failed), *(str(agent) for agent in agents)}
    exclusions: list[tuple[str, str]] = [
        (str(failed), f"本次失败者（原因：{reason}）"),
        *(  # 已经在场的工具也不再选：那会把"两工具循环"写成三次换人。
            (str(agent), "这张卡已经由它跑过（FR-027 的涉及工具上限）")
            for agent in agents
            if str(agent) != str(failed)
        ),
    ]
    # `rank_candidates(extra_excluded=…)` 只负责**把出局者记进留痕**，不负责从候选集里删人
    # （它的 docstring 写的是"候选集构造阶段就被拿掉的工具……必须跟着进 excluded"）。
    # 所以删人这件事在这里做：先按 `excluded_ids` 过滤，再把过滤掉的连同理由交给留痕。
    # 上一版本把这行写成"只传不删"，表现是换人换回了同一个失败者 —— 由 `test_the_first_handoff...` 抓到。
    eligible = [candidate for candidate in candidates if candidate.agent_id not in excluded_ids]
    task_need = sw.task_need_of(card)
    ranking = sw.rank_candidates(task_need, eligible, extra_excluded=exclusions)
    if ranking.selected is None:
        # 席位那一半的措辞（owner 2026-10-17 裁 Q7 乙："不许只说'换人失败'"）：原因原文由调用方从
        # `guard.triple.distinctness_violations` 递进来，这里只拼接、不重新判一次撞岗 ⇒ 口径仍只有一份。
        # `seat_blocked` 为空 ⇒ 这句一个字都不出现（否则升级理由会对不上真正原因）。
        who = "；".join(f"{agent_id}：{'，'.join(reasons)}" for agent_id, reasons in seat_blocked)
        seat = "" if not who else (
            f"席位占用导致合法候选为空：注册面递来 {len(candidates)} 支，被席位挡掉 {len(seat_blocked)} 支"
            f"（{who}）⇒ 三岗 {gp.SEATS} 已满，无可换人；"
        )
        return _escalate(
            session,
            card,
            reason=f"{seat}没有可替代的工具可换（FR-025 不许静默丢弃）；出局：{list(ranking.excluded)}；本次原因：{reason}",
            created_at=created_at,
            agents=agents,
        )

    successor = str(ranking.selected.agent_id)
    sw.apply_selection(
        session,
        ranking,
        feature_request_id=feature_request_id,
        actor_agent_id=sw.AUTOMATED_ACTOR,
        created_at=created_at,
    )
    card.handoff_count = card.handoff_count + 1
    card.status = LANDED
    events.record(
        session,
        entity="task_card",
        entity_id=card.id,
        kind="TASK",
        actor_agent_id=sw.AUTOMATED_ACTOR,
        payload={
            "node": NODE,
            "action": "handed_off",
            "reason": reason,
            "from_agent": str(failed),
            "to_agent": successor,
            "handoff_count": card.handoff_count,
            "agents_involved": sorted({*agents, successor}),
            "card_status_after": card.status,
        },
        created_at=created_at,
    )
    session.flush()
    return HandoffOutcome(
        card_id=card.id,
        from_agent=str(failed),
        to_agent=successor,
        handoff_count_after=card.handoff_count,
        agents_involved=tuple(sorted({*agents, successor})),
        escalated=False,
        status_after=card.status,
        reason=reason,
    )
