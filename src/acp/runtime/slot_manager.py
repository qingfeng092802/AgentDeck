"""T079：`runtime/slot_manager.py` —— 并发槽位（FR-021｜R-05，契约 `contracts/agent-protocol.md §6`）。

三件事各归一处，别混：**`usage()` 是 `agent_unit.current_load` 的唯一权威来源**（瞬态层），
PG 那一列只是上一次读到的快照，MUST NOT 反向当事实源（宪法原则 I 的存储分层）；
平台级上限读 `config/concurrency.yaml`，逐条上限读 EN-03 `max_parallel` —— 两根轴不能互相顶替
（`params-ref §9` 正文那句）。

**`SlotHandle` 只在进程内活着**：契约 §6 明写它 MUST NOT 被序列化进任务卡、日志、产物或 PG
—— 一落盘就等于自造第二层持久化（重启后"谁还占着槽"会变成一份谁也说不清的事实）。
消息侧那半条由 `test_handle_has_no_transient_slot_field` 钉（`protocol.Handle` 不含 `slot` 字段），
本模块这半条由 `tests/contract/test_slot_manager.py` 钉（写入面与替换实现的签名）。

**接口形状按 Redis 目标形状设计**：三个方法名与语义 = 取 / 放 / 读计数，MVP-2 换实现体
**不改签名、调用方零改动** —— 这条不是注释里的承诺，是原则 I 豁免条件②要求的**契约测试**，
落 `tests/contract/test_slot_manager.py`（同一 fixture 在两种实现下双向通过）。

**一处与契约措辞的偏离，已由 owner 裁决关闭（2026-10-14，Phase 6 待裁 1）**：§6 原文写"MVP-1 用进程内
**异步**信号量实现"，本实现是同步的（`threading.Condition`），理由是调用方 `Dispatch` 与选人是**同步图节点**：
asyncio 信号量在没有事件循环的调用栈里 `acquire()` 会直接 `RuntimeError`，而 LangGraph 的节点函数就是普通同步调用。
裁决结果 = **改契约措辞、不动代码**（`contracts/agent-protocol.md §6` 与 `research.md` D-3 已同步为
"进程内信号量，实现形态由调用方的同步性决定"）；签名与语义一个都没动（`acquire / release / usage` 三个方法），
所以豁免条件②那句"替换实现不改调用方签名"照样成立。**把图节点改成 async 不是本轮的选项**，本模块不再自行改调用链形状。
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import yaml

from acp.registry import loader

#: 平台级上限的唯一致源（本模块内没有任何兜底数字）。
CONFIG_PATH = loader.REPO_ROOT / "config" / "concurrency.yaml"

#: 清单允许的键集合。多一个键就是"没人读它，改它不红"的死配置 ⇒ 载入即拒（口径同 params-ref §9 的围栏）。
ALLOWED_KEYS: frozenset[str] = frozenset({"max_parallel_tasks"})


class SlotConfigError(ValueError):
    """配置或调用形状不对：清单读不出、上限不是正整数、给没申报的 agent 占槽、重复占同一个任务。"""


@dataclass(frozen=True)
class SlotHandle:
    """一次占槽的凭证。字段只有"谁在哪个任务上占着"，**没有任何可持久化标识**（没有租约 id、没有过期时刻）。

    刻意不做 `to_dict()` / `as_dict()` 这类方法：一旦有现成的序列化口子，"不落盘"就只靠纪律守着。
    """

    task_id: str
    agent_id: str


class SlotManager(Protocol):
    """契约 §6 的三个方法。名字与顺序都不许改 —— MVP-2 换 Redis 实现体时调用方要零改动。"""

    def acquire(self, task_id: str, agent_id: str) -> SlotHandle: ...

    def release(self, handle: SlotHandle) -> None: ...

    def usage(self) -> dict[str, int]: ...


def load_platform_limit(path: Path | None = None) -> int:
    """读 `config/concurrency.yaml` 的平台级上限。形状判据全在这里，坏值进不了内存。"""
    raw = yaml.safe_load((path or CONFIG_PATH).read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise SlotConfigError("concurrency.yaml 顶层必须是映射（现在是空文件或标量）")
    unknown = sorted(set(raw) - ALLOWED_KEYS)
    if unknown:
        raise SlotConfigError(
            f"concurrency.yaml 出现了没人读的键 {unknown}；允许的只有 {sorted(ALLOWED_KEYS)} —— "
            "写一个没有消费者的键等于造一份改了不红的第二清单"
        )
    missing = sorted(ALLOWED_KEYS - set(raw))
    if missing:
        raise SlotConfigError(f"concurrency.yaml 缺键 {missing}（本模块没有兜底默认值）")
    limit = raw["max_parallel_tasks"]
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise SlotConfigError(f"max_parallel_tasks 必须是正整数，读到 {limit!r}")
    return int(limit)


class InProcessSlotManager:
    """进程内实现：**一根锁 + 一个条件变量**，同时管两根轴（逐条 `max_parallel` 与平台级上限）。

    为什么不是"N 个信号量"：逐条与平台级两处各自 wait 会形成嵌套等待，那才是真的会死锁；
    单锁 + `Condition` 的等待谓词只看一个状态快照，"一个任务只占一个槽"这条不变式一守，
    占槽方之间就不存在循环等待（FR-021 后半句"MUST NOT 因此死锁"的可执行形态就是这一句）。
    """

    def __init__(self, *, per_agent_max_parallel: Mapping[str, int], platform_limit: int) -> None:
        if not per_agent_max_parallel:
            raise SlotConfigError("候选表为空 ⇒ 没有任何工具能占槽，acquire 会永久等待；这是装配错误")
        if platform_limit <= 0:
            raise SlotConfigError(f"platform_limit 必须是正整数，读到 {platform_limit!r}")
        self._caps: dict[str, int] = {}
        for agent_id, cap in per_agent_max_parallel.items():
            if isinstance(cap, bool) or not isinstance(cap, int) or cap <= 0:
                raise SlotConfigError(f"{agent_id} 的 max_parallel 必须是正整数，读到 {cap!r}")
            self._caps[agent_id] = cap
        self._platform_limit = platform_limit
        self._holds: dict[str, list[str]] = {agent_id: [] for agent_id in self._caps}
        self._by_task: dict[str, SlotHandle] = {}
        self._condition = threading.Condition()

    @classmethod
    def from_config(cls, per_agent_max_parallel: Mapping[str, int], path: Path | None = None) -> InProcessSlotManager:
        """平台级上限只从 `concurrency.yaml` 读 —— 调用方拿不到"传个数字进来"的口子（防第二处口径）。"""
        return cls(per_agent_max_parallel=per_agent_max_parallel, platform_limit=load_platform_limit(path))

    def acquire(self, task_id: str, agent_id: str) -> SlotHandle:
        if agent_id not in self._caps:
            raise SlotConfigError(
                f"{agent_id} 没有 `max_parallel` 申报 ⇒ 不许按默认值给它开槽"
                "（FR-011 把 max_parallel 列为逐条必填，运行时也不替它补）"
            )
        with self._condition:
            if task_id in self._by_task:
                raise SlotConfigError(
                    f"任务 {task_id} 已占着 {self._by_task[task_id].agent_id} 的一个槽 ⇒ 不许重复占；"
                    "占两个槽会让同时执行数被虚报，FR-021 的上限就只是装饰"
                )
            while True:
                free_agent = len(self._holds[agent_id]) < self._caps[agent_id]
                free_platform = len(self._by_task) < self._platform_limit
                if free_agent and free_platform:
                    break
                self._condition.wait()
            handle = SlotHandle(task_id=task_id, agent_id=agent_id)
            self._holds[agent_id].append(task_id)
            self._by_task[task_id] = handle
            return handle

    def release(self, handle: SlotHandle) -> None:
        with self._condition:
            current = self._by_task.get(handle.task_id)
            if current is None or current.agent_id != handle.agent_id:
                raise SlotConfigError(
                    f"释放了一个不存在的槽（任务 {handle.task_id} 在 {handle.agent_id} 上没占着）⇒ "
                    "多半是有人自己造了 handle，或释放了两次"
                )
            del self._by_task[handle.task_id]
            self._holds[handle.agent_id].remove(handle.task_id)
            self._condition.notify_all()

    def usage(self) -> dict[str, int]:
        """`agent_id -> 当前占用数`。**已申报但空闲的 agent 也在字典里**（值为 0）——
        只列忙碌者会让调用方把"没人"与"没读到"读成同一件事。"""
        with self._condition:
            return {agent_id: len(task_ids) for agent_id, task_ids in self._holds.items()}

    @property
    def platform_in_use(self) -> int:
        with self._condition:
            return len(self._by_task)
