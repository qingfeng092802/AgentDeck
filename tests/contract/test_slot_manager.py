"""T079 / T080：`SlotManager` 的契约面判据（`contracts/agent-protocol.md §6`｜FR-021 / FR-011，R-05，宪法「三」+ 原则 I）。

档位：`tests/contract/` ⇒ fast 一档，不碰 PG、不碰网络、**不碰真 Redis**（`pyproject.toml` 里本来也不许有它，
见 `test_pyproject_declares_no_redis_client`）。

这一档问的问题和 `tests/guards/` 那一档不一样，别混：guards 问"进程内实现自己有没有做错"，
这里问的是**接口面能不能换身体** —— 宪法 v1.0.2 原则 I 的豁免条件②要的是"替换实现不改调用方签名"
的**证明**，所以下面每条正向判据都在**两种实现体**上跑同一个场景函数
（`in_process` = `src/acp/runtime/slot_manager.py`；`hash_table` = 本文件里那个第二身体）。

第二身体为什么这么造（不是为了凑数）：
- 内部数据结构**零共享**：进程内实现记的是"每个 agent 一串任务"（`_holds`），第二身体记的是
  Redis hash 的形状 —— 一张 `task_id -> agent_id` 全局表，占用数靠扫表得出；
- 等待机制也不同：前者是条件变量，后者是**轮询重读那张表**；真实 Redis 客户端在本进程里没有
  可等的条件变量，只能重读，所以这才是 MVP-2 的写实形状；
- 只共享两个词汇：`SlotHandle` 与 `SlotConfigError`。这不是偷懒，是契约面本身的一部分 ——
  调用方 `except` 的类型和 `release()` 接受的参数类型若跟着实现体变，"零改动"就是空话。
⇒ 于是"同一场景两个身体都绿"证明的是：**判据没押在任何一种内部表示或某一种等待机制上**。押上了就有一个身体红。

反向对照（缺任何一条，这批判据就可能是恒红或恒绿）：
- 四条 `_Saboteur*` 是**故意做错**的实现体，各破坏 §6 的一句话，跑同一批场景函数 ⇒ 必须红
  （证明场景函数有牙，而不是"换了实现体照样绿"的那种假符合性）；两根轴各破坏一次，
  只破坏一根的话"另一根轴的绿"从没被证明过；
- 阻塞类判据两侧都查：满的时候真的挡（`test_each_axis_blocks_when_only_it_is_full`），
  放宽那一根之后同样的形状立刻放行（`test_raising_the_binding_axis_lets_the_same_shape_through`）；
- 形状判据各带一条 GREEN 对照（只改注释 / 只加一个无关依赖 ⇒ 不许红）；
- 场景函数一律用裸 `assert`、不用 `pytest.raises` ⇒ 破坏点表现为可读的 `AssertionError`，
  而不是 `DID NOT RAISE`（后者在"证明有牙"那三条里会逃过我自己的外层断言）。

T127 / T128（owner 2026-10-17 授权）加在**节 五·补**四条：解释器钉值的字面断言、`plan.md` 对同一字面的引用、
豁免登记行 ③ 那句"不含 redis"与依赖面扫描的**双向**符合性、以及 ② 那句点名文件的活性对照（点名的就是本文件）。
⇒ 本文件从"T079 / T080 的契约面"升格为**原则 I 豁免条件 ②③ 的取证入口**：那两格的关闭读数只在这里取，
别处（含 `plan.md` 的 Constitution Check 表）都是对它的引用，一致性由上面第三条判据负责。
"""

from __future__ import annotations

import dataclasses
import inspect
import re
import sys
import threading
import time
import tomllib
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml
from sqlalchemy import Column, Integer, MetaData, String, Table

from acp.guard import naming
from acp.persistence import models_core, models_registry  # noqa: F401  —— 摘 sys.modules 不算真拔：表要靠 import 才进 metadata
from acp.persistence.base import Base
from acp.protocol import TaskResult
from acp.review import policy as review_policy
from acp.runtime import slot_manager as sm

REPO_ROOT = Path(__file__).resolve().parents[2]
PARAMS_REF = REPO_ROOT / "config" / "params-ref.md"
AGENTS_YAML = REPO_ROOT / "src" / "acp" / "registry" / "agents.yaml"

#: 场景里"已申报的 agent"用这两个名字，与 `agents.yaml` 的真实 `agent_id` 无关 ——
#: 契约面只认"调用方给的是一个声明过的 id"，把真实 id 抄进场景就等于又多抄了一份清单。
DECLARED = ("alpha", "beta")


class HashTableSlotManager:
    """第二实现体（**只在测试里**，不进 `src/`）：占用记成一张 `task_id -> agent_id` 的全局表。

    与 `InProcessSlotManager` 的三点刻意不同，就是为了给"双向通过"提供真正的对照面：
    ① 数据结构（全局 hash vs 逐 agent 列表）；② 计数方式（扫表 vs 维护列表长度）；
    ③ 等待方式（轮询重读 vs 条件变量）。
    它另加一条进程内实现没有的自保：等待超过 `wait_deadline` 秒就抛 —— 真实 Redis 侧拿不到槽
    就是拿不到，一直转圈不是契约要求。场景里的最坏等待远小于它，所以不破坏双向性；
    反过来它让"死锁"表现为一条可读的失败，而不是把整个测试会话挂死。
    """

    def __init__(
        self,
        *,
        per_agent_max_parallel: dict[str, int],
        platform_limit: int,
        poll_seconds: float = 0.004,
        wait_deadline: float = 20.0,
    ) -> None:
        if not per_agent_max_parallel:
            raise sm.SlotConfigError("逐条 max_parallel 声明表为空 ⇒ 没有任何工具能占槽")
        if platform_limit <= 0:
            raise sm.SlotConfigError(f"platform_limit 必须是正整数，读到 {platform_limit!r}")
        self._caps: dict[str, int] = {}
        for agent_id, cap in per_agent_max_parallel.items():
            if isinstance(cap, bool) or not isinstance(cap, int) or cap <= 0:
                raise sm.SlotConfigError(f"{agent_id} 的 max_parallel 必须是正整数，读到 {cap!r}")
            self._caps[agent_id] = cap
        self._limit = platform_limit
        self._leases: dict[str, str] = {}
        self._lock = threading.Lock()
        self._poll = poll_seconds
        self._deadline = wait_deadline

    def _count(self, agent_id: str) -> int:
        return sum(1 for holder in self._leases.values() if holder == agent_id)

    def acquire(self, task_id: str, agent_id: str) -> sm.SlotHandle:
        if agent_id not in self._caps:
            raise sm.SlotConfigError(f"{agent_id} 未申报 max_parallel ⇒ 拒绝按默认值给它开槽")
        waited = 0.0
        while True:
            with self._lock:
                holder = self._leases.get(task_id)
                if holder is not None:
                    raise sm.SlotConfigError(f"任务 {task_id} 已占 {holder} 的一个槽 ⇒ 拒绝重复占槽")
                if self._count(agent_id) < self._caps[agent_id] and len(self._leases) < self._limit:
                    self._leases[task_id] = agent_id
                    return sm.SlotHandle(task_id=task_id, agent_id=agent_id)
            time.sleep(self._poll)
            waited += self._poll
            if waited >= self._deadline:
                raise sm.SlotConfigError(
                    f"任务 {task_id} 等待槽位超过 {self._deadline}s 仍未拿到 —— 这就是 FR-021 禁止的死锁形状"
                )

    def release(self, handle: sm.SlotHandle) -> None:
        with self._lock:
            holder = self._leases.get(handle.task_id)
            if holder != handle.agent_id:
                raise sm.SlotConfigError(
                    f"放槽被拒：任务 {handle.task_id} 当前不在 {handle.agent_id} 上（表里读到 {holder!r}）"
                )
            del self._leases[handle.task_id]

    def usage(self) -> dict[str, int]:
        with self._lock:
            return {agent_id: self._count(agent_id) for agent_id in self._caps}


IMPLS = ("in_process", "hash_table")


def make(name: str, *, caps: dict[str, int], platform_limit: int):
    """两种实现体的唯一入口：形状一样（逐条 `max_parallel` + 平台上限），身体不同。"""
    if name == "in_process":
        return sm.InProcessSlotManager(per_agent_max_parallel=caps, platform_limit=platform_limit)
    if name == "hash_table":
        return HashTableSlotManager(per_agent_max_parallel=caps, platform_limit=platform_limit)
    raise AssertionError(f"未知实现体 {name!r} ⇒ 双向通过的前提没了")


class Waiter:
    """在别的线程里试着占一个槽，用"它在多长时间内没返回"来读**阻塞**这件事。

    阻塞本身没法直接断言，能断言的是：给了跑完的机会之后，它跑完了没有。
    """

    def __init__(self, mgr, task_id: str, agent_id: str) -> None:
        self._mgr = mgr
        self.task_id = task_id
        self.agent_id = agent_id
        self._outcome: list[object] = []
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        try:
            self._outcome.append(self._mgr.acquire(self.task_id, self.agent_id))
        except BaseException as exc:  # 实现体抛什么都算"这一趟没拿到槽"
            self._outcome.append(exc)

    def start(self) -> Waiter:
        self._thread.start()
        return self

    def finished(self, timeout: float) -> bool:
        self._thread.join(timeout)
        return not self._thread.is_alive()

    @property
    def outcome(self) -> object:
        return self._outcome[0] if self._outcome else None


def assert_refused(exc_type: type[BaseException], names: str, call: Callable[[], object]) -> None:
    """裸 assert 版的"该拒并且要点名"：给场景函数用，免得 `DID NOT RAISE` 逃出外层断言。"""
    try:
        call()
    except exc_type as exc:
        assert names in str(exc), f"报错要点名 {names!r}，实际读到：{exc}"
        return
    except Exception as exc:  # 抛错类型不对 = 调用方 catch 不到，同样是契约破坏
        raise AssertionError(f"该拒的拒了，但抛的是 {type(exc).__name__}（调用方 catch {exc_type.__name__} 接不住）：{exc}") from exc
    raise AssertionError(f"该拒的没拒：期望 {exc_type.__name__}")


# —— 场景函数：正向判据与"破坏点必须红"共用同一份正文 ——


def check_usage_lists_every_declared_agent(mgr, *, declared=DECLARED) -> None:
    """空闲的 agent MUST 出现在 `usage()` 里（值为 0）。

    只列"此刻占着槽的"会让调用方把"没读到"和"是 0"混成同一件事，而 `usage()` 是
    `agent_unit.current_load` 的唯一权威来源 ⇒ 那一列缺键时，快照会被写成一个凭空多出来的数。
    """
    usage = mgr.usage()
    assert set(usage) == set(declared), f"键集要等于已申报清单：读到 {sorted(usage)}，期望 {sorted(declared)}"
    assert all(isinstance(v, int) for v in usage.values()), usage
    assert sum(usage.values()) == 0, usage


def check_roundtrip_and_counts(mgr) -> None:
    first = mgr.acquire("T01", "alpha")
    second = mgr.acquire("T02", "alpha")
    assert isinstance(first, sm.SlotHandle) and (first.task_id, first.agent_id) == ("T01", "alpha")
    assert mgr.usage()["alpha"] == 2, mgr.usage()
    mgr.release(first)
    assert mgr.usage() == {"alpha": 1, "beta": 0}, mgr.usage()
    mgr.release(second)
    assert mgr.usage() == {"alpha": 0, "beta": 0}, mgr.usage()


def check_second_acquire_blocks_then_passes(mgr, *, second_agent: str, binding_axis: str) -> None:
    """满的时候真的挡，release 之后真的放 —— 挡人的那一根由 `binding_axis` 点名。

    形状上的关键：`second_agent` 与首次占槽同一个 agent 时，考验的是 `per_agent`
    （逐条 `max_parallel`，FR-011）；换一个 agent 时逐条那一根明确富余，挡住的只可能是
    `platform`（`concurrency.yaml` 的上限，FR-021）。两根轴互相顶替（读错一根）就在这里红。
    """
    first = mgr.acquire("T01", "alpha")
    blocked = Waiter(mgr, "T02", second_agent).start()
    assert not blocked.finished(0.25), (
        f"{binding_axis} 这一轴本该挡住第二次占槽，却放行了 ⇒ 上限没生效（usage={mgr.usage()}）"
    )
    mgr.release(first)
    assert blocked.finished(2.0), f"腾出槽后 {binding_axis} 的等待者应在 2s 内拿到槽；outcome={blocked.outcome!r}"
    assert isinstance(blocked.outcome, sm.SlotHandle), blocked.outcome
    mgr.release(blocked.outcome)


def check_second_acquire_passes_immediately(mgr, *, second_agent: str, binding_axis: str) -> None:
    """上一条的 GREEN 一侧：把**满的那一根**放宽，同一个形状就该当场放行，一次都不用等。

    只查挡不查放，"任何第二次占槽一律被拒"那种坏实现会一路绿到底；两条合起来才把
    "上限正好是那个数"钉住，而不是"上限存在"。
    """
    first = mgr.acquire("T01", "alpha")
    waiter = Waiter(mgr, "T02", second_agent).start()
    assert waiter.finished(0.5), f"放宽 {binding_axis} 之后仍在等 ⇒ 挡人的不是这一根：outcome={waiter.outcome!r}"
    assert isinstance(waiter.outcome, sm.SlotHandle), waiter.outcome
    mgr.release(first)
    mgr.release(waiter.outcome)


def check_refusals_keep_the_table_intact(mgr) -> None:
    """三种被拒的调用：重复占同一任务、放一个没占着的 handle、给未申报的 agent 开槽。

    每条都同时查"报错了""点名了是谁""槽位表没被改动"—— 只查前一半的话，
    一个"先把计数加上去再抛"的实现体照样绿，而那是会漏槽的那种坏法。
    """
    held = mgr.acquire("T01", "alpha")
    baseline = mgr.usage()
    assert_refused(sm.SlotConfigError, "T01", lambda: mgr.acquire("T01", "alpha"))
    assert mgr.usage() == baseline, mgr.usage()
    assert_refused(
        sm.SlotConfigError, "T404", lambda: mgr.release(sm.SlotHandle(task_id="T404", agent_id="alpha"))
    )
    assert mgr.usage() == baseline, mgr.usage()
    assert_refused(sm.SlotConfigError, "gamma", lambda: mgr.acquire("T02", "gamma"))
    assert mgr.usage() == baseline, mgr.usage()
    assert_refused(sm.SlotConfigError, "T01", lambda: (mgr.release(held), mgr.release(held)))
    assert mgr.usage() == {"alpha": 0, "beta": 0}, mgr.usage()


# —— 一、双向符合性：同一场景在两种实现体下都绿（豁免条件②的正面取证）——


@pytest.mark.parametrize("impl", IMPLS)
def test_the_three_method_signatures_are_identical_across_implementations(impl):
    """§6 的三个方法名 + 参数名 + 标注，两个身体一字不差 ⇒ "调用方零改动"是可查的。"""
    mgr = make(impl, caps={a: 2 for a in DECLARED}, platform_limit=3)

    def surface(sig: inspect.Signature, *, drop_self: bool) -> tuple:
        params = list(sig.parameters.values())[1:] if drop_self else list(sig.parameters.values())
        return tuple((p.name, p.annotation, p.kind) for p in params), sig.return_annotation

    for method in ("acquire", "release", "usage"):
        # `eval_str=True`：两个文件各自开了 PEP 563，标注在磁盘上是字符串，而字符串前缀跟着
        # 各自的 import 写法变（`SlotHandle` vs `sm.SlotHandle`）。比解析后的真类型，
        # 不然这条会去比"两个文件怎么写 import"，那不是契约面。
        got = surface(inspect.signature(getattr(mgr, method), eval_str=True), drop_self=False)
        want = surface(inspect.signature(getattr(sm.SlotManager, method), eval_str=True), drop_self=True)
        assert got == want, f"{impl}.{method}：调用面 {got} ≠ 契约面 {want}"


@pytest.mark.parametrize("impl", IMPLS)
def test_the_two_bodies_are_really_two_bodies(impl):
    """双向性的前提：第二身体不是第一身体的子类，也不是别名。

    否则"两个都绿"只是把同一份逻辑跑了两遍，什么也没证明。`make` 是唯一入口，改一处就红。
    """
    mgr = make(impl, caps={a: 2 for a in DECLARED}, platform_limit=3)
    assert type(mgr).__module__ == (__name__ if impl == "hash_table" else sm.__name__)
    other = sm.InProcessSlotManager if impl == "hash_table" else HashTableSlotManager
    assert not isinstance(mgr, other), f"{impl} 的身体居然是另一个身体的实例 ⇒ 两个身体塌成一个"
    assert HashTableSlotManager.__mro__[1] is object, "第二身体不许继承进程内实现"


@pytest.mark.parametrize("impl", IMPLS)
def test_usage_lists_declared_agents_and_counts_track_acquires(impl):
    mgr = make(impl, caps={a: 2 for a in DECLARED}, platform_limit=3)
    check_usage_lists_every_declared_agent(mgr)
    check_roundtrip_and_counts(mgr)
    assert mgr.usage() == {"alpha": 0, "beta": 0}, mgr.usage()


@pytest.mark.parametrize(
    ("binding_axis", "caps", "limit", "second_agent"),
    [("per_agent", {"alpha": 1, "beta": 3}, 5, "alpha"), ("platform", {"alpha": 3, "beta": 3}, 1, "beta")],
)
@pytest.mark.parametrize("impl", IMPLS)
def test_each_axis_blocks_when_only_it_is_full(impl, binding_axis, caps, limit, second_agent):
    """FR-011 与 FR-021 是两根不同的轴：满的那根挡人，另一根在这一趟明确富余。"""
    check_second_acquire_blocks_then_passes(
        make(impl, caps=caps, platform_limit=limit), second_agent=second_agent, binding_axis=binding_axis
    )


@pytest.mark.parametrize(
    ("binding_axis", "caps", "limit", "second_agent"),
    [("per_agent", {"alpha": 2, "beta": 3}, 5, "alpha"), ("platform", {"alpha": 3, "beta": 3}, 2, "beta")],
)
@pytest.mark.parametrize("impl", IMPLS)
def test_raising_the_binding_axis_lets_the_same_shape_through(impl, binding_axis, caps, limit, second_agent):
    """有牙的判据两侧都得查：挡（上一条）与不挡（这一条）。只查挡 ⇒ "任何第二次占槽都被拒"也一路绿。"""
    check_second_acquire_passes_immediately(
        make(impl, caps=caps, platform_limit=limit), second_agent=second_agent, binding_axis=binding_axis
    )


@pytest.mark.parametrize("impl", IMPLS)
def test_refused_calls_leave_the_slot_table_untouched(impl):
    check_refusals_keep_the_table_intact(make(impl, caps={a: 2 for a in DECLARED}, platform_limit=3))


@pytest.mark.parametrize("impl", IMPLS)
def test_contention_finishes_without_deadlock_and_frees_every_slot(impl):
    """FR-021 的"不得死锁"跑成真并发：6 条线程分 12 张卡，平台上限 3，逐条上限 1/2/2。

    每次 `acquire()` 之后立刻读一次 `usage()`，查两根轴都没被越过。这里断言写成**上界**是刻意的：
    别的线程刚放槽只会让读数偏小，偏大才是实现体的错。

    **这一条证明到哪儿要说清楚**：它是"跑一遍看会不会卡死 + 采样看有没有越界"，
    采样读数是机会性的，不是保证 —— 实测把逐条守卫摘掉（MU-E2）时，红的是那两条阻塞用例，
    这一条照常绿（12 张卡 / 6 线程的形状里两个 alpha 没在同一瞬间重叠上）。
    所以"逐条上限真的生效"的权威判据是 `test_each_axis_blocks_when_only_it_is_full`，
    本条负责的是"不卡死 + 收尾归零"。
    """
    caps = {"alpha": 1, "beta": 2, "gamma": 2}
    mgr = make(impl, caps=caps, platform_limit=3)
    tasks = [f"T{i:02d}" for i in range(12)]
    agents = list(caps)
    overruns: list[str] = []
    done: list[str] = []

    def worker(slice_no: int) -> None:
        for task_id in tasks[slice_no::6]:
            agent_id = agents[tasks.index(task_id) % len(agents)]
            handle = mgr.acquire(task_id, agent_id)
            usage = mgr.usage()
            if usage.get(agent_id, 0) > caps[agent_id]:
                overruns.append(f"逐条轴：{agent_id} 占用 {usage[agent_id]} > max_parallel={caps[agent_id]}")
            if sum(usage.values()) > 3:
                overruns.append(f"平台轴：同时占用 {sum(usage.values())} > 3")
            time.sleep(0.005)
            mgr.release(handle)
            done.append(task_id)

    threads = [threading.Thread(target=worker, args=(n,), daemon=True) for n in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20.0)
    alive = sum(1 for thread in threads if thread.is_alive())
    assert alive == 0, f"{alive} 条线程 20s 没跑完 ⇒ 死锁（FR-021）；未完成 {sorted(set(tasks) - set(done))}"
    assert sorted(done) == sorted(tasks), f"任务没全跑完：{sorted(set(tasks) - set(done))}"
    assert not overruns, overruns
    assert mgr.usage() == {"alpha": 0, "beta": 0, "gamma": 0}, mgr.usage()


# —— 二、破坏点：同一批场景函数在故意做坏的身体上必须红 ——


class SaboteurDropsIdleAgents(sm.InProcessSlotManager):
    """破坏点①：`usage()` 只列此刻占着槽的 agent。"""

    def usage(self) -> dict[str, int]:
        return {k: v for k, v in super().usage().items() if v}


class SaboteurIgnoresPlatformCap:
    """破坏点②：只认逐条 `max_parallel`，把平台上限那一根轴当不存在（两轴顶替的写实版本）。"""

    def __init__(self, *, caps: dict[str, int], platform_limit: int) -> None:
        self._inner = sm.InProcessSlotManager(per_agent_max_parallel=caps, platform_limit=platform_limit * 10**6)

    def acquire(self, task_id: str, agent_id: str) -> sm.SlotHandle:
        return self._inner.acquire(task_id, agent_id)

    def release(self, handle: sm.SlotHandle) -> None:
        self._inner.release(handle)

    def usage(self) -> dict[str, int]:
        return self._inner.usage()


class SaboteurSilentRelease:
    """破坏点③：把"放槽"做成静默幂等 —— 放错对象也不喊（丢槽时没人知道）。"""

    def __init__(self, *, caps: dict[str, int], platform_limit: int) -> None:
        self._inner = sm.InProcessSlotManager(per_agent_max_parallel=caps, platform_limit=platform_limit)

    def acquire(self, task_id: str, agent_id: str) -> sm.SlotHandle:
        return self._inner.acquire(task_id, agent_id)

    def release(self, handle: sm.SlotHandle) -> None:
        try:
            self._inner.release(handle)
        except sm.SlotConfigError:
            pass

    def usage(self) -> dict[str, int]:
        return self._inner.usage()


class SaboteurIgnoresPerAgentCap:
    """破坏点⑤：只认平台上限，把逐条 `max_parallel` 当不存在（两根轴顶替的反方向）。"""

    def __init__(self, *, caps: dict[str, int], platform_limit: int) -> None:
        self._inner = sm.InProcessSlotManager(
            per_agent_max_parallel={agent_id: 10**6 for agent_id in caps}, platform_limit=platform_limit
        )

    def acquire(self, task_id: str, agent_id: str) -> sm.SlotHandle:
        return self._inner.acquire(task_id, agent_id)

    def release(self, handle: sm.SlotHandle) -> None:
        self._inner.release(handle)

    def usage(self) -> dict[str, int]:
        return self._inner.usage()


def test_the_shared_check_sees_a_body_that_drops_idle_agents():
    mgr = SaboteurDropsIdleAgents(per_agent_max_parallel={a: 2 for a in DECLARED}, platform_limit=3)
    with pytest.raises(AssertionError, match="键集"):
        check_usage_lists_every_declared_agent(mgr)


def test_the_shared_check_sees_a_body_that_ignores_the_platform_cap():
    mgr = SaboteurIgnoresPlatformCap(caps={"alpha": 3, "beta": 3}, platform_limit=1)
    with pytest.raises(AssertionError, match="platform 这一轴本该挡住"):
        check_second_acquire_blocks_then_passes(mgr, second_agent="beta", binding_axis="platform")


def test_the_shared_check_sees_a_body_that_releases_silently():
    mgr = SaboteurSilentRelease(caps={a: 2 for a in DECLARED}, platform_limit=3)
    with pytest.raises(AssertionError, match="该拒的没拒"):
        check_refusals_keep_the_table_intact(mgr)


def test_the_shared_check_sees_a_body_that_ignores_the_per_agent_cap():
    """第二个轴单独破坏一次：只摘 `per_agent` 那道守卫，`platform` 的形状必须照常绿。

    两根轴若只有一条破坏点，"另一根轴的绿"就没被证明过 —— 它可能只是没被测。
    """
    mgr = SaboteurIgnoresPerAgentCap(caps={"alpha": 1, "beta": 3}, platform_limit=5)
    with pytest.raises(AssertionError, match="per_agent 这一轴本该挡住"):
        check_second_acquire_blocks_then_passes(mgr, second_agent="alpha", binding_axis="per_agent")


def test_the_three_breakages_are_not_faults_of_the_scenario_functions():
    """同一批场景函数换回真身体必须全绿 ⇒ 上面几条红的是身体，不是场景写死通不过。"""
    check_second_acquire_blocks_then_passes(
        make("hash_table", caps={"alpha": 3, "beta": 3}, platform_limit=1), second_agent="beta", binding_axis="platform"
    )
    check_second_acquire_blocks_then_passes(
        make("hash_table", caps={"alpha": 1, "beta": 3}, platform_limit=5), second_agent="alpha", binding_axis="per_agent"
    )
    check_usage_lists_every_declared_agent(make("hash_table", caps={a: 2 for a in DECLARED}, platform_limit=3))
    check_refusals_keep_the_table_intact(make("hash_table", caps={a: 2 for a in DECLARED}, platform_limit=3))


# —— 三、`concurrency.yaml` 的形状（T080：平台侧上限只有这一个键）——


def write_yaml(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "concurrency.yaml"
    path.write_bytes(text.encode("utf-8"))
    return path


@pytest.mark.parametrize(
    ("text", "why", "expect"),
    [
        ("", "空文件", "必须是映射"),
        ("- 3\n", "顶层是序列", "必须是映射"),
        ("max_parallel_tasks: 3\nmax_parallel_agents: 2\n", "多一个人读的键", "没人读的键"),
        ("{}\n", "是映射但缺键", "缺键"),
        ("max_parallel: 3\n", "把逐条那一根轴抄进平台文件", "没人读的键"),
        ("max_parallel_tasks: 0\n", "0", "正整数"),
        ("max_parallel_tasks: -1\n", "负数", "正整数"),
        ("max_parallel_tasks: true\n", "布尔", "正整数"),
        ("max_parallel_tasks: 2.5\n", "小数", "正整数"),
        ("max_parallel_tasks: '3'\n", "字符串", "正整数"),
    ],
)
def test_bad_shapes_are_rejected_by_the_branch_that_owns_them(tmp_path, text, why, expect):
    """读不出就报错，**不返回默认值** —— 兜底值就是第二份事实源，而那正是 §3 / §9 分家要禁的。

    每条都要求报错点名**它踩的是哪一道守卫**（空/非标量、多余键、缺键、值不合法），
    因为"抛了个错"分不出是哪一道在挡。上一轮的教训就在这：我拿"既缺键又多余键"的形状去测
    缺键那道守卫，结果被前面那道多余键的守卫先挡住，于是"摘掉缺键守卫"的变异照常绿。
    """
    with pytest.raises(sm.SlotConfigError, match=expect):
        sm.load_platform_limit(write_yaml(tmp_path, text))


def test_comments_only_change_still_loads(tmp_path):
    """GREEN 对照：加两行注释 ⇒ 照样读得出。红必须只因形状，不因排版。"""
    path = write_yaml(tmp_path, "# 新增一句说明\n\nmax_parallel_tasks: 3\n# 尾部注释\n")
    assert sm.load_platform_limit(path) == 3


def test_the_platform_file_declares_no_per_agent_knob(tmp_path):
    """`max_parallel` 的载体是 `agents.yaml` 逐条申报（FR-011），抄进平台文件必须被拒。

    这是两轴顶替最容易发生的地方：把 3 写进 `concurrency.yaml` 当默认值，
    `agents.yaml` 头里那句"任何一条都不许按默认 3 过关"的申报义务就悄悄失效了。
    """
    assert "max_parallel" not in sm.ALLOWED_KEYS, sm.ALLOWED_KEYS
    path = write_yaml(tmp_path, "max_parallel_tasks: 3\nmax_parallel: 3\n")
    with pytest.raises(sm.SlotConfigError, match="max_parallel"):
        sm.load_platform_limit(path)


def test_from_config_takes_the_limit_only_from_the_yaml(tmp_path):
    """调用方没有"传个数进来"的口子 ⇒ 平台上限的第二条输入路径不存在（一事实一载体）。"""
    path = write_yaml(tmp_path, "max_parallel_tasks: 2\n")
    mgr = sm.InProcessSlotManager.from_config({a: 5 for a in DECLARED}, path=path)
    assert mgr.usage() == {"alpha": 0, "beta": 0}
    first = mgr.acquire("T01", "alpha")
    second = mgr.acquire("T02", "beta")
    waiter = Waiter(mgr, "T03", "alpha").start()
    assert not waiter.finished(0.25), f"yaml 里写的 2 没生效：usage={mgr.usage()}"
    mgr.release(first)
    assert waiter.finished(2.0), waiter.outcome
    mgr.release(second)
    mgr.release(waiter.outcome)


# —— 四、跨载体一致性：`params-ref §3` 的声明 ⇄ `concurrency.yaml` 的值 ⇄ 首批 Agent 数 ——

SECTION_THREE = "## 3. 并发默认值"
LIMIT_ROW = "并发上限默认"


def section_three_rows(text: str) -> list[list[str]]:
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith(SECTION_THREE)), None)
    assert start is not None, f"params-ref.md 里找不到锚点 {SECTION_THREE!r}"
    rows = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        if line.startswith("|") and set(line) != {"|", "-", " ", "\t"}:
            rows.append([cell.strip() for cell in line.strip().strip("|").split("|")])
    assert rows, f"{SECTION_THREE} 下没解析出表体"
    return rows


def declared_limit_carrier_and_relation(params_ref_text: str) -> tuple[int, str, int]:
    """从 §3 那一行读出（声明值, 指向的配置文件, 关系式里的"首批 N 个"）。读不出即报错，不兜底。"""
    row = next((r for r in section_three_rows(params_ref_text) if r[0] == LIMIT_ROW), None)
    assert row is not None, f"§3 表里没有『{LIMIT_ROW}』那一行 ⇒ 关系式没人写"
    value = re.fullmatch(r"\*\*(\d+)\*\*", row[1])
    assert value, f"§3 的值列要写成 **N**，读到 {row[1]!r}"
    carrier = row[3].strip("`")
    relation = re.search(r"首批 (\d+) 个", row[2])
    assert relation, f"§3 的关系式列要含『首批 N 个 ⇒ N』，读到 {row[2]!r}"
    return int(value.group(1)), carrier, int(relation.group(1))


def cross_carrier_mismatches(yaml_limit: int, params_ref_text: str, agent_count: int) -> list[str]:
    """四个读数一致时返回空表；任何一处漂开就点名。判据写成关系式，不是"我看到 3"。"""
    declared, carrier, relation = declared_limit_carrier_and_relation(params_ref_text)
    shipped = sm.CONFIG_PATH.relative_to(REPO_ROOT).as_posix()
    out = []
    if declared != yaml_limit:
        out.append(f"params-ref §3 声明 {declared} ⇄ concurrency.yaml 实际 {yaml_limit}")
    if relation != agent_count:
        out.append(f"§3 的关系式说首批 {relation} 个 ⇄ agents.yaml 在册 {agent_count} 条")
    if declared != agent_count:
        out.append(f"§3 的值 {declared} ⇄ 首批接入 Agent 数 {agent_count}（宪法「三」要求两者相等）")
    if carrier != shipped:
        out.append(f"§3 指向 {carrier} ⇄ 运行时真读的 {shipped}")
    return out


def registered_agent_count() -> int:
    """在册的**执行** Agent 条数。审查专用身份（`REVIEW_ONLY_AGENT_IDS`）不占执行槽，不计入。"""
    entries = yaml.safe_load(AGENTS_YAML.read_text(encoding="utf-8"))
    assert isinstance(entries, list), type(entries)
    assert all(isinstance(e, dict) and "agent_id" in e for e in entries), entries
    missing = [e["agent_id"] for e in entries if "max_parallel" not in e]
    assert not missing, f"逐条 max_parallel 是 FR-011 的必项，缺申报：{missing}"
    return len([e for e in entries if e["agent_id"] not in review_policy.REVIEW_ONLY_AGENT_IDS])


def test_the_platform_limit_matches_params_ref_section_3():
    assert cross_carrier_mismatches(sm.load_platform_limit(), PARAMS_REF.read_text(encoding="utf-8"), registered_agent_count()) == []


def test_the_relation_is_a_relationship_not_a_constant():
    """§3 的数、yaml 的数、§3 关系式里的"首批 N"、`agents.yaml` 的在册条数 —— 四个现算读数两两相等。

    这条替代了"assert 平台上限 == 3"那种写法：抄一个常量进用例，改数时它会变成一条没人敢删的假绿。
    """
    declared, _carrier, relation = declared_limit_carrier_and_relation(PARAMS_REF.read_text(encoding="utf-8"))
    count = registered_agent_count()
    assert sm.load_platform_limit() == declared == relation == count, (sm.load_platform_limit(), declared, relation, count)


@pytest.mark.parametrize(
    ("yaml_limit", "mutate", "agent_count", "expected"),
    [
        (4, None, 3, "concurrency.yaml 实际 4"),
        (3, "value", 3, "声明 5"),
        (3, None, 4, "在册 4 条"),
        (3, None, 3, None),
    ],
)
def test_the_cross_carrier_check_has_teeth_and_a_green_side(yaml_limit, mutate, agent_count, expected):
    """变异对照：每个载体单独漂开都要被点名；只改说明文字（不动数）不许红。"""
    text = PARAMS_REF.read_text(encoding="utf-8")
    if mutate == "value":
        after = text.replace(f"| {LIMIT_ROW} | **3** |", f"| {LIMIT_ROW} | **5** |")
        assert "**5**" in after and after != text, "这轮变异没注入进去"
        text = after
    else:
        after = text.replace("默认并发上限 = 首批接入 Agent 数", "默认并发上限 = 首批接入的 Agent 支数")
        assert "支数" in after, "这轮变异没注入进去"
        text = after
    got = cross_carrier_mismatches(yaml_limit, text, agent_count)
    if expected is None:
        assert got == [], got
    else:
        assert any(expected in reason for reason in got), got


# —— 五、依赖面 + handle 形态 + 持久化面（§6 剩下那几句 MUST NOT）——


def redis_shaped(names) -> list[str]:
    """名形含 `redis` 即算 —— §6 说的"及其替代品"没有可枚举清单（那份清单的唯一来源是 `params-ref §8`，
    而那里只登记屏幕自动化一族），所以判据只能按名形收紧；宽松侧的替代品登记为残余。"""
    return sorted({n for n in names if "redis" in n.lower().replace("_", "-")})


def test_pyproject_declares_no_redis_client():
    assert redis_shaped(naming.declared_dependencies()) == []


def test_the_dependency_check_has_teeth_and_a_green_side(tmp_path):
    real = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "dependencies = [" in real

    bad = real.replace("dependencies = [\n", 'dependencies = [\n    "redis>=5.0",\n', 1)
    assert "redis" in bad, "这轮变异没注入进去"
    bad_path = tmp_path / "pyproject.toml"
    bad_path.write_bytes(bad.encode("utf-8"))
    assert redis_shaped(naming.declared_dependencies(bad_path)) == ["redis"]

    ok = real.replace("dependencies = [\n", 'dependencies = [\n    "orjson>=3.9",\n', 1)
    assert "orjson" in ok and "redis" not in ok, "这轮 GREEN 变异不该引入 redis"
    ok_path = tmp_path / "clean-pyproject.toml"
    ok_path.write_bytes(ok.encode("utf-8"))
    assert redis_shaped(naming.declared_dependencies(ok_path)) == []


# —— 五·补：T127 / T128 的闭合断言（把豁免登记行 ②③ 从"设计判据"变成"读回的事实"）——

#: 宪法原则 I 钉死的解释器区间。**权威载体 = `pyproject.toml` 的 `requires-python`**，
#: `plan.md` 的 Technical Context 那一行是对它的引用 ⇒ 两处必须同字，不许各漂一半。
PIN = ">=3.12,<3.13"
PLAN = REPO_ROOT / "specs" / "001-multi-agent-collab-platform" / "plan.md"
#: 用来把对照对象钉在**那一行**上："不含 `redis`"这个短语在 `plan.md` 里出现 **2** 次（登记行 + 后面那段取证口径串），
#: 全文搜会把两处分不清 ⇒ 先按行锚取行，再在行内查短语。
EXEMPTION_ROW = "I 豁免登记·Redis 批次性推迟"


def plan_lines() -> list[str]:
    return PLAN.read_text(encoding="utf-8").split("\n")


def exemption_row() -> str:
    hits = [line for line in plan_lines() if EXEMPTION_ROW in line]
    assert len(hits) == 1, f"豁免登记行 MUST 唯一，现读 {len(hits)} 行命中 ⇒ 这条符合性判据没有对照对象"
    return hits[0]


def declared_requires_python() -> str:
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    return tomllib.loads(text)["project"]["requires-python"]


def test_requires_python_is_exactly_the_pin_the_constitution_locks():
    """T128 第一断言：钉的是**字面**，不是"这台机器上恰好能跑"。

    两格各有独立红点：① `pyproject.toml` 的字面与 `PIN` 不等 ⇒ 红；② 跑这条用例的解释器不在 3.12 那一档 ⇒ 红
    （否则"区间被满足"这件事从没被真解释器验过，`plan.md` 里"CI 断言解释器版本"那半句就是空的）。
    """
    declared = declared_requires_python()
    assert declared == PIN, f"pyproject 的 requires-python 现读 {declared!r}，与宪法钉的 {PIN!r} 不同字"
    assert sys.version_info[:2] == (3, 12), (
        f"本用例跑在 {'.'.join(map(str, sys.version_info[:3]))} 上 ⇒ 钉的那个区间从没被真解释器验过"
    )


def test_plan_cites_the_same_pin_pyproject_declares():
    """跨载体一致性：`plan.md` 引用的那串必须**就是** `pyproject.toml` 声明的那串。

    只比"两边都等于 `PIN`"不够 —— 那样两边一起漂到别的值照样绿。这里先拿 pyproject 的现值去 plan 里找同字引用
    （改 pyproject ⇒ 找不到 ⇒ 红），再复查 pyproject 那个现值仍是宪法钉的字面（改 plan ⇒ 第一条仍绿、
    第二条不背这个锅，所以两条断言各配一发变异）。
    """
    declared = declared_requires_python()
    citation = f'requires-python = "{declared}"'
    hits = sum(1 for line in plan_lines() if citation in line)
    assert hits >= 1, f"plan.md 里找不到与 pyproject 同字的那句引用（找的是 {citation}）⇒ 两份载体已各漂一半"
    assert declared == PIN, f"两边同字但一起漂到了 {declared!r}，而宪法钉的是 {PIN!r}"


def test_the_exemption_row_dependency_claim_matches_the_scan():
    """T128 第二断言（豁免条件 ③）：读**发出去的那句报告**，与依赖面扫描对照。

    两个方向都要红：报告写"不含"而扫描读到东西 ⇒ 第二条断言红；报告被改成"含"而扫描仍然空 ⇒ 第一条断言红
    （把"措辞被改掉"当成对照对象丢失来失败，而不是悄悄变绿 —— 否则这条判据只有一边有牙）。
    """
    row = exemption_row()
    assert "不含 `redis`" in row, "豁免登记行里 ③ 那句的措辞变了 ⇒ 这条符合性判据失去对照对象"
    scanned = redis_shaped(naming.declared_dependencies())
    assert scanned == [], f"plan 那行仍写'不含'，而依赖面扫描读到 {scanned} ⇒ 报告与实况偏离"


def test_the_exemption_row_names_this_suite_as_its_evidence():
    """豁免条件 ② 的活性对照：那一行点名的取证入口文件**就是本文件**。

    点名关系断了（改文件名，或把这些判据搬去别的文件）⇒ "契约测试双向通过"那半句就指向了一个不跑这些东西的文件。
    """
    row = exemption_row()
    assert "tests/contract/test_slot_manager.py" in row, "② 那句点名的取证入口被改掉 ⇒ 豁免条件②的指向断了"
    assert Path(__file__).name in row, "本文件不是那行所点名的文件 ⇒ 这条判据在替别的文件作证"


def test_the_handle_carries_no_serialization_surface():
    """§6：`SlotHandle` MUST NOT 被序列化 ⇒ 从源头上不给它任何"可携带"的形态。

    字段就两个（`task_id` / `agent_id`，连槽位 id 都没有，更没有租约到期时间），
    方法面上 `to_dict / as_dict / json / serialize / model_dump` 一个都不许出现 ——
    留一个现成的导出口子，"别落盘"就只剩一句注释在守。
    """
    assert [f.name for f in dataclasses.fields(sm.SlotHandle)] == ["task_id", "agent_id"]
    for name in ("to_dict", "as_dict", "dict", "json", "serialize", "model_dump", "as_json"):
        assert not hasattr(sm.SlotHandle, name), f"SlotHandle 上出现了可携带面 {name}"


def slot_shaped_columns(metadata: MetaData) -> list[tuple[str, str]]:
    return [
        (table.name, column.name)
        for table in metadata.tables.values()
        for column in table.columns
        if re.search(r"slot|handle", column.name, re.IGNORECASE)
    ]


def test_no_persistent_column_can_hold_a_slot():
    """瞬态对象一旦有列可落，它就一定会落 —— 模型面与迁移文件两侧都 MUST 零命中。"""
    assert {"task_card", "event_log"} <= set(Base.metadata.tables), "模型模块没进 metadata ⇒ 这一面是空的，判据恒绿"
    assert slot_shaped_columns(Base.metadata) == []
    migrations = sorted((REPO_ROOT / "src" / "acp" / "persistence" / "migrations").rglob("*.py"))
    assert migrations, "迁移目录读不到 ⇒ 这一面的判据是空的"
    hits = [
        (path.name, line.strip())
        for path in migrations
        for line in path.read_text(encoding="utf-8").splitlines()
        if re.search(r"Column\(\s*['\"][^'\"]*(slot|handle)", line, re.IGNORECASE)
    ]
    assert hits == [], hits


def test_the_column_scan_would_see_a_slot_column_if_one_were_added():
    """注入面：真给一张表加 `slot_handle` 列 ⇒ 扫描必须点名（否则上面那条是恒绿）。"""
    metadata = MetaData()
    Table("task_card", metadata, Column("id", String, primary_key=True), Column("slot_handle", String))
    assert slot_shaped_columns(metadata) == [("task_card", "slot_handle")]
    Table("artifact", metadata, Column("bytes", Integer))
    assert slot_shaped_columns(metadata) == [("task_card", "slot_handle")], "无辜的表不该被一起点名"


class MergedUsageFace:
    """破坏点④（N-11 那种合并）：`usage` 同时是消息侧的字段名和类上的可调用体。"""

    model_fields = {"usage": None}
    usage = staticmethod(lambda: {})


def check_usage_stays_two_different_things(slot_cls, message_cls) -> None:
    """§6 的消歧声明：一个是瞬态层的占用读取，一个是跨 Agent 的用量通告载体，同名不同物。

    合并成一个类型 = 瞬态数据混进跨 Agent 消息（触原则 I 的存储分层 + 原则 II 的单一总线）。
    这里查三件查得到的：形态不同（方法 vs 字段）、瞬态侧不认 `UsageNotice`、读计数不收参数。
    """
    assert callable(slot_cls.usage), "瞬态侧的 usage 应当是方法"
    assert "usage" in message_cls.model_fields, "消息侧的 usage 应当是字段"
    assert not hasattr(message_cls, "usage"), "字段名升到了类属性上 ⇒ 两侧开始共用一个体"
    params = list(inspect.signature(slot_cls.usage).parameters)
    assert params == ["self"], f"读计数不收参数，否则它就成了按 agent 过滤的另一种语义：{params}"


def test_usage_method_and_task_result_usage_field_stay_two_different_things():
    check_usage_stays_two_different_things(sm.SlotManager, TaskResult)
    source = Path(inspect.getsourcefile(sm.SlotManager)).read_text(encoding="utf-8")
    assert "UsageNotice" not in source, "瞬态层源码里出现了 UsageNotice ⇒ 两个同名不同物开始共用类型"


def test_the_usage_disambiguation_check_sees_a_merged_face():
    with pytest.raises(AssertionError, match="类属性"):
        check_usage_stays_two_different_things(sm.SlotManager, MergedUsageFace)


@pytest.mark.parametrize("impl", IMPLS)
@pytest.mark.parametrize(
    ("caps", "limit"),
    [
        ({}, 3),
        ({"alpha": 0}, 3),
        ({"alpha": -1}, 3),
        ({"alpha": True}, 3),
        ({"alpha": 1.5}, 3),
        ({"alpha": 1}, 0),
        ({"alpha": 1}, -2),
    ],
    ids=["empty-caps", "cap-0", "cap-negative", "cap-bool", "cap-float", "limit-0", "limit-negative"],
)
def test_both_bodies_refuse_the_same_unusable_constructions(impl, caps, limit):
    """构造期就拒，而且**两个身体拒得一样** —— 换实现体时"原来会抛的现在默默收下"是最坏的一种漂移。

    七种坏形状：空候选表（acquire 会永久等待，那是装配错误）、逐条上限 0 / 负 / 布尔 / 小数、
    平台上限 0 / 负。`cap-bool` 单独有一条：`True == 1` 在 Python 里成立，
    不写 `isinstance(value, bool)` 那道判断的话，`max_parallel: true` 会安静地变成"能占 1 个槽"。
    """
    with pytest.raises(sm.SlotConfigError):
        make(impl, caps=caps, platform_limit=limit)


def test_the_usable_construction_is_the_one_the_shipped_configs_give():
    """正向对照（必须绿）：`concurrency.yaml` 的上限 + `agents.yaml` 的逐条申报拼出来的形状能用。

    上面那七条只证明"坏形状被拒"，不证明我没把好形状一起拒了。
    """
    per_agent = {
        entry["agent_id"]: entry["max_parallel"]
        for entry in yaml.safe_load(AGENTS_YAML.read_text(encoding="utf-8"))
    }
    for impl in IMPLS:
        mgr = make(impl, caps=per_agent, platform_limit=sm.load_platform_limit())
        check_usage_lists_every_declared_agent(mgr, declared=tuple(per_agent))
