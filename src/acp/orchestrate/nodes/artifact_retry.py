"""`ARTIFACT_INVALID` 之后的人工出口 —— 与 `escalation.py` 同形的第二条"人怎么接手"。

**这条边缺的同样是出口，不是入口**：`validate_artifact.py` 在产物不达标（含"零产物"这一格）时把卡置
`ARTIFACT_INVALID`，那是状态机里一条**真实存在**的边；但 `ARTIFACT_INVALID` 只有一条出边
（`data-model.md §2` 迁移图：`ARTIFACT_INVALID ──▶ HANDED_OFF ──▶ RUNNING（换人≤1）`），
而那条出边**只由 `handoff.handoff()` 实现**，`handoff()` 的调用点在今天的产品路径上**一个都没有**
（`realrun.py::_human_stop` 的 handoff 分支明写"一字不写"，且那条"停点不落库"由
`tests/guards/test_realrun_driver.py::test_the_stop_nodes_structurally_cannot_write` 用哨兵会话钉住）。
⇒ 现场表现是：执行器空交付一次，卡就**永久停在** `ARTIFACT_INVALID`，
`dispatch.DISPATCHABLE` 里没有这一格，于是「继续执行」推它不动、评审里也没有它、待办里也不露头 ——
人唯一的出路是手改库（2026-10-28 现场：单 `9b715923` 的 T03，`examined: 0`，卡死 1 小时）。

**为什么不是"在停点里自动调 `handoff()`"**：`handoff()` 要候选集（EN-03 + EN-05 申报）与三岗席位，
那是**派发段**的依赖面；停点拿不到它们，硬塞进去就得把注册表读取搬进"只做状态迁移"的节点体里，
而那条结构保证（停点不碰 session）正是 T083 用来证明"门禁不代评审"的东西。
更实际的一条：`handoff()` 会**换人**，而换人后新执行者要重新过三岗互斥 ——
这张卡的两个把关位往往已经被另外两支工具占着（现场 T03：executor=qoder / reviewer=doubao / acceptor=workbuddy），
换谁都撞岗 ⇒ `handoff()` 会走"无可替代 ⇒ ESCALATED"那一支，等于让人多点一次才回到同一处。
所以这里的语义是**原地重跑**：执行者不变、`handoff_count` 不动，只把卡从"没有出边的态"
挪到状态机原文写着的那一格 `HANDED_OFF` —— 而它恰好在 `DISPATCHABLE` 里，于是「继续执行」推得动。

**为什么目标态从文档解析而不是写 `Literal["HANDED_OFF"]`**：与 `escalation.EXIT_TARGETS` 同一条理由 ——
域值的唯一定义处是 `data-model.md §2`（该文件"域值引用规矩"那一段），代码里再抄一份就是第二个载体。
`exit_target_in_doc()` 取**那条线上第一个 `▶` 右侧的第一个态名**：那条线后面还有第二个 `▶`
（`HANDED_OFF ──▶ RUNNING`），取最后一个会把 `RUNNING` 当成出口 —— 而 `RUNNING` 是派发段写的，
从这里直接写过去等于跳过占槽（`dispatch.py` 模块头第 3 条：`RUNNING` 与"占了槽"必须是同一件事）。

**不动执行者、不动 `handoff_count`**：`handoff_count` 计的是"换人"（`data-model.md` 规则区：
"返工计 `rework_count`，换人计 `handoff_count`，验收计 `review_round` —— 三列三个触发点"）。
原地重跑没有换人，计一次就把 FR-027 那唯一一次换人额度用光了，而这次失败**根本没换过工具**。
想真换人走 `handoff()`（FR-026 的自动路径）或 `POST /tasks/{id}/worker`（T071 的覆盖路径）——
两条都不在本模块，本模块只负责"把卡放回能被派发的那一格"。
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

NODE = "artifact_retry"
#: 唯一的入口态。写在这里而不从 `validate_artifact.INVALID_STATE` 取，是为了让"这个模块服务哪一格"
#: 在本文件里一眼可读；两处同值由 `test_the_trigger_state_is_the_gate_s_own_literal` 钉住。
TRIGGER = "ARTIFACT_INVALID"
#: 留痕里的动作名（与 `escalation.py` 的 `human_exit`、`handoff.py` 的 `handed_off` 并列）。
ACTION = "artifact_retried"

#: `ARTIFACT_INVALID` 那一行的锚点。用**包含**匹配而不是整行相等：态名与箭头两侧的空白随排版变。
EXIT_LINE_MARK = "ARTIFACT_INVALID"


class ArtifactRetryMisuse(ValueError):
    """这次重跑的用法错。`code` 是给端点与日志用的稳定键名，消息里不带产物内容。"""

    code = "artifact_retry_misuse"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        if code is not None:
            self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class ArtifactRetryOutcome:
    """一次"放回可派发态"的结果。`prior_status` 恒为 `ARTIFACT_INVALID`（不是它就直接抛错）。"""

    card_id: str
    prior_status: str
    status_after: str
    executor_agent_id: str | None
    principal: str
    reason: str
    handoff_count: int


def exit_line_in_doc(path: Path | None = None) -> str:
    """取状态机里 `ARTIFACT_INVALID` 那一行原文（判据拿它比对，不在用例里再抄一份）。"""
    text = (path or data_model_default()).read_text(encoding="utf-8")
    hits = [line for line in text.split("\n") if EXIT_LINE_MARK in line and "▶" in line]
    if len(hits) != 1:
        raise ArtifactRetryMisuse(
            f"状态机原文里带「{EXIT_LINE_MARK}」且有箭头的行读到 {len(hits)} 条（应为 1）"
            "⇒ 文档形态变了，先改这里再改判据",
            code="doc_anchor_ambiguous",
        )
    return hits[0]


def exit_target_in_doc(path: Path | None = None) -> str:
    """从那条线的**第一个** `▶` 右侧取第一个合法态名（解析而不是拿常量比常量，理由见模块头）。"""
    line = exit_line_in_doc(path)
    _left, _mark, right = line.partition("▶")
    legal = set(values_of("task_card_state"))
    for token in re.split(r"[^A-Z_]+", right.upper()):
        if token in legal:
            return token
    raise ArtifactRetryMisuse(
        f"从原文行里解析不出目标态：{line!r} ⇒ 锚点匹配到了别的行",
        code="doc_anchor_unparsed",
    )


def resolve_artifact_retry(
    session: Session,
    *,
    feature_request_id: uuid.UUID,
    task_id: str,
    principal: str,
    created_at: dt.datetime,
    reason: str = "",
) -> ArtifactRetryOutcome:
    """把一张停在 `ARTIFACT_INVALID` 的卡挪到状态机写的下一格（`HANDED_OFF`），并留一条可复核的痕。

    五道检查，顺序是定的：**先看是谁在按，再看卡在不在，再看卡在哪一格，最后看按下去会不会变成死路**。
    把"是谁"放最前，是因为这条边的全部意义就是"这里有人做过判断"——
    与 `handoff` / `dispatch` 用 `AUTOMATED_ACTOR` 的口径正好相反（那两处的主语确实是机器）。
    """
    if not principal or not principal.strip():
        raise ArtifactRetryMisuse(
            "缺 `principal` ⇒ 这条人工出口留下的痕没人认领",
            code="principal_required",
        )
    card = (
        session.query(TaskCard)
        .filter(TaskCard.feature_request_id == feature_request_id, TaskCard.id == task_id)
        .one_or_none()
    )
    if card is None:
        raise ArtifactRetryMisuse(
            f"任务卡 {task_id} 在需求单 {feature_request_id} 下不存在 ⇒ 没有可处置的卡",
            code="card_not_found",
        )
    before = str(card.status)
    if before != TRIGGER:
        raise ArtifactRetryMisuse(
            f"卡 {task_id} 现在是 {before}，不在 {TRIGGER} ⇒ 本出口只服务被门禁判为产物不达标的那一格，"
            "别的迁移各回各的触发点（状态机每条边只有一个来源态，从这里绕过去就没人判过合法性）",
            code="state_not_artifact_invalid",
        )
    # 最后这道不是"多一层保险"：`HANDED_OFF` 之后唯一的下家是 `dispatch`，而它在执行者为空时
    # 直接抛 `executor_missing`（`dispatch.py:175`）⇒ 放行一张没有执行者的卡，只是把死路从
    # `ARTIFACT_INVALID` 搬到 `HANDED_OFF`，而且搬到了一个**看起来**能跑、点下去才报错的格子里。
    if card.executor_agent_id is None:
        raise ArtifactRetryMisuse(
            f"卡 {task_id} 没有执行者（`executor_agent_id` 为空）⇒ 放回可派发态也没有人能接，"
            "先给它指派一个执行者再重跑",
            code="executor_required",
        )
    target = exit_target_in_doc()
    card.status = target
    events.record(
        session,
        entity="task_card",
        entity_id=card.id,
        kind="TASK",
        actor_agent_id=principal.strip(),
        payload={
            "node": NODE,
            "action": ACTION,
            "prior_status": before,
            "reason": reason,
            "executor_agent_id": card.executor_agent_id,
            "handoff_count": card.handoff_count,
            "card_status_after": card.status,
        },
        created_at=created_at,
    )
    session.flush()
    return ArtifactRetryOutcome(
        card_id=card.id,
        prior_status=before,
        status_after=str(card.status),
        executor_agent_id=card.executor_agent_id,
        principal=principal.strip(),
        reason=reason,
        handoff_count=int(card.handoff_count),
    )
