"""T086 前置（owner 2026-10-14 裁 P6-P1-1）：一张卡的**生命周期装配点** —— 谁借的槽，谁来还。

**为什么要有这个模块**：`Dispatch`（T078）在开工前占一格槽，`dispatch.release()` 那一句从 Phase 6 起
就写好了却**没有任何生产调用方**（复算 `grep -rn "\\.release(" src/ --include=*.py` 只命中 `dispatch.py` 自己
模块内的两处），于是"槽只能借不能还"：平台上限那一个数一旦用完，第 4 张卡起 `acquire()` 就永久阻塞
（`slot_manager.py` 的 `Condition.wait()` 无超时）。owner 的处置是"在连接执行段图到主管图时接上"——
本模块就是那个"接上"的**落点形状**：它把「占槽 → 跑卡级那半条链 → 退槽」三段收在一处，
T123 把主管图与执行段接起来时调的是这里，不是各节点自己。

**为什么退槽不放进 `ExecutionDeps`**：`graph.py` 里那份依赖面的 docstring 已经给过理由 ——
"把 slot manager 塞进回投段，就会出现'门禁自己放槽'这种谁都不认的收尾"。本模块不改那条口径，
而是把它兑现：槽由 `Dispatch` 借、由**装配方**（这里）还，门禁段与回投段都不知道槽的存在。

**为什么退槽写在 `finally` 而不是成功路径末尾**：卡级段会抛（落库失败、门禁判不完、出边上的节点报错），
而"抛了就不退"恰好是 owner 点名要防的那件事 —— 一次失败泄漏一格槽，跑几次就把并发上限吃光，
表现为"系统卡住"而不是"哪里报错"。`finally` 是这里唯一诚实的形状。

**【owner 2026-10-16 裁决 1 选甲：占槽上移到图节点，退槽留在这里】**
`ASSIGN_GUARDS → DISPATCH` 那条边补上之后，`dispatch()` 在**生产里只剩一个调用点** = 图上的 `DISPATCH`
节点（两处各派一次就是原则 I 违例：两份就绪判定会漂）。于是本模块的 `run_card()` 不再自己占槽，
改成**从进程内台账 `LeaseLedger` 领回那张卡已经占到的槽**，跑段，再在 `finally` 里退。三条口径没有动过：
① 退槽仍在 `finally`；② 退完仍读一次 `usage()`，`SlotLeak` 那句复查仍是死代码的反证；
③ `SlotHandle` 仍不进图 state、不落库、不进日志（契约 §6）—— 台账是**进程内对象**，
它被传进图节点的参数里、又被传回 `run_card()`，中间没有任何一次序列化。
"借与还在同一处"这一条从"同一函数"降级成"同一个台账"，是这次裁决的代价，如实写在这里而不是抹平；
换来的是 (a) 图上那条边存在、(b) 生产里 `dispatch()` 恰一处、(c) 本函数仍有生产调用方。
（**追记 2026-10-16 第 3 项 · 甲**：上面那句 (c) 在 `3c5ca6d` 那一刻是**空头**——当时 `run_card()` 的
生产调用方为 0，`evidence/phase-12.md §七` C3 的读数点名的就是这一格。今天它才成立：调用方 =
`run.py::run_execute()`。留着一句当时兑现不了的宣称，下一轮就会把它当成已完成去接别的东西，
所以补这一句而不是把它抹掉。）

**【2026-10-17 乙 轮：入参从 `result` 换成 `produce`】** 执行段开始真调 `adapter.submit()` / `collect()` 之后，
"拿结果"这件事本身会变成一次会抛错的动作。它必须落在借与还之间，否则失败那一发没人退槽 ——
理由与那条"装配点不许自己退槽"的口径是同一件事的两面，全文写在 `run_card()` 的 docstring 里。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import uuid

from acp.orchestrate.nodes import dispatch as dp
from acp.orchestrate.state import ExecutionState
from acp.protocol.messages import TaskResult

NODE = "scheduler"


class MissingLease(RuntimeError):
    """台账里没有这张卡的槽 ⇒ 派发那一步没跑过（或跑在别的进程里）。

    这条不是防御性冗余：占槽上移之后，"段跑了而槽根本没占"是**可达的装配缺陷** ——
    图上 `DISPATCH` 节点被摘掉、或运行段被单独调用时都会落到这里。静默跳过等于让平台上限失效。
    """


class SlotLeak(RuntimeError):
    """退槽之后**这一张卡那一格**没少 ⇒ 借与还不是一对，装配点必须响亮失败。

    口径（2026-10-16 第 3 项改定，原文"瞬态层仍报有占用"是整层口径，会把并发下的正常收尾判成泄漏）：
    比较的是同一支工具退槽前后的差额必须恰为 1。这条不是"防御性编程"：`SlotManager.release()`
    对陌生 handle 是静默的（它还不出不属于它的槽），所以"退了但没退干净"在退槽调用本身上看不出来，
    只有在**退之前后各读一次 `usage()`** 才暴露。
    """


class LeaseLedger:
    """进程内的借还交接面：图节点 `put`，`run_card()` `take`。

    刻意**不是一个 dict 的对外视图**，也没有 `to_dict()` / `items()`：唯一的出路是 `take(task_id)`，
    于是"把 handle 连同别的读数一起导出"那种形状在本类型上不可表示（契约 §6）。
    """

    def __init__(self) -> None:
        self._entries: dict[str, dp.Lease] = {}

    def put(self, lease: dp.Lease) -> None:
        self._entries[lease.card_id] = lease

    def take(self, task_id: str) -> dp.Lease:
        try:
            return self._entries.pop(task_id)
        except KeyError:
            raise MissingLease(
                f"卡 {task_id} 在台账里没有槽 ⇒ 图上 `DISPATCH` 那一步没跑过，"
                "运行段不许在没有占槽的情况下执行"
            ) from None

    def pending(self) -> tuple[str, ...]:
        """只给判据与日志用的**卡号**集合，不含 handle。"""
        return tuple(sorted(self._entries))


@dataclass(frozen=True)
class RunOutcome:
    """一次卡运行的收尾读数。`usage_after` 是瞬态层的全量占用（权威口径，见契约 §6）。

    `feature_request_id` 一起带出来：EN-02 的主键是 `(需求单, 卡号)`，只回卡号的读数
    到了两支需求单里就指不定是哪张卡 —— 与 `cost/budget.py` 那条"复合主键"的口径同一件事。
    """

    feature_request_id: uuid.UUID
    card_id: str
    agent_id: str
    segment_result: ExecutionState
    usage_after: dict[str, int]


def run_card(
    *,
    feature_request_id: uuid.UUID,
    task_id: str,
    slots,
    leases: LeaseLedger,
    produce: Callable[[], TaskResult],
    segment: Callable[[ExecutionState], ExecutionState],
) -> RunOutcome:
    """从台账领回这张卡已占的槽 → 干活 → 运行卡级那半条链 → **无论如何**退槽。

    `segment` 是卡级那半条链（`graph.build_execution_graph(...)` 编译体的 `invoke`）：
    这里不认识它里面有什么节点，只负责"进来的时候槽是占着的、出去的时候必须是空的"。
    不认识段内形状是刻意的 —— 段内加了节点（Phase 7 的 `Review` / `Accept` / `Assemble`）
    不该逼这一层跟着改，否则生命周期就有两个地方各管一半。

    `produce` 是"**这一发真正干活那一步**"（把卡组装成 `Task`、`submit()` 出去、`collect()` 回一份
    `TaskResult`）而不是那份结果本身。2026-10-17 乙 轮从 `result: TaskResult` 改过来的理由只有一条：
    写这一步**会失败**（仿真侧那支 `expected_files()` 认不出交付物名就要抛 `StubMisuse`），而失败发生在
    `run_card()` 之前 ⇒ 槽已经借出去、`finally` 还没开始 ⇒ 那一格永久挂在瞬态层里，而装配点又被
    owner 2026-10-16 裁 甲 明令**不许自己退槽**（"装配点自己退一次，就会出现段抛了而两边都退"）。
    改成传一个 callable，就等于把"干活"这一段挪进借与还之间 —— 生命周期层仍不认识段内形状，
    它只多认识"结果是要现产的"这一件事。

    领槽放在**进 `try` 之前**：台账里没有槽就是装配缺陷（图上 `DISPATCH` 没跑过），
    那种情况下退槽无从谈起，也不该假装跑过一段 —— `MissingLease` 直接抛出去。
    """
    lease = leases.take(task_id)
    try:
        segment_result = segment(
            {
                "feature_request_id": feature_request_id,
                "task_id": task_id,
                "result": produce(),
            }
        )
    finally:
        # **退槽前先把这一支工具的读数抄一份**：哨兵要比的是"这一格有没有少 1"，不是"整层空不空"。
        # 这里有两重会踩的错，都踩过：
        # ① `usage()` 的形状是"每支在册工具各一格，空闲也占一个键"（契约 §6），空槽时它是
        #    `{"qoder": 0, "workbuddy": 0, "doubao": 0}` —— 非空但全零，所以 `if usage:` 会把每一次
        #    正常收尾都判成泄漏（上一轮撞的就是这一重）。
        # ② 更重的一重：**别的卡合法占着的槽也会被"整层归零"这个口径算成泄漏**。一张卡跑完时台账里
        #    还有另一张卡没跑完，是并发运行的常态而不是缺陷；按①的写法每一次这样的收尾都会抛 `SlotLeak`，
        #    等于把平台上限 3 变成"同一次只能有一张卡在飞"。这一重是 2026-10-16 第 3 项那条
        #    "跑完一张不许退别人那一格"的判据当场撞出来的（`tests/guards/test_run_execute.py`）。
        # 这一句故意放在 `finally` 里：段内报错与槽没退干净同时发生时，**后者更该被看到**
        # （前者是一次任务的失败，后者是"后面所有卡都会静默阻塞"的故障源）。
        # 原异常不丢 —— Python 把它挂在 `SlotLeak.__context__` 上，traceback 里两段都在。
        before = dict(slots.usage())
        dp.release(slots, lease)
        usage = dict(slots.usage())
        expected = before.get(lease.agent_id, 0) - 1
        if usage.get(lease.agent_id) != expected:
            raise SlotLeak(
                f"卡 {task_id} 退槽后 {lease.agent_id} 的占用是 {usage.get(lease.agent_id)}，退之前是 "
                f"{before.get(lease.agent_id)} ⇒ 借与还不是一对（那一格该少 1），"
                "后面的卡会把平台上限吃光后静默阻塞"
            )
    return RunOutcome(
        feature_request_id=feature_request_id,
        card_id=lease.card_id,
        agent_id=lease.agent_id,
        # `segment` 的声明就是 `ExecutionState -> ExecutionState`；再套一次 `dict(... or {})`
        # 会把类型退化成 `dict[str, object]`，也让"段返回了 None"这个**做不到**的事
        # 看起来被兜住了。真返回 None 时下一跳会直接 AttributeError，比悄悄变成空字典好查。
        segment_result=segment_result,
        usage_after=dict(slots.usage()),
    )
