"""T063：人工审批卡点的宪法级判据（FR-041｜R-26，`tasks.md:2394` 的 Independent Test）。

四条判据逐字对着 tasks.md 那一行：
1. 并发投两份结论仅一份生效；
2. 后到记 `duplicate` 不二次迁移；
3. 恢复后**已完成节点执行次数 = 1**（FR-041 的"不得重跑整条链路"）；
4. B 未跑满轮次时超时**绝不**收敛。

**本文件真连 PG**（`ACP_DB_DSN` 缺失时整档 skip）。为什么这四条不能用内存 checkpointer：
内存版活在一个进程里，"挂起 → 另开一个图对象 → 恢复"这条路径它验的是同一份 dict，
而 FR-041 要的是"**现场落在库里**"——`tests/conftest.py` 把 `guards` 档定义成"秒级、不碰 PG"，
本文件与那条口径冲突，已作为待裁登记在 `evidence/phase-04.md §T063`（路径是 `tasks.md:2394` 指定的）。

第三条的两个面都要断：**上游节点没重跑**（拆单器只被调一次、存档点只有一行、卡只有那三行），
以及**卡点节点重跑了**（`interrupt` 的语义就是从节点顶部再走一遍，所以单据主键必须是推导式的）。
只断前者会漏掉"重放插出第二张单"这个真实缺陷；只断后者会把"整条链路重跑"放过去。
"""

from __future__ import annotations

import datetime as dt
import os
import uuid

import psycopg
import pytest
from langgraph.types import Command
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from acp.archive import root as archive_root
from acp.approval import repository as repo
from acp.approval import service
from acp.approval.timeouts import (
    CONVERGE,
    EXTEND,
    KIND_BY_LETTER,
    WAIT,
    decide,
    parse_timeout_table,
    review_round_limit,
)
from acp.config.loader import DSN_ENV, db_url
from acp.orchestrate.graph import (
    ADVANCEABLE,
    OrchestrateDeps,
    advance_request,
    build_graph,
    resume_after_approval,
)
from acp.orchestrate.nodes import approval_gate as approval_gate_module
from acp.orchestrate.nodes.approval_gate import GateMisuse, run_gate
from acp.orchestrate.nodes.split import AcceptanceLink, CardDraft, CardSplitter
from acp.orchestrate.state import IntakeState
from acp.persistence.checkpointer import (
    allowed_modules_for,
    build_checkpointer,
    conninfo,
    ensure_checkpoint_schema,
    thread_config,
    thread_id_for,
)
from acp.persistence.models_approval import ApprovalTicket
from acp.persistence.models_core import FeatureRequest, TaskCard
from acp.persistence.models_registry import Snapshot
from acp.protocol import ArtifactFloor

pytestmark = pytest.mark.skipif(
    not os.environ.get(DSN_ENV),
    reason=f"{DSN_ENV} 未注入 ⇒ 本文件需要 PG（挂起现场与 CAS 只能在库里验）",
)

NOW = dt.datetime(2026, 10, 13, 18, 0, 0, tzinfo=dt.timezone.utc)
FLOOR = ArtifactFloor(min_bytes=512, min_lines=12, format="html")
DIGEST = {"自行补全项": ["页面数量取 1 个首页"], "澄清问题": ["联系方式用邮箱还是表单？"]}
TABLE = parse_timeout_table()


def card(task_id: str, depends_on: list[str] | None = None) -> CardDraft:
    return CardDraft(
        task_id=task_id, title=f"卡 {task_id}", difficulty=2, skill_tags=["html", "css"],
        depends_on=depends_on or [], artifact_floor=FLOOR,
        acceptance_criteria=["首屏有标题", "联系方式可见"],
    )


class CountingSplitter(CardSplitter):
    """拆单器被调几次 = `_split` 节点跑了几次。第三条判据的**执行计数器**就挂在这里。

    为什么用计数器而不是"数库里的行数"：行数只证明"没插出重复行"，而节点重跑不一定插重复行
    （它会先撞主键、或者插出另一批卡号）。调用次数是直接量。
    """

    def __init__(self, drafts: list[CardDraft]):
        self.drafts = drafts
        self.split_calls = 0
        self.link_calls = 0

    def split(self, summary_digest):
        self.split_calls += 1
        return list(self.drafts)

    def acceptance_links(self, summary_digest):
        self.link_calls += 1
        return [
            AcceptanceLink(
                task_id=draft.task_id,
                requirement_item=f"需求条目：{draft.title}",
                acceptance_item=item,
                produced_by="guard-splitter",
            )
            for draft in self.drafts
            for item in draft.acceptance_criteria
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
        try:
            yield s
        finally:
            s.close()
            transaction.rollback()


@pytest.fixture
def checkpointer_conn():
    """checkpointer 用**独立**的 autocommit 连接（`checkpointer.py` 模块头第 1 条）。

    它写下的 checkpoint 行不在业务事务里，所以不会被 `session` 的回滚带走 ⇒
    用例通过 `claim_thread` 登记自己用过的 thread，本 fixture 负责按 thread 清场，
    否则这批行会留在库里成为下一次跑的脏现场。
    """
    connection = psycopg.connect(conninfo(), autocommit=True)
    ensure_checkpoint_schema(connection)
    connection.threads_used = []
    try:
        yield connection
    finally:
        for thread in connection.threads_used:
            with connection.cursor() as cur:
                for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                    cur.execute(f"delete from {table} where thread_id = %s", (thread,))
        connection.close()


def claim_thread(checkpointer_conn, request_id: uuid.UUID) -> str:
    thread = thread_id_for(kind=KIND_BY_LETTER["A"], feature_request_id=request_id)
    checkpointer_conn.threads_used.append(thread)
    return thread


def service_ticket_id(request_id: uuid.UUID) -> uuid.UUID:
    return service.ticket_id_for(kind=KIND_BY_LETTER["A"], feature_request_id=request_id)


def seed_request(session) -> FeatureRequest:
    row = FeatureRequest(
        id=uuid.uuid4(), project_key="demo", raw_input="做个介绍公司产品的静态网站",
        summary_digest=DIGEST, clarification_waived=True, status=ADVANCEABLE, created_at=NOW,
    )
    session.add(row)
    session.flush()
    return row


def saver_for(checkpointer_conn):
    return build_checkpointer(checkpointer_conn, state_schema=IntakeState)


@pytest.fixture(autouse=True)
def archive_bus(bus, monkeypatch):
    """T109 起 `run_graph` 里那次拆单要真打一拍 ⇒ 存档根必须真存在（owner 裁 ㉝ 归 T109）。

    本文件的重点是"挂起 / 恢复"，`_normalize` 只是它经过的一个节点，所以走 autouse 注入而不是
    给每个用例加一个参数。显式传 `bus_root=` 与"没配根就拒"那两面归 `test_snapshot_ref.py`。
    恢复那半边不受影响：`Command(resume=…)` 只重跑被中断的那个节点（本文件自己就钉了
    `split_calls == 0`），所以它不会再打一拍 —— 挂起前后各一个点才是"每轮两拍"，不是"每次 invoke 两拍"。
    """
    monkeypatch.setenv(archive_root.BUS_ROOT_ENV, str(bus.path))
    bus.with_project("demo")
    return bus


def run_graph(session, checkpointer_conn, request_id, drafts):
    splitter = CountingSplitter(drafts)
    result = advance_request(
        request_id, session=session, splitter=splitter, checkpointer=saver_for(checkpointer_conn)
    )
    return splitter, result

# ---------------------------------------------------------------- 挂起 / 恢复本体

def test_the_graph_suspends_at_the_gate_and_leaves_everything_in_the_db(session, checkpointer_conn):
    request = seed_request(session)
    claim_thread(checkpointer_conn, request.id)
    splitter, result = run_graph(session, checkpointer_conn, request.id, [card("T01"), card("T02")])

    assert "__interrupt__" in result, "没挂起 = 卡点没生效"
    assert splitter.split_calls == 1
    # 挂起时库里必须已经有"等人"的两件事实：单据 PENDING + 卡 PENDING_APPROVAL。
    assert session.scalar(text("select count(*) from approval_ticket where state='PENDING'")) == 1
    statuses = {
        row.id: str(row.status)
        for row in session.scalars(select(TaskCard).where(TaskCard.feature_request_id == request.id))
    }
    assert statuses == {"T01": "PENDING_APPROVAL", "T02": "PENDING_APPROVAL"}
    assert str(session.get(FeatureRequest, request.id).status) == "SPLIT"


def test_resume_without_a_human_decision_in_the_db_is_refused(session, checkpointer_conn):
    """**反向判据（本文件最要紧的一条）**：有人 `Command(resume="approve")` 而库里没人批过单 ⇒
    图不许往前走。写反了就是"任何拿到 thread_id 的人都能替 owner 批准"。
    """
    request = seed_request(session)
    claim_thread(checkpointer_conn, request.id)
    run_graph(session, checkpointer_conn, request.id, [card("T01")])
    with pytest.raises(GateMisuse, match="有人 resume 却没有人批过单"):
        resume_after_approval(
            request.id, session=session, splitter=CountingSplitter([card("T01")]),
            checkpointer=saver_for(checkpointer_conn), conclusion="approve",
        )
    statuses = [
        str(row.status)
        for row in session.scalars(select(TaskCard).where(TaskCard.feature_request_id == request.id))
    ]
    assert statuses == ["PENDING_APPROVAL"], f"resume 没生效却改了卡态：{statuses}"


def test_an_unknown_resume_value_is_refused_not_asked_again(session, checkpointer_conn):
    request = seed_request(session)
    claim_thread(checkpointer_conn, request.id)
    run_graph(session, checkpointer_conn, request.id, [card("T01")])
    with pytest.raises(GateMisuse, match="不在 resolution_verdict 域里"):
        resume_after_approval(
            request.id, session=session, splitter=CountingSplitter([card("T01")]),
            checkpointer=saver_for(checkpointer_conn), conclusion="LGTM",
        )


# ---------------------------------------------------------------- 判据 1 + 2：或签只一份生效

def test_two_arriving_conclusions_produce_one_migration_and_one_duplicate(session, checkpointer_conn):
    """US-12 判据 1 + 2 在**图这条链**上的形态：两份结论都投（看板 + CLI），只有一份迁移，
    后到那份额外留下 `applied=false` 的流水；恢复只跑一次图，卡态落定一次。"""
    request = seed_request(session)
    thread = claim_thread(checkpointer_conn, request.id)
    run_graph(session, checkpointer_conn, request.id, [card("T01")])
    ticket_id = service_ticket_id(request.id)

    first = repo.resolve(
        session, ticket_id=ticket_id, verdict="approve", version=0, channel=repo.DASHBOARD,
        principal="owner-1", approvers=frozenset({"owner-1", "owner-2"}), now=NOW,
    )
    second = repo.resolve(
        session, ticket_id=ticket_id, verdict="reject", version=0, channel=repo.CLI,
        principal="owner-2", approvers=frozenset({"owner-1", "owner-2"}), now=NOW + dt.timedelta(minutes=1),
    )
    assert (first.applied, first.code) == (True, None)
    assert (second.applied, second.code, second.logged_as) == (False, "already_decided", "duplicate")

    resume_after_approval(
        request.id, session=session, splitter=CountingSplitter([card("T01")]),
        checkpointer=saver_for(checkpointer_conn), conclusion="approve",
    )
    card_row = session.scalars(
        select(TaskCard).where(TaskCard.feature_request_id == request.id)
    ).one()
    assert str(card_row.status) == "READY"
    assert str(session.get(ApprovalTicket, ticket_id).state) == "APPROVED"
    assert thread.startswith("acp:A_SPLIT:")


def test_the_ticket_key_is_derived_so_replay_inserts_no_second_row(session, checkpointer_conn):
    """`interrupt` 会把节点**从头重跑一遍** ⇒ 单据主键必须是推导式，否则重放插出第二行。

    这一条就是那条实测读数的落点：恢复之后库里仍然只有一张单，且它的 id 与
    `service.ticket_id_for()` 算出来的完全一致。
    """
    request = seed_request(session)
    claim_thread(checkpointer_conn, request.id)
    run_graph(session, checkpointer_conn, request.id, [card("T01")])
    before = session.scalar(text("select count(*) from approval_ticket"))
    ticket_id = service_ticket_id(request.id)

    repo.resolve(
        session, ticket_id=ticket_id, verdict="approve", version=0, channel=repo.DASHBOARD,
        principal="owner-1", approvers=frozenset({"owner-1"}), now=NOW,
    )
    resume_after_approval(
        request.id, session=session, splitter=CountingSplitter([card("T01")]),
        checkpointer=saver_for(checkpointer_conn), conclusion="approve",
    )
    assert session.scalar(text("select count(*) from approval_ticket")) == before == 1
    assert ticket_id == service.ticket_id_for(
        kind=KIND_BY_LETTER["A"], feature_request_id=request.id
    ), "主键不是推导出来的 ⇒ 重放迟早插出第二张单"


# ---------------------------------------------------------------- 判据 3：已完成节点不重跑

def test_resume_reruns_only_the_gate_and_no_upstream_node_twice(session, checkpointer_conn):
    """判据 3 的完整形状：**上游一次、卡点两次**（挂起那次 + 重放那次），三张卡一行不重复。"""
    request = seed_request(session)
    claim_thread(checkpointer_conn, request.id)
    drafts = [card("T01"), card("T02"), card("T03")]
    splitter, _ = run_graph(session, checkpointer_conn, request.id, drafts)
    assert (splitter.split_calls, splitter.link_calls) == (1, 1), "拆单在挂起前只该跑一次"

    ticket_id = service_ticket_id(request.id)
    repo.resolve(
        session, ticket_id=ticket_id, verdict="approve", version=0, channel=repo.DASHBOARD,
        principal="owner-1", approvers=frozenset({"owner-1"}), now=NOW,
    )
    resume_after_approval(
        request.id, session=session, splitter=splitter,
        checkpointer=saver_for(checkpointer_conn), conclusion="approve",
    )

    assert (splitter.split_calls, splitter.link_calls) == (1, 1), (
        f"恢复时上游节点重跑了：split={splitter.split_calls} links={splitter.link_calls}"
    )
    assert session.scalar(select(func.count()).select_from(Snapshot)) == 1, "存档点被写了两行"
    assert session.scalar(
        select(func.count()).select_from(TaskCard).where(TaskCard.feature_request_id == request.id)
    ) == 3, "卡行数变了 ⇒ normalize 重跑过"
    # EN-07 的映射也不许翻倍。
    assert session.scalar(text("select count(*) from acceptance_map")) == 6, "映射翻倍 = 链路重跑"


# ---------------------------------------------------------------- 判据 4：B 未跑满不收敛

def test_b_never_converges_before_the_rounds_are_full():
    """判据 4：第 1、2 轮超时只延长等待；跑满才收敛。上限读宪法（`review_round_limit()`），不是这里的字面量。"""
    limit = review_round_limit()
    assert decide(KIND_BY_LETTER["B"], review_round=1, table=TABLE) == EXTEND
    for round_no in range(1, limit):
        action = decide(KIND_BY_LETTER["B"], review_round=round_no, table=TABLE)
        assert action != CONVERGE, f"第 {round_no} 轮就收敛 = FR-035 的轮次上限被跳过"
    assert decide(KIND_BY_LETTER["B"], review_round=limit - 1, table=TABLE) != CONVERGE
    assert decide(KIND_BY_LETTER["B"], review_round=limit, table=TABLE) == CONVERGE
    # 延长次数跑满之后也只能等待，不能提前收敛（FR-043 的"未跑满时超时只能延长等待"）。
    assert decide(
        KIND_BY_LETTER["B"], review_round=1, extensions_used=3, table=TABLE
    ) == WAIT


def test_a_c_d_timeouts_never_auto_pass_at_any_round():
    """宪法 原则 V 的那句"A/C/D 不得自动放行"：任何轮次、任何延长次数下都只能 WAIT。"""
    for letter in "ACD":
        for round_no in range(0, 6):
            for extensions in range(0, 4):
                action = decide(
                    KIND_BY_LETTER[letter], review_round=round_no,
                    extensions_used=extensions, table=TABLE,
                )
                assert action != CONVERGE, f"{letter} 类在 round={round_no}/ext={extensions} 收敛了"
                assert action == WAIT, f"{letter} 类到点竟然被延长等待：{action}"


# ---------------------------------------------------------------- 卡点的入态守卫

def test_the_gate_only_moves_a_freshly_split_card(session):
    """§2 的边：A 类卡点只动 DRAFT / PENDING_APPROVAL。RUNNING 的卡落到这里 = 派发后又回头 ⇒ 报错。"""
    request = seed_request(session)
    snapshot = Snapshot(
        id=uuid.uuid4(), round_no=1, phase="PRE_WORK", task_card_ids=["T01"],
        ref="local:probe-guard", rolled_back_at=None,
    )
    session.add(snapshot)
    session.flush()
    stuck = TaskCard(
        id="T01", feature_request_id=request.id, title="已在跑的卡", difficulty=2,
        skill_tags=["html"], depends_on=[], artifact_floor_min_bytes=512,
        artifact_floor_min_lines=12, artifact_floor_format="html",
        acceptance_criteria=["首屏有标题"], handoff_count=0, review_round=0,
        status="RUNNING", snapshot_id=snapshot.id,
    )
    session.add(stuck)
    session.flush()

    with pytest.raises(GateMisuse, match="不在合法入态上"):
        run_gate(session, request=request, cards=[stuck], now=NOW)
    assert str(session.get(TaskCard, (request.id, "T01")).status) == "RUNNING", "拒了却还是把在跑的卡改了态"


def test_an_empty_card_set_is_refused_before_the_ticket_is_written(session):
    """空清单不呈交：批一个"看不见的东西"比不批更坏，所以这道守卫在建单**之前**。"""
    request = seed_request(session)
    with pytest.raises(GateMisuse, match="没有任何卡"):
        run_gate(session, request=request, cards=[], now=NOW)
    assert session.scalar(text("select count(*) from approval_ticket")) == 0


def test_system_cards_do_not_trip_the_gate(session, monkeypatch):
    """系统节点卡（文档/检查/验收/打包）不进审批对象集，即使它们建出来就是 `DELIVERED`。

    2026-10-28 真跑实测的断点：`_persist_doc_cards` 把 T90–T92 落成 `DELIVERED` 之后，
    A 类卡点拿**整单所有卡**去套 `ALLOWED_FROM`，三张系统卡被判"不在合法入态上" ⇒
    `GateMisuse` ⇒ 拆单事务整体回滚 ⇒ 卡一张不剩、需求单永久停在 `INTAKE`。
    这条用例把"混着系统卡也能批、且只动执行卡"钉住。

    `interrupt` 换成一个哨兵异常：它在 `_set_card_status` **之后**才被调用，所以
    "跑到哨兵"本身就证明整批过了入态守卫（没有 `GateMisuse`），且此时卡的态已经落笔。
    要验的是**对象集**，不是挂起机制（挂起/恢复那一组另有 6 条，真跑图）。
    """

    class _AtInterrupt(Exception):
        """走到挂起点就停：不假装人批了（库里那张单还是 PENDING，假批会被卡点自己查出来）。"""

    request = seed_request(session)
    snapshot = Snapshot(
        id=uuid.uuid4(), round_no=1, phase="PRE_WORK", task_card_ids=["T01", "T90"],
        ref="local:probe-system-card", rolled_back_at=None,
    )
    session.add(snapshot)
    session.flush()
    fresh = TaskCard(
        id="T01", feature_request_id=request.id, title="刚拆完的卡", difficulty=2,
        skill_tags=["html"], depends_on=[], artifact_floor_min_bytes=512,
        artifact_floor_min_lines=12, artifact_floor_format="html",
        acceptance_criteria=["首屏有标题"], handoff_count=0, review_round=0,
        status="DRAFT", snapshot_id=snapshot.id,
    )
    done = TaskCard(
        id="T90", feature_request_id=request.id, title="PRD 已定稿", difficulty=1,
        skill_tags=["system"], depends_on=[], artifact_floor_min_bytes=100,
        artifact_floor_min_lines=4, artifact_floor_format="md",
        acceptance_criteria=["含页面清单"], handoff_count=0, review_round=0,
        status="DELIVERED", snapshot_id=snapshot.id,
    )
    session.add_all([fresh, done])
    session.flush()

    def _stop(payload):
        raise _AtInterrupt(payload["pending_item"])

    monkeypatch.setattr(approval_gate_module, "interrupt", _stop)

    with pytest.raises(_AtInterrupt):
        run_gate(session, request=request, cards=[fresh, done], now=NOW)

    assert str(session.get(TaskCard, (request.id, "T01")).status) == "PENDING_APPROVAL", "执行卡没挂到卡点上"
    assert str(session.get(TaskCard, (request.id, "T90")).status) == "DELIVERED", "系统卡被卡点改动了"


def test_a_request_with_only_system_cards_still_refuses_to_open_a_ticket(session):
    """全是系统卡 ⇒ 过滤后为空 ⇒ 仍然拒。否则会呈交一张"没有任何执行卡"的单给人批。"""
    request = seed_request(session)
    snapshot = Snapshot(
        id=uuid.uuid4(), round_no=1, phase="PRE_WORK", task_card_ids=["T90"],
        ref="local:probe-only-system", rolled_back_at=None,
    )
    session.add(snapshot)
    session.flush()
    done = TaskCard(
        id="T90", feature_request_id=request.id, title="PRD 已定稿", difficulty=1,
        skill_tags=["system"], depends_on=[], artifact_floor_min_bytes=100,
        artifact_floor_min_lines=4, artifact_floor_format="md",
        acceptance_criteria=["含页面清单"], handoff_count=0, review_round=0,
        status="DELIVERED", snapshot_id=snapshot.id,
    )
    session.add(done)
    session.flush()

    with pytest.raises(GateMisuse, match="没有任何卡"):
        run_gate(session, request=request, cards=[done], now=NOW)
    assert str(session.get(TaskCard, (request.id, "T90")).status) == "DELIVERED"
    assert session.scalar(text("select count(*) from approval_ticket")) == 0


# ---------------------------------------------------------------- thread 与序列化

def test_the_thread_id_is_derived_from_the_columns_and_is_stable_across_calls():
    """FR-041 的"同一 `thread_id` 恢复"不是靠谁记住它：它是 (类, 需求单[, 卡]) 的纯函数。"""
    request_id = uuid.uuid4()
    other = uuid.uuid4()
    kind = KIND_BY_LETTER["A"]
    first = thread_id_for(kind=kind, feature_request_id=request_id)
    assert first == thread_id_for(kind=kind, feature_request_id=request_id)
    assert first != thread_id_for(kind=kind, feature_request_id=other)
    assert first != thread_id_for(kind=KIND_BY_LETTER["D"], feature_request_id=request_id)
    assert first != thread_id_for(kind=kind, feature_request_id=request_id, task_card_id="T01")
    assert thread_config(kind=kind, feature_request_id=request_id) == {
        "configurable": {"thread_id": first}
    }


def test_the_allowlist_is_derived_from_the_state_annotations_not_hand_written():
    assert set(allowed_modules_for(IntakeState)) == {
        ("acp.orchestrate.nodes.split", "CardDraft"),
        ("acp.orchestrate.nodes.split", "AcceptanceLink"),
    }


def test_a_checkpoint_written_with_the_deried_allowlist_comes_back_as_the_same_objects(
    checkpointer_conn,
):
    """**序列化保真**（不是"零告警"）：实测 LangGraph 对未注册类是 **拒绝并降级**，
    走 `logger.warning` 而非 `warnings` —— 所以断"没有 warning"是一条死判据（恒绿），
    要断的是"值取回来还是不是那个对象"。

    反向对照在下一条：白名单给空列表 ⇒ 同一份 payload 取回来**不等**。
    """
    draft = card("T01")
    serde = build_checkpointer(checkpointer_conn, state_schema=IntakeState).serde
    tagged = serde.dumps_typed({"drafts": [draft], "links": []})
    restored = serde.loads_typed(tagged)
    assert restored["drafts"] == [draft], "白名单没起作用 ⇒ 恢复时 state 里的草稿是降级形状"
    assert type(restored["drafts"][0]) is CardDraft


def test_an_empty_allowlist_blocks_the_same_payload_and_degrades_it(checkpointer_conn):
    """变异对照（这条**该**红，红在原实现之外）：白名单清空，同一份 payload 必须回不来。
    它证明上一条用例断的"相等"是白名单买来的，不是 msgpack 本来就认得自定义类。"""
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    draft = card("T01")
    serde = JsonPlusSerializer(allowed_msgpack_modules=[])
    tagged = serde.dumps_typed({"drafts": [draft], "links": []})
    restored = serde.loads_typed(tagged)
    assert restored["drafts"] != [draft], "空名单也能原样取回 ⇒ 上面那条白名单用例是空对空"
    assert type(restored["drafts"][0]) is not CardDraft


def test_the_suspend_resume_round_trip_survives_a_fresh_graph_object(session, checkpointer_conn):
    """FR-041 的"重启恢复"含义：**恢复用的是另一个 compile 出来的图**（新 checkpointer 对象、
    同一条连接），现场从库里读回来 —— 内存版 saver 做不到这件事，所以这条 MUST 走 PG。
    """
    request = seed_request(session)
    claim_thread(checkpointer_conn, request.id)
    drafts = [card("T01"), card("T02")]
    run_graph(session, checkpointer_conn, request.id, drafts)
    ticket_id = service_ticket_id(request.id)
    repo.resolve(
        session, ticket_id=ticket_id, verdict="approve", version=0, channel=repo.DASHBOARD,
        principal="owner-1", approvers=frozenset({"owner-1"}), now=NOW,
    )

    splitter = CountingSplitter(drafts)
    fresh_graph = build_graph(
        OrchestrateDeps(session=session, splitter=splitter, checkpointer=saver_for(checkpointer_conn))
    )
    result = fresh_graph.invoke(
        Command(resume="approve"),
        thread_config(kind=KIND_BY_LETTER["A"], feature_request_id=request.id),
    )
    assert splitter.split_calls == 0, "换了图对象就从头重跑 ⇒ thread 现场没被读回来"
    assert result.get("gate_decision") == "approve"
    assert {
        str(row.status)
        for row in session.scalars(select(TaskCard).where(TaskCard.feature_request_id == request.id))
    } == {"READY"}


def test_the_pending_ticket_carries_the_five_elements_the_gate_rendered(session, checkpointer_conn):
    """挂起时呈交的是 FR-039 的五要素（唯一载体在 `approval/service.py`）。"""
    request = seed_request(session)
    claim_thread(checkpointer_conn, request.id)
    run_graph(session, checkpointer_conn, request.id, [card("T01")])
    row = session.get(ApprovalTicket, service_ticket_id(request.id))
    assert set(row.payload) == set(service._FIVE)
    assert row.payload["suggested_verdict"] == "approve"
    assert row.payload["impact"]["downstream_task_ids"] == ["T01"]
    assert row.payload["impact"]["estimated_extra_usage"] == {}, (
        "账本归 T114：这里必须是空映射，不许凑一个 0 冒充「零用量」"
    )


# ---------------------------------------------------------------- 等待点不许被覆盖（U1）

def test_advancing_a_parked_request_is_refused_and_the_waiting_point_survives(
    session, checkpointer_conn
):
    """停在卡点之后再调一次 `advance_request` ⇒ 拒绝，且 `next` 仍然是 `approval_gate`（U1）。

    这是 2026-10-28 真跑里"一次误触发废掉一张单"的正身：`graph.invoke(dict)` 不是"从上次停的地方
    继续"，而是把新输入并进已有 state 再从入口走一遍 —— 而 LangGraph 在**节点开跑之前**就落了
    一版 checkpoint（`next=('plan',)`）。于是等待点被覆盖，随后 `_plan` 撞 `AlreadyAdvanced`。
    之后 `run_resume` 读到 `next` 非空（绕过 `no_waiting_point` 那道守卫）⇒ 从 `plan` 续跑 ⇒
    必然再撞 `AlreadyAdvanced`。只能删 checkpoint 才救得回来。

    所以判据量的不是"有没有报错"，而是**报错之后等待点还在不在**。
    """
    from acp.orchestrate.graph import (
        APPROVAL_GATE,
        GATE_KIND,
        AlreadyAdvanced,
        build_graph,
        thread_config,
    )

    request = seed_request(session)
    claim_thread(checkpointer_conn, request.id)
    run_graph(session, checkpointer_conn, request.id, [card("T01")])

    graph = build_graph(
        OrchestrateDeps(
            session=session,
            splitter=CountingSplitter([card("T01")]),
            checkpointer=saver_for(checkpointer_conn),
        )
    )
    before = tuple(str(node) for node in (graph.get_state(thread_config(kind=GATE_KIND, feature_request_id=request.id)).next or ()))
    assert before == (APPROVAL_GATE,), f"前置：首发应当停在卡点，实际 {before}"

    with pytest.raises(AlreadyAdvanced, match="人工卡点"):
        advance_request(
            request.id,
            session=session,
            splitter=CountingSplitter([card("T01")]),
            checkpointer=saver_for(checkpointer_conn),
        )

    after = tuple(str(node) for node in (graph.get_state(thread_config(kind=GATE_KIND, feature_request_id=request.id)).next or ()))
    assert after == before, f"等待点被这次误推进覆盖了：{before} → {after}"
