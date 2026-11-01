"""T116 的预算半边：EN-02 `token_budget` 阈值比对 + 超预算 ⇒ C 类卡点的建单路径（FR-060｜R-25）。

**MVP-1 是结构预留，不是启用**（owner 2026-10-16 对 T116 的预先裁决 + Phase 4 裁决 6「MVP-1 只做 A/D 两类卡点」）：

- 比对与建单的**代码路径必须存在** —— 否则「结构预留」这句话没有载体，M2 启用时要从零写；
- 启用位**不在本模块**：`ACTIVE_KINDS` 归 `approval/service.py`（那边 C 类现在会抛 `KindNotEnabled`），
  图上"挂起等人"的那一步属状态机面（动它 = 硬停项）⇒ 本模块只到"把单建出来"为止，
  谁来调它、什么时候真调，由装配点显式注入（`ledger.record_usage(..., over_budget_action=…)`，默认 `None`）。
- 因此 MVP-1 的真实行为是：**算出"达没达阈值"并如实报出去，不建单、不暂停**。
  `tests/guards/test_cost.py` 里"达阈值 ⇒ 真建出 C 类单"那一条按 owner 的裁定 `skip` 并写明原因，
  不许靠"路径存在"就当它绿了。

**阈值取"用量达到预算上限"这一读法**：FR-060 原文是「每个任务 MUST 设有用量预算上限；达到阈值 MUST 暂停
并请示」，`token_budget` 就是那个上限（裁决 3：`NULL` = 无预算，不用 0 表示），而"阈值的比例"文档里没给。
⇒ 不发明 80% 这种预警线；要一条预警线得 owner 先给载体（`params-ref` 目前没有那一行）。

**"用量"这一维可以跨口径相加，"金额"那一维不行**（FR-061 的边界在这里很要紧）：
`token_budget` 是 token 数，`INPUT` 与 `OUTPUT` 的 `token` 数量加在一起才是"这次任务花了多少 token"，
这个和是**同一种单位的物理量**；而把 `TOKEN` / `SEAT_CREDIT` / `BYOK` 三个金额小计加成一个数就是假账
（⇒ 那条禁令的入口是 `ledger.Breakdown.total()`）。本模块只碰前者，一次 `cost_amount` 都不读。

**三处置项（追加预算 / 换低成本档 / 终止）** 是给人看的选项，权威出处 = `plan.md` 设计项 7；
它们进 payload 的 `pending_item` 与 `context_digest`（五要素的形状是固定的，`extra="forbid"`，
塞不进第六个键），而 `impact.estimated_extra_usage` 交的是**各口径 0** —— 含义见那个函数里的说明。
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from acp.approval import service
from acp.approval.timeouts import KIND_BY_LETTER
from acp.persistence import enums
from acp.persistence.models_core import TaskCard
from acp.persistence.models_registry import CostRecord

#: C 类的码值 = `approval_kind` 域里那一个（从 `KIND_BY_LETTER` 派生，不写字面量）。
BUDGET_KIND: str = KIND_BY_LETTER["C"]

#: 三个处置项，逐字照 `plan.md` 设计项 7 那一行。
DISPOSAL_OPTIONS: tuple[str, ...] = ("追加预算", "换低成本档", "终止")

#: 参与预算比对的单位（`token_budget` 是 token 数 ⇒ 别的单位一律不进这个和）。
BUDGET_UNITS: frozenset[str] = frozenset({"token"})

ZERO = Decimal(0)


class BudgetMisuse(ValueError):
    """比对这一发本身不成立（卡不存在）。"""

    def __init__(self, message: str, *, code: str = "card_missing") -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class BudgetVerdict:
    """一张卡的预算读数。`reached` 为真就是 FR-060 说的"达到阈值"。"""

    task_card_id: str
    budget: Decimal | None
    used: Decimal
    reached: bool
    note: str

    def to_line(self) -> str:
        limit = "无预算" if self.budget is None else str(self.budget)
        state = "REACHED" if self.reached else "WITHIN"
        return (
            f"COST BUDGET :: task_card_id={self.task_card_id} used={self.used} budget={limit} "
            f"{state}（{self.note}）"
        )


def token_used(session: Session, *, task_card_id: str) -> Decimal:
    """这张卡已记的 **token 数量**合计（`INPUT` + `OUTPUT`，跨 `billing_basis` 也跨不了单位）。

    刻意用 `unit` 过滤而不是"排除 BYOK 行"：BYOK 的调用同样消耗 token，只是那笔钱不进平台账。
    这一条判据钉的是"本函数一次都没读 `cost_amount`"，因为读了它就把 FR-061 的禁令带进来了。

    **已知的定位精度限制（登记为待裁，不在本模块擅自修）**：EN-13 只有 `task_card_id` 这一列文本，
    没有 `feature_request_id`（`models_registry.py:68` 明写"未标 FK ⇒ 按字面不建外键"），而卡号只在
    需求单内唯一（owner 裁决 2 的复合主键）。因此两张需求单里同名的卡（都有 `T10`）在这里会被读成同一张。
    要修得先动 EN-13 的列与那条唯一索引 = 迁移 + 唯一索引形状，两者都属硬停项 ⇒ 交 owner 裁。
    """
    values = session.scalars(
        select(CostRecord.quantity).where(
            CostRecord.task_card_id == task_card_id, CostRecord.unit.in_(sorted(BUDGET_UNITS))
        )
    ).all()
    return sum((Decimal(str(value)) for value in values), ZERO)


def check(session: Session, *, feature_request_id: uuid.UUID, task_card_id: str) -> BudgetVerdict:
    """比一次预算。**没有上限就是没有上限**，不拿 0 当"预算无限"也不拿它当"已超"。

    `feature_request_id` 是必填的：`task_card.id` 只在需求单内唯一（复合主键 `(feature_request_id, id)`，
    owner 裁决 2），只按卡号取会在两个需求单之间挑错那张卡 ⇒ 预算比的是别人的额度。
    """
    card = (
        session.query(TaskCard)
        .filter(TaskCard.feature_request_id == feature_request_id, TaskCard.id == task_card_id)
        .one_or_none()
    )
    if card is None:
        raise BudgetMisuse(
            f"任务卡 ({feature_request_id}, {task_card_id}) 读不出来 ⇒ 预算比对没有对象", code="card_missing"
        )
    budget = None if card.token_budget is None else Decimal(str(card.token_budget))
    used = token_used(session, task_card_id=task_card_id)
    if budget is None:
        return BudgetVerdict(
            task_card_id=task_card_id, budget=None, used=used, reached=False, note="裁决 3：NULL = 无预算"
        )
    return BudgetVerdict(
        task_card_id=task_card_id,
        budget=budget,
        used=used,
        reached=used >= budget,
        note="用量达到 EN-02 的预算上限（FR-060）",
    )


def build_over_budget_payload(
    *, task_card_id: str, verdict: BudgetVerdict, downstream_task_ids: list[str] | None = None
) -> dict[str, object]:
    """C 类单的五要素 payload（走 `service.build_payload`，超时那一要素由 §1 现算）。

    两处取舍点名：
    - `impact.estimated_extra_usage` 交的是**三个口径各自的 0**。MVP-1 没有"追加多少预算"的权威来源
      （§6 只有单价，追加额是人的决定），而 `Impact` 的校验要求键在 `billing_basis` 域里、值非负整数
      ⇒ 交空字典会被"必须是非空列表"那类形状校验挡住吗？不会，但它会把"三口径分列"这个形状丢掉。
      取 `0` 的含义是**平台不替人估**，人在单子上填追加额；这不是估算，是零主张。
    - `downstream_task_ids` 缺省交**这张卡自己**：`Impact` 不许空列表，而超预算的直接影响对象就是
      "这张卡还要不要继续"。交空表等于把事实藏起来，交一个编出来的后续卡号等于造事实。
    """
    impact = {
        "downstream_task_ids": list(downstream_task_ids or [task_card_id]),
        "estimated_extra_usage": {basis: 0 for basis in enums.values_of("billing_basis")},
    }
    pending = f"任务 {task_card_id} 的用量已达预算上限 ⇒ 处置项：{' / '.join(DISPOSAL_OPTIONS)}"
    digest = (
        f"已用 {verdict.used} / 上限 {verdict.budget}（{verdict.note}）；"
        "金额按 billing_basis 分列在成本账上，不以单一总成本呈现（FR-061）"
    )
    return service.build_payload(
        kind=BUDGET_KIND,
        pending_item=pending,
        context_digest=digest,
        suggested_verdict="need_info",
        impact=impact,
    )


def open_over_budget_ticket(
    session: Session,
    *,
    feature_request_id: uuid.UUID,
    task_card_id: str,
    verdict: BudgetVerdict | None = None,
    downstream_task_ids: list[str] | None = None,
    now: dt.datetime | None = None,
) -> service.Ticket:
    """C 类建单路径（与 Phase 4 的 `ApprovalGate` 同一个出口 = `service.create_approval`）。

    **MVP-1 调用它会抛 `service.KindNotEnabled`** —— 那是 Phase 4 裁决 6 的现状（只启 A/D），
    不是这条路径坏了。启用 C 的那一笔动的是 `ACTIVE_KINDS`，不是这里。
    主键走 `service.ticket_id_for()`（可推导）：重放时回到同一行，与 A 类同一处理由。
    """
    decision = verdict if verdict is not None else check(
        session, task_card_id=task_card_id, feature_request_id=feature_request_id
    )
    ticket_id = service.ticket_id_for(
        kind=BUDGET_KIND, feature_request_id=feature_request_id, task_card_id=task_card_id
    )
    return service.create_approval(
        session,
        kind=BUDGET_KIND,
        payload=build_over_budget_payload(
            task_card_id=task_card_id, verdict=decision, downstream_task_ids=downstream_task_ids
        ),
        feature_request_id=feature_request_id,
        task_card_id=task_card_id,
        ticket_id=ticket_id,
        now=now,
    )
