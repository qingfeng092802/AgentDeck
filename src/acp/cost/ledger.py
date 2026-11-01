"""T114 的记账半边：写入点 + 双维查询 + 三口径分列（FR-059 / FR-061｜R-25）。

**写入点在哪、不在哪**：`orchestrate/nodes/result_ingest.py` 在**它自己那次回收的事务里**调
`record_usage()`（owner 2026-10-16 对 T114 的预先裁决：调用方开事务，本模块不 `begin()` 也不 `commit()`，
与 Phase 7 的 A-1、T108 的 `record_point()`、T113 的 `insert_usage()` 同一条口径）。
落库本体在 `persistence/costs.py`，幂等与唯一索引那半边已经在那里钉住，本模块不重复实现一遍。

**金额是算出来的，不是报上来的**：适配器交回的 `UsageNotice` 刻意没有 `cost_amount`（协议层的设计说明在
`protocol/refs.py`），单价一律现取 `cost/pricing.py`（⇒ 权威 = `params-ref §6`）。
读不出价 ⇒ `PriceUndetermined` 原样抛出去，回收这一发跟着失败。**不在这里做"记不上就跳过"**：
FR-059 是「每次调用 MUST 记账」，静默跳过等于把 SC-011 那句「100% 可归因」改成"看情况"。

**双维查询 = 按 `agent_id` 汇总 + 按 `task_card_id` 汇总**（`plan.md` 设计项 7 的原文两维）。
两维都只返回**按 `billing_basis` 分列的小计**（`Breakdown`），而**没有**一个"总成本"字段：
FR-061 与宪法「二」禁的是把三类口径加成一个数，所以这里连"顺手提供一个 total"的入口都不给。
`Breakdown.total()` 是专门留着用来**报错**的那一个接口 —— 有人去找总成本时会撞到它，而不是拿到一个假数。

**聚合在 Python 里做，不在 SQL 里 `SUM`**（代价点名）：一次任务卡的流水行数在 MVP-1 是个位数到几十，
换到 SQL 聚合省的是读放大，代价是 `SUM(cost_amount)` 会**恰好是 FR-061 禁的那个形状** —— 它不报错，
只是"忘了加 `GROUP BY billing_basis`"就完成了一次违规合并。把加法放在这里，三口径要合并就得显式穿过
`CrossBasisSumRefused` 这一道。

**用量维度（`Subtotal.usage`）与金额维度是两件事**：`token` 单位的数量可以跨 `INPUT` / `OUTPUT` 相加
（预算比对要用它，见 `cost/budget.py`），**不同 `unit` 的数量绝不互相加**（`seat-day` 加 `token` 无意义）。
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import NoReturn

from sqlalchemy import select
from sqlalchemy.orm import Session

from acp.cost import budget, pricing
from acp.persistence import costs, enums
from acp.persistence.models_registry import CostRecord
from acp.protocol.refs import UsageNotice

#: 三口径的呈现顺序 = `billing_basis` 域的声明顺序（不是字母序：报表与日志都按这条走）。
BASIS_ORDER: tuple[str, ...] = tuple(enums.values_of("billing_basis"))

#: 零金额（分列时"这一口径本期没有行"用的那个 0，不是"算出来是 0"）。
ZERO = Decimal(0)


class LedgerMisuse(ValueError):
    """调用方交来的查询条件不成立（口径名不在域里）。"""

    def __init__(self, message: str, *, code: str = "basis_domain") -> None:
        self.code = code
        super().__init__(message)


class CrossBasisSumRefused(ValueError):
    """有人要把不同 `billing_basis` 的金额加成一个数 ⇒ 报错，不给数字（FR-061｜R-25）。

    这一条码名是判据要认的靶子：`tests/guards/test_cost.py` 钉的是"调用它抛这个错"，
    而不是"某个函数返回的数偏小"——后者只要多算一遍就绿了。
    """

    def __init__(self, bases: tuple[str, ...]) -> None:
        self.bases = bases
        super().__init__(
            f"跨口径求和被拒：{bases} 属于不同计费口径，加成一个数就冒充了平台总成本"
            "（`params-ref §6` 那句「三者不可合并为单一数字」与宪法「二」同一条）"
        )


@dataclass(frozen=True)
class Subtotal:
    """一个口径内的小计：金额、行数，以及**按 `usage_kind` 分开的用量数量**。"""

    basis: str
    cost_amount: Decimal
    currency: str
    rows: int
    usage: dict[str, Decimal]

    def quantity_of(self, *usage_kinds: str) -> Decimal:
        """同一口径内、同一 `unit` 家族的数量合计（预算比对用）。不同 unit 的数量不许互加。"""
        return sum((self.usage.get(kind, ZERO) for kind in usage_kinds), ZERO)


@dataclass(frozen=True)
class Breakdown:
    """一次双维查询的结果：**只有分列，没有合计**。"""

    dimension: str
    subject: str
    subtotals: dict[str, Subtotal]

    def bases(self) -> tuple[str, ...]:
        return tuple(basis for basis in BASIS_ORDER if basis in self.subtotals)

    def subtotal(self, basis: str) -> Subtotal:
        """按口径取小计；这一维没有行时返回零小计（**0 不是缺失，是没发生**）。"""
        if basis not in BASIS_ORDER:
            raise LedgerMisuse(f"口径 {basis!r} 不在 billing_basis 域里：{list(BASIS_ORDER)}")
        found = self.subtotals.get(basis)
        if found is not None:
            return found
        return Subtotal(basis=basis, cost_amount=ZERO, currency=pricing.CURRENCY, rows=0, usage={})

    def total(self) -> NoReturn:
        """**故意报错**的那道接口：FR-061 禁的就是这个动作，所以它一个数字都不返回。

        留着它而不是留一个"别调 total"的注释，是因为装配方与报表方迟早会来要"总成本"，
        那时撞到一条带文档的异常比拿到一个能继续往下算的数安全得多。
        """
        raise CrossBasisSumRefused(self.bases())

    def to_line(self) -> str:
        parts = [
            f"{basis}={self.subtotals[basis].cost_amount}{self.subtotals[basis].currency}"
            for basis in self.bases()
        ]
        return (
            f"COST {self.dimension}={self.subject} :: "
            + (" | ".join(parts) if parts else "（无行）")
            + "（按口径分列，不可相加）"
        )


@dataclass(frozen=True)
class LedgerRun:
    """一次回收记下的账：每一发的落库结果、金额出处，以及入账后的预算读数。"""

    writes: tuple[costs.Write, ...]
    sources: tuple[str, ...]
    budget: budget.BudgetVerdict | None = None

    @property
    def rows_added(self) -> int:
        return sum(1 for write in self.writes if write.added)

    @property
    def duplicates(self) -> int:
        return sum(1 for write in self.writes if not write.added)

    def to_line(self) -> str:
        head = (
            f"COST RECORD :: task_card_id={self.writes[0].dedup_key[0] if self.writes else '-'} "
            f"notices={len(self.writes)} added={self.rows_added} dup={self.duplicates}"
        )
        return head if self.budget is None else f"{head} || {self.budget.to_line()}"


def _aggregate(rows: list[CostRecord], *, dimension: str, subject: str) -> Breakdown:
    """按 `billing_basis` 分列累加金额，并按 `usage_kind` 分列累加数量。"""
    buckets: dict[str, dict[str, Decimal | int | str]] = {}
    usage: dict[str, dict[str, Decimal]] = {}
    for row in rows:
        basis = str(row.billing_basis)
        buckets.setdefault(basis, {"amount": ZERO, "rows": 0, "currency": str(row.currency)})
        buckets[basis]["amount"] = Decimal(str(buckets[basis]["amount"])) + Decimal(str(row.cost_amount))
        buckets[basis]["rows"] = int(buckets[basis]["rows"]) + 1
        usage.setdefault(basis, {})
        kind = str(row.usage_kind)
        usage[basis][kind] = usage[basis].get(kind, ZERO) + Decimal(str(row.quantity))

    subtotals = {
        basis: Subtotal(
            basis=basis,
            cost_amount=Decimal(str(peek["amount"])),
            currency=str(peek["currency"]),
            rows=int(peek["rows"]),
            usage=dict(usage[basis]),
        )
        for basis, peek in buckets.items()
    }
    return Breakdown(dimension=dimension, subject=subject, subtotals=subtotals)


def record_usage(
    session: Session,
    *,
    project_key: str,
    task_card_id: str,
    agent_id: str,
    role_id: str,
    notices: Sequence[UsageNotice],
    price_table: pricing.RateTable | None = None,
    feature_request_id: uuid.UUID | None = None,
    over_budget_action: Callable[..., object] | None = None,
) -> LedgerRun:
    """把一批用量通告记进 EN-13：**先算价、后落库、再比预算，全程只 `flush` 不 `commit`**。

    - 单价来自 `cost/pricing.py`（⇒ 权威 = `params-ref §6`），读不出就抛，不发第二份价目。
    - 幂等、约束分流、SAVEPOINT 全在 `costs.insert_usage()` 里；本函数一遇 `PriceUndetermined`
      就停（**这一发之前的行留在事务里**，跟着调用方的事务一起提交或一起回滚 —— 不做"部分回滚"，
      那等于本模块自己开事务边界，与模块头那条冲突）。
    - `plan.md` 设计项 7 那句"每笔入账后比对任务预算阈值"落在最后一段：**给了 `feature_request_id` 才比**
      （卡号只在需求单内唯一，缺它就没有比对对象，不猜一张卡）。达阈值时的动作 = `over_budget_action`
      （装配点注入 `budget.open_over_budget_ticket` 才是启用；不给 = MVP-1 的现状：**只报不建单**）。
      默认 `None` 不是疏忽 —— 启用哪几类卡点是 Phase 4 裁决 6 的范围，本模块不自扩。
    """
    table = price_table if price_table is not None else pricing.parse_rates()
    writes: list[costs.Write] = []
    sources: list[str] = []
    for notice in notices:
        amount = pricing.price_of(notice, table=table)
        write = costs.insert_usage(
            session,
            project_key=project_key,
            task_card_id=task_card_id,
            agent_id=agent_id,
            role_id=role_id,
            notice=notice,
            cost_amount=amount.cost_amount,
            currency=amount.currency,
        )
        writes.append(write)
        sources.append(amount.source)

    verdict: budget.BudgetVerdict | None = None
    if writes and feature_request_id is not None:
        verdict = budget.check(session, feature_request_id=feature_request_id, task_card_id=task_card_id)
        if verdict.reached and over_budget_action is not None:
            over_budget_action(
                session, feature_request_id=feature_request_id, task_card_id=task_card_id, verdict=verdict
            )
    return LedgerRun(writes=tuple(writes), sources=tuple(sources), budget=verdict)


def by_agent(
    session: Session,
    *,
    agent_id: str,
    project_key: str | None = None,
    since: dt.datetime | None = None,
    until: dt.datetime | None = None,
) -> Breakdown:
    """FR-059 的第一维：按**工具**汇总，口径分列。

    `since` / `until` 只过滤 `occurred_at`（`until` 取开区间）；给就按瞬间比，不给就全量。
    """
    conditions = [CostRecord.agent_id == agent_id]
    if project_key is not None:
        conditions.append(CostRecord.project_key == project_key)
    if since is not None:
        conditions.append(CostRecord.occurred_at >= since)
    if until is not None:
        conditions.append(CostRecord.occurred_at < until)
    rows = list(session.scalars(select(CostRecord).where(*conditions).order_by(CostRecord.occurred_at)))
    return _aggregate(rows, dimension="agent", subject=agent_id)


def by_task(session: Session, *, task_card_id: str, project_key: str | None = None) -> Breakdown:
    """FR-059 的第二维：按**任务**汇总，口径分列。"""
    conditions = [CostRecord.task_card_id == task_card_id]
    if project_key is not None:
        conditions.append(CostRecord.project_key == project_key)
    rows = list(
        session.scalars(
            select(CostRecord).where(*conditions).order_by(CostRecord.occurred_at, CostRecord.id)
        )
    )
    return _aggregate(rows, dimension="task", subject=task_card_id)
