"""T031 + T036 的判据：`Plan → Split → Normalize` 接进图，并跑出第一次**真实状态迁移**。

**本文件真连 PG**（缺 `ACP_DB_DSN` 时整档 skip 而不是假绿，口径同 `test_schema_invariants.py`）。
它要回答的问题与前两份 integration 用例不同：那里验的是"节点自己的形状"，这里验的是
"节点串起来之后，事实态里真的动了"——owner 2026-10-12 的话是"完工后应有真实的状态迁移可验
（不只是单元测试）"。所以断言一律读**库里的行**，不读图返回的 state：state 是过程态（见
`orchestrate/state.py` 的头），拿它当证据就等于用一方的话给另一方作证。

**Phase 4 的三处连带改写**（`interrupt` 上了图之后，本文件的三条读数必须跟着变形态，
逐条写明是有意改写而不是顺手改坏 Phase 2 的判据）：
- `advance_request` / `build_graph` 现在**要求 checkpointer**（缺它 `interrupt` 不报错却没有可恢复
  的现场，见 `graph.py` 的 `OrchestrateDeps` 头）⇒ 本文件的 `run()` 统一注入 `InMemorySaver`；
  **"真实挂起 / 恢复"不归这里**，那几条在 `tests/guards/test_approval_resume.py` 里用 PG 版跑。
- `test_zero_answers_still_advances_to_split` 原来断"返回里没有 `__interrupt__`"；图上有了等待点之后
  这句永远不成立，而 FR-003 要的是"**plan → split 这条边上**没有等待点"。改写后断三件事：
  图确实走到了 SPLIT（拆单没被阻塞）、挂起点是 A 类卡点、`plan → split` 仍是无条件普通边。
- `test_second_run_on_the_same_request_is_refused` 的 `AlreadyAdvanced` 不靠运气：实测不带
  `Command(resume=…)` 的 `invoke(dict)` 是从 START 重跑（`evidence/phase-04.md §T048` 读数三）⇒
  `_plan` 看见 EN-01 已是 SPLIT 就抛。这条现在同时是"恢复姿势用错了会响亮失败"的判据。

三条写法上的刻意选择：
1. **判据写成关系**："卡号顺序满足每条依赖的前件在前"，而不是"顺序等于我手写的 `(T01, T02, T03)`"。
2. **建行与校验的先后要能被证伪**：`test_illegal_dependency_structure_creates_no_card_rows` 用一份
   成环的拆单结果，要求 EN-02 **零行**、EN-01 **仍是 INTAKE**、EN-12 **零行** —— 如果实现把建行
   放在校验之前，这三条一起红。
3. **"不提交"是一条独立判据**（`test_the_run_neither_commits_nor_closes_the_borrowed_session`）：
   会话收尾归装配方（T123），节点只 `flush`。这条首版是**死判据**（换成 `commit()` 照样全绿），
   成因与修法写在该用例的 docstring 里 —— 变异对照把它救活了。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
import os
import uuid

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from acp.archive import root as archive_root
from acp.approval.service import _FIVE as SERVICE_FIVE
from acp.approval.timeouts import KIND_BY_LETTER
from acp.config.loader import DSN_ENV, db_url
from acp.orchestrate.graph import (
    ADVANCEABLE,
    APPROVAL_GATE,
    NORMALIZE,
    PLAN,
    SPLIT,
    AlreadyAdvanced,
    CheckpointerNotConfigured,
    MissingSession,
    OrchestrateDeps,
    RequestNotFound,
    advance_request,
    build_graph,
)
from acp.orchestrate.nodes.normalize import DependencyViolation
from acp.orchestrate.nodes.plan import IncompleteDigest
from acp.orchestrate.nodes.split import (
    AcceptanceLink,
    CardDraft,
    MappingViolation,
    SplitNotConfigured,
)
from acp.orchestrate.state import IntakeState
from acp.persistence.checkpointer import thread_config
from acp.persistence.models_core import FeatureRequest, TaskCard
from acp.persistence.models_registry import EventLog, Snapshot
from acp.persistence.states import states_declared_in_data_model
from acp.protocol import ArtifactFloor

pytestmark = pytest.mark.skipif(
    not os.environ.get(DSN_ENV),
    reason=f"{DSN_ENV} 未注入 ⇒ 本文件需要 PG（真实状态迁移只能在库里读）",
)

NOW = dt.datetime(2026, 10, 12, 16, 30, 0, tzinfo=dt.timezone.utc)
#: LangGraph 给终点节点的名字（`END` 常量的字符串形态）。写在这里是因为结构判据要按名字比边集，
#: 而这个字符串由依赖决定、不由本仓库决定 ⇒ 有一条用例专门测它（`test_the_graph_refuses_a_second_path...`
#: 的正半段），它变了就先红在这里，而不是让边集断言悄悄变成空对空。
END_NAME = "__end__"
FLOOR = ArtifactFloor(min_bytes=512, min_lines=12, format="html")
DIGEST = {"自行补全项": ["页面数量取 1 个首页"], "澄清问题": ["联系方式用邮箱还是表单？"]}


class StubSplitter:
    """占位拆单器：原样返回给定的草稿，并记下收到的摘要。

    它存在的理由是"拆单器的实体 = Phase 3 的适配器，产品侧故意不给默认实现"。
    `received` 那条记录用来证明"摘要从 EN-01 流到 Split 途中没被重新补全过"。
    FR-007 的映射同样只能由它申报 ⇒ 默认按"每张卡的每条判据配一条映射"生成，
    `links=` 可以整批换成坏形状（负例用它，而不是在断言里手工凑）。
    """

    def __init__(self, drafts: list[CardDraft], *, links=None, produced_by="stub-orchestrator"):
        self.drafts = drafts
        self._links = links
        self.produced_by = produced_by
        self.received: list[dict[str, list[str]]] = []

    def split(self, summary_digest):  # 形状与 CardSplitter 协议一致
        self.received.append(dict(summary_digest))
        return list(self.drafts)

    def acceptance_links(self, summary_digest):
        if self._links is not None:
            return list(self._links)
        return [
            AcceptanceLink(
                task_id=draft.task_id,
                requirement_item=f"需求条目：{draft.title}",
                acceptance_item=item,
                produced_by=self.produced_by,
            )
            for draft in self.drafts
            for item in draft.acceptance_criteria
        ]


def card(task_id: str, depends_on: list[str] | None = None) -> CardDraft:
    return CardDraft(
        task_id=task_id,
        title=f"卡 {task_id}",
        difficulty=2,
        skill_tags=["html", "css"],
        depends_on=depends_on or [],
        artifact_floor=FLOOR,
        acceptance_criteria=["首屏有标题", "联系方式可见"],
    )


@pytest.fixture(scope="session")
def engine():
    eng = create_engine(db_url(), future=True)
    yield eng
    # 口径同 `test_schema_invariants.py`：不 dispose 会让池里的连接在解释器退出时被 GC，
    # psycopg 的 `__del__` 抛 ResourceWarning ⇒ 表现为按 GC 时序抖的偶发 warning。
    eng.dispose()


@pytest.fixture
def session(engine):
    with engine.connect() as conn:
        transaction = conn.begin()
        s = Session(bind=conn)
        yield s
        s.close()
        transaction.rollback()


@pytest.fixture(autouse=True)
def archive_bus(bus, monkeypatch):
    """T109 起，本文件每一条走完 `_normalize` 的用例都需要一个真存档根（owner 裁 ㉝ 归 T109）。

    用 autouse + 环境变量而不是逐个用例传 `bus_root=`：本文件测的是"节点串起来之后事实态动了"，
    存档根只是它的前置现场；漏传的后果是全部用例红在"存档根不存在"，而那看起来像机制坏了。
    显式传参那条路归 `tests/guards/test_snapshot_ref.py`（它同时钉"没设变量 ⇒ 拒"这一面，
    免得这里的注入把"运行时缺配置起不来"这件事掩盖掉）。
    """
    monkeypatch.setenv(archive_root.BUS_ROOT_ENV, str(bus.path))
    bus.with_project("demo")
    return bus


def seed_request(session, *, digest=None, waived=False, status=ADVANCEABLE) -> FeatureRequest:
    row = FeatureRequest(
        id=uuid.uuid4(),
        project_key="demo",
        raw_input="做个介绍公司产品的静态网站",
        summary_digest=digest if digest is not None else DIGEST,
        clarification_waived=waived,
        status=status,
        created_at=NOW,
    )
    session.add(row)
    session.flush()
    return row


def run(request_id: uuid.UUID, session: Session, drafts: list[CardDraft] | None = None):
    return advance_request(
        request_id,
        session=session,
        splitter=StubSplitter(drafts or [card("T01")]),
        # Phase 4 起图上有挂起点 ⇒ 必须给 checkpointer。这里给内存版是有意的：本文件验的是
        # "节点串起来之后事实态动了"，不是"挂起 / 恢复动得了"（后者归 test_approval_resume.py，
        # 那里用 `PostgresSaver`）。写在这里免得被读成"忘了接 PG 版"。
        checkpointer=InMemorySaver(),
    )


def cards_of(session, request_id) -> list[TaskCard]:
    return (
        session.query(TaskCard)
        .filter(TaskCard.feature_request_id == request_id)
        .order_by(TaskCard.id)
        .all()
    )


# —— 拆单来源留痕（2026-10-31：`.env` 里 key 失效时系统静默用了 Stub，屏上读不出）


def _splitter_reporting(source: str, reason: str = ""):
    splitter = StubSplitter([card("T01")])
    splitter.last_source = source
    splitter.last_error = reason or None
    return splitter


def test_a_fallback_split_is_recorded_on_the_request_and_in_the_event_stream(session):
    """降级必须**两处**留痕：EN-01 的 `split_source`（读面据此打标识）+ EN-14 的 `split_fallback`。"""
    row = seed_request(session)
    advance_request(
        row.id,
        session=session,
        splitter=_splitter_reporting("fallback_stub", "split_json_invalid"),
        checkpointer=InMemorySaver(),
    )
    session.refresh(row)
    assert str(row.split_source) == "fallback_stub"
    rows = session.query(EventLog).filter(EventLog.kind == "split_fallback").all()
    assert len(rows) == 1, "降级那一发必须恰好一条留痕"
    assert rows[0].entity == "feature_request" and rows[0].entity_id == str(row.id)
    assert rows[0].payload["source"] == "fallback_stub"
    assert rows[0].payload["reason"] == "split_json_invalid"


def test_a_model_split_is_recorded_without_the_fallback_event(session):
    """反向对照：真模型拆的那一单只写来源，不许打 `split_fallback` —— 否则这条留痕没有鉴别力。"""
    row = seed_request(session)
    advance_request(
        row.id,
        session=session,
        splitter=_splitter_reporting("llm"),
        checkpointer=InMemorySaver(),
    )
    session.refresh(row)
    assert str(row.split_source) == "llm"
    assert session.query(EventLog).filter(EventLog.kind == "split_fallback").count() == 0


def test_a_splitter_without_a_self_report_is_treated_as_stub(session):
    """没有 `last_source` 这一格的对象 = 直接注入的确定性拆单器 ⇒ `stub`（按降级处理）。

    这是 `StubSplitter` 的形态，也是"模型适配器忘了自报来源"时的兜底：宁可多标一次降级，
    不许把一条不知道来源的单画成"模型拆的"。
    """
    row = seed_request(session)
    run(row.id, session)
    session.refresh(row)
    assert str(row.split_source) == "stub"
    assert session.query(EventLog).filter(EventLog.kind == "split_fallback").count() == 1


def independent_row_counts() -> tuple[int, int]:
    """换一个连接数行 —— 这条读取是"提交与否"的唯一可见证据（同一会话里查永远看得见）。"""
    eng = create_engine(db_url(), future=True)
    try:
        with eng.connect() as conn:
            return (
                conn.execute(text("select count(*) from task_card")).scalar(),
                conn.execute(text("select count(*) from snapshot")).scalar(),
            )
    finally:
        eng.dispose()


# ---------------------------------------------------------------- T031：这条边不设等待人


def test_zero_answers_still_advances_to_split(session):
    """FR-003：一条澄清都没答，图也必须走完拆单 —— 不许在 `plan → split` 这条边上等答复。

    **Phase 4 的有意改写**（原文断的是"返回里没有 `__interrupt__`"）：图上从本笔起有了 A 类卡点
    （FR-008 要求首次派发前必过），那句永远不成立，而 FR-003 管的是**这条边**不是整张图。
    现在断三件事，缺一半就漏一种坏法：
    ① 等人的是卡点，不是澄清问题 —— 挂起载荷就是审批单的五要素（`kind = A_SPLIT`），
       里面没有"请回答澄清问题"这一说；
    ② EN-01 确实走到了 `SPLIT`（拆单这件事没被任何人阻塞）；
    ③ 卡停在 `PENDING_APPROVAL` 而不是 `READY`（等待点在派发**之前**，FR-008 的"之前"是有读数的）。
    """
    row = seed_request(session, waived=True)
    state = run(row.id, session, [card("T01"), card("T02", ["T01"])])
    interrupts = state.get("__interrupt__") or []
    assert len(interrupts) == 1, f"图上的等待点必须恰有一个（A 类卡点），读到 {len(interrupts)} 个"
    payload = interrupts[0].value
    assert set(payload) == set(SERVICE_FIVE), f"挂起载荷不是五要素，而是 {sorted(payload)}"
    assert payload["timeout_policy"]["kind"] == KIND_BY_LETTER["A"], "等在这里的不是 A 类卡点 ⇒ 澄清答复被做成了等待点"
    session.refresh(row)
    assert str(row.status) == "SPLIT"
    assert [card_row.status for card_row in cards_of(session, row.id)] == [
        "PENDING_APPROVAL",
        "PENDING_APPROVAL",
    ], "卡应当停在待批，而不是已经 READY（FR-008 的'派发之前'）"


@pytest.mark.parametrize("waived", [True, False], ids=["澄清被放弃", "澄清已答"])
def test_waived_and_not_waived_take_the_same_edge(session, waived):
    """`clarification_waived` 不参与推进判定：两种取值都走同一条边、都到 SPLIT。

    写成参数化的两档而不是只测 True，是因为"只测放弃那一支"会漏掉反方向：
    如果哪天有人把这条边改成"没答就等"，只测 True 的用例照样绿（True 正是"没答"的另一种写法），
    而正常答了的那一支反而被卡住 —— 一条判据管不住两种坏法。
    """
    row = seed_request(session, waived=waived)
    run(row.id, session)
    session.refresh(row)
    assert str(row.status) == "SPLIT"
    assert row.clarification_waived is waived


def test_plan_reads_the_stored_digest_and_does_not_replan(session):
    """摘要从 EN-01 原样流到 Split：图里没有第二次补全（两次模型调用要记两次账）。"""
    row = seed_request(session)
    splitter = StubSplitter([card("T01")])
    graph = build_graph(
        OrchestrateDeps(session=session, splitter=splitter, checkpointer=InMemorySaver())
    )
    graph.invoke(
        {"feature_request_id": row.id},
        thread_config(kind=KIND_BY_LETTER["A"], feature_request_id=row.id),
    )
    assert splitter.received == [DIGEST], "Split 收到的摘要应与 EN-01 里那份逐字相同"
    session.refresh(row)
    assert dict(row.summary_digest) == DIGEST, "运行不得改写 EN-01 的 summary_digest"


def test_corrupt_digest_on_en_01_stops_before_split(session):
    """摘要形状不合法（缺 `自行补全项`）⇒ 在推进之前就拒，且一行不写。

    FR-002 的形状判据只有一个强制点（`plan.validate_summary_digest`）：这里证明图走的是**同一个**
    判据，而不是入口校过就算完 —— 旁路写入（直接改库）也过不去。
    """
    row = seed_request(session, digest={"澄清问题": ["少了一项"]})
    with pytest.raises(IncompleteDigest):
        run(row.id, session)
    assert cards_of(session, row.id) == []
    session.refresh(row)
    assert str(row.status) == ADVANCEABLE


# ---------------------------------------------------------------- T036：真实状态迁移


def test_en_01_moves_intake_to_split(session):
    row = seed_request(session)
    assert str(row.status) == "INTAKE"
    state = run(row.id, session)
    session.refresh(row)
    assert str(row.status) == "SPLIT"
    assert state["request_status"] == "SPLIT"


def test_cards_are_created_as_draft_with_no_assignment(session):
    """EN-02 有行、四个指派列全 NULL —— 这是 T-14 ① 打开的那个形态。

    **Phase 4 的有意改写**：`status` 的末态读数从 `DRAFT` 变成 `PENDING_APPROVAL` —— 图现在走完
    `Normalize` 会进 A 类卡点（FR-008），而 §2 的那条边就叫"拆单"。
    "建行那一刻是 DRAFT"这件事不再能在同一次 invoke 的末态里看见，改由两处钉住：
    ① `_normalize` 写 `status="DRAFT"` 这行本身由 `test_the_gate_only_moves_a_freshly_split_card`
    反证（守卫只接受 DRAFT 进来）；② 本文件 `test_http_entry_then_graph_end_to_end` 里的同一条读数。
    """
    row = seed_request(session)
    run(row.id, session, [card("T01"), card("T02", ["T01"])])
    stored = cards_of(session, row.id)
    assert [c.id for c in stored] == ["T01", "T02"]
    for c in stored:
        assert c.status == "PENDING_APPROVAL"
        assert c.status in states_declared_in_data_model()
        assert c.executor_agent_id is None
        assert c.executor_role_id is None
        assert c.reviewer_agent_id is None
        assert c.acceptor_agent_id is None


def test_a_pre_work_snapshot_row_exists_and_cards_point_at_it(session):
    """`snapshot_id` 是 NOT NULL ⇒ 建行前必须先有 EN-12 的 `PRE_WORK` 行，且两边对得上。"""
    row = seed_request(session)
    state = run(row.id, session, [card("T01"), card("T02", ["T01"])])
    snap = session.get(Snapshot, state["snapshot_id"])
    assert snap is not None, "图返回了 snapshot_id 而 EN-12 里没有对应行"
    assert snap.phase == "PRE_WORK"
    assert snap.round_no == 1
    assert sorted(snap.task_card_ids) == ["T01", "T02"]
    assert snap.rolled_back_at is None
    for c in cards_of(session, row.id):
        assert c.snapshot_id == snap.id, "卡指向的存档点与本轮那一行不是同一条"


def test_stored_dependencies_match_drafts_and_order_is_topological(session):
    """`depends_on` 逐字入库，且 EN-12 记的顺序满足"前件在前"（关系式，不是手写序列）。"""
    drafts = [card("T03", ["T02"]), card("T02", ["T01"]), card("T01")]
    row = seed_request(session)
    state = run(row.id, session, drafts)
    stored = {c.id: list(c.depends_on) for c in cards_of(session, row.id)}
    assert stored == {"T01": [], "T02": ["T01"], "T03": ["T02"]}
    order = state["card_order"]
    position = {task_id: index for index, task_id in enumerate(order)}
    for task_id, listed in stored.items():
        for dep in listed:
            assert position[dep] < position[task_id], f"{dep} 必须排在 {task_id} 前面"


def test_illegal_dependency_structure_creates_no_card_rows(session):
    """FR-005 的后果"退回重排，不入 READY"在这里的形态：**结构非法就一行都不建**。

    三处一起断言：EN-02 零行、EN-12 零行、EN-01 仍是 INTAKE。只断言"抛了异常"是不够的 ——
    实现完全可能先建行再抛，那时库里留着两张半成品卡，而下一轮重跑会撞上主键。
    """
    row = seed_request(session)
    with pytest.raises(DependencyViolation):
        run(row.id, session, [card("T01", ["T02"]), card("T02", ["T01"])])
    assert cards_of(session, row.id) == []
    assert session.query(Snapshot).count() == 0
    session.refresh(row)
    assert str(row.status) == ADVANCEABLE


def test_no_splitter_fails_closed(session):
    """拆单器与补全器同一条口径：**没有默认实现**，缺它不许降级成一份固定清单。"""
    row = seed_request(session)
    with pytest.raises(SplitNotConfigured):
        advance_request(row.id, session=session, splitter=None, checkpointer=InMemorySaver())
    assert cards_of(session, row.id) == []


def test_second_run_on_the_same_request_is_refused(session):
    """已经走过拆单的需求单不许再走一次 —— 否则同一需求会有两套卡与两个存档点。"""
    row = seed_request(session)
    run(row.id, session)
    before = len(cards_of(session, row.id))
    with pytest.raises(AlreadyAdvanced):
        run(row.id, session)
    assert len(cards_of(session, row.id)) == before
    session.refresh(row)
    assert str(row.status) == "SPLIT"


def test_missing_request_fails_loudly(session):
    """推进一个不存在的需求单 ⇒ 报错而不是跑出空结果（空结果读起来像"成功拆了 0 张卡"）。"""
    with pytest.raises(RequestNotFound):
        advance_request(
            uuid.uuid4(),
            session=session,
            splitter=StubSplitter([card("T01")]),
            checkpointer=InMemorySaver(),
        )


def test_a_run_needs_one_shared_session_not_none(session):
    """会话是工作单元的边界：给 None 就报错，不在节点里偷偷自己开一个（那会绕过装配方的提交点）。"""
    with pytest.raises(MissingSession):
        graph = build_graph(
            OrchestrateDeps(session=None, splitter=StubSplitter([card("T01")]), checkpointer=InMemorySaver())
        )
        graph.invoke({"feature_request_id": uuid.uuid4()})


def test_the_graph_refuses_to_compile_without_a_checkpointer(session):
    """D-9 / 宪法 V 的装配期判据：图上有 `ApprovalGate`，不带 checkpointer 就不许 compile。

    这条不是洁癖，是实测逼出来的：**不接 checkpointer 时 `interrupt()` 不报错**，只是返回一个
    带 `__interrupt__` 的结果而没有任何可恢复现场（`evidence/phase-04.md §T048` 读数四）。
    那种形状读起来像"卡在人工审批"，实际下一次 `Command(resume=…)` 无处可续 ⇒
    就是宪法 V 禁的"重启恢复"的伪装形态。装配期抛错把它换成了响亮失败。
    """
    with pytest.raises(CheckpointerNotConfigured):
        build_graph(OrchestrateDeps(session=session, splitter=StubSplitter([card("T01")])))


def test_artifact_floor_and_criteria_land_in_their_own_columns(session):
    """FR-017 的三项下限与 FR-029 的逐条比对依据：拆单给的必须真的在列里，而不是埋在 JSON 里。"""
    row = seed_request(session)
    run(row.id, session)
    stored = cards_of(session, row.id)[0]
    assert stored.artifact_floor_min_bytes == FLOOR.min_bytes
    assert stored.artifact_floor_min_lines == FLOOR.min_lines
    assert stored.artifact_floor_format == FLOOR.format
    assert list(stored.acceptance_criteria) == ["首屏有标题", "联系方式可见"]
    assert list(stored.skill_tags) == ["html", "css"]
    assert stored.difficulty == 2


def test_counters_start_at_zero_and_budget_is_null(session):
    """`handoff_count` / `review_round` 从 0 起算，`token_budget` NULL = 无预算（裁决 3）。

    这两列的初值文档没给（只给了上限），所以这里同时钉住"取的是 0"与"0 在允许区间内"；
    裁决 3 那条 CHECK 对 NULL 恒不违反，所以 NULL 必须能落进去。
    """
    row = seed_request(session)
    run(row.id, session)
    stored = cards_of(session, row.id)[0]
    assert stored.handoff_count == 0
    assert stored.review_round == 0
    assert stored.token_budget is None
    assert stored.deadline_minutes is None
    assert bool(stored.review_standard_locked) is True


def test_graph_has_exactly_the_documented_nodes_and_plain_edges(session):
    """节点集合 = 文档那四个；`plan → split` 是**普通边**；`normalize` 的唯一出边是卡点。

    这条是结构判据，与行为判据 `test_zero_answers_still_advances_to_split` 配对：
    行为那条证明"跑得起"，这条证明"没有多一个等待节点/条件边"。二者红的含义不同，缺一个就漏一半。

    Phase 4 起多一个节点（`approval_gate`），并且多钉两件事（FR-008 的"派发之前"要能在拓扑上被证伪）：
    ① `normalize` 的出边**恰有一条**且指向卡点；
    ② 通向终点的边**只有卡点那一条** —— 图上不存在"拆完直接结束/直接派发"的第二条路，
       所以将来接 `Dispatch` 时必须接在卡点之后，否则这条判据先红。
    """
    graph = build_graph(
        OrchestrateDeps(
            session=session, splitter=StubSplitter([card("T01")]), checkpointer=InMemorySaver()
        )
    ).get_graph()
    names = {n for n in graph.nodes if not n.startswith("__")}
    assert names == {PLAN, SPLIT, NORMALIZE, APPROVAL_GATE}, (
        f"多出的节点名要回 plan.md 登记，不能在这里加：{sorted(names)}"
    )
    plan_edges = [(e.target, e.conditional) for e in graph.edges if e.source == PLAN]
    assert plan_edges == [(SPLIT, False)], "Plan 的出边必须恰有一条、且指向 Split、且不是条件边"
    normalize_edges = [(e.target, e.conditional) for e in graph.edges if e.source == NORMALIZE]
    assert normalize_edges == [(APPROVAL_GATE, False)], f"拆单之后必须先过卡点（FR-008），读到 {normalize_edges}"
    to_end = [e.source for e in graph.edges if e.target == END_NAME]
    assert to_end == [APPROVAL_GATE], f"通向终点的唯一入口必须是卡点，读到 {to_end}"


def test_the_end_edge_moves_with_the_topology(session):
    """反向对照：上面那句 `to_end == [APPROVAL_GATE]` 不是恒真 —— 换拓扑它就得换读数。

    两支对照各证一件事：
    ① **撤掉卡点**（三节点图）⇒ 通向终点的入口变成 `normalize` ⇒ 说明这条断言真的在量"谁连到终点"；
    ② **加一条旁路**（`normalize` 用条件边分叉到卡点与终点）⇒ 入口列表由 1 个变 2 个 ⇒
       说明它也能抓到"绕过卡点直接收尾"那种坏法。
    ② 特意用条件边而不是 `add_edge(normalize, END)` 再来一条：实测静态边同一-来源写第二条会被
    `get_graph()` 静默丢掉（探针读数：三条边、`normalize → __end__` 不在里面）——
    那就不是产品能表达的形态，拿它当对照等于测了个空。这一点也登记在 `evidence/phase-04.md §T049`，
    因为它同时意味着**"旁路"只能以条件边的形式出现**，将来的判据只需要盯条件边。
    """
    deps = OrchestrateDeps(
        session=session, splitter=StubSplitter([card("T01")]), checkpointer=InMemorySaver()
    )
    product = build_graph(deps).get_graph()
    assert [e.source for e in product.edges if e.target == END_NAME] == [APPROVAL_GATE]

    without_gate = StateGraph(IntakeState)
    without_gate.add_node(NORMALIZE, lambda state: {})
    without_gate.add_edge(START, NORMALIZE)
    without_gate.add_edge(NORMALIZE, END)
    moved = [e.source for e in without_gate.compile(checkpointer=InMemorySaver()).get_graph().edges
             if e.target == END_NAME]
    assert moved == [NORMALIZE], f"撤掉卡点后通向终点的入口没跟着变：{moved}"

    bypass = StateGraph(IntakeState)
    bypass.add_node(NORMALIZE, lambda state: {})
    bypass.add_node(APPROVAL_GATE, lambda state: {})
    bypass.add_edge(START, NORMALIZE)
    bypass.add_conditional_edges(
        NORMALIZE, lambda state: "gate", {"gate": APPROVAL_GATE, "end": END}
    )
    bypass.add_edge(APPROVAL_GATE, END)
    sources = sorted(
        {e.source for e in bypass.compile(checkpointer=InMemorySaver()).get_graph().edges
         if e.target == END_NAME}
    )
    assert len(sources) == 2 and NORMALIZE in sources, f"旁路没被建出来，这条对照是空的：{sources}"


def test_http_entry_then_graph_end_to_end(session):
    """owner 2026-10-12 的验收清单第 1~5 步，串成一条可复跑的判据（不是手工一次就算数）。

    路径就是真实路径：`POST /requests`（T029，写 EN-01 = INTAKE）→ 图（T036）→ 读回事实态。
    第 5 步（EN-07 映射表）自 **T034** 起有内容了：上一版这里断言的是 `count() == 0`
    （那条当时钉的是"T034 没做"这个事实），现在按实际的 3 卡 × 2 判据 = 6 行断，
    并要求映射集合与卡上声明的判据集合**相等**（漏一条 = FR-030 漏判，多一条 = 卡外标准）。
    把"这条还没实现"钉成一个会翻红的读数，而不是留成一句文档里的话。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from acp.api.requests import router
    from acp.orchestrate.nodes.plan import CompleterOutcome
    from acp.persistence.models_approval import AcceptanceMap

    class StubCompleter:
        def complete(self, raw_input):
            return CompleterOutcome(
                self_completed=("页面数量取 1 个首页",),
                clarifications=("联系方式用邮箱还是表单？",),
            )

    app = FastAPI()
    app.include_router(router)
    app.state.completer = StubCompleter()
    app.state.session_factory = lambda: session
    response = TestClient(app).post(
        "/requests", json={"project_key": "demo", "raw_input": "做个介绍公司产品的静态网站"}
    )
    assert response.status_code == 201, response.text
    body = response.json()
    request_id = uuid.UUID(body["request_id"])
    assert body["status"] == "INTAKE"
    assert body["clarification_waived"] is True, "一条澄清都没答 ⇒ 必须记成 waived（FR-003）"

    state = run(request_id, session, [card("T01"), card("T02", ["T01"]), card("T03", ["T01", "T02"])])

    row = session.get(FeatureRequest, request_id)
    assert str(row.status) == "SPLIT", "第 2 步：EN-01 必须真的从 INTAKE 走到 SPLIT"
    stored = cards_of(session, request_id)
    assert [c.id for c in stored] == ["T01", "T02", "T03"], "第 3 步：EN-02 有拆单后的行"
    for c in stored:
        # Phase 4 起这条链的末态是"挂在 A 类卡点上"（`test_cards_are_created_as_draft_with_no_assignment`
        # 的头部写了改写理由）；`E2E-READINGS` 那行打印里能看到卡态，所以读数不是靠注释撑着。
        assert c.status == "PENDING_APPROVAL"
        assert c.executor_agent_id is None and c.executor_role_id is None
        assert c.snapshot_id == state["snapshot_id"]
    snap = session.get(Snapshot, state["snapshot_id"])
    assert snap is not None and snap.phase == "PRE_WORK", "第 4 步：EN-12 有 PRE_WORK 行（snapshot_id NOT NULL）"
    assert sorted(snap.task_card_ids) == ["T01", "T02", "T03"]
    # FR-007（T034）：映射表与卡同步落库，覆盖必须**恰好**等于卡上声明的判据集合。
    # 上一版这条断言写的是 `count() == 0`（那时 T034 还没做，把"没做"钉成读数）；
    # 现在按 owner 2026-10-12 的话改成实际行数 —— 3 张卡 × 每卡 2 条判据 = 6 行。
    maps = (
        session.query(AcceptanceMap)
        .filter(AcceptanceMap.feature_request_id == request_id)
        .order_by(AcceptanceMap.task_card_id, AcceptanceMap.acceptance_item)
        .all()
    )
    assert len(maps) == 6, f"期望 3 卡 × 2 判据 = 6 行，实际 {len(maps)}"
    assert {(m.task_card_id, m.acceptance_item) for m in maps} == {
        (c.id, item) for c in stored for item in c.acceptance_criteria
    }, "映射必须与卡的判据集合一一对应：漏一条 = FR-030 漏判，多一条 = 引入卡外标准"
    assert all(m.requirement_item.strip() for m in maps)
    assert {m.produced_by for m in maps} == {"stub-orchestrator"}

    # 读数打印：让"端到端状态迁移"这五个数**每次复跑都能拿到**，而不是只活在某一次对话里。
    # 一律用裸 SQL 读，好让这几个数由 PG 回答而不是由 ORM 对象状态回答（`pytest -q -s` 可见）。
    probe = session.execute
    en01 = probe(
        text("select status from feature_request where id=:i"), {"i": request_id}
    ).scalar()
    cards = probe(
        text("select id, status, executor_agent_id, executor_role_id, snapshot_id "
             "from task_card where feature_request_id=:i order by id"), {"i": request_id}
    ).all()
    snaps = probe(
        text("select round_no, phase, task_card_ids, ref from snapshot where id=:i"),
        {"i": state["snapshot_id"]},
    ).all()
    maps = probe(
        text("select task_card_id, acceptance_item, requirement_item, produced_by "
             "from acceptance_map where feature_request_id=:i order by task_card_id, acceptance_item"),
        {"i": request_id},
    ).all()
    print(
        "\nE2E-READINGS "
        f"step1_post=201 step2_en01={en01} "
        f"step3_cards={[(c[0], c[1], c[2], c[3]) for c in cards]} "
        f"step4_snapshot={[(s[0], s[1], s[2], s[3][:30]) for s in snaps]} "
        f"step5_en07_rows={len(maps)} "
        f"step5_pairs={[(m[0], m[1][:6], m[3]) for m in maps]}"
    )


def test_the_run_neither_commits_nor_closes_the_borrowed_session(session, monkeypatch):
    """节点只 `flush`：提交与收尾归装配方（T123），用例则靠外层事务回滚保持库可重复跑。

    **这条判据首版是死的，写明过程以免被照抄**：首版只断言"换一个连接数行 = 0"，
    而变异"把节点里的 `flush()` 换成 `commit()`"跑出来仍 **18 passed** ——
    因为 `Session(bind=conn)` 绑在一个**外部已 begin 的连接**上时，`session.commit()` 不会把
    COMMIT 发到 PG（它只结束会话自己的事务记录），所以那个连接视角永远读不到东西，
    这条断言在任何实现下都恒真。⇒ 现在改用**直接的绊线**：把 `commit` / `close` 换成记账函数，
    节点一旦调用就看得见。两半各自钉一件事，缺一不可：
    - 绊线 = "节点没越权提交 / 没关别人的会话"；
    - 会话内查到 2 行 + 另一个连接查到 0 行 = "确实写了，而库里确实没留下东西"。
    """
    row = seed_request(session)
    calls: list[str] = []
    monkeypatch.setattr(session, "commit", lambda *a, **k: calls.append("commit"))
    monkeypatch.setattr(session, "close", lambda *a, **k: calls.append("close"))

    run(row.id, session, [card("T01"), card("T02", ["T01"])])

    assert calls == [], f"编排节点越权收尾了借来的会话：{calls}"
    assert len(cards_of(session, row.id)) == 2, "会话内查不到 ⇒ 根本没写，后半个读数是空的"
    assert independent_row_counts() == (0, 0)


# —— 请求级路由的接线（2026-10-31 路线 B）


def test_a_routed_executor_is_written_to_the_card_and_the_factory_gets_the_route(session):
    """路由点名的执行者：① 卡上真的写成它；② 留痕里带 `routed_executor`；③ 工厂拿到同一份路由。"""
    from acp import realrun
    from acp.orchestrate.nodes import select_worker

    routing = {"executor": {"kind": "api", "vendor": "深度求索 DeepSeek", "model": "deepseek-flash"}}
    row = seed_request(session)
    row.model_routing = routing
    session.flush()
    run(row.id, session)
    # 执行者列有外键 ⇒ 先把注册面按 `agents.yaml` 种进库（与生产同一路径，不手搓 AgentUnit）。
    from acp.registry import roles as role_registry
    from acp.registry.persist import persist_registrations

    role_registry.seed_builtin_roles(session)
    persist_registrations(session, declared_at=NOW)
    card = cards_of(session, row.id)[0]

    ranking = select_worker.rank_candidates(select_worker.task_need_of(card), [], [])
    written = select_worker.apply_selection(
        session,
        ranking,
        feature_request_id=row.id,
        actor_agent_id="orchestrator",
        created_at=NOW,
        forced="doubao",
    )
    assert written.executor_agent_id == "doubao"
    latest = (
        session.query(EventLog)
        .filter(EventLog.kind == "SELECTION", EventLog.entity_id == card.id)
        .order_by(EventLog.id.desc())
        .first()
    )
    assert latest is not None and latest.payload["routed_executor"] == "doubao"
    assert latest.payload["overridden"] is True and latest.payload["decided_by"] == "model_routing"

    seen: list[dict] = []

    def fake_factory(agent_id, gateway, cwd, **kwargs):
        seen.append({"agent_id": agent_id, "routing": kwargs.get("routing")})
        return object()

    bound = realrun.transport_factory_for(session, row.id, fake_factory)
    bound("doubao", object(), Path("x"))
    assert seen == [{"agent_id": "doubao", "routing": routing}]


def test_a_request_without_routing_keeps_the_original_factory(session):
    """反向对照：没有路由的请求拿到的是**同一个**工厂对象（老路径一字不改）。"""
    from acp import realrun

    row = seed_request(session)

    def fake_factory(agent_id, gateway, cwd):
        return object()

    assert realrun.transport_factory_for(session, row.id, fake_factory) is fake_factory


def test_the_select_node_forces_the_routed_executor_over_the_ranking(session):
    """选人**节点**（不是 `apply_selection`）本身要读路由：拆掉那次读取，这条必须红。"""
    from acp.orchestrate import graph as graph_module
    from acp.registry import roles as role_registry
    from acp.registry.persist import persist_registrations

    routing = {"executor": {"kind": "api", "vendor": "深度求索 DeepSeek", "model": "deepseek-flash"}}
    row = seed_request(session)
    row.model_routing = routing
    session.flush()
    run(row.id, session)
    role_registry.seed_builtin_roles(session)
    persist_registrations(session, declared_at=NOW)

    deps = graph_module.AssignmentDeps(
        session=session,
        created_at=NOW,
        actor_agent_id="orchestrator",
        slots=object(),
        leases=object(),
    )
    state = graph_module._select_worker(deps)({"feature_request_id": row.id, "task_id": "T01"})
    assert state["executor_agent_id"] == "doubao"
    session.refresh(cards_of(session, row.id)[0])
    assert cards_of(session, row.id)[0].executor_agent_id == "doubao"
    latest = (
        session.query(EventLog)
        .filter(EventLog.kind == "SELECTION", EventLog.entity_id == "T01")
        .order_by(EventLog.id.desc())
        .first()
    )
    assert latest is not None and latest.payload["decided_by"] == "model_routing"


def test_routing_preferences_cannot_put_the_executor_on_a_guard_seat(session):
    """真库半边：路由把**执行者**点成审查员 ⇒ 当席候选里没有它 ⇒ 忽略并留痕，三席仍两两不同。"""
    from acp.orchestrate.nodes import assign_guards, select_worker
    from acp.registry import roles as role_registry
    from acp.registry.persist import persist_registrations

    row = seed_request(session)
    row.model_routing = {
        "executor": {"kind": "api", "vendor": "深度求索 DeepSeek", "model": "deepseek-flash"},
        "reviewer": {"agent_id": "doubao"},
        "acceptor": {"agent_id": "qoder"},
    }
    session.flush()
    run(row.id, session)
    role_registry.seed_builtin_roles(session)
    persist_registrations(session, declared_at=NOW)
    card = cards_of(session, row.id)[0]
    select_worker.apply_selection(
        session,
        select_worker.rank_candidates(select_worker.task_need_of(card), [], []),
        feature_request_id=row.id,
        actor_agent_id="orchestrator",
        created_at=NOW,
        forced="doubao",
    )
    picked = assign_guards.assign(
        session,
        feature_request_id=row.id,
        task_id=card.id,
        created_at=NOW,
        actor_agent_id="orchestrator",
    )
    session.refresh(card)
    seats = (card.executor_agent_id, card.reviewer_agent_id, card.acceptor_agent_id)
    assert seats[0] == "doubao"
    assert len(set(seats)) == 3, f"偏好把执行者塞进了把关席：{seats}"
    assert picked.ignored.get("reviewer") == "doubao", "被忽略的偏好必须留痕"
    # 两条偏好都被忽略：执行者自己不能当审查员；`qoder` 被审查席先坐走 ⇒ 验收席按互斥让位。
    # 记录口径是"每条偏好恰好落 applied 或 ignored 之一"，所以这两条都必须出现在 ignored 里。
    assert picked.ignored.get("reviewer") == "doubao"
    assert picked.ignored.get("acceptor") == "qoder"
    assert picked.applied == {}


# —— 路线 C：三个席位的模型实例身份（2026-10-31）


def _routed_request(session, routing: dict):
    row = seed_request(session)
    row.model_routing = routing
    session.flush()
    run(row.id, session)
    from acp.registry import roles as role_registry
    from acp.registry.persist import persist_registrations

    role_registry.seed_builtin_roles(session)
    persist_registrations(session, declared_at=NOW)
    return row, cards_of(session, row.id)[0]


def _force_executor(session, row, card, agent="doubao"):
    from acp.orchestrate.nodes import select_worker

    select_worker.apply_selection(
        session,
        select_worker.rank_candidates(select_worker.task_need_of(card), [], []),
        feature_request_id=row.id,
        actor_agent_id="orchestrator",
        created_at=NOW,
        forced=agent,
    )


def test_the_same_model_and_key_cannot_take_two_seats(session):
    """**纪律判据 ①**：同一模型实例（同 vendor/model/key 指纹）出现在两个席位 ⇒ `assign_guards` 拦。"""
    from acp.guard.triple import TripleViolation
    from acp.orchestrate.nodes import assign_guards

    routing = {
        "executor": {"kind": "api", "vendor": "某厂商", "model": "same-model", "key_fp": "abc123"},
        "reviewer": {"vendor": "某厂商", "model": "same-model", "key_fp": "abc123"},
        "acceptor": {"vendor": "某厂商", "model": "other-model", "key_fp": "abc123"},
    }
    row, card = _routed_request(session, routing)
    _force_executor(session, row, card)
    with pytest.raises(TripleViolation) as caught:
        assign_guards.assign(
            session, feature_request_id=row.id, task_id=card.id, created_at=NOW, actor_agent_id="orchestrator"
        )
    assert "not_independent" in str(getattr(caught.value, "code", "")) or caught.value.violations


def test_three_distinct_models_can_take_the_three_seats(session):
    """**纪律判据 ②**：三个不同的模型 ID 可以分别执行/审查/验收（互斥按实例指纹成立）。"""
    from acp.orchestrate.nodes import assign_guards

    routing = {
        "executor": {"kind": "api", "vendor": "某厂商", "model": "model-a", "key_fp": "k1"},
        "reviewer": {"vendor": "某厂商", "model": "model-b", "key_fp": "k1"},
        "acceptor": {"vendor": "某厂商", "model": "model-c", "key_fp": "k1"},
    }
    row, card = _routed_request(session, routing)
    _force_executor(session, row, card)
    picked = assign_guards.assign(
        session, feature_request_id=row.id, task_id=card.id, created_at=NOW, actor_agent_id="orchestrator"
    )
    session.refresh(card)
    assert picked.reviewer == "deepseek-reviewer", "审查席载体必须是审查跑者认的那个身份"
    assert picked.acceptor == "doubao"
    latest = (
        session.query(EventLog)
        .filter(EventLog.kind == "TASK", EventLog.entity_id == card.id)
        .order_by(EventLog.id.desc())
        .first()
    )
    triple = latest.payload["triple"]
    assert len(set(triple.values())) == 3, triple
    assert set(triple.values()) == {"model:某厂商:model-a:k1", "model:某厂商:model-b:k1", "model:某厂商:model-c:k1"}
