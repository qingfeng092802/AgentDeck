"""T084：换人上限的**端到端**判据（FR-026 / FR-027｜R-11）—— 换人 1 次成功、第 2 次必升人工、审计含原因。

**本文件真连 PG**（缺 `ACP_DB_DSN` 时整档 skip 而不是假绿，口径同 `test_orchestrate_graph.py`），
而且断言一律读**库里的行**：图返回的 state 是过程态，拿它当证据等于用一方的话给另一方作证。

与 `tests/guards/test_handoff.py` 的分工（两份都在，各自只有一种坏法）：
那份问的是 `handoff()` **这一个函数**的判定与上限；本文件问的是
**"回投 → 门禁 → 换人"串起来之后事实态里真的动了**，而且用的是三个**真身体**：

- `dp.dispatch`（T078）—— 不是桩：第二次进图前要靠它把 `HANDED_OFF` 的卡重新投出去，
  这正是那条"状态机第二边"在真库上的唯一取证点；
- `ri.record_result` + `va.validate_card`（T075 / T076）—— 经 `build_execution_graph` 装上；
- `hp.handoff`（T081）—— 作为图上的节点体注入，`build_execution_graph` 明不给默认实现（T076 那段）。

候选集走 `sw.candidates_from_registry(session)`（生产取数路径：EN-03 生效值 + 最新一份 EN-05 申报），
不是用例手搓的 `WorkerCandidate` —— 手搓的话"注册口径与换人用的是同一批人"这件事就没被测过。

**四条判据各自只有一种坏法能撞红**：
1. 第一次换人成立：`HANDED_OFF` + `handoff_count = 1` + 执行者换了（红法：上限判断写反、次数没 +1）。
2. 第二次必升人工：`ESCALATED` 而**次数仍是 1**、执行者仍是那一个（红法：升级时顺手再换一次人，
   或者把次数写成 2 —— 后者会被库面 CHECK 顶成 `IntegrityError`，同样红）。
3. 审计含原因，而且**原因是从门禁传下来的**：`EN-14` 的换人留痕里那句理由 == 那条产物的 `invalid_reason`。
   写成"理由非空"就放过了"换人自己编一句"的那种实现（FR-026 要的是同一条因果链可查）。
4. 涉及的工具始终 ≤ 2（从 `SELECTION` 行现数，不读常量）：宪法「三」那句"MUST NOT 三工具循环"的库面取证。

反向对照（缺了就等于判据恒红）：第二条跟着一条"第一次成立"（同一形状、次数还在界内），
第四条跟着一条"图上确实发生了两次换人动作的读取"（`..._first_run` 的那条 SELECTION 行必须在数里）。
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from collections.abc import Callable
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from acp.config.loader import DSN_ENV, db_url
from acp.orchestrate.graph import ExecutionDeps, build_execution_graph
from acp.orchestrate.nodes import dispatch as dp
from acp.orchestrate.nodes import handoff as hp
from acp.orchestrate.nodes import result_ingest as ri
from acp.orchestrate.nodes import select_worker as sw
from acp.persistence.models_core import (
    AgentUnit,
    Artifact,
    CapabilityManifest,
    FeatureRequest,
    TaskCard,
)
from acp.persistence.models_registry import EventLog, Snapshot
from acp.protocol.messages import ArtifactRef, TaskResult
from acp.runtime.slot_manager import InProcessSlotManager

pytestmark = pytest.mark.skipif(
    not os.environ.get(DSN_ENV), reason=f"缺 {DSN_ENV} ⇒ 本档要真 PG，整档 skip（不是红）"
)

NOW = dt.datetime(2026, 10, 14, 4, 10, tzinfo=dt.timezone.utc)
FLOOR_BYTES, FLOOR_LINES, FLOOR_FORMAT = 512, 12, "html"
QODER, WORKBUDDY, DOUBAO = "qoder", "workbuddy", "doubao"
CAPS = ["frontend_coding", "repository_editing", "test_execution"]


def seed_agent(session, agent_id: str, tier: int) -> None:
    """EN-03（生效口径）+ 一份 EN-05 申报（能力唯一来源）—— 两支都在，候选集才收它。

    `doubao` 给 `tier=1`：卡上 `difficulty=2` ⇒ 它在键②上必然输给 `workbuddy`（同为 2 档）。
    这样"第一次换人换到谁"只有一个来源（排序规则），不靠我在用例里点名。
    """
    session.add(
        AgentUnit(
            agent_id=agent_id,
            form="SDK",
            tier=tier,
            cost_band="MID",
            max_parallel=3,
            current_load=0,
            historical_success_rate=Decimal("0.5"),
            success_rate_sample=4,
            workspace_dir=f"/tmp/{agent_id}-workspace",
            enabled=True,
        )
    )
    session.add(
        CapabilityManifest(
            agent_id=agent_id,
            capabilities=CAPS,
            tier=tier,
            cost_band="MID",
            max_parallel=3,
            declared_at=NOW,
        )
    )


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


@pytest.fixture
def running_card(session) -> tuple[FeatureRequest, TaskCard]:
    """一张已被首次派发、正跑着的卡，执行者是首次选人的结果（走生产写者，不是手写列）。"""
    for agent_id, tier in ((QODER, 2), (WORKBUDDY, 2), (DOUBAO, 1)):
        seed_agent(session, agent_id, tier)
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
    # 没有 `relationship()` 的两张表，UoW 不按表间外键排 INSERT ⇒ 存档点行必须先到库
    # （同一个坑在 `orchestrate/graph.py::_normalize` 与 `evidence/phase-04.md` 各记一次）。
    session.flush()
    card = TaskCard(
        id="T01",
        feature_request_id=request.id,
        title="卡 T01",
        difficulty=2,
        skill_tags=["frontend_coding"],
        depends_on=[],
        artifact_floor_min_bytes=FLOOR_BYTES,
        artifact_floor_min_lines=FLOOR_LINES,
        artifact_floor_format=FLOOR_FORMAT,
        acceptance_criteria=["首屏有标题"],
        handoff_count=0,
        review_round=0,
        status="READY",
        snapshot_id=snapshot_id,
    )
    session.add(card)
    session.flush()
    candidates, _dropped = sw.candidates_from_registry(session)
    sw.apply_selection(
        session,
        sw.rank_candidates(sw.task_need_of(card), candidates),
        feature_request_id=request.id,
        actor_agent_id=sw.AUTOMATED_ACTOR,
        created_at=NOW,
    )
    session.refresh(card)
    assert card.executor_agent_id == QODER, (
        f"首次选人结果不是 {QODER}（{card.executor_agent_id}）⇒ 本文件的形状不再唯一，先修形状"
    )
    dp.dispatch(
        session,
        feature_request_id=request.id,
        task_id=card.id,
        slots=InProcessSlotManager.from_config({a: 3 for a in (QODER, WORKBUDDY, DOUBAO)}),
        created_at=NOW,
    )
    return request, card


def zero_byte_result() -> TaskResult:
    """工具自述 success，但交回一份 0 字节的产物 —— R-07 那个样例第一步的失败形状。"""
    return TaskResult(
        task_id="T01",
        self_reported_status="success",
        artifacts=[ArtifactRef(rel_path="index.html", bytes=0, lines=0, format=FLOOR_FORMAT)],
        log_excerpt="build ok",
        self_assessment="已完成",
        issues=[],
        usage=[],
    )


def run_once(session, request_id, task_id: str, *, review: Callable) -> dict:
    """把真身体装上卡级图跑一遍。`review` 由调用方给（Phase 7 的 T089 才有真审查节点）。

    回收方（`actor_agent_id`）**从库里现读**而不是写死：第二次进图时执行者已经换成 `workbuddy`，
    写死 `qoder` 会在回收段被 `writer_not_executor` 拒掉 —— 那正是 FR-033 生效的表现，
    但它会把本文件的判据变成"测我自己的常量抄得对不对"。
    """
    executor = session.get(TaskCard, (request_id, task_id)).executor_agent_id

    def handoff_node(state):
        candidates, _dropped = sw.candidates_from_registry(session)
        hp.handoff(
            session,
            feature_request_id=state["feature_request_id"],
            task_id=state["task_id"],
            reason=state["invalid_reason"],
            candidates=candidates,
            created_at=NOW,
        )
        return {}

    graph = build_execution_graph(
        ExecutionDeps(session=session, created_at=NOW, actor_agent_id=str(executor)),
        review=review,
        handoff=handoff_node,
    )
    return graph.invoke(
        {"feature_request_id": request_id, "task_id": task_id, "result": zero_byte_result()}
    )


def counting_review(counts: dict) -> Callable:
    def node(state):
        counts["review"] = counts.get("review", 0) + 1
        return {}

    return node


def task_rows(session, task_id: str) -> list[EventLog]:
    return list(
        session.scalars(
            select(EventLog)
            .where(EventLog.entity_id == task_id, EventLog.kind == "TASK")
            .order_by(EventLog.id)
        ).all()
    )


def selection_rows(session, task_id: str) -> list[EventLog]:
    return list(
        session.scalars(
            select(EventLog)
            .where(EventLog.entity_id == task_id, EventLog.kind == "SELECTION")
            .order_by(EventLog.id)
        ).all()
    )


def artifacts(session, request_id) -> list[Artifact]:
    return list(
        session.scalars(
            select(Artifact).where(
                Artifact.feature_request_id == request_id, Artifact.task_card_id == "T01"
            )
        ).all()
    )


def test_the_first_failed_run_hands_off_once_through_the_real_graph(session, running_card):
    """T084 ①③：真图上第一次不达标 ⇒ 换人成立，且换人留痕里的原因就是门禁写在产物行上那句。"""
    request, card = running_card
    counts: dict = {}
    run_once(session, request.id, card.id, review=counting_review(counts))
    session.expire_all()

    fresh = session.get(TaskCard, (request.id, card.id))
    assert (fresh.status, fresh.handoff_count) == (hp.LANDED, 1), (fresh.status, fresh.handoff_count)
    assert fresh.executor_agent_id == WORKBUDDY, fresh.executor_agent_id
    assert counts.get("review") is None, "产物不达标却进了审查 ⇒ FR-018 的出边被走反了"

    handed = [row for row in task_rows(session, card.id) if row.payload["action"] == "handed_off"]
    assert len(handed) == 1, [row.payload for row in task_rows(session, card.id)]
    payload = handed[0].payload
    stored = artifacts(session, request.id)
    assert len(stored) == 1 and stored[0].validated_status == "invalid"
    # **因果链**而不是两句各自的话：留痕里的原因必须**等于**门禁盖在产物行上的那句
    assert payload["reason"] == stored[0].invalid_reason, (payload["reason"], stored[0].invalid_reason)
    assert str(FLOOR_BYTES) in payload["reason"], "理由没点名是哪一项下限没过 ⇒ 复核不了"
    assert (payload["from_agent"], payload["to_agent"], payload["handoff_count"]) == (
        QODER,
        WORKBUDDY,
        1,
    ), payload


def test_the_second_failed_run_escalates_without_a_third_agent(session, running_card):
    """T084 ②④：第二次失败必升人工 —— 次数仍是 1、执行者没再动、库里出现过的工具不超过 2 支。

    两次之间靠 `dp.dispatch` 把 `HANDED_OFF` 的卡重新投出去（状态机第二条边在真库上的唯一取证点）：
    换过人的卡如果回不到 `RUNNING`，本文件第二次 `run_once` 会在回收段就被拒，红在这里而不是红在别处。
    """
    request, card = running_card
    counts: dict = {}
    run_once(session, request.id, card.id, review=counting_review(counts))
    session.expire_all()
    assert session.get(TaskCard, (request.id, card.id)).status == hp.LANDED

    slots = InProcessSlotManager.from_config({a: 3 for a in (QODER, WORKBUDDY, DOUBAO)})
    lease = dp.dispatch(
        session, feature_request_id=request.id, task_id=card.id, slots=slots, created_at=NOW
    )
    assert session.get(TaskCard, (request.id, card.id)).status == dp.STARTED
    slots.release(lease.handle)

    run_once(session, request.id, card.id, review=counting_review(counts))
    session.expire_all()

    fresh = session.get(TaskCard, (request.id, card.id))
    assert fresh.status == hp.ESCALATED, fresh.status
    assert fresh.handoff_count == 1, "升级把次数写成 2 ⇒ 库面那条 CHECK 会先炸，这里点名是第二次换人"
    assert fresh.executor_agent_id == WORKBUDDY, "人工出口不是'由我再挑一个人'：升级不许顺手换人"

    actions = [row.payload["action"] for row in task_rows(session, card.id)]
    assert actions.count("handed_off") == 1 and actions[-1] == "escalated", actions
    assert task_rows(session, card.id)[-1].payload["reason"], "升级也要带原因，人工接手时读不到就白升级"

    involved = {row.payload["selected"] for row in selection_rows(session, card.id)}
    assert involved == {QODER, WORKBUDDY}, (
        f"涉及的工具集合是 {sorted(involved)} ⇒ 第三支工具被拉进来了（宪法「三」的三工具循环）"
    )
    assert counts.get("review") is None, counts
