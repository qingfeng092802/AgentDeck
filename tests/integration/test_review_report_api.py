"""Phase 17 P3：`GET /tasks/{task_id}/review-report` 的只读判据（EN-18）。

本文件问四件事：

1. **只返回该卡的报告**：另一张卡的同号报告不许混进来（按卡号过滤）。
2. **按 `created_at` 倒序**，`limit` 越界 422（不静默截断）。
3. **未知卡 404**；`feature_request_id` 可精确寻址，卡号跨单重名时 409（与 `GET /tasks/{id}` 同口径）。
4. **不返回原始 prompt/response 正文**：响应里只有结构化字段与 sha256/长度/token。

现场用外层事务 + 同一连接上的会话工厂：读端点只读，`close()` 不动外层，收尾 rollback 清干净。
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from acp.app import create_app
from acp.config.loader import DSN_ENV, db_url
from acp.persistence.models_approval import ApprovalTicket, ReviewVerdict
from acp.persistence.models_core import AgentUnit, FeatureRequest, TaskCard
from acp.persistence.models_registry import Snapshot
from acp.persistence.models_review import ReviewReport

pytestmark = pytest.mark.skipif(
    not os.environ.get(DSN_ENV), reason=f"{DSN_ENV} 未注入 ⇒ 本文件需要 PG"
)

NOW = dt.datetime(2026, 10, 23, 13, 0, tzinfo=dt.timezone.utc)
FID = uuid.UUID("11111111-1111-4111-8111-111111111111")
OTHER_FID = uuid.UUID("22222222-2222-4222-8222-222222222222")
CRITERIA = ["首屏有标题", "刷新后仍在"]


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
def client(session):
    factory = lambda: Session(bind=session.get_bind())  # noqa: E731 - 只读夹具，共享外层事务
    app = create_app(session_factory=factory)
    return TestClient(app)


def _seed(session: Session) -> tuple[TaskCard, ReviewVerdict, ApprovalTicket, ReviewReport, ReviewReport]:
    for agent_id in ("qoder", "deepseek-reviewer", "workbuddy"):
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
                workspace_dir=f"{agent_id}-workspace",
                enabled=True,
            )
        )
    request = FeatureRequest(
        id=FID,
        project_key="demo",
        raw_input="做个介绍公司产品的静态网站",
        summary_digest={"自行补全项": [], "澄清问题": []},
        clarification_waived=True,
        status="SPLIT",
        created_at=NOW,
    )
    session.add(request)
    snapshot = Snapshot(
        id=uuid.uuid4(),
        round_no=1,
        phase="PRE_WORK",
        task_card_ids=["T01"],
        ref=f"local:probe-{uuid.uuid4()}",
        rolled_back_at=None,
    )
    session.add(snapshot)
    session.flush()
    card = TaskCard(
        id="T01",
        feature_request_id=FID,
        title="卡 T01",
        difficulty=2,
        skill_tags=["frontend_coding"],
        depends_on=[],
        artifact_floor_min_bytes=1,
        artifact_floor_min_lines=1,
        artifact_floor_format="html",
        acceptance_criteria=list(CRITERIA),
        handoff_count=0,
        review_round=0,
        status="REVIEWING",
        snapshot_id=snapshot.id,
        executor_agent_id="qoder",
        reviewer_agent_id="deepseek-reviewer",
        acceptor_agent_id="workbuddy",
    )
    session.add(card)
    session.flush()

    verdict = ReviewVerdict(
        id=uuid.uuid4(),
        feature_request_id=FID,
        task_card_id="T01",
        reviewer_agent_id="deepseek-reviewer",
        verdict="APPROVE",
        item_findings=[{"criterion": item, "met": True, "evidence": "index.html"} for item in CRITERIA],
        extra_criteria_used=False,
    )
    ticket = ApprovalTicket(
        id=uuid.uuid4(),
        task_card_id="T01",
        feature_request_id=FID,
        kind="E_REVIEW_UNCERTAIN",
        state="PENDING",
        payload={"pending_item": "审查不确定", "context_digest": "x", "suggested_verdict": "need_info"},
        risk_exempt_eligible=False,
        version=1,
        decided_by_principal=None,
        decided_at=None,
        timeout_at=None,
    )
    session.add_all([verdict, ticket])
    session.flush()

    passed = ReviewReport(
        id=uuid.uuid4(),
        feature_request_id=FID,
        task_card_id="T01",
        attempt_no=1,
        reviewer_agent_id="deepseek-reviewer",
        model_id="deepseek-flash",
        decision="PASS",
        model_decision="PASS",
        reason=None,
        reason_detail=None,
        criteria=[{"criterion": item, "met": True, "evidence": "index.html"} for item in CRITERIA],
        issues=[],
        prompt_sha256="a" * 64,
        raw_sha256="b" * 64,
        raw_chars=10,
        latency_ms=123,
        prompt_tokens=100,
        completion_tokens=20,
        total_tokens=120,
        verdict_id=verdict.id,
        verdict_round_no=1,
        approval_ticket_id=None,
        created_at=NOW,
    )
    uncertain = ReviewReport(
        id=uuid.uuid4(),
        feature_request_id=FID,
        task_card_id="T01",
        attempt_no=2,
        reviewer_agent_id="deepseek-reviewer",
        model_id=None,
        decision="NEED_HUMAN",
        model_decision=None,
        reason="artifact_unreadable",
        reason_detail="index.html: read_failed",
        criteria=[{"criterion": item, "met": None, "evidence": "读不出来"} for item in CRITERIA],
        issues=[],
        prompt_sha256=None,
        raw_sha256=None,
        raw_chars=0,
        latency_ms=0,
        prompt_tokens=None,
        completion_tokens=None,
        total_tokens=None,
        verdict_id=None,
        verdict_round_no=None,
        approval_ticket_id=ticket.id,
        created_at=NOW + dt.timedelta(seconds=1),
    )
    session.add_all([passed, uncertain])
    session.flush()
    return card, verdict, ticket, uncertain, passed


def _get(client: TestClient, path: str, **params):
    return client.get(path, params=params or None)


def test_reports_are_scoped_to_the_card_and_newest_first(session, client):
    _seed(session)
    response = _get(
        client,
        "/tasks/T01/review-report",
        feature_request_id=str(FID),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["attempt_no"] for item in body] == [2, 1], body
    assert [item["decision"] for item in body] == ["NEED_HUMAN", "PASS"]
    assert body[0]["approval_ticket_id"] is not None and body[0]["verdict_id"] is None
    assert body[1]["verdict_id"] is not None and body[1]["approval_ticket_id"] is None
    assert body[0]["reason"] == "artifact_unreadable"
    assert body[0]["model_id"] is None, "没调用模型时 model_id 是 NULL，不是假模型名"
    assert body[1]["model_id"] == "deepseek-flash"
    assert {item["criterion"] for item in body[1]["criteria"]} == set(CRITERIA)


def test_unknown_card_is_404_and_limit_is_capped(session, client):
    _seed(session)
    response = _get(client, "/tasks/NOPE/review-report", feature_request_id=str(FID))
    assert response.status_code == 404, response.text
    assert "task_not_found" in response.text

    assert _get(client, "/tasks/T01/review-report", limit=0).status_code == 422
    assert _get(client, "/tasks/T01/review-report", limit=101).status_code == 422
    one = _get(client, "/tasks/T01/review-report", limit=1)
    assert one.status_code == 200 and [item["attempt_no"] for item in one.json()] == [2]


def test_ambiguous_card_across_requests_is_409(session, client):
    _seed(session)
    other = FeatureRequest(
        id=OTHER_FID,
        project_key="demo",
        raw_input="另一个需求",
        summary_digest={"自行补全项": [], "澄清问题": []},
        clarification_waived=True,
        status="SPLIT",
        created_at=NOW,
    )
    snapshot = Snapshot(
        id=uuid.uuid4(),
        round_no=1,
        phase="PRE_WORK",
        task_card_ids=["T01"],
        ref=f"local:probe-{uuid.uuid4()}",
        rolled_back_at=None,
    )
    session.add_all([other, snapshot])
    session.flush()
    session.add(
        TaskCard(
            id="T01",
            feature_request_id=OTHER_FID,
            title="同名卡",
            difficulty=2,
            skill_tags=["frontend_coding"],
            depends_on=[],
            artifact_floor_min_bytes=1,
            artifact_floor_min_lines=1,
            artifact_floor_format="html",
            acceptance_criteria=list(CRITERIA),
            handoff_count=0,
            review_round=0,
            status="REVIEWING",
            snapshot_id=snapshot.id,
            executor_agent_id="qoder",
            reviewer_agent_id="deepseek-reviewer",
            acceptor_agent_id="workbuddy",
        )
    )
    session.flush()

    response = _get(client, "/tasks/T01/review-report")
    assert response.status_code == 409, response.text
    assert "task_ambiguous" in response.text
    scoped = _get(client, "/tasks/T01/review-report", feature_request_id=str(OTHER_FID))
    assert scoped.status_code == 200 and scoped.json() == []


def test_the_response_carries_no_raw_prompt_or_response_body(session, client):
    _seed(session)
    response = _get(client, "/tasks/T01/review-report", feature_request_id=str(FID))
    assert response.status_code == 200
    for item in response.json():
        keys = set(item)
        assert {"prompt", "response", "prompt_text", "raw_response", "log"} & keys == set(), keys
        assert "prompt_sha256" in keys and "raw_sha256" in keys
