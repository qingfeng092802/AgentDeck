"""T112（FR-064｜R-14）：两个演练场景的恢复判据 —— 无重复任务卡、已完成任务不被重跑、PG 回来后按一致性规则校验。

**"三条回退路径"在本文件里怎么数的**（对位 owner 2026-10-16 那句"三条回退路径 MUST 有可跑用例"、
宪法「三」崩溃回退那三段与 `contracts/file-bus.md §6` 的关系式一）：

| 路径 | 宪法 / 契约原文 | 入口 | 节 |
|---|---|---|---|
| 一 | "LangGraph state 损坏 → 从最近一次 git 存档点重建执行态，重放该存档点之后的任务事件" | `recover --from-snapshot` | 一 |
| 二 | "PostgreSQL 断连 → 从文件总线重建最小状态…三元组" | `recover --from-file-bus` | 二 |
| 三 | 同第二段末句"PG 恢复后按一致性规则回填" | `recover --verify-consistency` | 三 |

档位：`tests/guards/`，混合文件。一、三节要真 PG（一节还要真 checkpointer）；二节那两条不连库 ——
**"不连库"本身就是那条路径的判据**（它要证的正是"没有 PG 也捞得出来"）。

**"回填"的写入侧本文件不交，这是决定不是遗漏**：把 `status.md` 的值写回 EN-02 就是给
`task_card.status` 造第二个写者（唯一写者是 `persistence/states.py` 那张迁移表 + 各节点），
Phase 4 之后的整条状态迁移口径会被推翻。所以 `reconcile()` 只成对比、只报差额；写入归 T123 的
装配点，且 MUST 先由 owner 裁"哪一列允许被回填"（登记在 `tasks.md` 的 T112 追记里）。

**两条实测才找到的形状**（都留成用例，不留在注释里）：
① 存档点如果没被"按项目的卡反查"捞出来，恢复读到的是 `no_points` 而不是那个坏点 ——
   所以本文件的夹具一律**先打点、再建卡**（`task_card.snapshot_id` 是 NOT NULL 的 FK，反过来建不出来）；
② `self_reported_status` 的域值是 `success` / `failed`，不是 `completed` —— 第三节的产物行
   一开始被 PG 枚举拒掉，报的是"驱动异常"而不是"你写的值不在域里"，所以这一格也留了断言。
"""

from __future__ import annotations

import ast
import datetime as dt
import inspect
import io
import os
import uuid
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

import acp
from acp import chaos, recover
from acp.archive import cadence
from acp.bus import layout
from acp.bus.gateway import BusGateway, TaskCardBody
from acp.bus.orchestrator_writer import OrchestratorWriter, StatusRow
from acp.config.loader import DSN_ENV, db_url
from acp.persistence import checkpointer as cp
from acp.persistence import snapshots as snap
from acp.persistence.models_core import Artifact, FeatureRequest, TaskCard
from acp.persistence.models_registry import EventLog, Snapshot

SRC_ROOT = Path(acp.__file__).resolve().parent
REPO_ROOT = SRC_ROOT.parents[1]
PROJECT = "acp-recovery"
OTHER_PROJECT = "acp-other-project"
CARD_A = "T01"
CARD_B = "T02"
NOW = dt.datetime(2026, 10, 16, 11, 0, tzinfo=dt.timezone.utc)
AGENTS = ("workbuddy", "qoder", "doubao")
DIGEST = {"自行补全项": ["页面数量取 1 个首页"], "澄清问题": ["联系方式用邮箱还是表单？"]}
BODY = TaskCardBody(
    objective="产出静态首页",
    inputs=f"tasks/{CARD_A}-home.md",
    expected_output="qoder-workspace/index.html",
)
FIVE_CLASSES = {
    "artifact_floor": "min_bytes=200, min_lines=8, format=html",
    "acceptance_criteria": "[含 h1 标题, 首屏无外链脚本]",
    "token_budget": "1000",
    "executor_agent_id": "qoder",
    "reviewer_agent_id": "workbuddy",
    "acceptor_agent_id": "doubao",
    "review_round": "1",
    "handoff_count": "0",
}
REL_PATH = "qoder-workspace/index.html"


# ---------------------------------------------------------------- 夹具

@pytest.fixture
def engine():
    if not os.environ.get(DSN_ENV):
        pytest.skip(f"缺 {DSN_ENV} ⇒ 本条要真 PG，整条 skip（不是绿）")
    eng = create_engine(db_url(), future=True)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine):
    conn = engine.connect()
    trans = conn.begin()
    yield Session(bind=conn)
    trans.rollback()
    conn.close()


@pytest.fixture
def checkpointer_conn():
    if not os.environ.get(DSN_ENV):
        pytest.skip(f"缺 {DSN_ENV} ⇒ 这一节要真 checkpointer，整条 skip（不是绿）")
    connection = psycopg.connect(cp.conninfo(), autocommit=True)
    cp.ensure_checkpoint_schema(connection)
    connection.threads_used = []
    try:
        yield connection
    finally:
        for thread in connection.threads_used:
            with connection.cursor() as cursor:
                for table in chaos.THREAD_TABLES:
                    cursor.execute(f'DELETE FROM "{table}" WHERE thread_id = %s', (thread,))
        connection.close()


@pytest.fixture
def writer(bus):
    """总管单点写者，指向 `<总线根>/<项目>` —— 就是 T109 定稿的那个存档根**同一个目录**。

    两个角色共用一个目录不是巧合，是那一轮的决定（`revert 可复原`、`assemble` 的入口文件检查、
    `recover --from-file-bus` 三者要作用在同一份现场上）。所以这里同时是"存档根"与"总线项目目录"。
    `stop()` 在收尾调：`_submit` 是同步排队的，但工作线程不停就会漏给下一个用例。
    """
    instance = OrchestratorWriter(BusGateway(bus.with_project(PROJECT)))
    instance.ensure_layout(AGENTS)
    yield instance
    instance.stop()


class StubSplitter:
    """占位拆单器（协议同 `test_orchestrate_graph.StubSplitter`）：`calls` 就是"有没有被重跑"的那只表。

    本文件第一节把它的计数当主判据用：恢复之后 MUST 还是 1。这不是"顺手记个数"——
    FR-064 那句"已完成任务不被重跑"在生产里对应的就是"又一次模型调用 + 又一次计费"，
    而这条链上唯一能观测到"重跑"的地方就是拆单器被问了几回。
    """

    def __init__(self, task_ids: list[str]) -> None:
        from acp.orchestrate.nodes.split import AcceptanceLink, CardDraft
        from acp.protocol import ArtifactFloor

        self._link = AcceptanceLink
        self.drafts = [
            CardDraft(
                task_id=task_id,
                title=f"卡 {task_id}",
                difficulty=2,
                skill_tags=["html", "css"],
                depends_on=[],
                artifact_floor=ArtifactFloor(min_bytes=200, min_lines=8, format="html"),
                acceptance_criteria=["含 h1 标题", "首屏无外链脚本"],
            )
            for task_id in task_ids
        ]
        self.calls = 0

    def split(self, summary_digest):
        self.calls += 1
        return list(self.drafts)

    def acceptance_links(self, summary_digest):
        return [
            self._link(
                task_id=draft.task_id,
                requirement_item=f"需求条目：{draft.title}",
                acceptance_item=item,
                produced_by="stub-orchestrator",
            )
            for draft in self.drafts
            for item in draft.acceptance_criteria
        ]


def _intake_state():
    from acp.orchestrate.state import IntakeState

    return IntakeState


def _request(session: Session, request_id: uuid.UUID) -> None:
    session.add(
        FeatureRequest(
            id=request_id,
            project_key=PROJECT,
            raw_input="做个介绍公司产品的静态网站",
            summary_digest=DIGEST,
            clarification_waived=True,
            status="INTAKE",
            created_at=NOW,
        )
    )
    session.flush()


def _agent(session: Session, agent_id: str = "qoder") -> None:
    """EN-03 那一侧先有主体，EN-02 / EN-06 的 FK 才落得下去（实测：不建就会红在 FK 上，
    而报的是 `ForeignKeyViolation` 而不是"夹具忘了"，所以这一步单独抽出来）。
    """
    from acp.persistence.models_core import AgentUnit

    session.add(
        AgentUnit(
            agent_id=agent_id,
            form="SDK",
            tier=2,
            cost_band="MID",
            max_parallel=2,
            current_load=0,
            historical_success_rate=Decimal("0.5"),
            success_rate_sample=4,
            workspace_dir=f"/tmp/{agent_id}-workspace",
            enabled=True,
        )
    )
    session.flush()


def _card(session: Session, request_id: uuid.UUID, task_id: str, snapshot_id: uuid.UUID) -> None:
    """给第三 / 四节直接搭一张卡（不经拆单）：那两节要测的是产物两侧成对，不是拆单。

    三个岗只填执行者：`reviewer_agent_id` / `acceptor_agent_id` 也是 FK，为这一节多建两支工具
    没有信息量（差额判定只看 `(卡号, rel_path)`）。
    """
    session.add(
        TaskCard(
            id=task_id,
            feature_request_id=request_id,
            title=f"卡 {task_id}",
            difficulty=2,
            skill_tags=["html"],
            depends_on=[],
            executor_agent_id="qoder",
            executor_role_id=None,
            reviewer_agent_id=None,
            acceptor_agent_id=None,
            artifact_floor_min_bytes=200,
            artifact_floor_min_lines=8,
            artifact_floor_format="html",
            acceptance_criteria=["含 h1 标题"],
            handoff_count=0,
            review_round=1,
            status="DELIVERED",
            snapshot_id=snapshot_id,
        )
    )
    session.flush()


def _point(session: Session, project_dir: Path, cards: list[str]) -> Snapshot:
    """真打一拍存档点（盘上有对象、库里有留痕），回 EN-12 那一行。"""
    committed = cadence.commit_archive_point(
        project_dir,
        phase=cadence.OPEN_PHASE,
        round_no=1,
        project_key=PROJECT,
        task_card_ids=cards,
    )
    return snap.record_point(
        session,
        project_key=PROJECT,
        round_no=1,
        phase=cadence.OPEN_PHASE,
        task_card_ids=cards,
        ref=committed.ref,
        now=NOW,
    )


def _artifact(session: Session, request_id: uuid.UUID, task_id: str, rel_path: str) -> None:
    session.add(
        Artifact(
            id=uuid.uuid4(),
            feature_request_id=request_id,
            task_card_id=task_id,
            writer_agent_id="qoder",
            rel_path=rel_path,
            bytes=512,
            lines=18,
            format="html",
            self_reported_status="success",
            validated_status=None,
            invalid_reason=None,
            created_at=NOW,
        )
    )
    session.flush()
    assert session.scalar(
        select(func.count()).select_from(Artifact).where(Artifact.rel_path == rel_path)
    ) == 1, "产物行没落库 = 枚举值写错了，先红在这里而不是红在差额断言上"


def _rows(session: Session) -> tuple[int, int, int, int]:
    return (
        int(session.scalar(select(func.count()).select_from(TaskCard)) or 0),
        int(session.scalar(select(func.count()).select_from(EventLog)) or 0),
        int(session.scalar(select(func.count()).select_from(Snapshot)) or 0),
        int(session.scalar(select(func.count()).select_from(Artifact)) or 0),
    )


def _seed_project(writer: OrchestratorWriter, *, index: bool = True) -> None:
    writer.write_agents_md(PROJECT, AGENTS)
    writer.write_task_card(CARD_A, "home", BODY, FIVE_CLASSES)
    writer.upsert_status(
        StatusRow(
            task_id=CARD_A,
            status="DELIVERED",
            executor="qoder",
            reviewer="workbuddy",
            acceptor="doubao",
            artifact_path=REL_PATH,
            usage={"TOKEN": Decimal("0"), "SEAT_CREDIT": Decimal("3"), "BYOK": Decimal("0")},
        )
    )
    if index:
        writer.write_result_index(CARD_A, [(REL_PATH, 512, 18, "html")])


# ---------------------------------------------------------------- 一、路径一：state 损坏 → 从存档点恢复

def test_scenario_one_recovers_without_producing_a_second_set_of_cards(
    session: Session, checkpointer_conn, bus
) -> None:
    """跑完整条推进链 → 注入损坏 → 恢复 ⇒ **卡还是那两张，四张表一行都不多**。

    这是 owner 那句"两场景恢复后均无重复任务卡"的第一场景正身。它走完整链而不是手拼现场：
    `advance_request` 让 `_normalize` 真打一拍、真建两张卡、真挂起在 A 类卡点，
    然后 `chaos` 把这个 thread 删空，`recover` 从 EN-12 那行把执行态写回去。
    """
    from acp.orchestrate import graph

    project_dir = bus.with_project(PROJECT)
    request_id = uuid.uuid4()
    _request(session, request_id)
    splitter = StubSplitter([CARD_A, CARD_B])
    saver = cp.build_checkpointer(checkpointer_conn, state_schema=_intake_state())

    graph.advance_request(
        request_id, session=session, splitter=splitter, checkpointer=saver, bus_root=bus.path
    )
    before = _rows(session)
    assert before[0] == 2, "前置：拆单本身要真建出两张卡，否则下面那句「无重复」是空的"

    thread = chaos.thread_id(kind=graph.GATE_KIND, feature_request_id=request_id)
    checkpointer_conn.threads_used.append(thread)
    chaos.kill_graph_state(checkpointer_conn, thread)

    recovery = recover.restore_execution_state(
        session, project_key=PROJECT, bus_root=bus.path, saver=saver, moment=NOW
    )

    assert _rows(session) == before, "恢复之后四张表的行数与恢复前逐位相等"
    assert recovery.next_nodes == (graph.APPROVAL_GATE,)
    assert sorted(recovery.restoration.cards_at_point) == [CARD_A, CARD_B]
    assert splitter.calls == 1, "拆单器被问第二次 = 整链路重跑"


def test_completed_work_keeps_its_status_and_its_events_across_the_recovery(
    session: Session, checkpointer_conn, bus
) -> None:
    """已完成的任务不被重跑：**状态逐条不变、流水不新增、回退只落在那一列上**。"""
    from acp.orchestrate import graph

    bus.with_project(PROJECT)
    request_id = uuid.uuid4()
    _request(session, request_id)
    saver = cp.build_checkpointer(checkpointer_conn, state_schema=_intake_state())
    graph.advance_request(
        request_id,
        session=session,
        splitter=StubSplitter([CARD_A, CARD_B]),
        checkpointer=saver,
        bus_root=bus.path,
    )
    statuses_before = list(session.scalars(select(TaskCard.status).order_by(TaskCard.id)))
    events_before = int(session.scalar(select(func.count()).select_from(EventLog)) or 0)

    thread = chaos.thread_id(kind=graph.GATE_KIND, feature_request_id=request_id)
    checkpointer_conn.threads_used.append(thread)
    chaos.kill_graph_state(checkpointer_conn, thread)
    recovery = recover.restore_execution_state(
        session, project_key=PROJECT, bus_root=bus.path, saver=saver, moment=NOW
    )

    assert list(session.scalars(select(TaskCard.status).order_by(TaskCard.id))) == statuses_before
    assert int(session.scalar(select(func.count()).select_from(EventLog)) or 0) == events_before, (
        "回退不写流水：EN-12 的 `rolled_back_at` 就是它的载体（D-12 那句「回退动作落审计」由这一列交）"
    )
    assert session.get(Snapshot, recovery.restoration.snapshot_id).rolled_back_at == NOW


def test_a_second_recovery_on_the_same_point_still_adds_no_rows(
    session: Session, checkpointer_conn, bus
) -> None:
    """幂等的可跑形态：**同一个点恢复两次**，业务表一行不多，只有 checkpoint 多一个。

    checkpoint 是图自己的历史（不是事实态），多一个是对的；"多按一次恢复键会不会造出第二套卡"
    才是 owner 那句话里要钉的那一件。
    """
    from acp.orchestrate import graph

    bus.with_project(PROJECT)
    request_id = uuid.uuid4()
    _request(session, request_id)
    splitter = StubSplitter([CARD_A])
    saver = cp.build_checkpointer(checkpointer_conn, state_schema=_intake_state())
    graph.advance_request(
        request_id, session=session, splitter=splitter, checkpointer=saver, bus_root=bus.path
    )
    thread = chaos.thread_id(kind=graph.GATE_KIND, feature_request_id=request_id)
    checkpointer_conn.threads_used.append(thread)
    chaos.kill_graph_state(checkpointer_conn, thread)
    before = _rows(session)

    recover.restore_execution_state(
        session, project_key=PROJECT, bus_root=bus.path, saver=saver, moment=NOW
    )
    middle = _rows(session)
    recover.restore_execution_state(
        session, project_key=PROJECT, bus_root=bus.path, saver=saver, moment=NOW
    )

    assert middle == before and _rows(session) == before
    assert chaos.read_thread_rows(checkpointer_conn, thread).counts[2][1] == 2
    assert splitter.calls == 1


def test_the_recovery_does_not_reopen_delivered_work(session: Session, checkpointer_conn, bus) -> None:
    """恢复不许把已交付的卡重新开放：卡号唯一性 + 报告里的状态 = 库里的状态。

    与第三节那条分开设是因为它们挡的是两种不同的错：一防"多插行"，一防"改已有行的状态"。
    后者在行数上完全看不出来，是一张卡都没多、语义上却把已交付的活重新变成可调度。
    """
    from acp.orchestrate import graph

    bus.with_project(PROJECT)
    request_id = uuid.uuid4()
    _request(session, request_id)
    saver = cp.build_checkpointer(checkpointer_conn, state_schema=_intake_state())
    graph.advance_request(
        request_id,
        session=session,
        splitter=StubSplitter([CARD_A, CARD_B]),
        checkpointer=saver,
        bus_root=bus.path,
    )
    thread = chaos.thread_id(kind=graph.GATE_KIND, feature_request_id=request_id)
    checkpointer_conn.threads_used.append(thread)
    chaos.kill_graph_state(checkpointer_conn, thread)

    recovery = recover.restore_execution_state(
        session, project_key=PROJECT, bus_root=bus.path, saver=saver, moment=NOW
    )

    pairs = list(session.execute(select(TaskCard.feature_request_id, TaskCard.id)).all())
    assert len(pairs) == len({(str(a), str(b)) for a, b in pairs}), "出现重复主键 = 恢复造了第二套卡"
    in_db = {(str(row[0]), str(row[1])) for row in recovery.restoration.card_statuses}
    assert in_db <= {
        (str(row[0]), str(row[1])) for row in session.execute(select(TaskCard.id, TaskCard.status)).all()
    }, "恢复报告里的状态必须就是库里的状态，不是它自己算出来的第三份"


# ---------------------------------------------------------------- 二、路径二：PG 断连 → 从文件总线重建

def test_scenario_two_rebuilds_the_triple_without_any_dsn(monkeypatch, writer) -> None:
    """**整条路径一次都不读 `ACP_DB_DSN`**：删掉那个变量，三元组照样重建。

    这是"回退路径不得只在文档里存在"在本项目的具体形状：契约 §3 条件③（正文三行不依赖
    frontmatter）+ 本用例的 `delenv`，两个条件合起来才把"其实偷偷连了库"这条路堵死。
    """
    monkeypatch.delenv(DSN_ENV, raising=False)
    _seed_project(writer)

    result = recover.rebuild(writer.gateway.root.parent, PROJECT)

    assert result.pg_side_checked is False
    assert [triple.task_id for triple in result.triples] == [CARD_A]
    assert result.triples[0].status == "DELIVERED"
    assert result.triples[0].artifacts == ((REL_PATH, 512, 18, "html"),)
    assert set(inspect.signature(recover.rebuild).parameters) == {"bus_root", "project_key"}, (
        "总线侧那条路 MUST NOT 有第三个「会话」参数：一给会话就等于把「没有 PG」这个前提收回去了"
    )


def test_scenario_two_fails_loudly_on_a_missing_status_row_and_writes_nothing(
    monkeypatch, writer, bus
) -> None:
    """缺 `status.md` 那一行 ⇒ CLI 退出码 1 且点名差在哪；这条路永远是只读的。"""
    monkeypatch.delenv(DSN_ENV, raising=False)
    writer.write_agents_md(PROJECT, AGENTS)
    writer.write_task_card(CARD_A, "home", BODY, FIVE_CLASSES)

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = recover.main(["recover", "--from-file-bus", PROJECT, "--bus-root", str(bus.path)])
    out = buffer.getvalue()

    assert code == 1 and "status.md" in out and CARD_A in out
    assert "RECOVER OK" not in out
    assert (bus.path / PROJECT / layout.TASKS_DIR / f"{CARD_A}-home.md").is_file(), (
        "拒的是缺状态行，不是把已经落盘的任务卡删掉：回退路径不毁现场"
    )


def test_the_two_paths_do_not_answer_for_each_other(writer) -> None:
    """两个模式的读数各说各的：`--from-file-bus` 的 `pg_side_checked` MUST 仍是 `False`，
    而 `--verify-consistency` 那一格才是 `True`。

    这条防的是"第三个模式建成之后，第一个模式被顺手读成已全验"。`Reconstruction` 与 `Consistency`
    是两个类、两个属性值，不是一个全局开关。
    """
    _seed_project(writer, index=False)

    reconstruction = recover.rebuild(writer.gateway.root.parent, PROJECT)

    assert reconstruction.pg_side_checked is False
    assert recover.Consistency(pairs=0, bus_only=(), pg_only=()).pg_side_checked is True


# ---------------------------------------------------------------- 三、路径三：PG 回来后的一致性校验

def test_verify_consistency_passes_when_both_sides_hold_the_same_pairs(session, writer) -> None:
    """契约 §6 关系式一第一次**两侧都数**：同一批 `(卡号, rel_path)` 在两边各出现一次 ⇒ 通过。

    两侧同源（都来自 `TaskResult.artifacts` 的 `ref.rel_path`），所以按成对比而不是只比总数；
    只比总数会放过"一边少一条、另一边多一条"那种抵消掉的形状。
    """
    request_id = uuid.uuid4()
    _request(session, request_id)
    point = _point(session, writer.gateway.root, [CARD_A])
    _agent(session)
    _card(session, request_id, CARD_A, point.id)
    _seed_project(writer)
    _artifact(session, request_id, CARD_A, REL_PATH)

    result = recover.reconcile(writer.gateway.root.parent, PROJECT, session)

    assert result.pairs == 1 and result.bus_only == () and result.pg_only == ()
    assert result.pg_side_checked is True


def test_verify_consistency_names_the_drift_in_both_directions(session, writer) -> None:
    """两个方向各造一次差额，两个方向都 MUST 被点名（只测一边 = 另一边的过滤条件是死的）。"""
    request_id = uuid.uuid4()
    _request(session, request_id)
    point = _point(session, writer.gateway.root, [CARD_A])
    _agent(session)
    _card(session, request_id, CARD_A, point.id)
    _seed_project(writer)

    with pytest.raises(recover.ConsistencyError) as missing_in_pg:
        recover.reconcile(writer.gateway.root.parent, PROJECT, session)
    assert REL_PATH in str(missing_in_pg.value), "只在总线侧的那一条要能被点名，否则差额报告是空的"

    extra = "qoder-workspace/extra.html"
    _artifact(session, request_id, CARD_A, extra)

    with pytest.raises(recover.ConsistencyError) as both:
        recover.reconcile(writer.gateway.root.parent, PROJECT, session)
    assert extra in str(both.value), "只报总线侧 = 反方向的过滤条件是死的"


def test_reconcile_writes_nothing_because_the_backfill_side_is_not_authorized(session, writer) -> None:
    """`reconcile()` 只报差额、不回填：四张表行数前后逐位相等，**包括抛出去那一发**。

    先跑一次会抛的差额再数行数 —— 失败的一侧也不留痕迹，这才是"只读"而不是"通常只读"。
    """
    request_id = uuid.uuid4()
    _request(session, request_id)
    point = _point(session, writer.gateway.root, [CARD_A])
    _agent(session)
    _card(session, request_id, CARD_A, point.id)
    _seed_project(writer)
    before = _rows(session)

    with pytest.raises(recover.ConsistencyError):
        recover.reconcile(writer.gateway.root.parent, PROJECT, session)

    assert _rows(session) == before
    statuses = list(session.scalars(select(TaskCard.status).order_by(TaskCard.id)))
    assert statuses == ["DELIVERED"], "回填的第一候选就是这一列，而它的写者不是 `recover`"


def test_verify_consistency_is_scoped_to_the_named_project(session, writer, bus) -> None:
    """**这一条是变异 P9-T112-2 补出来的**：把项目过滤拔掉，原本 0 红点 ⇒ 用例缺陷，不是实现没问题。

    差额按 `(卡号, rel_path)` 成对算，而 `pg_pairs` 是按 `Artifact.feature_request_id ∈ 该项目的需求单`
    取的。少了这一层过滤，第二个项目的产物会算进第一个项目的差额里 —— 报错方向是"本项目缺一条"，
    于是每次恢复都被判成漂移，而真正的漂移（同项目内少一条）反而混在噪音里读不出来。
    """
    mine = uuid.uuid4()
    theirs = uuid.uuid4()
    _request(session, mine)
    _agent(session)
    my_point = _point(session, writer.gateway.root, [CARD_A])
    _card(session, mine, CARD_A, my_point.id)

    from acp.persistence.models_core import FeatureRequest as FR

    other_cards = ["T70", "T71"]
    session.add(
        FR(
            id=theirs,
            project_key=OTHER_PROJECT,
            raw_input="第二个项目：跨项目差额必须不相干",
            summary_digest=DIGEST,
            clarification_waived=True,
            status="INTAKE",
            created_at=NOW,
        )
    )
    session.flush()
    other_dir = bus.with_project(OTHER_PROJECT)
    assert other_dir.parent == writer.gateway.root.parent, (
        "两个项目要住在同一个总线根下，否则这条测的不是「同根之下的两个项目」"
    )
    other_point = snap.record_point(
        session,
        project_key=OTHER_PROJECT,
        round_no=1,
        phase=cadence.OPEN_PHASE,
        task_card_ids=other_cards,
        ref=cadence.commit_archive_point(
            other_dir,
            phase=cadence.OPEN_PHASE,
            round_no=1,
            project_key=OTHER_PROJECT,
            task_card_ids=other_cards,
        ).ref,
        now=NOW,
    )
    for card_id in other_cards:
        session.add(
            TaskCard(
                id=card_id,
                feature_request_id=theirs,
                title=f"另一项目的卡 {card_id}",
                difficulty=2,
                skill_tags=["html"],
                depends_on=[],
                executor_agent_id="qoder",
                executor_role_id=None,
                reviewer_agent_id=None,
                acceptor_agent_id=None,
                artifact_floor_min_bytes=200,
                artifact_floor_min_lines=8,
                artifact_floor_format="html",
                acceptance_criteria=["含 h1 标题"],
                handoff_count=0,
                review_round=1,
                status="DELIVERED",
                snapshot_id=other_point.id,
            )
        )
    # 这道 flush 不是顺手加的：`artifact` 的复合外键指向 `task_card(feature_request_id, id)`，
    # 而**没有 `relationship()` 的两个 mapper，SQLAlchemy 的工作单元不按表间外键排 INSERT 顺序**
    # （口径同 `graph.py:176-183` 与 `test_archive_cadence.py` 里那道同款 flush）。
    # 不先让卡落库，下面那两支产物会以 FK 违反红在这里，看起来像"恢复路径写坏了库"。
    session.flush()
    for card_id in other_cards:
        _artifact(session, theirs, card_id, f"qoder-workspace/other-{card_id}.html")
    _seed_project(writer)
    _artifact(session, mine, CARD_A, REL_PATH)

    result = recover.reconcile(writer.gateway.root.parent, PROJECT, session)

    assert result.pairs == 1 and result.bus_only == () and result.pg_only == (), (
        f"另一项目的两条产物被算进本项目的差额：{result.pg_only}"
    )


def test_a_project_with_nothing_on_either_side_is_zero_pairs_not_a_failure(session, writer) -> None:
    """两侧都空 ⇒ 通过（`pairs=0`）。这是关系式的空集形态，不是"没查到"。

    反向对照在同一条里：一边有一条而另一边没有 ⇒ 必须抛（见
    `test_verify_consistency_names_the_drift_in_both_directions`）。两条合起来才确认
    `reconcile` 既不是恒过也不是恒红。
    """
    request_id = uuid.uuid4()
    _request(session, request_id)
    point = _point(session, writer.gateway.root, [CARD_A])
    _agent(session)
    _card(session, request_id, CARD_A, point.id)
    _seed_project(writer, index=False)

    result = recover.reconcile(writer.gateway.root.parent, PROJECT, session)

    assert result.pairs == 0 and result.pg_side_checked is True


# ---------------------------------------------------------------- 四、分层与扫描面

def test_the_archive_layer_scan_covers_every_module_under_archive() -> None:
    """T108 那条分层判据扫的是**清单**而不是包 ⇒ 新增的 `restore.py` 必须自动进射程。

    口径同 T106 那次"扫描根要跟被 import 的那棵树走"：按硬编码文件名扫，新文件就是扫描盲区，
    而恢复这一层恰恰最需要"拿不到图"这个保证（它是最有条件去重跑链路的一层）。
    """
    scanned = sorted(path.name for path in (SRC_ROOT / "archive").glob("*.py"))
    text = (REPO_ROOT / "tests" / "guards" / "test_archive_cadence.py").read_text(encoding="utf-8")

    assert "restore.py" in scanned, f"存档包里的模块：{scanned}"
    assert 'glob("*.py")' in text, "分层判据又变回逐份点名了 ⇒ 新增模块不进射程"


def test_recover_and_chaos_do_not_import_each_other() -> None:
    """恢复侧与注入侧互不依赖：注入不许知道怎么恢复，恢复也不许调用注入。

    合在一起就成了"一个工具既能删又能建"，那正是最难审的 CLI 形状；而 T112 的两场景用例需要
    分别控制两侧（删的时候不该顺手把 checkpoint 建回去）。
    """
    for module, other in (("recover.py", "acp.chaos"), ("chaos.py", "acp.recover")):
        tree = ast.parse((SRC_ROOT / module).read_text(encoding="utf-8"))
        names = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
        assert other not in names and not any(str(name).startswith(f"{other}.") for name in names), (
            f"{module} 依赖了 {other}"
        )


def test_every_recover_path_ends_with_a_machine_readable_line() -> None:
    """三条路各有一条可机读的收尾行（`RECOVER OK` / `RECOVER FAIL`），前缀统一。

    口径同 T106 的 CLI 判据：演练的读数要能被脚本接走，不能只给人眼看。
    """
    tree = ast.parse((SRC_ROOT / "recover.py").read_text(encoding="utf-8"))
    heads = {
        node.args[0].values[0].value.split(" ::")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", "") == "print"
        and node.args
        and isinstance(node.args[0], ast.JoinedStr)
        and node.args[0].values
        and isinstance(node.args[0].values[0], ast.Constant)
    }

    assert {"RECOVER OK", "RECOVER FAIL"} <= heads, f"实际前缀：{sorted(heads)}"


def test_recover_does_not_re_derive_the_archive_root() -> None:
    """恢复不许自己解析存档根，也不许自己读环境（T109 定稿：解析只走 `archive/root.py`）。

    第二处解析 = 第二份"根在哪儿"的事实：`.env` 改了 `ACP_BUS_ROOT` 而恢复这一侧没跟着改，
    读出来就是"在 A 处存的档、回 B 处找"这种看起来像存档丢了的现象。

    断的是**读环境的语句**而不是变量名：`recover.py` 的模块头要交代凭据口径，那里 MUST 提到
    `ACP_DB_DSN` / `ACP_BUS_ROOT` 这两个**名字**（名字不是秘密）。第一版按"文件里不出现那个串"来断，
    红在了自己写的那句说明上 —— 那正是"判据锚点只能有一个出处"这一族里的又一次。
    """
    text = (SRC_ROOT / "recover.py").read_text(encoding="utf-8")

    assert "os.environ" not in text and "getenv" not in text and "environ[" not in text, (
        "恢复侧自己读环境 = 第二份总线根事实"
    )
    assert "require_archive_root" not in text, "解析存档根只有那一个函数，调用点在 archive/restore.py"
    assert "bus_root" in text, "恢复只把调用方给的那个根传下去，不自己算"
