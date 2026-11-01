"""`ARTIFACT_INVALID` 的人工出口（2026-10-28）：`ARTIFACT_INVALID ──▶ HANDED_OFF`。

本文件问四件事，各自只有一种坏法能把它撞红：

1. **出口是从状态机原文读的，不是常量自证**：`exit_target_in_doc()` 解析出来的那个态名必须
   ① 是文档里那条线**第一个箭头**右侧的第一个态名（不是第二个 —— 第二个是 `RUNNING`，那是派发段写的），
   ② 落在 `dispatch.DISPATCHABLE` 里。第②条是这条出口**之所以叫出口**的全部理由：
   落在不可派发的态上，等于把死路从 `ARTIFACT_INVALID` 搬到另一个同样没人推得动的格子。
2. **入口态就是门禁自己落的那个字**：`artifact_retry.TRIGGER` 与 `validate_artifact.INVALID_STATE` 同值，
   且它**不在** `DISPATCHABLE` 里 —— 后者是"这一格确实是一条死路"的现状登记，改掉它就该重写本文件。
3. **放回时只动 `status` 一列**：`executor_agent_id` / `handoff_count` 必须与进来时一样。
   换人计 `handoff_count`（`data-model.md` 规则区），原地重跑没有换人；
   顺手把次数 +1，FR-027 那唯一一次换人额度就被这次"根本没换工具"的重跑吃掉了。
4. **没有执行者的卡不许放行**：`HANDED_OFF` 之后唯一的下家是 `dispatch`，而它在执行者为空时抛
   `executor_missing`（`dispatch.py:175`）⇒ 放行它只是把死路搬进一个"看起来能跑、点下去才报错"的格子。
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from acp.api import tasks as tasks_api
from acp.config.loader import DSN_ENV, db_url
from acp.orchestrate.nodes import artifact_retry as ar
from acp.orchestrate.nodes import dispatch as dp
from acp.orchestrate.nodes import validate_artifact as va
from acp.persistence.models_core import AgentUnit, FeatureRequest, TaskCard
from acp.persistence.models_registry import EventLog, Snapshot

needs_pg = pytest.mark.skipif(
    not os.environ.get(DSN_ENV), reason=f"缺 {DSN_ENV} ⇒ 本条要真 PG，整条 skip（不是红）"
)

NOW = dt.datetime(2026, 10, 28, 11, 30, tzinfo=dt.timezone.utc)
HUMAN = "owner-1"
EXECUTOR = "qoder"


# ------------------------------------------------------------------ 纯读表（不需要 PG）


def test_the_exit_target_is_read_from_the_state_machine_not_restated_in_code():
    """目标态从 `data-model.md §2` 那一条线解析，并且是**第一个**箭头 —— 不是第二个。

    那条线原文是 `ARTIFACT_INVALID ──▶ HANDED_OFF ──▶ RUNNING（换人≤1）`，一条线上两个箭头：
    第一个是"这一格去哪"，第二个是"去了之后谁来推"。取错一个，出口就变成"由人工直接写 RUNNING"，
    而 `RUNNING` 与"占了槽"必须是同一件事（`dispatch.py` 模块头第 3 条）—— 从人工出口写过去等于跳过占槽。
    """
    line = ar.exit_line_in_doc()
    assert "ARTIFACT_INVALID" in line and "HANDED_OFF" in line, line
    target = ar.exit_target_in_doc()
    assert target == "HANDED_OFF", target
    assert target != "RUNNING", "解析到了第二个箭头 ⇒ 人工出口会跳过占槽直接写执行态"


def test_the_exit_lands_somewhere_that_can_actually_be_dispatched():
    """出口必须落在 `DISPATCHABLE` 里 —— 否则"放回"只是把死路换个格子。

    这条是关系式而不是枚举复述：将来若有人把 `HANDED_OFF` 从 `DISPATCHABLE` 里拿掉，
    `resolve_artifact_retry` 会照旧把卡写进那一格，而本判据会红在"出口不可派发"上。
    """
    assert ar.exit_target_in_doc() in dp.DISPATCHABLE


def test_the_trigger_is_the_gate_s_own_literal_and_it_is_a_dead_end_today():
    """入口态 = 门禁自己落的那个字；而且它**今天确实**不在可派发集合里（这条死路是真的）。"""
    assert ar.TRIGGER == va.INVALID_STATE == "ARTIFACT_INVALID"
    assert ar.TRIGGER not in dp.DISPATCHABLE, (
        "`ARTIFACT_INVALID` 进了 DISPATCHABLE ⇒ 这个出口的前提变了，本模块的说明书要重写"
    )


def test_an_ambiguous_doc_anchor_is_refused_rather_than_silently_picking_one(tmp_path: Path):
    """文档里出现两条带箭头的 `ARTIFACT_INVALID` 行 ⇒ 响亮报错，不猜。"""
    doc = tmp_path / "data-model.md"
    doc.write_text(
        "ARTIFACT_INVALID ──▶ HANDED_OFF ──▶ RUNNING（换人≤1）\n"
        "ARTIFACT_INVALID ──▶ REJECTED\n",
        encoding="utf-8",
    )
    with pytest.raises(ar.ArtifactRetryMisuse) as caught:
        ar.exit_line_in_doc(doc)
    assert caught.value.code == "doc_anchor_ambiguous"


def test_a_doc_line_without_an_arrow_is_refused_rather_than_inventing_a_target(tmp_path: Path):
    """锚点行里没有 `▶` ⇒ 解析不出目标态，报错而不是退回一个默认值。"""
    doc = tmp_path / "data-model.md"
    doc.write_text("ARTIFACT_INVALID 不得产生任何 review_verdict 行 ▶ 无\n", encoding="utf-8")
    # 这一行有 `▶` 但右侧没有态名 ⇒ 走 `doc_anchor_unparsed` 那一支。
    with pytest.raises(ar.ArtifactRetryMisuse) as caught:
        ar.exit_target_in_doc(doc)
    assert caught.value.code == "doc_anchor_unparsed"


# ------------------------------------------------------------------ 真 PG


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


def seed_agent(session, agent_id: str) -> None:
    session.add(
        AgentUnit(
            agent_id=agent_id,
            form="SDK",
            tier=3,
            cost_band="MID",
            max_parallel=3,
            current_load=0,
            historical_success_rate=Decimal("0.5"),
            success_rate_sample=4,
            workspace_dir=f"/tmp/{agent_id}-workspace",
            enabled=True,
        )
    )


@pytest.fixture
def invalid_card(session) -> tuple[FeatureRequest, TaskCard]:
    """一张被门禁判为 `ARTIFACT_INVALID` 的卡：执行者 `qoder`、零产物那一格。"""
    seed_agent(session, EXECUTOR)
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
    card = TaskCard(
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
        executor_agent_id=EXECUTOR,
        handoff_count=0,
        review_round=0,
        status=ar.TRIGGER,
        snapshot_id=snapshot_id,
    )
    session.add(card)
    session.flush()
    return request, card


def audits(session, task_id: str, kind: str) -> list[EventLog]:
    return list(
        session.scalars(
            select(EventLog)
            .where(EventLog.entity_id == task_id, EventLog.kind == kind)
            .order_by(EventLog.id)
        ).all()
    )


@needs_pg
def test_the_retry_puts_the_card_back_where_dispatch_can_reach_it(session, invalid_card):
    """主路径：状态走一格到 `HANDED_OFF`，执行者与换人次数一个都不动，留痕可复核。"""
    request, card = invalid_card
    outcome = ar.resolve_artifact_retry(
        session,
        feature_request_id=request.id,
        task_id=card.id,
        principal=HUMAN,
        reason="执行器返回零产物（examined: 0）",
        created_at=NOW,
    )
    assert outcome.prior_status == "ARTIFACT_INVALID"
    assert outcome.status_after == "HANDED_OFF" == ar.exit_target_in_doc(), outcome.status_after
    assert outcome.executor_agent_id == EXECUTOR, "原地重跑不许顺手换人"
    assert outcome.handoff_count == 0, "原地重跑不是换人 ⇒ 不许吃掉 FR-027 那唯一一次额度"

    fresh = session.get(TaskCard, (request.id, card.id))
    assert str(fresh.status) == "HANDED_OFF"
    assert fresh.executor_agent_id == EXECUTOR and fresh.handoff_count == 0
    assert str(fresh.status) in dp.DISPATCHABLE, "放回去的格子必须能被派发，否则只是换了个死路"

    rows = audits(session, card.id, "TASK")
    assert len(rows) == 1, [row.payload for row in rows]
    payload = rows[0].payload
    assert payload["action"] == ar.ACTION
    assert payload["prior_status"] == "ARTIFACT_INVALID"
    assert payload["card_status_after"] == "HANDED_OFF"
    assert payload["reason"] == "执行器返回零产物（examined: 0）", "理由必须原文可查"
    assert rows[0].actor_agent_id == HUMAN, "这条边的痕的主语必须是按下去的那个人，不是 orchestrator"


@needs_pg
def test_a_card_in_another_state_is_refused(session, invalid_card):
    """别的态不许从这里绕过去：状态机每条边只有一个来源态，绕过就没人判过合法性。"""
    request, card = invalid_card
    card.status = "REVIEWING"
    session.flush()
    with pytest.raises(ar.ArtifactRetryMisuse) as caught:
        ar.resolve_artifact_retry(
            session,
            feature_request_id=request.id,
            task_id=card.id,
            principal=HUMAN,
            created_at=NOW,
        )
    assert caught.value.code == "state_not_artifact_invalid"


@needs_pg
def test_a_card_without_an_executor_is_refused_instead_of_moving_the_dead_end(session, invalid_card):
    """没有执行者 ⇒ 拒。放行只是把死路从 `ARTIFACT_INVALID` 搬到 `HANDED_OFF`。"""
    request, card = invalid_card
    card.executor_agent_id = None
    session.flush()
    with pytest.raises(ar.ArtifactRetryMisuse) as caught:
        ar.resolve_artifact_retry(
            session,
            feature_request_id=request.id,
            task_id=card.id,
            principal=HUMAN,
            created_at=NOW,
        )
    assert caught.value.code == "executor_required"
    assert str(card.status) == "ARTIFACT_INVALID", "拒了就不许动状态"


@needs_pg
def test_a_missing_principal_is_refused(session, invalid_card):
    """没有"谁按的" ⇒ 这条人工边留下的痕没人认领，与 escalation 同一条理由。"""
    request, card = invalid_card
    with pytest.raises(ar.ArtifactRetryMisuse) as caught:
        ar.resolve_artifact_retry(
            session,
            feature_request_id=request.id,
            task_id=card.id,
            principal="   ",
            created_at=NOW,
        )
    assert caught.value.code == "principal_required"


# ------------------------------------------------------------------ HTTP 形状


def build_app(session) -> FastAPI:
    app = FastAPI()
    app.include_router(tasks_api.router)
    # 每次请求一个新会话，绑在**同一条连接**上：端点会 `close()` 它借到的会话
    # （`api/tasks.py::_store` 的 finally），把夹具那个交出去，回读判据就全哑了。
    app.state.session_factory = lambda: Session(bind=session.get_bind())
    return app


def retry_call(client: TestClient, card, **over) -> object:
    body = {
        "feature_request_id": str(card.feature_request_id),
        "principal": HUMAN,
        "reason": "重跑一次",
    }
    body.update(over)
    return client.post(f"/tasks/{card.id}/artifact-retry", json=body)


@needs_pg
def test_the_endpoint_returns_the_two_counts_unchanged(session, invalid_card):
    """响应里带 `handoff_count` / `rework_count`：这条路由只动 `status`，两个数必须与进来时一样。"""
    request, card = invalid_card
    client = TestClient(build_app(session))
    response = retry_call(client, card)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["prior_status"] == "ARTIFACT_INVALID" and body["status_after"] == "HANDED_OFF"
    assert body["executor_agent_id"] == EXECUTOR
    assert body["handoff_count"] == 0 and body["rework_count"] == 0


@needs_pg
def test_the_endpoint_refuses_the_automated_actor_as_a_human(session, invalid_card):
    """`orchestrator` 不能给自己签一张"人已决定"的通行证 —— 与 escalation 那条同一条门。"""
    request, card = invalid_card
    client = TestClient(build_app(session))
    response = retry_call(client, card, principal="orchestrator")
    assert response.status_code == 422, response.text
    assert "human_principal_required" in response.text


@needs_pg
def test_the_endpoint_maps_the_wrong_state_to_409(session, invalid_card):
    """错态是 409（冲突），不是 500，也不是静默 200。"""
    request, card = invalid_card
    card.status = "RUNNING"
    session.flush()
    client = TestClient(build_app(session))
    response = retry_call(client, card)
    assert response.status_code == 409, response.text
    assert "state_not_artifact_invalid" in response.text


@needs_pg
def test_the_endpoint_maps_an_unknown_card_to_404(session, invalid_card):
    """不存在的卡是 404 —— 端点不许把"查不到"读成"没什么可做"。"""
    request, card = invalid_card
    client = TestClient(build_app(session))
    body = {"feature_request_id": str(request.id), "principal": HUMAN, "reason": ""}
    response = client.post("/tasks/T99/artifact-retry", json=body)
    assert response.status_code == 404, response.text
