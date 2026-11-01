"""T078：`Dispatch` 的判据（FR-020 依赖就绪｜FR-021 并发上限，R-04 / R-05，宪法「三」+ 原则 I）。

档位：`tests/guards/`，混合文件 —— 就绪判定那批是纯函数，占槽那批各挂 `skipif`（要真 PG）。
**每条 PG 读数都必须注明"带 `.env`"**；不带时它们是 skip 而不是红。

分工：并发上限**本身**的形状与双向符合性归 `tests/contract/test_slot_manager.py`（契约 §6），
本文件只问一件事 —— **`Dispatch` 有没有真的去用那根上限**。
两者的判据不同：前者查"acquire 在满的时候会不会挡"，后者查"派发一张卡会不会经过 acquire"。
只留前者的话，一个"`card.status = RUNNING` 然后直接返回、从不占槽"的实现体照样全绿，
而那正是 FR-021 的空转形状（槽位表永远是空的，`usage()` 永远读不到东西）。

三条反向对照（缺了就等于判据恒红）：
- "依赖没齐 ⇒ 挡"的每条都跟着一条"只把那一格改成 `DELIVERED` ⇒ 放行"；
- `DELIVERED_WITH_ISSUES` 算不算就绪，文档两处都只写了"依赖全 `DELIVERED`"（`plan.md:509`、`tasks.md` T078 行），
  所以取保守侧不解锁，并把它单独钉成一条 —— 改口径时只动那一条用例和一个集合；
- 平台上限挡住的那条，另有一条"上限放宽一格就放行"的对照 ⇒ 挡住的是**数**，不是我的线程编排。
"""

from __future__ import annotations

import datetime as dt
import os
import threading
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from acp.config.loader import DSN_ENV, db_url
from acp.orchestrate import scheduler as sc
from acp.orchestrate.nodes import dispatch as dp
from acp.persistence.models_core import AgentUnit, FeatureRequest, TaskCard
from acp.persistence.models_registry import EventLog, Snapshot
from acp.protocol.messages import ArtifactRef, TaskResult
from acp.runtime.slot_manager import InProcessSlotManager, SlotConfigError, SlotHandle

needs_pg = pytest.mark.skipif(
    not os.environ.get(DSN_ENV), reason=f"缺 {DSN_ENV} ⇒ 本条要真 PG，整条 skip（不是红）"
)

NOW = dt.datetime(2026, 10, 14, 3, 40, tzinfo=dt.timezone.utc)


def need(card_id: str, status: str, deps: tuple[str, ...] = (), *, executor: str | None = "qoder") -> dp.CardNeed:
    return dp.CardNeed(card_id=card_id, status=status, depends_on=deps, executor_agent_id=executor)


def raises_code(exc_type, code: str, call) -> str:
    """按 `exc.code` 断言拒绝原因 —— 措辞可以改，代号改了就是契约变了。"""
    with pytest.raises(exc_type) as caught:
        call()
    assert caught.value.code == code, f"代号是 {caught.value.code!r}，期望 {code!r}：{caught.value}"
    return str(caught.value)


# —————————————————————————————— 一、就绪判定（纯函数，FR-020）


def test_a_card_whose_dependencies_are_all_delivered_is_ready():
    verdict = dp.readiness(
        [need("T01", "DELIVERED"), need("T02", "READY", ("T01",))]
    )
    assert [c.card_id for c in verdict.ready] == ["T02"], verdict
    assert verdict.blocked == (), verdict.blocked


def test_a_card_with_no_dependencies_is_ready_on_its_own():
    verdict = dp.readiness([need("T01", "READY")])
    assert [c.card_id for c in verdict.ready] == ["T01"]


def test_an_unmet_dependency_is_blocked_and_names_the_card_and_its_state():
    verdict = dp.readiness([need("T01", "RUNNING"), need("T02", "READY", ("T01",))])
    assert verdict.ready == ()
    reason = verdict.reason_of("T02")
    assert reason is not None and "T01=RUNNING" in reason, reason


def test_a_dangling_dependency_reference_blocks_rather_than_reading_as_delivered():
    """`depends_on` 指向本批不存在的卡号 ⇒ 挡并点名。

    "查不到状态就当已交付"是这一层最省事也最致命的写法：FR-005 的引用完整性错误会被读成放行。
    """
    verdict = dp.readiness([need("T02", "READY", ("T404",))])
    reason = verdict.reason_of("T02")
    assert reason and "T404" in reason, reason


def test_a_dependency_delivered_with_issues_does_not_unlock_its_successor():
    """**口径分叉点**：文档只写"依赖全 `DELIVERED` 才放行"，没提 `DELIVERED_WITH_ISSUES`。

    取保守侧（不解锁），理由是那条状态的定义就是"验收轮次跑满仍未通过"（FR-035）：
    一份已知有问题的交付被下游静默接着用，等于把"带问题"这件事藏进依赖链里。
    代价点名：这样一条依赖会**卡住**它的下游，出口只能是人工升级。
    将来若裁定"两种交付态都算就绪"，需要动的就是 `dispatch.DELIVERED` 这一个常量与这一条用例。
    """
    verdict = dp.readiness([need("T01", "DELIVERED_WITH_ISSUES"), need("T02", "READY", ("T01",))])
    assert verdict.ready == (), verdict.ready
    assert "T01=DELIVERED_WITH_ISSUES" in verdict.reason_of("T02")


def test_the_same_readiness_only_needs_one_state_to_change():
    """上一条的反向对照：同一形状里只把依赖那格改成 `DELIVERED` ⇒ 立刻放行。"""
    verdict = dp.readiness([need("T01", "DELIVERED"), need("T02", "READY", ("T01",))])
    assert [c.card_id for c in verdict.ready] == ["T02"]


@pytest.mark.parametrize("status", ["DRAFT", "RUNNING", "REVIEWING", "ARTIFACT_INVALID", "DELIVERED", "ESCALATED"])
def test_a_card_that_is_not_ready_appears_in_neither_list(status):
    """阻塞清单只装"本来可以派发却被挡住的卡"。

    把 RUNNING / DELIVERED 的卡也算进 `blocked`，这个计数就变成"卡总数"，
    FR-025 要给用户看的那句"还有哪些在等"就读不出东西了。
    """
    verdict = dp.readiness([need("T01", status)])
    assert verdict.ready == ()
    assert verdict.blocked == (), (status, verdict.blocked)


def test_both_state_machine_edges_are_dispatchable_and_the_second_one_reaches_ready():
    """模块头把可派发面写成"状态机上真实存在的边"，这个文件就得同时点名每条边 —— 只从反面钉不够。

    反面那批（上一条的 6 个状态）删掉 `HANDED_OFF` 时全绿：红的是 `test_handoff.py` 里那条跨文件用例，
    归因会指向换人，而缺陷在派发面（MU-D1 实测就是这个分布）。
    所以这里写**状态机原文**（不是 `dp.DISPATCHABLE` 的镜像），并且把行为也跑一遍：
    一张 `HANDED_OFF` 的卡必须真能通过就绪判定，否则"换过人的卡再也没有入口"。
    """
    # owner 2026-10-14 待裁 ⑩ 加第三条边：`REWORK ──▶ RUNNING`（`data-model.md:301` 早就写着，
    # 缺的是这里 —— 于是 `mark_rework` 放出去的卡没有任何入口，状态机断头）。
    assert dp.DISPATCHABLE == frozenset({"READY", "HANDED_OFF", "REWORK"}), dp.DISPATCHABLE
    verdict = dp.readiness([need("T01", "HANDED_OFF", executor="workbuddy")])
    assert [c.card_id for c in verdict.ready] == ["T01"], verdict


def test_a_card_returning_from_rework_really_gets_an_entry_into_running():
    """⑩ 不是集合里多一个串就算数：一张 `REWORK` 的卡必须真能通过就绪判定进到可派发那一侧。

    反向对照放在同一条里（`CHANGES_REQUESTED` 不许被派发）：那个态是"谁判的不通过"那一格，
    返工还没被承认就先放行，等于把 `mark_rework` 那一格跳过去。
    """
    from_rework = dp.readiness([need("T01", "REWORK", executor="qoder")])
    assert [c.card_id for c in from_rework.ready] == ["T01"], from_rework
    not_yet = dp.readiness([need("T02", "CHANGES_REQUESTED", executor="qoder")])
    assert not_yet.ready == () and not_yet.blocked == (), not_yet


def test_the_input_order_does_not_change_the_ready_sequence():
    cards = [need("T03", "READY"), need("T01", "READY"), need("T02", "READY")]
    forward = [c.card_id for c in dp.readiness(cards).ready]
    backward = [c.card_id for c in dp.readiness(list(reversed(cards))).ready]
    assert forward == backward == ["T01", "T02", "T03"], (forward, backward)


def test_readiness_does_not_look_at_the_executor_column_while_dispatch_does():
    """`executor_agent_id` 为空 = `SelectWorker` 还没填 ⇒ 没有可占槽的工具。

    这件事**不在就绪判定里查**（那一层只管依赖），而在 `dispatch()` 里查：两处都查就是同一规则两个载体，
    一处都不查就是派发一张没人跑的卡。所以这里断言就绪列表**包含**它，
    而 `test_dispatch_refuses_a_card_without_an_executor_and_consumes_no_slot` 断言派发拒它。
    """
    verdict = dp.readiness([need("T01", "READY", executor=None)])
    assert [c.card_id for c in verdict.ready] == ["T01"], "就绪判定管依赖，不管执行者"


# —————————————————————————————— 二、占槽真的经过 SlotManager（真 PG）


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


def seed_unit(session, agent_id: str, *, max_parallel: int = 2) -> None:
    session.add(
        AgentUnit(
            agent_id=agent_id,
            form="SDK",
            tier=2,
            cost_band="MID",
            max_parallel=max_parallel,
            current_load=0,
            historical_success_rate=Decimal("0.5"),
            success_rate_sample=4,
            workspace_dir=f"/tmp/{agent_id}-workspace",
            enabled=True,
        )
    )


def add_card(session, request_id, snapshot_id, card_id: str, *, status="READY", deps=(), executor="qoder") -> TaskCard:
    card = TaskCard(
        id=card_id,
        feature_request_id=request_id,
        title=f"卡 {card_id}",
        difficulty=2,
        skill_tags=["html"],
        depends_on=list(deps),
        executor_agent_id=executor,
        artifact_floor_min_bytes=512,
        artifact_floor_min_lines=12,
        artifact_floor_format="html",
        acceptance_criteria=["首屏有标题"],
        handoff_count=0,
        review_round=0,
        status=status,
        snapshot_id=snapshot_id,
    )
    session.add(card)
    session.flush()
    return card


@pytest.fixture
def project(session) -> tuple[FeatureRequest, uuid.UUID]:
    """一支在册工具 + 一条需求单 + 一个存档点。

    存档点必须先单独 flush 再建卡：两张表之间没有 `relationship()`，UoW 不按表间外键排 INSERT 顺序
    （同 `graph.py::_normalize` 与 `test_artifact_gate.py` 里那处注释）。
    """
    seed_unit(session, "qoder")
    request = FeatureRequest(
        id=uuid.uuid4(),
        project_key="demo",
        raw_input="做个介绍公司产品的静态网站",
        summary_digest={"自行补全项": ["首页取 1 个"], "澄清问题": []},
        clarification_waived=True,
        status="SPLIT",
        created_at=NOW,
    )
    session.add(request)
    snapshot_id = uuid.uuid4()
    session.add(
        Snapshot(
            id=snapshot_id,
            round_no=1,
            phase="PRE_WORK",
            task_card_ids=["T01"],
            ref=f"local:probe-{snapshot_id}",
            rolled_back_at=None,
        )
    )
    session.flush()
    return request, snapshot_id


def slots_for(*, platform_limit: int, per_agent: int = 3) -> InProcessSlotManager:
    """两种轴分开给数：`per_agent` 一律给到富余，本文件里唯一被改变的数就是 `platform_limit`。"""
    return InProcessSlotManager(
        per_agent_max_parallel={"qoder": per_agent, "workbuddy": per_agent, "doubao": per_agent},
        platform_limit=platform_limit,
    )


@needs_pg
def test_dispatch_acquires_a_slot_moves_the_card_and_leaves_one_audit_row(session, project):
    request, snapshot_id = project
    add_card(session, request.id, snapshot_id, "T01")
    slots = slots_for(platform_limit=3)

    lease = dp.dispatch(session, feature_request_id=request.id, task_id="T01", slots=slots, created_at=NOW)

    assert isinstance(lease.handle, SlotHandle), lease.handle
    assert (lease.card_id, lease.agent_id) == ("T01", "qoder")
    assert session.get(TaskCard, (request.id, "T01")).status == "RUNNING"
    assert slots.usage() == {"qoder": 1, "workbuddy": 0, "doubao": 0}, slots.usage()
    audit = session.scalars(
        select(EventLog).where(EventLog.entity_id == "T01", EventLog.kind == "TASK")
    ).all()
    assert len(audit) == 1, [row.payload for row in audit]
    assert audit[0].payload["action"] == "dispatched"
    assert audit[0].payload["agent_id"] == "qoder"


@needs_pg
def test_pg_current_load_column_stays_zero_while_the_slot_layer_reports_one(session, project):
    """原则 I 的存储分层：`usage()` 是实时事实源，PG 那一列是上一次读到的快照。

    这条故意读**没有被 dispatch 写过**的 `agent_unit.current_load`：派发只动卡状态与留痕，
    不去"顺手同步"那一列。谁要是把它当权威去写，两处就各有作者，恢复路径会读到两份账。
    """
    request, snapshot_id = project
    add_card(session, request.id, snapshot_id, "T01")
    slots = slots_for(platform_limit=3)
    dp.dispatch(session, feature_request_id=request.id, task_id="T01", slots=slots, created_at=NOW)
    unit = session.get(AgentUnit, "qoder")
    assert unit.current_load == 0, "快照列被运行时反向写成了事实源"
    assert slots.usage()["qoder"] == 1


@needs_pg
def test_dispatch_refuses_a_card_that_is_not_ready_and_consumes_no_slot(session, project):
    request, snapshot_id = project
    add_card(session, request.id, snapshot_id, "T01", status="RUNNING")
    slots = slots_for(platform_limit=3)
    raises_code(
        dp.DispatchMisuse,
        "task_not_ready",
        lambda: dp.dispatch(session, feature_request_id=request.id, task_id="T01", slots=slots, created_at=NOW),
    )
    assert slots.usage() == {"qoder": 0, "workbuddy": 0, "doubao": 0}, slots.usage()


@needs_pg
def test_dispatch_refuses_a_card_without_an_executor_and_consumes_no_slot(session, project):
    request, snapshot_id = project
    add_card(session, request.id, snapshot_id, "T01", executor=None)
    slots = slots_for(platform_limit=3)
    raises_code(
        dp.DispatchMisuse,
        "executor_missing",
        lambda: dp.dispatch(session, feature_request_id=request.id, task_id="T01", slots=slots, created_at=NOW),
    )
    assert sum(slots.usage().values()) == 0


@needs_pg
def test_dispatch_refuses_unmet_dependencies_and_consumes_no_slot(session, project):
    """FR-020 与 FR-021 的交界：依赖没齐的卡**不许先占槽再等**——那会把上限吃出空洞。"""
    request, snapshot_id = project
    add_card(session, request.id, snapshot_id, "T01", status="REVIEWING")
    add_card(session, request.id, snapshot_id, "T02", deps=("T01",))
    slots = slots_for(platform_limit=3)
    raises_code(
        dp.DispatchMisuse,
        "dependencies_unmet",
        lambda: dp.dispatch(session, feature_request_id=request.id, task_id="T02", slots=slots, created_at=NOW),
    )
    assert sum(slots.usage().values()) == 0
    assert session.get(TaskCard, (request.id, "T02")).status == "READY"


@needs_pg
def test_dispatch_unlocks_the_moment_the_dependency_lands_on_delivered(session, project):
    """上一条的反向对照：同一形状只把依赖那格改成 `DELIVERED` ⇒ 放行且占槽。"""
    request, snapshot_id = project
    add_card(session, request.id, snapshot_id, "T01", status="DELIVERED")
    add_card(session, request.id, snapshot_id, "T02", deps=("T01",))
    slots = slots_for(platform_limit=3)
    lease = dp.dispatch(session, feature_request_id=request.id, task_id="T02", slots=slots, created_at=NOW)
    assert lease.card_id == "T02" and slots.usage()["qoder"] == 1


@needs_pg
def test_the_platform_limit_is_the_thing_that_blocks_the_second_card(session, project):
    """FR-021 接到 `Dispatch` 上：三张卡都就绪、平台上限 1 ⇒ 第二张必须等，直到第一张放槽。

    三张卡**全部指派同一支工具且该工具的 `max_parallel` 给足**，所以挡住第二张的只可能是平台那一根轴
    （逐条那根在这一趟明确富余）—— 两根轴互相顶替的写法在这里会读成绿，在那一条契约用例里会红。
    """
    request, snapshot_id = project
    for card_id in ("T01", "T02", "T03"):
        add_card(session, request.id, snapshot_id, card_id)
    slots = slots_for(platform_limit=1, per_agent=9)

    first = dp.dispatch(session, feature_request_id=request.id, task_id="T01", slots=slots, created_at=NOW)
    outcome: list[object] = []
    started = threading.Event()

    def worker() -> None:
        started.set()
        try:
            outcome.append(dp.dispatch(session, feature_request_id=request.id, task_id="T02", slots=slots, created_at=NOW))
        except BaseException as exc:  # 共享会话不是线程安全的：这一趟只关心"它有没有拿到槽"
            outcome.append(exc)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    started.wait(2.0)
    thread.join(0.25)
    assert thread.is_alive(), f"平台上限没挡住第二张卡 ⇒ 上限只是装饰：usage={slots.usage()}"

    dp.release(slots, first)
    thread.join(10.0)
    assert not thread.is_alive(), f"放槽之后第二张仍拿不到槽：usage={slots.usage()}"
    assert len(outcome) == 1 and isinstance(outcome[0], dp.Lease), f"第二张卡没派发成功：{outcome}"
    assert sum(slots.usage().values()) == 1, slots.usage()


@needs_pg
def test_raising_the_platform_limit_by_one_lets_the_second_card_through(session, project):
    """上一条的 GREEN 一侧：同一个形状只把上限从 1 放宽到 2 ⇒ 第二张当场过，一次都不用等。"""
    request, snapshot_id = project
    for card_id in ("T01", "T02"):
        add_card(session, request.id, snapshot_id, card_id)
    slots = slots_for(platform_limit=2, per_agent=9)
    dp.dispatch(session, feature_request_id=request.id, task_id="T01", slots=slots, created_at=NOW)
    second = dp.dispatch(session, feature_request_id=request.id, task_id="T02", slots=slots, created_at=NOW)
    assert second.card_id == "T02"
    assert slots.usage()["qoder"] == 2


@needs_pg
def test_a_failed_database_write_gives_the_slot_back(session, project):
    """落库炸了 ⇒ 槽必须退回。泄漏一格没人知道，而 `usage()` 是唯一权威源（FR-021）。

    注入方式取"写留痕时 `created_at` 为 None"：那一列是 NOT NULL，PG 会在 flush 时拒。
    不 mock、不改实现体 —— 走的就是真异常路径。
    """
    request, snapshot_id = project
    add_card(session, request.id, snapshot_id, "T01")
    slots = slots_for(platform_limit=3)
    raises_code(
        dp.DispatchMisuse,
        "rolled_back_slot",
        lambda: dp.dispatch(
            session, feature_request_id=request.id, task_id="T01", slots=slots, created_at=None
        ),
    )
    assert sum(slots.usage().values()) == 0, slots.usage()
    # 失败的 flush 会让这条连接上的事务脱钩；当场收回会话，免得用例结束时 fixture 再撞一次 rollback。
    session.rollback()


@needs_pg
def test_the_handle_is_not_written_anywhere_but_stays_releaseable(session, project):
    """契约 §6：`SlotHandle` MUST NOT 被序列化进任务卡、日志、产物或 PG。

    三面都查：① EN-14 的 payload 键集就是那五个，不含 handle 或其内容；
    ② `Lease.describe()` 里不含槽位结构（`SlotHandle(`、`task_id=` 这类 repr 片段）；
    ③ 卡行本身仍只有它自己的列（派发不该在卡上留瞬态字段）。
    反面是"留痕里顺手把 handle 存成 dict"——那是给瞬态层造第二个持久化面，恢复时会拿旧槽位当真。
    """
    request, snapshot_id = project
    add_card(session, request.id, snapshot_id, "T01")
    slots = slots_for(platform_limit=3)
    lease = dp.dispatch(session, feature_request_id=request.id, task_id="T01", slots=slots, created_at=NOW)

    row = session.scalars(select(EventLog).where(EventLog.entity_id == "T01")).one()
    assert set(row.payload) == {"node", "action", "agent_id", "card_status_after", "dependencies"}, row.payload
    assert "SlotHandle" not in repr(row.payload)
    assert "SlotHandle" not in lease.describe() and "handle" not in lease.describe().lower()
    card = session.get(TaskCard, (request.id, "T01"))
    assert card.status == "RUNNING"
    dp.release(slots, lease)
    assert sum(slots.usage().values()) == 0


@needs_pg
def test_releasing_twice_is_refused_rather_than_undercounting_the_pool(session, project):
    """`release()` 是薄接线：重复放槽由瞬态层拒，不在这里吞掉。"""
    request, snapshot_id = project
    add_card(session, request.id, snapshot_id, "T01")
    slots = slots_for(platform_limit=3)
    lease = dp.dispatch(session, feature_request_id=request.id, task_id="T01", slots=slots, created_at=NOW)
    dp.release(slots, lease)
    with pytest.raises(SlotConfigError, match="T01"):
        dp.release(slots, lease)


# ---------------------------------------------------------------- 五、收尾退槽（P6-P1-1 / T086 前置）
#
# 上面那些用例都在 `dispatch()` 之后**手工**调 `dp.release(...)`，所以它们证的是"release 这个函数能用"，
# 不是"运行一次卡会把槽退掉"。owner 2026-10-14 裁 P6-P1-1 要的是后者 ⇒ 这一节测装配点本身：
# `scheduler.run_card()` 是生产路径上第一个（也是唯一一个）`release()` 调用方。


def card_result(task_id: str = "T01") -> TaskResult:
    """段输入用的最小自述结果。本节不测门禁，所以产物给足下限，让段体只负担"被走到"这件事。"""
    return TaskResult(
        task_id=task_id,
        self_reported_status="success",
        artifacts=[ArtifactRef(rel_path="index.html", bytes=2048, lines=40, format="html")],
        log_excerpt="build ok",
        self_assessment="已完成",
        usage=[],
        issues=[],
    )


def lease_up(session, request_id, slots, ledger, task_id: str = "T01") -> None:
    """把"图上 `DISPATCH` 节点会做的那一步"在测试里先做掉：派发 + 记进台账。

    **为什么不直接调 `dp.dispatch()` 就算完**：那一发与生产同一处代码（`orchestrate/graph.py::_dispatch`
    调的就是它），而本节的对象是**退槽侧**的生命周期，不是派发判定（派发判定归节 一~四）。
    owner 2026-10-16 裁 甲 之后，`run_card()` 不再自己占槽 ⇒ 每一发的入场券就是这张台账里的凭据。
    """
    lease = dp.dispatch(
        session,
        feature_request_id=request_id,
        task_id=task_id,
        slots=slots,
        created_at=NOW,
    )
    ledger.put(lease)


@needs_pg
def test_a_finished_card_gives_its_slot_back_and_the_assembly_point_is_the_one_who_returns_it(session, project):
    """正常收尾退槽（Phase 6 明写"没有用例覆盖"的那一格）：段内看得见占用，段外必须归零。

    【2026-10-16 裁 甲 的期望值改法，上面那段判据原文不删】：占槽从 `run_card()` 上移到图上的
    `DISPATCH` 节点 ⇒ 本条"段内看得见占用"这一句量的**还是同一件事**，只是占用现在由 `lease_up()`
    交给它（生产里是图节点交）。原来那句"装配点在派发之后立刻占槽"的隐含期望（`run_card` 自己 dispatch）
    已随裁决改掉；改掉的只有这一个来源，`usage_in_segment` 的期望值一字未动。
    """
    request, snapshot_id = project
    add_card(session, request.id, snapshot_id, "T01")
    slots = slots_for(platform_limit=3)
    ledger = sc.LeaseLedger()
    lease_up(session, request.id, slots, ledger)
    inside: dict[str, object] = {}

    def segment(state):
        inside["task_id"] = state["task_id"]
        inside["usage_in_segment"] = dict(slots.usage())
        return {"validated_status": "valid"}

    out = sc.run_card(
        feature_request_id=request.id,
        task_id="T01",
        slots=slots,
        leases=ledger,
        produce=card_result,
        segment=segment,
    )

    assert inside["task_id"] == "T01"
    assert inside["usage_in_segment"] == {"qoder": 1, "workbuddy": 0, "doubao": 0}, (
        "段内看不见占用 ⇒ 装配点是在派发之后才占槽，或者根本没占槽"
    )
    assert out.usage_after == {"qoder": 0, "workbuddy": 0, "doubao": 0}, out.usage_after
    assert out.segment_result == {"validated_status": "valid"}
    assert (out.card_id, out.agent_id) == ("T01", "qoder")


@needs_pg
def test_a_segment_that_raises_still_gives_the_slot_back(session, project):
    """段内报错也必须退槽：一次失败泄漏一格槽，跑几次就把平台上限吃光，表现为"系统卡住"而不是"哪里报错"。

    断的是**原异常照原样抛出**（不是被 `SlotLeak` 顶掉）—— 退槽检查只在"没退干净"这种装配缺陷时才换异常。
    """
    request, snapshot_id = project
    add_card(session, request.id, snapshot_id, "T01")
    slots = slots_for(platform_limit=3)
    ledger = sc.LeaseLedger()
    lease_up(session, request.id, slots, ledger)

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
    assert ledger.pending() == (), "领走的凭据不许回流到台账里"


@needs_pg
def test_more_cards_than_the_platform_cap_finish_instead_of_blocking(session, project):
    """R-07 全链路会真的占槽：上限 3 时连跑 4 张卡必须都跑完（不退槽则第 4 张永久阻塞）。

    这一条是 P6-P1-1 的产品形态判据：它不问"release 存在吗"，问"用得完吗"。
    上限取 `slots_for(platform_limit=3)`，卡数取 4 = 上限 + 1（关系式：多一张就够撞界）。
    """
    request, snapshot_id = project
    for index in range(1, 5):
        add_card(session, request.id, snapshot_id, f"T0{index}")
    slots = slots_for(platform_limit=3)
    ledger = sc.LeaseLedger()
    done: list[str] = []

    def segment(state):
        done.append(state["task_id"])
        return {"validated_status": "valid"}

    for index in range(1, 5):
        task_id = f"T0{index}"
        # 生产里这一步是图上的 `DISPATCH` 节点做的（占槽在上游），这里按同一处代码逐发补上。
        lease_up(session, request.id, slots, ledger, task_id)
        sc.run_card(
            feature_request_id=request.id,
            task_id=task_id,
            slots=slots,
            leases=ledger,
            produce=lambda: card_result(task_id),
            segment=segment,
        )
    assert done == ["T01", "T02", "T03", "T04"], done
    assert sum(slots.usage().values()) == 0, slots.usage()
    assert ledger.pending() == (), ledger.pending()


@needs_pg
def test_a_leaked_slot_is_reported_loudly_instead_of_silently_draining_the_pool(session, project):
    """开火对照（反证 `finally` 里那句占用复查不是死代码）：瞬态层报"还占着" ⇒ 装配点必须抛。

    假的身体只在**这次运行里**存在（`slots` 是个替身），不碰真 `InProcessSlotManager` 的账本，
    所以它不会把别的用例带成红 —— 它要钉的是"退了之后有人读一次 `usage()`"这个动作本身。
    """
    request, snapshot_id = project
    add_card(session, request.id, snapshot_id, "T01")
    real = slots_for(platform_limit=3)

    class LyingSlots:
        """照契约 §6 的三个动词，但 `usage()` 撒谎：永远报有一格占用。"""

        def acquire(self, task_id, agent_id):
            return real.acquire(task_id, agent_id)

        def release(self, handle):
            real.release(handle)

        def usage(self):
            return {"qoder": 1}

    def segment(state):
        return {"validated_status": "valid"}

    ledger = sc.LeaseLedger()
    lease_up(session, request.id, real, ledger)
    with pytest.raises(sc.SlotLeak, match="T01"):
        sc.run_card(
            feature_request_id=request.id,
            task_id="T01",
            slots=LyingSlots(),
            leases=ledger,
            produce=card_result,
            segment=segment,
        )
    # 真账本其实已经退干净了 ⇒ 抛的不是"退槽没调用"，而是"复查这一步真的在看读数"。
    assert sum(real.usage().values()) == 0
