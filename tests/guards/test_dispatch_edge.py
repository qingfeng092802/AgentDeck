"""T123 的那条边：`ASSIGN_GUARDS → DISPATCH` 与借还交接面（owner 2026-10-16 裁 甲）。

WorkBuddy `phase7-review-v1.0.1` 的 P1-1 钉的是宪法原则 III 里"**下发之前**"那三个字的拓扑落点。
本文件按三节分：节 一 是拓扑（**不连库**，compile 出来读图就行）；节 二 是"生产里只有一处派发"
（AST，也不连库）；节 三 才跑库看借还与失败形状。

四条口径，都是这一动最容易写歪的地方：

① **借与还被拆成两处是有意的**，但交接面只有一个对象：图节点 `leases.put(lease)`、`run_card()`
   `leases.take(task_id)`。判据同时盯两侧 —— 只盯"put 发生了"会漏掉"没人 take ⇒ 槽永久泄漏"，
   只盯 take 会漏"根本没 put ⇒ 段在没有槽的情况下跑了"。
② **`SlotHandle` 不进 state**（契约 §6）：`DISPATCH` 节点返回空 dict，可观察效果全在库里与台账里。
   所以台账这个类型**故意不提供** `to_dict()` / `items()` / `__iter__` —— 导出形状不可表示才算守住。
③ **反向对照**在节 一 里以"摘掉节点/加旁路"两种坏法各红一次；纯措辞改动不许红。
④ 期望值不取自被测实现：`DISPATCH` 这个名字从 `dispatch.py` 的 `NODE` 常量取，
   但**断言的是图上的实际节点集合**，不是"常量等于常量"。
"""

from __future__ import annotations

import ast
import datetime as dt
import inspect
import os
import pathlib
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from acp.config.loader import DSN_ENV, db_url
from acp.orchestrate import graph as og
from acp.orchestrate import scheduler as sc
from acp.orchestrate.nodes import dispatch as dp
from acp.orchestrate.nodes.select_worker import AUTOMATED_ACTOR
from acp.persistence.models_core import FeatureRequest, TaskCard
from acp.persistence.models_registry import Snapshot
from acp.protocol.messages import ArtifactRef, TaskResult
from acp.registry import roles as role_registry
from acp.registry.persist import persist_registrations
from acp.runtime.slot_manager import InProcessSlotManager

needs_pg = pytest.mark.skipif(
    not os.environ.get(DSN_ENV),
    reason=f"{DSN_ENV} 未注入 ⇒ 本节的借还形状只有真库量得出来（缺凭据时 skip，不是假绿）",
)

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "acp"
NOW = dt.datetime(2026, 10, 16, 15, 30, tzinfo=dt.timezone.utc)
END_NAME = "__end__"
START_NAME = "__start__"


def _tree(rel_path: str) -> ast.Module:
    return ast.parse((SRC / rel_path).read_text(encoding="utf-8"))


def calls_in(rel_path: str, name: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(_tree(rel_path))
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == name)
            or (isinstance(node.func, ast.Attribute) and node.func.attr == name)
        )
    ]


@pytest.fixture(scope="session")
def engine():
    eng = create_engine(db_url(), future=True)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine):
    with engine.connect() as conn:
        transaction = conn.begin()
        s = Session(bind=conn)
        yield s
        s.close()
        transaction.rollback()


def slots_for(*, platform_limit: int, per_agent: int = 3) -> InProcessSlotManager:
    return InProcessSlotManager(
        per_agent_max_parallel={
            "qoder": per_agent,
            "workbuddy": per_agent,
            "doubao": per_agent,
        },
        platform_limit=platform_limit,
    )


def seed_project(session) -> tuple[FeatureRequest, uuid.UUID]:
    """一张 `READY` 卡 + 三岗可指派的**注册面**（内置岗位 + 三支在册工具）。

    工具与岗位走 `acp.registry` 的真路径（`persist_registrations` 读 `config/agents.yaml`），
    不在这里手搓 `AgentUnit`：指派段要的是"矩阵能闭合"，而闭合靠的是 EN-04 挂载与 EN-05 申报
    的**配对**——手搓一行 `capabilities` 会把这条判据测成"我自己搭的现场能跑"。
    """
    role_registry.seed_builtin_roles(session)
    report = persist_registrations(session, declared_at=NOW)
    assert sorted(report.agents) == [
        "deepseek-reviewer",
        "doubao",
        "qoder",
        "workbuddy",
    ], report.agents

    request = FeatureRequest(
        id=uuid.uuid4(),
        project_key="edge-demo",
        raw_input="做个介绍公司产品的静态网站",
        summary_digest={"自行补全项": ["一个首页"], "澄清问题": []},
        clarification_waived=True,
        status="SPLIT",
        created_at=NOW,
    )
    session.add(request)
    session.flush()
    snapshot_id = uuid.uuid4()
    session.add(
        Snapshot(
            id=snapshot_id,
            round_no=1,
            phase="PRE_WORK",
            task_card_ids=["T01"],
            ref=f"local:edge-{snapshot_id}",
            rolled_back_at=None,
        )
    )
    session.flush()
    session.add(
        TaskCard(
            id="T01",
            feature_request_id=request.id,
            title="卡 T01",
            difficulty=2,
            skill_tags=["frontend_coding"],
            depends_on=[],
            artifact_floor_min_bytes=512,
            artifact_floor_min_lines=12,
            artifact_floor_format="html",
            acceptance_criteria=["首屏有标题"],
            handoff_count=0,
            review_round=0,
            status="READY",
            snapshot_id=snapshot_id,
        )
    )
    session.flush()
    return request, snapshot_id


def deps_for(session, *, slots=None, leases=None, task_limit=3):
    return og.AssignmentDeps(
        session=session,
        created_at=NOW,
        actor_agent_id=AUTOMATED_ACTOR,
        slots=slots if slots is not None else slots_for(platform_limit=task_limit),
        leases=leases if leases is not None else sc.LeaseLedger(),
    )


def topology_shape():
    """只 compile 读图，不进任何节点 ⇒ 这一节的四条**不连库**（无 DSN 也真跑，不是 skip）。

    `session=None` 是合法的：那三个节点各自在**运行时**才检查会话（`MissingSession`），
    `get_graph()` 只读拓扑。反过来，把这一节挂上 `session` fixture 会让它在缺凭据时整节 skip ——
    而它立的恰恰是 P1-1 那条拓扑判据，"没库所以没测"读起来像通过，是这里最要防的绿。
    """
    return og.build_assignment_graph(deps_for(None)).get_graph()


# ---------------------------------------------------------------- 一、拓扑：那条边在，且是唯一入口


def test_the_assignment_segment_now_has_three_nodes():
    """节点集合 = `{select_worker, assign_guards, dispatch}`，多一个少一个都红。

    这是 P1-1 的直接对偶：Phase 7 时这条断言的期望值是**两个**节点、通向终点的唯一入口是
    `assign_guards`（`test_triple_roles.py` 那一条原样如此，行号基准见 `evidence/phase-12.md`）。
    owner 2026-10-16 裁 甲 之后那个"止于 END"的拓扑被有意改写 ⇒ 本文件的这一节与那一节测的是同一件事，
    区别只在期望值跟着实况走（`evidence/phase-12.md` 记 before/after）。
    """
    names = {node for node in topology_shape().nodes if not node.startswith("__")}
    assert names == {og.SELECT_WORKER, og.ASSIGN_GUARDS, og.DISPATCH}, sorted(names)


def test_dispatch_is_reachable_only_through_assign_guards():
    """进 `DISPATCH` 的边**恰有一条**且来自 `ASSIGN_GUARDS`。

    这条才是"下发之前必过三岗"的拓扑形式：只要还有第二条入口（比如 `START → dispatch`），
    "没指派就派发"就是可表示的形态，那条断言就退化成一句运气。
    前驱集合从 `shape.edges` 现算，不写死。
    """
    into_dispatch = [(e.source, e.conditional) for e in topology_shape().edges if e.target == og.DISPATCH]
    assert into_dispatch == [(og.ASSIGN_GUARDS, False)], f"进派发节点的路不止一条或不是普通边：{into_dispatch}"
    assert [e.source for e in topology_shape().edges if e.target == END_NAME] == [og.DISPATCH], (
        "通向终点的唯一入口必须是派发 ⇒ 指派之后不许有第二条收尾路"
    )


def test_the_three_step_order_is_select_assign_dispatch():
    """逐条点名列的顺序：`START → SelectWorker → AssignGuards → Dispatch → END`，且全段无条件边。"""
    ordered = [(e.source, e.target, e.conditional) for e in topology_shape().edges]
    plain = {(s, t) for s, t, conditional in ordered if not conditional}
    assert {
        (START_NAME, og.SELECT_WORKER),
        (og.SELECT_WORKER, og.ASSIGN_GUARDS),
        (og.ASSIGN_GUARDS, og.DISPATCH),
        (og.DISPATCH, END_NAME),
    } <= plain, f"缺边：{sorted(plain)}"
    # **判"有没有条件边"要先筛再判空**：`[cond for ... ]` 是四个 `False` 的**非空列表**，
    # `not [...]` 恒为 False ⇒ 这条会红在任何实现上（第一版就栽在这里）。
    assert not [c for c in (cond for _, _, cond in ordered) if c], f"这一段上有条件边：{ordered}"


def test_the_dispatch_node_name_is_the_dispatch_module_own_constant():
    """图上的名字与 `dispatch.py` 的 `NODE` 是同一个对象值，而**节点集合里真的有它**。

    如果有人在图里另写一个字面量 `"dispatch"`，名字照样相等、这条也会绿 —— 所以还补一半：
    从 compile 出来的图上取节点名集合，断它含 `dp.NODE`（"改了常量而图没跟着改"这一步就红了）。
    """
    assert og.DISPATCH == dp.NODE
    names = {node for node in topology_shape().nodes if not node.startswith("__")}
    assert dp.NODE in names, sorted(names)


# ---------------------------------------------------------------- 二、派发只有一个生产调用点


def test_dispatch_is_called_from_exactly_one_production_site():
    """`dispatch(` 的调用点在整个 src 里恰一处，且它在 `orchestrate/graph.py` 的派发节点里。

    这一条立的是 owner 裁 甲 的 (b)：两处各派一次 = 两份就绪判定，而两份判定必然漂。
    计数用 AST 而不是 grep：grep 会把 `def dispatch(` 与文档字符串里的词一起算进来。
    """
    hits = [
        path.relative_to(SRC).as_posix()
        for path in sorted(SRC.rglob("*.py"))
        if calls_in(path.relative_to(SRC).as_posix(), "dispatch")
    ]
    assert hits == ["orchestrate/graph.py"], f"派发不止一处（或图节点不再派发）：{hits}"


def test_run_card_takes_the_lease_instead_of_taking_the_slot():
    """`scheduler.py` 里不许再有 `dispatch(` 调用；它做的是 `leases.take()` + `dp.release()`。

    与上一条是同一件事的两侧：那条数"生产派发只有一处"，这条数"生命周期层不自己占槽"。
    只钉一侧的话，把派发从图里挪回 `run_card()`（也就是回到改动前那个形状）也能两边各自成立。
    """
    assert calls_in("orchestrate/scheduler.py", "dispatch") == []
    takes = calls_in("orchestrate/scheduler.py", "take")
    releases = calls_in("orchestrate/scheduler.py", "release")
    assert len(takes) == 1, f"`run_card()` 领槽的次数不是恰一次：{len(takes)}"
    assert len(releases) == 1, f"退槽次数不是恰一次：{len(releases)}"


def test_the_ledger_cannot_be_exported():
    """`LeaseLedger` 没有 `to_dict` / `items` / `keys` / `__iter__` —— 瞬态凭据的导出形状不可表示。

    契约 §6 那句"MUST NOT 被序列化进任务卡、日志、产物或 PG"在本类型上的实现方式就是"不给口子"：
    只要有一个能列出全部 lease 的方法，下一个写日志的人就会顺手把它 `json.dumps` 进去。
    """
    members = {name for name, _ in inspect.getmembers(sc.LeaseLedger)}
    banned = {"to_dict", "items", "keys", "values", "__iter__", "__len__"}
    assert not (members & banned), f"台账开了导出口子：{sorted(members & banned)}"
    ledger = sc.LeaseLedger()
    assert hasattr(ledger, "pending") and ledger.pending() == ()


def test_the_dispatch_node_returns_nothing_into_the_state():
    """图节点的函数体只 `return {}`：`Lease` 不进 state（state 是可能被 checkpointer 搬走的载体）。"""
    module = _tree("orchestrate/graph.py")
    builder = next(
        node for node in ast.walk(module) if isinstance(node, ast.FunctionDef) and node.name == "_dispatch"
    )
    inner = next(
        node for node in ast.walk(builder) if isinstance(node, ast.FunctionDef) and node.name == "node"
    )
    returns = [node for node in ast.walk(inner) if isinstance(node, ast.Return)]
    assert len(returns) == 1, f"派发节点有两个 return（多出来的那个会往 state 放东西）：{len(returns)}"
    empty = returns[0].value
    assert isinstance(empty, ast.Dict) and empty.keys == [], "派发节点没把 lease 留在进程内：返回的不是空 dict"


# ---------------------------------------------------------------- 三、借还与失败形状（要 PG）


@needs_pg
def test_running_the_segment_takes_the_slot_into_the_ledger(session):
    """跑指派段 ⇒ 台账里有一张待退的凭据，而**卡已经在 `RUNNING`**（派发真的发生了，不只是记了个数）。"""
    request, snapshot_id = seed_project(session)
    slots = slots_for(platform_limit=3)
    ledger = sc.LeaseLedger()
    graph = og.build_assignment_graph(deps_for(session, slots=slots, leases=ledger))
    result = graph.invoke({"feature_request_id": request.id, "task_id": "T01"})

    assert ledger.pending() == ("T01",), ledger.pending()
    assert dict(slots.usage())["qoder"] == 1, slots.usage()
    # **state 面的两条各管一种坏法，别把它们读成一条**：
    # ① 键集合 —— `AssignmentState` 就声明这五个键，多出来的键 LangGraph **静默丢掉**（现跑探针：
    #    节点 `return {"dispatched": True}` 时 invoke 结果仍是 `{'a': 'x'}` 那种形状），
    #    所以这一句抓不到"往未声明的键里放东西"，那条由节 二 的 AST 判据负责；
    # ② 值形态 —— 五个键**内部**被塞进一个不可重放的对象（`Lease` / `SlotHandle`）时只有这一句抓得到。
    # `isinstance(v, (uuid.UUID, str)) or v is None` 这个式子取自 state 自己的声明（`:83-87`），
    # 不是取自某个节点的实现：瞬态凭据是 dataclass，两者天然不同形。
    assert set(result) <= {
        "feature_request_id",
        "task_id",
        "executor_agent_id",
        "reviewer_agent_id",
        "acceptor_agent_id",
    }, f"state 里出现了未声明的键：{sorted(result)}"
    unsafe = {
        key: type(value).__name__
        for key, value in result.items()
        if not (value is None or isinstance(value, (uuid.UUID, str)))
    }
    assert unsafe == {}, f"state 里有不可安全重放的瞬态值（契约 §6）：{unsafe}"
    session.expire_all()
    card = (
        session.query(TaskCard)
        .filter(TaskCard.feature_request_id == request.id, TaskCard.id == "T01")
        .one()
    )
    assert str(card.status) == "RUNNING", card.status


@needs_pg
def test_the_ledger_hands_over_to_run_card_which_gives_the_slot_back(session):
    """借（图节点）→ 还（`run_card()` 的 `finally`）这一对成立 ⇒ 用完的平台上占用归零。

    这条是"借与还分成两处"这一代价的看守：任何一处被改掉，占用就不会归零。
    """
    request, _ = seed_project(session)
    slots = slots_for(platform_limit=3)
    ledger = sc.LeaseLedger()
    og.build_assignment_graph(deps_for(session, slots=slots, leases=ledger)).invoke(
        {"feature_request_id": request.id, "task_id": "T01"}
    )
    seen: dict[str, int] = {}

    def segment(state):
        seen["in_segment"] = sum(dict(slots.usage()).values())
        return {"validated_status": "valid"}

    out = sc.run_card(
        feature_request_id=request.id,
        task_id="T01",
        slots=slots,
        leases=ledger,
        produce=card_result,
        segment=segment,
    )
    assert seen["in_segment"] == 1, "段内看不见占用 ⇒ 图节点没占槽，或 run_card 提前退了"
    assert out.usage_after == {"qoder": 0, "workbuddy": 0, "doubao": 0}, out.usage_after
    assert ledger.pending() == (), "退完还留在台账里 ⇒ 下一次 take 会拿到一张已失效的凭据"
    assert (out.card_id, out.agent_id) == ("T01", "qoder"), out
    assert out.feature_request_id == request.id


@needs_pg
def test_a_segment_without_a_lease_fails_loudly_instead_of_running_unleased(session):
    """台账是空的 ⇒ `MissingLease`，段体一次都不许被走到。

    这条防的是占槽上移之后新出现的坏形状："图上 DISPATCH 被摘了，段照跑" ——
    那等于绕着槽跑卡，平台上限失效而没有任何一处会报错。
    """
    request, _ = seed_project(session)
    slots = slots_for(platform_limit=3)
    walked: list[str] = []

    def segment(state):
        walked.append(state["task_id"])
        return {"validated_status": "valid"}

    with pytest.raises(sc.MissingLease, match="T01"):
        sc.run_card(
            feature_request_id=request.id,
            task_id="T01",
            slots=slots,
            leases=sc.LeaseLedger(),
            produce=card_result,
            segment=segment,
        )
    assert walked == [], "没有槽也跑了段 ⇒ 缺凭据被当成了可以放行的状态"
    assert sum(slots.usage().values()) == 0, slots.usage()


@needs_pg
def test_the_slot_is_still_released_when_the_segment_raises(session):
    """借还拆两处之后，"抛了也要退"仍要成立：段内报错 ⇒ 占用归零、原异常照原样抛出。"""
    request, _ = seed_project(session)
    slots = slots_for(platform_limit=3)
    ledger = sc.LeaseLedger()
    og.build_assignment_graph(deps_for(session, slots=slots, leases=ledger)).invoke(
        {"feature_request_id": request.id, "task_id": "T01"}
    )

    class SegmentFailed(RuntimeError):
        pass

    def segment(state):
        raise SegmentFailed("段内炸了")

    with pytest.raises(SegmentFailed):
        sc.run_card(
            feature_request_id=request.id,
            task_id="T01",
            slots=slots,
            leases=ledger,
            produce=card_result,
            segment=segment,
        )
    assert sum(slots.usage().values()) == 0, slots.usage()


def card_result(task_id: str = "T01") -> TaskResult:
    """段输入用的最小自述结果（形状与 `test_dispatch.py` 同一件，本文件不复写判定）。"""
    return TaskResult(
        task_id=task_id,
        self_reported_status="success",
        artifacts=[ArtifactRef(rel_path="index.html", bytes=2048, lines=40, format="html")],
        log_excerpt="build ok",
        self_assessment="已完成",
        issues=[],
        usage=[],
    )
