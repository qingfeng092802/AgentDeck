"""T113：EN-13 `cost_record` 的落库半边 = **幂等的写入**与**按四元组的读回**（FR-059｜R-25）。

**表早就在，写者一直空着**：`cost_record` 与那条唯一索引 `uq_cost_record_dedup` 是 T025（迁移 0003）
按 `data-model.md` EN-13 建的，键顺序逐字 = `(task_card_id, agent_id, occurred_at, usage_kind)`。
从那一刻到本轮，全仓库唯一的写入者是 `tests/integration/test_schema_invariants.py` 里那条
"索引本身在不在"的探针 ⇒ FR-059 那句"每次工具调用 MUST 记账"在代码侧一直是零落点。
**本模块不新建 schema**（owner 2026-10-16 的范围约束第 2 条）：它只给那张表第一个写者。

**权威是那条唯一索引，不是应用层的预检**：`insert_usage()` 走的是"直接写、撞了才知道重复"，
不做"先 SELECT 再 INSERT" —— 后者看起来省一次 INSERT，实际把幂等做成了两个并发投递都能读到"还没有"
再双双写进去（唯一索引随后把其中一发打回，但调用方已经拿到"我落了这一行"的返回值）。
所以顺序是：**试写 → 只在 `23505` 且约束名 = `uq_cost_record_dedup` 时判定为重复**。

**只吞这一种冲突**（这是本模块唯一一处危险代码，代价点名）：唯一约束也用于 PK，
`pk_cost_record` 那一发的含义是"同一个 id 写两次"= 调用方把两次不同的投递塞了同一个主键，
那是缺陷不是重复；NOT NULL 那一发更是直接把数据写坏了。两类都 MUST 抛出。
⇒ 判据里有一条**反向**用例：拿同一个 `record_id` 投两张不同卡 ⇒ `IntegrityError` 原样抛、
不降级成"重复"。把 `_is_dedup_violation()` 放宽成"只看 sqlstate"就会漏掉这条。

**重复的那一发不改第一行**：第二次投递返回 `added=False` 且第一行的 `quantity` / `cost_amount`
一字不变。选"丢弃"而不是"更新"的理由：EN-13 是**流水**，流水被后到的同名投递覆盖 = 把已经核销过的
用量改掉（FR-061 的口径分列会跟着漂），而后到者到底是不是同一发，机制层无从判断。

**事务边界归调用方**（口径同 `persistence/snapshots.py` 与 `persistence/events.py`）：本模块一次
`commit()` 都不发。吞掉重复那一发靠的是 SAVEPOINT（`session.begin_nested()`）而不是回滚整个事务
—— 外层事务里往往还挂着别的写入，把整笔回滚掉等于让一次重复投递赔上别人的写入。

**不做的事**（都在后面几格里，别在本模块顺手补）：单价换算与 `billing_basis` 分列 = `cost/ledger.py`
（T114 / T115，`protocol/refs.py::UsageNotice` 刻意不带 `cost_amount` 与 `currency`，金额由 ledger 依
`config/params-ref.md §6` 算，让工具自报金额等于把 FR-061 交回报价方）；预算阈值与 C 类单 = `cost/budget.py`
（T116）；不写 EN-14 留痕 —— `cost_record` 自己就是流水，再造一条留痕就是同一事实两个载体。
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from acp.persistence import enums
from acp.persistence.models_registry import CostRecord
from acp.protocol import refs

#: 防重复入账的四个键，**逐字照 `data-model.md` EN-13 那一行**（顺序也是文档的顺序）。
#: 这一条名单是写者与读者的共同出处：`dedup_key()` 与 `find_by_dedup_key()` 都从它拼，
#: 判据按"它在本模块只出现一次定义"钉（两处各写一份的话，改了写者忘了改读者 = 幂等静默失效）。
DEDUP_COLUMNS: tuple[str, ...] = ("task_card_id", "agent_id", "occurred_at", "usage_kind")

#: 唯一索引名（迁移 0003 与 mapper 的 `__table_args__` 同名；`data-model.md:188` 说的就是它）。
DEDUP_INDEX = "uq_cost_record_dedup"

#: PG 的 unique_violation SQLSTATE。只有**这一码 + 上面那个约束名**才允许被当成就重复。
UNIQUE_VIOLATION = "23505"

#: 本模块写入的列（`tests/guards/test_cost.py` 的 AST 判据拿它当对照物：加一列要一起进名单）。
COST_WRITABLE_COLUMNS: tuple[str, ...] = (
    "id",
    "occurred_at",
    "project_key",
    "task_card_id",
    "agent_id",
    "role_id",
    "model_ref",
    "usage_kind",
    "quantity",
    "unit",
    "cost_amount",
    "currency",
    "billing_basis",
)


class CostMisuse(ValueError):
    """调用方交来的形状不成立（时间没带时区、取值不在域里、金额不是 `Decimal`、定位键为空）。

    全部发生在**一行都没写之前**，所以抛出来 = 库里这一发什么都没有。
    """

    def __init__(self, message: str, *, code: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class Write:
    """一发投递的结果：库里那一行（新写的或本来就有的）+ 这一发是否真的落了一行。"""

    row: CostRecord
    added: bool
    dedup_key: tuple[str, str, str, str]

    def to_line(self) -> str:
        verb = "ADD" if self.added else "DUP"
        return (
            f"COST {verb} :: id={self.row.id} task_card_id={self.dedup_key[0]} "
            f"agent_id={self.dedup_key[1]} usage_kind={self.dedup_key[3]} "
            f"occurred_at={self.dedup_key[2]} billing_basis={self.row.billing_basis}"
        )


def _utc(moment: dt.datetime) -> dt.datetime:
    """归一到 UTC。

    不归一的话，`09:00+08:00` 与 `01:00+00:00` 是同一个瞬间：PG 按瞬间判重（两个值存成同一条），
    而 Python 侧的键串会不相等 ⇒ "这一发是不是重复"在两侧读到不同答案。键串要能代表索引的判定，
    只能把两个写法都化成同一个瞬间的 UTC 读数。
    """
    if moment.tzinfo is None:
        raise CostMisuse(
            f"occurred_at={moment!r} 没带时区 ⇒ 这一列是 timestamptz，落库时按会话时区解释，"
            "同一个通告从两条路径来会算成两个键 ⇒ 幂等直接失效",
            code="occurred_at_naive",
        )
    return moment.astimezone(dt.timezone.utc)


def dedup_key(
    *, task_card_id: str, agent_id: str, occurred_at: dt.datetime, usage_kind: str
) -> tuple[str, str, str, str]:
    """四元组的唯一拼法（顺序照 `DEDUP_COLUMNS`）。写者用它拼键，读面与返回值也用它。"""
    return (task_card_id, agent_id, _utc(occurred_at).isoformat(), usage_kind)


def _is_dedup_violation(exc: IntegrityError) -> bool:
    """只有 `23505` 且约束名 = `uq_cost_record_dedup` 才算"这一发是重复投递"。

    约束名读不到时退回看原文（`diag` 在少数驱动包装下是 `None`）；两种都拿不到就**不算重复**
    —— 宁可让调用方收到一个真异常，也不能把"写坏了数据"读成"记过账了"。
    """
    original = getattr(exc, "orig", None)
    if getattr(original, "sqlstate", None) != UNIQUE_VIOLATION:
        return False
    constraint = getattr(getattr(original, "diag", None), "constraint_name", None)
    if constraint is not None:
        return str(constraint) == DEDUP_INDEX
    return DEDUP_INDEX in str(original)


def _check_shape(project_key: str, task_card_id: str, agent_id: str, role_id: str, unit: str, currency: str) -> list[str]:
    """空定位键 / 空单位 / 空币种（PG 侧它们全是 NOT NULL，但空串能过 NOT NULL —— 幂等的键不能是空串）。"""
    return [
        name
        for name, value in (
            ("project_key", project_key),
            ("task_card_id", task_card_id),
            ("agent_id", agent_id),
            ("role_id", role_id),
            ("unit", unit),
            ("currency", currency),
        )
        if value is None or not str(value).strip()
    ]


def insert_usage(
    session: Session,
    *,
    project_key: str,
    task_card_id: str,
    agent_id: str,
    role_id: str,
    notice: refs.UsageNotice,
    cost_amount: Decimal,
    currency: str,
    record_id: uuid.UUID | None = None,
) -> Write:
    """把一条用量通告落成一行 EN-13；重复投递（四元组已存在）返回 `added=False` 且不动第一行。

    - 只 `flush()`，不 `commit()`（事务边界归调用方，模块头那条）。
    - **取值域不在这里管**：`usage_kind` / `billing_basis` 由协议层 `refs.UsageNotice` 的 `Literal`
      挡（构造期就拒），落库这一发再挡一次就是第二处决定权（原则 I）；库里那两列是 PG 原生枚举，
      绕过协议硬塞进来的坏值由 PG 直接打回。本模块只挡**协议管不着的那三样**：空定位键、没时区的时间、
      非 `Decimal` 的金额。
    - `cost_amount` MUST 是 `Decimal`：本列是 `numeric`，`float` 进去带二进制误差，
      `int` 会把 params-ref §6 那种 0.000012 的单价在调用处截成 0 —— 两种都比"报错"贵。
    - 返回值里的 `row` 在 `added=False` 时是**已存在的那一行**，读回它而不是复制参数：
      调用方要能拿它去比对自己这一发的数量（比出差别就是"重复但不同值"，那是下一轮的事）。
    - **已知限制（owner 2026-10-16 裁 ㉟ 取保守版②：只登记、不改索引、不改 EN-14 语义）**：
      同一 `(task_card_id, agent_id, occurred_at, usage_kind)` 的第二发被幂等丢弃；
      **若这第二发是不同 `billing_basis`，丢弃是有意的** —— 那四列是 `plan.md` 设计项 7 与
      `data-model.md` EN-13 逐字给的口径，加第五列属迁移，动它 = 硬停项。
      场景（同一瞬间两口径）、何时该真改索引（T123 装配时若发现真实场景）与同根的第二处
      （`cost/budget.py::token_used()` 只能按 `task_card_id` 匹配）都写在
      `evidence/phase-10.md` 的 ㉟ 登记段，本模块的行为以那一段为准。
    """
    key = dedup_key(
        task_card_id=task_card_id, agent_id=agent_id, occurred_at=notice.occurred_at, usage_kind=notice.usage_kind
    )
    blank = _check_shape(project_key, task_card_id, agent_id, role_id, notice.unit, currency)
    if blank:
        raise CostMisuse(f"定位键与单位里有空值：{blank} ⇒ 空字符串当键会让幂等判不出来", code="shape")
    if not isinstance(cost_amount, Decimal):
        raise CostMisuse(
            f"cost_amount 收到 {type(cost_amount).__name__} ⇒ 只接 Decimal（模块头那条理由）", code="amount_shape"
        )

    row = CostRecord(
        id=record_id if record_id is not None else uuid.uuid4(),
        occurred_at=notice.occurred_at,
        project_key=project_key,
        task_card_id=task_card_id,
        agent_id=agent_id,
        role_id=role_id,
        model_ref=notice.model_ref,
        usage_kind=notice.usage_kind,
        quantity=notice.quantity,
        unit=notice.unit,
        cost_amount=cost_amount,
        currency=currency,
        billing_basis=notice.billing_basis,
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError as exc:
        if not _is_dedup_violation(exc):
            raise
        existing = find_by_dedup_key(
            session, task_card_id=task_card_id, agent_id=agent_id, occurred_at=notice.occurred_at,
            usage_kind=notice.usage_kind,
        )
        if existing is None:
            raise CostMisuse(
                f"唯一索引报了冲突却按四元组读不回那一行（键={key}）⇒ 冲突另有其因，不当成重复",
                code="dedup_row_missing",
            )
        return Write(row=existing, added=False, dedup_key=key)
    return Write(row=row, added=True, dedup_key=key)


def find_by_dedup_key(
    session: Session,
    *,
    task_card_id: str,
    agent_id: str,
    occurred_at: dt.datetime,
    usage_kind: str,
) -> CostRecord | None:
    """按四元组读回一行；读的是**值相同的那一个瞬间**（`timestamptz` 比的是瞬间，不是字面串）。

    时间参数原样交给 SQL 而不是先 `isoformat()`：把键串当查询条件就等于自己造第二套判定口径。
    """
    return session.scalars(
        select(CostRecord)
        .where(
            CostRecord.task_card_id == task_card_id,
            CostRecord.agent_id == agent_id,
            CostRecord.occurred_at == _utc(occurred_at),
            CostRecord.usage_kind == usage_kind,
        )
        .order_by(CostRecord.id)
    ).first()


def rows_for_task(session: Session, *, task_card_id: str) -> list[CostRecord]:
    """按任务列出用量流水，顺序 = `(occurred_at ↑, usage_kind 按域声明顺序, id ↑)`。

    排 `usage_kind` 用域下标而不是 SQL 的 `ORDER BY usage_kind`：PG 枚举按 OID 排，
    不保证等于 `refs.UsageKind` 的声明顺序（口径同 `persistence/snapshots.py::list_points()`）。
    """
    rows = list(
        session.scalars(
            select(CostRecord).where(CostRecord.task_card_id == task_card_id).order_by(CostRecord.occurred_at, CostRecord.id)
        )
    )
    order = {str(name): index for index, name in enumerate(enums.values_of("usage_kind"))}
    return sorted(rows, key=lambda row: (row.occurred_at, order.get(str(row.usage_kind), len(order)), str(row.id)))
