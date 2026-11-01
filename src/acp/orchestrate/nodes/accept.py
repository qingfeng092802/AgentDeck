"""T091 + T092 + T093：验收环节 —— 对照原始需求逐条判，跑满上限就带未达标项交付。

**轮次上限不在这里解析**。`data-model.md:46` 那条 C9 修正写的是"上限取自 `constitution.md`
原则 III 的硬值，MUST NOT 下沉 `params-ref`"—— 下沉成配置等于让一次改配置就悄悄放宽一条
NON-NEGOTIABLE 判据。而"从宪法解析"这件事本仓已经有唯一的一处（`approval/timeouts.py` 的
`review_round_limit`，Phase 4 由 T057 落地、判据在 `tests/contract/test_approval_timeouts.py`）⇒
本模块 **MUST NOT** 再写第二个解析式：两个正则对同一句权威话会有两种失败方式（一个取第一处、
一个要求恰一处），那正是原则 I 禁的"第二份事实"。命中数不是 1 就由那唯一的一处喊 `ParamsRefError`。

**验收结论只认验收岗**（`AcceptMisuse(code="acceptor_mismatch")`）。这不是对称美学：
三岗互斥的全部意义是"判的那一方不是干的那一方"，如果任何人递来一份 `ItemVerdict` 都能落进
`DELIVERED`，那 T085 的断言就只管指派不管执行 —— 指派对了、结论由执行者自己递的，形状上完全看不出。
`record_verdict`（审查侧）从一开始就有这道比对，这里补齐另一半。

**轮次计数器是两列，不是一列**（owner 2026-10-14 待裁 ⑬ 的裁定，落库在迁移 `0007`）：
`review_round` = **验收轮次**，只由 `accept()` 每轮 +1，并受宪法原则 III 那个硬值约束；
`rework_count` = **返工次数**，只由 `mark_rework()` +1，**没有上限**（有上限的是验收轮次），
也**不计成换人**（`handoff_count` 那条 `<= 1` 的 CHECK 与它无关）。
此前一列两用：`data-model.md:46` 写的是"验收轮次"，而 T092 的返工也想往里记 ⇒ 两个口径挤在同一列上，
登记的正是"第二列一旦出现就说明裁定还没做"那道关系式判据（现在它按两列成立，见
`tests/guards/test_review_accept.py` 第七节）。这一笔不是风格选择：一列两用会让"第 3 轮仍未过 MUST 进
`DELIVERED_WITH_ISSUES`"那条判据把"返了 2 次工"也算进轮次，于是审查返工能替验收轮次把上限吃光。
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from acp.approval.timeouts import constitution_default, review_round_limit
from acp.persistence import events
from acp.persistence.models_approval import AcceptanceMap
from acp.persistence.models_core import TaskCard

NODE = "accept"

#: EN-14 上"这一轮验收"那条留痕的动作名。**写侧在本模块**，所以名字也归这里：
#: 读侧（`run._acceptance_audit` / `run.open_issues_of`）引用它，不在装配点再写一遍同一个字符串 ——
#: 两处各写一份，改一处就会让另一处静默读到空载荷（表现是"清单读不出来"，而不是报错在改名那一行）。
ACTION_ROUND = "acceptance_round"

#: `open_issues_items` 那一格的键名（清单原文）。读侧用它取清单，判据用它钉"载体确实在载荷里"。
OPEN_ISSUES_ITEMS = "open_issues_items"

DELIVERED = "DELIVERED"
DELIVERED_WITH_ISSUES = "DELIVERED_WITH_ISSUES"
CHANGES_REQUESTED = "CHANGES_REQUESTED"
REWORK = "REWORK"
ACCEPTING = "ACCEPTING"


class AcceptMisuse(ValueError):
    """验收动作的用法错。`code` 是给端点与审计行的稳定键名，消息里不带需求原文。"""

    code = "accept_misuse"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        if code is not None:
            self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class ItemVerdict:
    """一条验收条目的判定。`evidence` 指向产物，不写正文也不写凭据。"""

    acceptance_item: str
    met: bool
    evidence: str
    gap: str = ""

    def to_issue(self) -> dict[str, str]:
        """未达标项的三件套（FR-035 原文："哪项未达标、差在哪、建议"）。"""
        return {"item": self.acceptance_item, "gap": self.gap, "suggestion": self.evidence}


@dataclass(frozen=True)
class AcceptanceOutcome:
    card_id: str
    round_no: int
    status_after: str
    met: int
    unmet: int
    open_issues: tuple[dict[str, str], ...]
    limit: int


def round_limit(path: Path | None = None) -> int:
    """验收轮次上限 —— **转调**本仓唯一的那处解析（`approval/timeouts.py`），这里不写第二份正则。

    保留这个名字只为让调用方读得直白（`round_limit()` 而非 `review_round_limit()`）；
    判据里钉的是"它与那处的读数永远是同一个数"，见 `tests/guards/test_review_accept.py` 第四节。
    """
    return review_round_limit(path or constitution_default())


def _card(session: Session, feature_request_id: uuid.UUID, task_id: str) -> TaskCard:
    card = (
        session.query(TaskCard)
        .filter(TaskCard.feature_request_id == feature_request_id, TaskCard.id == task_id)
        .one_or_none()
    )
    if card is None:
        raise AcceptMisuse(f"任务卡 {task_id} 在需求单 {feature_request_id} 下不存在", code="card_not_found")
    return card


def expected_items(session: Session, feature_request_id: uuid.UUID, task_id: str) -> tuple[str, ...]:
    """这张卡在 EN-07 上对应的验收条目集合（FR-030 的"对照映射表逐条判定"里那份表）。

    空集合 ⇒ 抛。映射表是拆单时同步生成的（FR-007），没有它就等于验收员自拟口径（原则 III 禁的那件事）。
    """
    rows = session.scalars(
        select(AcceptanceMap).where(
            AcceptanceMap.feature_request_id == feature_request_id,
            AcceptanceMap.task_card_id == task_id,
        )
    ).all()
    items = tuple(sorted({str(row.acceptance_item) for row in rows}))
    if not items:
        raise AcceptMisuse(
            f"卡 {task_id} 在 EN-07 上没有任何映射行 ⇒ 验收没有可对照的条目，MUST NOT 由验收方自拟口径",
            code="map_empty",
        )
    return items


def check_items(expected: Sequence[str], verdicts: Sequence[ItemVerdict]) -> tuple[str, ...]:
    """逐条比对的完整性：条目集合 MUST 与映射表**相等**（双向差集），不是"覆盖了就行"。

    少一条 = 有一条没人判；多一条 = 验收方自己加了口径（原则 III 的"不得自拟验收口径"）。
    """
    want = set(expected)
    seen = [str(verdict.acceptance_item) for verdict in verdicts]
    reasons: list[str] = []
    if len(seen) != len(set(seen)):
        reasons.append("同一条验收条目被记了不止一行 ⇒ 一份结论里一个条目只能有一个判定")
    missing = sorted(want - set(seen))
    extra = sorted(set(seen) - want)
    if missing:
        reasons.append(f"映射表里有 {missing} 没有对应判定 ⇒ 没判过不算判完（FR-030）")
    if extra:
        reasons.append(f"这些条目不在映射表里：{extra} ⇒ 验收口径 MUST NOT 由验收方自拟")
    return tuple(reasons)


def accept(
    session: Session,
    *,
    feature_request_id: uuid.UUID,
    task_id: str,
    verdicts: Sequence[ItemVerdict],
    created_at: dt.datetime,
    actor_agent_id: str,
) -> AcceptanceOutcome:
    """落一轮验收结论：全过 ⇒ `DELIVERED`；有未过且还没到上限 ⇒ 回返工；到上限 ⇒ 带未达标项交付。

    顺序是 `判完整性 → 计轮次 → 落状态`：任何一步不过都不动卡，所以库里不会出现
    "轮次加了但结论没落"这种半格状态。
    """
    card = _card(session, feature_request_id, task_id)
    if str(card.status) != ACCEPTING:
        raise AcceptMisuse(
            f"卡 {task_id} 现在是 {card.status}，验收只在 {ACCEPTING} 上进行（状态机上 `ACCEPTING` 才有出边到交付）",
            code="state_not_accepting",
        )
    if str(card.acceptor_agent_id) != actor_agent_id:
        raise AcceptMisuse(
            f"验收结论来自 {actor_agent_id!r}，而这张卡的验收岗是 {card.acceptor_agent_id!r}"
            " ⇒ 不是同人就不收（三岗互斥若只管指派、不管'谁的结论被收下'，指派对了也守不住交付那一格）",
            code="acceptor_mismatch",
        )

    expected = expected_items(session, feature_request_id, card.id)
    reasons = check_items(expected, verdicts)
    if reasons:
        raise AcceptMisuse(f"卡 {card.id} 的验收结论不入库：{'；'.join(reasons)}", code="items_not_compliant")

    limit = round_limit()
    round_no = int(card.review_round) + 1
    unmet = tuple(verdict for verdict in verdicts if not verdict.met)
    met = len(verdicts) - len(unmet)

    if not unmet:
        status = DELIVERED
        issues: tuple[dict[str, str], ...] = ()
    elif round_no >= limit:
        # T093：第 N 轮仍未过 ⇒ 带未达标项结束，`open_issues` 必填非空（FR-035 的"显式列出"）。
        status = DELIVERED_WITH_ISSUES
        issues = tuple(verdict.to_issue() for verdict in unmet)
    else:
        # T092：不通过自动进入下一轮 —— 状态机上是 `CHANGES_REQUESTED ──▶ REWORK`，
        # 这里落 `CHANGES_REQUESTED`（"谁判的不通过"那一格），返工那一格归 `mark_rework`。
        status = CHANGES_REQUESTED
        issues = tuple(verdict.to_issue() for verdict in unmet)

    card.review_round = round_no
    card.status = status
    events.record(
        session,
        entity="task_card",
        entity_id=card.id,
        kind="TASK",
        actor_agent_id=actor_agent_id,
        payload={
            "node": NODE,
            "action": ACTION_ROUND,
            "round_no": round_no,
            "limit": limit,
            "met": met,
            "unmet": len(unmet),
            "open_issues": len(issues),
            # 清单**原文**也落这一行。`open_issues` 那格只有条数，而 FR-035 要的是"显式列出"、
            # T131 要的"未达标项清单"与 T125 要的"四要素可查"都要能**事后从库里问出来** ——
            # 只有条数的那一份回答不了"哪一条、差在哪"。为什么写在这里而不是新开一列：
            # EN-07 没有 `met` 列（它是"卡↔条目"的映射表，不是判定表），新开一列 = 迁移 = 硬停项；
            # 而这一行本来就是"这一轮验收"的那一条事实，清单是它的内容，不是第二条事实。
            # 读侧只有一个（`run.open_issues_of`），不在这里重复解释怎么读。
            OPEN_ISSUES_ITEMS: [dict(issue) for issue in issues],
            "card_status_after": card.status,
        },
        created_at=created_at,
    )
    session.flush()
    return AcceptanceOutcome(
        card_id=card.id,
        round_no=round_no,
        status_after=card.status,
        met=met,
        unmet=len(unmet),
        open_issues=issues,
        limit=limit,
    )


def mark_rework(session: Session, *, feature_request_id: uuid.UUID, task_id: str, created_at: dt.datetime, actor_agent_id: str) -> str:
    """T092 的返工那一格：`CHANGES_REQUESTED ──▶ REWORK`，同一执行者返工，MUST NOT 计成换人。

    `handoff.py:53` 那行注释把这条分工写得很清楚（"返工与换人是两条路，别把审查意见计成换人次数"），
    所以这里只动状态列 + **返工计数** + 留痕，**不碰 `handoff_count`**，也不碰三个岗的指派。

    **计的是 `rework_count`，不是 `review_round`**（待裁 ⑬ 之后这两列各管一件事）：
    往 `review_round` 上记返工会让宪法那个验收轮次上限被返工吃掉，表现是"第 3 次审查返工后验收直接
    带问题交付"，而那张卡其实一轮验收都没跑完。这一句是被判据钉住的，不是解释性修辞。
    """
    card = _card(session, feature_request_id, task_id)
    if str(card.status) != CHANGES_REQUESTED:
        raise AcceptMisuse(
            f"卡 {task_id} 现在是 {card.status}，返工只接在 {CHANGES_REQUESTED} 之后",
            code="state_not_changes_requested",
        )
    before = str(card.status)
    card.status = REWORK
    card.rework_count = int(card.rework_count) + 1
    events.record(
        session,
        entity="task_card",
        entity_id=card.id,
        kind="TASK",
        actor_agent_id=actor_agent_id,
        payload={
            "node": NODE,
            "action": "rework_marked",
            "prior_status": before,
            "rework_count": card.rework_count,
            "review_round_unchanged": card.review_round,
            "handoff_count_unchanged": card.handoff_count,
            "executor_agent_id": card.executor_agent_id,
            "card_status_after": card.status,
        },
        created_at=created_at,
    )
    session.flush()
    return card.status
