"""T078：`Dispatch` 节点 —— 依赖全 `DELIVERED` 才放行（FR-020｜R-04）+ 开工前占一个槽（FR-021｜R-05）。

**两件事为什么在同一个节点里**：占槽与放行是同一个决定的两面 —— "这张卡现在可以跑"。
分开放就会出现"槽已经占了、依赖其实没齐"这种半决定状态，而它既不可观察也没人负责。
所以 `dispatch()` 的顺序是：**先判形状，再判依赖，最后才占槽并落库**。

三条口径，都是文档只给了一半、这里补全并点名：

1. **"就绪"= 依赖全部处于 `DELIVERED`**。`plan.md:509` 与 `tasks.md` 的 T078 行都写成"依赖全 `DELIVERED` 才放行"，
   所以**不**把 `DELIVERED_WITH_ISSUES` 算作就绪。方向是保守的：那条状态的定义是
   "验收轮次跑满仍未通过"（`data-model.md:303`，FR-035）⇒ 一份已知有问题的交付不该被下游静默接着用。
   代价如实写在这儿：**一条依赖落到"带问题交付"就会卡住它的下游**，出口是人工升级而不是自动放行。
   这条口径由 `test_a_dependency_delivered_with_issues_does_not_unlock_its_successor` 单独钉住 ——
   将来若要改成"两种交付态都算就绪"，需要动的只有那一处集合与那一条用例。
2. **悬空引用不当就绪，也不当"没有依赖"**。`depends_on` 里出现本批没有的卡号时，
   FR-005 本应在拆单阶段就退回重排；真到了这一层，表现是**挡住并点名那条引用**，
   而不是"查不到状态 ⇒ 当作已交付"（那是把引用完整性错误读成放行）。
3. **`SlotHandle` 只在进程内活着**（契约 §6：MUST NOT 被序列化进任务卡、日志、产物或 PG）。
   本节点返回的 `Lease` 里那个 `handle` 字段是** opaque 的进程内对象**：没有 `to_dict()`，
   EN-14 留痕的 `payload` 里也**不写**槽位信息 —— 一写就等于给瞬态层造了第二个持久化面。

**为什么占槽失败与落库失败都要把槽退回去**：FR-021 的上限是按 `usage()` 实时算的，
一个泄漏的槽位就是"永久少一格"，而且没有任何一行库会记载它 —— 这类缺陷不可观察，只能靠顺序防。
所以这里的顺序是"先 acquire 再落库"，并在落库异常时显式 `release`（不是"先落库再 acquire"：
那种写法在 acquire 阻塞期间会让卡显示 `RUNNING` 而实际一格槽都没占，`usage()` 与库两边都在说谎）。
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from acp.orchestrate.nodes.select_worker import AUTOMATED_ACTOR
from acp.persistence import events
from acp.persistence.models_core import TaskCard
from acp.runtime import slot_manager as slot_protocol

NODE = "dispatch"

#: 可被派发的前置卡态。**三个值各自对应状态机上一条真实存在的边**（`data-model.md §2`）：
#: `READY ──调度──▶ RUNNING`（首次派发）、`ARTIFACT_INVALID ──▶ HANDED_OFF ──▶ RUNNING（换人≤1）`
#: （换人之后重投）、`REWORK ──▶ RUNNING`（`:301`，返工之后重投）。
#: 前两条 Phase 6 就在；第三条是 **owner 2026-10-14 待裁 ⑩ 的裁定**（原话："返工后要能回到执行态，
#: 否则状态机断头"）—— 那条边文档一直写着，缺的是这里：`mark_rework` 把卡放到 `REWORK` 之后，
#: 这张卡在本集合里没有入口，`DISPATCHABLE` 就成了状态机上第一处"文档有边、代码无门"的断头。
#: 只收 `READY` 的话，一次换人之后那张卡就**再也没有入口**，同样的道理一次返工之后也是。
#: 别的状态要么正在跑，要么在等人（审查/验收/卡点/升级各归各的触发点）。
DISPATCHABLE = frozenset({"READY", "HANDED_OFF", "REWORK"})
#: 依赖的"就绪"取值 —— 见模块头第 1 条，`DELIVERED_WITH_ISSUES` 不在里面。
DELIVERED = "DELIVERED"
#: 放行后写入的卡态。
STARTED = "RUNNING"


class DispatchMisuse(ValueError):
    """派发的形状不成立（卡不存在 / 状态不对 / 没有执行者 / 依赖没齐 / 落库失败已退槽）。"""

    def __init__(self, message: str, *, code: str = "dispatch_invalid") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CardNeed:
    """派发判定要读的那四列。从 EN-02 搬来，不是第二份事实（`state.py` 模块头那条约束的同一条口径）。"""

    card_id: str
    status: str
    depends_on: tuple[str, ...] = ()
    executor_agent_id: str | None = None


@dataclass(frozen=True)
class Lease:
    """一次占槽的凭据。**只在进程内传递**，字段就三个：卡号、工具、那个 opaque 的 handle。

    刻意不做 `to_dict()`：`handle` 是 `SlotHandle`，任何"连它一起导出"的口子都会变成瞬态数据的落盘面。
    """

    card_id: str
    agent_id: str
    handle: slot_protocol.SlotHandle = field(compare=False, repr=False)

    def describe(self) -> str:
        """只给日志用的一行文本 —— 不含 handle 的任何结构，只有一个"占了谁的槽"的事实。"""
        return f"{self.card_id} 占 {self.agent_id} 的一个槽"


@dataclass(frozen=True)
class Readiness:
    """一批卡的就绪分类。`blocked` 非空就是 FR-025 要的"不静默丢弃"的载体。"""

    ready: tuple[CardNeed, ...]
    blocked: tuple[tuple[str, str], ...]

    def reason_of(self, card_id: str) -> str | None:
        for blocked_id, reason in self.blocked:
            if blocked_id == card_id:
                return reason
        return None


def need_of(card: TaskCard) -> CardNeed:
    return CardNeed(
        card_id=card.id,
        status=str(card.status),
        depends_on=tuple(card.depends_on or ()),
        executor_agent_id=card.executor_agent_id,
    )


def cards_of(session: Session, feature_request_id: uuid.UUID) -> tuple[CardNeed, ...]:
    """读这张需求单下的**全部**卡 —— 就绪判定必须看全集，按卡号升序（判据要可复现）。"""
    rows = (
        session.query(TaskCard)
        .filter(TaskCard.feature_request_id == feature_request_id)
        .order_by(TaskCard.id)
        .all()
    )
    return tuple(need_of(row) for row in rows)


def readiness(cards: Sequence[CardNeed]) -> Readiness:
    """纯判定：哪些 `READY` 卡的依赖全 `DELIVERED`，哪些被挡、各被谁挡。

    非 `READY` 的卡**不进两个列表**：它们不是"等着被派发"，而是根本不在派发这条路上 ——
    把它们混进 `blocked` 会让"阻塞计数"这个数变成"卡总数"，FR-025 要的提示就没法读了。
    """
    by_id = {card.card_id: card for card in cards}
    ready: list[CardNeed] = []
    blocked: list[tuple[str, str]] = []
    for card in sorted(cards, key=lambda c: c.card_id):
        if card.status not in DISPATCHABLE:
            continue
        dangling = [dep for dep in card.depends_on if dep not in by_id]
        if dangling:
            blocked.append((card.card_id, f"依赖引用在本批卡里不存在：{sorted(dangling)}（FR-005 该在拆单就退回）"))
            continue
        unmet = sorted(
            f"{dep}={by_id[dep].status}" for dep in card.depends_on if by_id[dep].status != DELIVERED
        )
        if unmet:
            blocked.append((card.card_id, f"依赖未全部 {DELIVERED}：{unmet}"))
            continue
        ready.append(card)
    return Readiness(ready=tuple(ready), blocked=tuple(blocked))


def dispatch(
    session: Session,
    *,
    feature_request_id: uuid.UUID,
    task_id: str,
    slots,
    created_at: dt.datetime,
    actor_agent_id: str = AUTOMATED_ACTOR,
) -> Lease:
    """判定 → 占槽 → 落库。异常路径上**已经占到的槽一定被退回**（模块头那段理由）。"""
    card = (
        session.query(TaskCard)
        .filter(TaskCard.feature_request_id == feature_request_id, TaskCard.id == task_id)
        .one_or_none()
    )
    if card is None:
        raise DispatchMisuse(f"任务卡 {task_id} 在需求单 {feature_request_id} 下不存在 ⇒ 无卡可派发")
    if str(card.status) not in DISPATCHABLE:
        raise DispatchMisuse(
            f"卡 {task_id} 现在是 {card.status}，不在可派发态 {sorted(DISPATCHABLE)} 里 ⇒ 派发只管已就绪或已换人的卡",
            code="task_not_ready",
        )
    if card.executor_agent_id is None:
        raise DispatchMisuse(
            f"卡 {task_id} 还没有执行者（`SelectWorker` 未填 `executor_agent_id`）⇒ 没有可占槽的工具",
            code="executor_missing",
        )
    agent_id = str(card.executor_agent_id)

    batch = cards_of(session, feature_request_id)
    verdict = readiness(batch)
    if task_id not in {c.card_id for c in verdict.ready}:
        raise DispatchMisuse(
            f"卡 {task_id} 未通过就绪判定：{verdict.reason_of(task_id) or '不在这批 READY 卡里'}",
            code="dependencies_unmet",
        )

    handle = slots.acquire(task_id, agent_id)
    try:
        card.status = STARTED
        events.record(
            session,
            entity="task_card",
            entity_id=task_id,
            kind="TASK",
            actor_agent_id=actor_agent_id,
            payload={
                "node": NODE,
                "action": "dispatched",
                "agent_id": agent_id,
                "card_status_after": card.status,
                "dependencies": sorted(str(dep) for dep in (card.depends_on or ())),
            },
            created_at=created_at,
        )
        session.flush()
    except Exception as exc:
        slots.release(handle)
        raise DispatchMisuse(
            f"卡 {task_id} 落库失败（{type(exc).__name__}）⇒ 已占的槽退回，不留一格谁也说不清的占用",
            code="rolled_back_slot",
        ) from exc
    return Lease(card_id=task_id, agent_id=agent_id, handle=handle)


def release(slots, lease: Lease) -> None:
    """收尾：把槽还给瞬态层。**归调用方（图的运行面）持有**，本模块不自己找时机。

    （**追记 2026-10-16 第 3 项 · 甲**：这里原来写的是"现在没有生产调用方 —— 接线点在 `Dispatch → … →
    ResultIngest` 那条边的装配上（Phase 7 的 T086），登记在 `evidence/phase-06.md` 的未闭清单里"。
    那句作为 T078 那一刻的读数保留，但今天已经不成立：唯一的生产调用点是 `orchestrate/scheduler.py:125`
    （`run_card()` 的 `finally`），而 `run_card()` 自本笔起有生产调用方 `run.py::run_execute()`。
    改这一句的理由不是格式化 —— 留着一句假的"没人调"，下一轮就会把它当成待裁项再报一遍。）
    """
    slots.release(lease.handle)
