"""M2-W8：人工评审入口的 HTTP 面 —— 收结论、过审自动验收、全交付后组包。

**真连 PG**（缺 `ACP_DB_DSN` 整档 skip）：评审结论、验收轮次、交付包各有一张真表，
用内存替身会把"端点回了 200"读成"库里真落了"。组包那一半还要真模板目录与真 JSON 文件，
所以 `bus_root` 用 `tmp_path`，不碰工作树里的产物仓库。

四条刻意的写法：
1. **过审那一发直接读库里的终态**：`review_verdict` 一行、卡 `DELIVERED`、`review_round=1`、
   `acceptance_items_proxy` 留痕存在 —— 响应体只是读数，不是事实源。
2. **打回不碰验收**：`CHANGES_REQUESTED` 之后没有 proxy 留痕、没有交付包；
   "打回"与"通过"必须是两条可区分的路径，否则一次误点通过会被验收代填掩盖。
3. **旧现场补开**：`RUNNING` + valid 产物也能提审（代码升级前跑出的卡），
   补开动作只写 `review_opened` 一条，进门之后结论路径与 `REVIEWING` 完全一致。
4. **入口页不存在时组包失败不回滚评审**：`delivery_error` 是响应字段，不是一次 5xx ——
   评审已经发生，不能因为最后一步组包没成而把人的结论一起撤掉。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import uuid
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from acp.api import tasks as tasks_api
from acp.api.approvals import PRINCIPAL_HEADER
from acp.config.loader import DSN_ENV, db_url
from acp.orchestrate.nodes import review as review_node
from acp.persistence.models_approval import AcceptanceMap, ReviewVerdict
from acp.persistence.models_core import AgentUnit, Artifact, FeatureRequest, TaskCard
from acp.persistence.models_registry import Deliverable, EventLog, Snapshot
from acp.registry import roles as role_registry
from acp.review import service as review_service

pytestmark = pytest.mark.skipif(
    not os.environ.get(DSN_ENV), reason=f"{DSN_ENV} 未注入 ⇒ 本文件需要 PG"
)

NOW = dt.datetime(2026, 10, 21, 20, 0, 0, tzinfo=dt.timezone.utc)
PRINCIPAL = "owner-1"
HEADERS = {PRINCIPAL_HEADER: PRINCIPAL}
CRITERIA = ["首屏有公司名", "样式文件被引用"]
ITEMS = ["网站能打开", "样式生效"]
ENTRY = "qoder-workspace/index.html"


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
        for agent_id in ("qoder", "doubao", "workbuddy"):
            s.add(
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
        role_registry.seed_builtin_roles(s)
        s.flush()
        try:
            yield s
        finally:
            s.close()
            transaction.rollback()


@pytest.fixture
def bus_root(tmp_path):
    root = tmp_path / "results" / "demo"
    (root / "qoder-workspace").mkdir(parents=True)
    # P0-4：seed() 的 Artifact.bytes 固定 128。文件必须真的是 128B，否则组包前的 stat()
    # 会（正确地）判 artifact_stale，这条用例就测不到"评审通过 ⇒ 组包"的语义。
    head = "<html><h1>公司介绍</h1></html>\n".encode("utf-8")
    (root / ENTRY).write_bytes((head + b" " * 128)[:128])
    return tmp_path / "results"


def seed(session, *, status="RUNNING", with_entry=True):
    request_id = uuid.uuid4()
    session.add(
        FeatureRequest(
            id=request_id,
            project_key="demo",
            raw_input="做个介绍公司产品的静态网站",
            summary_digest={"自行补全项": [], "澄清问题": []},
            clarification_waived=True,
            status="SPLIT",
            created_at=NOW,
        )
    )
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
    session.add(
        TaskCard(
            id="T01",
            feature_request_id=request_id,
            title="卡 T01",
            difficulty=2,
            skill_tags=["frontend_coding"],
            depends_on=[],
            artifact_floor_min_bytes=512,
            artifact_floor_min_lines=12,
            artifact_floor_format="html",
            acceptance_criteria=CRITERIA,
            handoff_count=0,
            review_round=0,
            status=status,
            snapshot_id=snapshot_id,
            executor_agent_id="qoder",
            reviewer_agent_id="doubao",
            acceptor_agent_id="workbuddy",
        )
    )
    session.flush()
    session.add(
        Artifact(
            id=uuid.uuid4(),
            feature_request_id=request_id,
            task_card_id="T01",
            writer_agent_id="qoder",
            rel_path=ENTRY if with_entry else "qoder-workspace/styles.css",
            bytes=128,
            lines=14,
            format="html" if with_entry else "css",
            self_reported_status="success",
            validated_status="valid",
            invalid_reason=None,
            created_at=NOW,
        )
    )
    for index, item in enumerate(ITEMS):
        session.add(
            AcceptanceMap(
                id=uuid.uuid4(),
                feature_request_id=request_id,
                requirement_item=f"需求条目 {index}",
                acceptance_item=item,
                task_card_id="T01",
                produced_by="orchestrator",
            )
        )
    session.flush()
    return request_id


def client_for(session, bus_root):
    app = FastAPI()
    app.include_router(tasks_api.router)
    app.state.session_factory = lambda: Session(bind=session.get_bind())
    app.state.bus_root = bus_root
    app.state.review_entry_name = "index.html"
    return TestClient(app)


def submit(client, request_id, *, verdict="pass", findings=None, **extra):
    body = {
        "feature_request_id": str(request_id),
        "verdict": verdict,
        "evidence": "人工核过入口页与样式",
        **extra,
    }
    if findings is not None:
        body["findings"] = findings
    return client.post("/tasks/T01/review", json=body, headers=HEADERS)


def event_actions(session, task_id="T01"):
    return [
        (event.payload or {}).get("action")
        for event in session.query(EventLog)
        .filter(EventLog.entity == "task_card", EventLog.entity_id == task_id)
        .order_by(EventLog.id)
        .all()
    ]


def test_a_pass_verdict_delivers_the_card_and_assembles_the_package(session, bus_root):
    request_id = seed(session)
    response = submit(client_for(session, bus_root), request_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["verdict"], body["card_status"], body["accepted"]) == ("APPROVE", "DELIVERED", True)
    assert body["delivery"] is not None, body
    # 清单里是 **5 份**，不是 1 份：执行卡 T01 + 三张检查卡（T93 E2E / T94 安全 / T95 门禁）
    # + 验收汇总卡 T96。`review/service.py` 在组包**之前**跑检查器与验收卡，注释写得很明确
    # （"报告要进交付包，所以必须在组包之前落库并判完"），所以它们出现在包里是设计意图。
    # 2026-10-28 实测读数：T01 128B、T93 249B、T94 147B、T95 469B、T96 402B，五份都是 valid。
    # T97（打包摘要）**不在**清单里 —— 清单不能包含它自己。
    assert body["delivery"]["manifest_count"] == 5, body
    package = bus_root / "demo" / body["delivery"]["package_path"]
    assert package.is_file(), body
    assert json.loads(package.read_text(encoding="utf-8"))["entry_path"] == ENTRY

    card = session.query(TaskCard).filter_by(feature_request_id=request_id, id="T01").one()
    assert str(card.status) == "DELIVERED" and card.review_round == 1
    verdict = session.query(ReviewVerdict).filter_by(feature_request_id=request_id).one()
    assert str(verdict.verdict) == "APPROVE" and len(verdict.item_findings) == len(CRITERIA)
    assert session.query(Deliverable).filter_by(feature_request_id=request_id).count() == 1
    actions = event_actions(session)
    assert actions.count(review_node.ACTION_OPENED) == 1
    assert "acceptance_items_proxy" in actions and "acceptance_round" in actions


def test_a_rejected_verdict_lands_changes_requested_and_touches_no_acceptance(session, bus_root):
    request_id = seed(session, status=review_node.REVIEWING)
    findings = [
        {"criterion": CRITERIA[0], "met": False, "evidence": "首屏没有公司名"},
        {"criterion": CRITERIA[1], "met": True, "evidence": "样式已引用"},
    ]
    response = submit(
        client_for(session, bus_root), request_id, verdict="changes_requested", findings=findings
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["verdict"], body["card_status"], body["accepted"]) == (
        "CHANGES_REQUESTED",
        "CHANGES_REQUESTED",
        False,
    )
    assert body["delivery"] is None
    card = session.query(TaskCard).filter_by(feature_request_id=request_id, id="T01").one()
    assert str(card.status) == "CHANGES_REQUESTED" and card.review_round == 0
    actions = event_actions(session)
    assert "acceptance_items_proxy" not in actions and "acceptance_round" not in actions
    assert session.query(Deliverable).filter_by(feature_request_id=request_id).count() == 0


def test_a_pass_word_with_a_failed_finding_is_refused_before_any_write(session, bus_root):
    request_id = seed(session, status=review_node.REVIEWING)
    findings = [
        {"criterion": CRITERIA[0], "met": False, "evidence": "没写公司名"},
        {"criterion": CRITERIA[1], "met": True, "evidence": "样式已引用"},
    ]
    response = submit(client_for(session, bus_root), request_id, findings=findings)
    assert response.status_code == 422, response.text
    assert "verdict_findings_conflict" in response.text
    assert session.query(ReviewVerdict).filter_by(feature_request_id=request_id).count() == 0
    card = session.query(TaskCard).filter_by(feature_request_id=request_id, id="T01").one()
    assert str(card.status) == review_node.REVIEWING


def test_the_principal_header_is_required(session, bus_root):
    request_id = seed(session, status=review_node.REVIEWING)
    response = client_for(session, bus_root).post(
        "/tasks/T01/review", json={"feature_request_id": str(request_id), "verdict": "pass"}
    )
    assert response.status_code == 403
    assert PRINCIPAL_HEADER in response.text


def test_a_legacy_running_card_is_opened_by_the_endpoint(session, bus_root):
    request_id = seed(session, status="RUNNING")
    client = client_for(session, bus_root)
    pending = client.get("/tasks/reviewing").json()
    row = next(item for item in pending if item["feature_request_id"] == str(request_id))
    assert (row["status"], row["requires_open"], row["valid_artifacts"]) == ("RUNNING", True, 1)

    response = submit(client, request_id)
    assert response.status_code == 200, response.text
    assert response.json()["card_status"] == "DELIVERED"
    assert event_actions(session).count(review_node.ACTION_OPENED) == 1


def test_the_pending_list_carries_the_validated_artifact_source(session, bus_root):
    """预览面读的是盘上那份真文件，不是库里的字节数：`content` 必须逐字等于写进去的源码。"""
    request_id = seed(session, status=review_node.REVIEWING)
    pending = client_for(session, bus_root).get("/tasks/reviewing").json()
    row = next(item for item in pending if item["feature_request_id"] == str(request_id))
    assert len(row["artifacts"]) == 1, row
    artifact = row["artifacts"][0]
    assert (artifact["rel_path"], artifact["writer_agent_id"], artifact["format"]) == (
        ENTRY,
        "qoder",
        "html",
    )
    assert artifact["content_error"] is None, artifact
    assert artifact["content"] == (bus_root / "demo" / ENTRY).read_text(encoding="utf-8")


def test_an_artifact_path_outside_the_archive_root_is_not_read(session, bus_root):
    """产物行里的相对路径是外部进程写的 ⇒ 面板预览这一层 MUST 再验一次落点。
    `../secret.txt` 解析后落在存档根外，只回原因词，绝不把那份文件的内容带进 HTTP 面。"""
    request_id = seed(session, status=review_node.REVIEWING)
    (bus_root / "secret.txt").write_text("这一行不许进浏览器", encoding="utf-8")
    artifact = session.query(Artifact).filter_by(feature_request_id=request_id).one()
    artifact.rel_path = "../secret.txt"
    session.flush()

    pending = client_for(session, bus_root).get("/tasks/reviewing").json()
    row = next(item for item in pending if item["feature_request_id"] == str(request_id))
    preview = row["artifacts"][0]
    assert preview["content"] is None and preview["content_error"] == "path_outside_archive", preview
    assert "这一行" not in json.dumps(row, ensure_ascii=False)


def test_a_too_large_artifact_returns_the_reason_instead_of_a_truncated_source(
    session, bus_root, monkeypatch
):
    """超限不截半份：截断的 CSS 读起来像模型生成坏了，而事实是这一份不该整段传。"""
    from acp.review import service as review_service

    monkeypatch.setattr(review_service, "MAX_PREVIEW_BYTES", 16)
    request_id = seed(session, status=review_node.REVIEWING)
    pending = client_for(session, bus_root).get("/tasks/reviewing").json()
    row = next(item for item in pending if item["feature_request_id"] == str(request_id))
    preview = row["artifacts"][0]
    assert preview["content"] is None and preview["content_error"] == "too_large", preview


def test_all_cards_delivered_syncs_the_request_status_even_when_package_is_pending(session):
    """P1-6：卡全 DELIVERED ⇒ 需求单必须是 DELIVERED，不能卡在 SPLIT。"""
    request_id = seed(session, status="DELIVERED")

    review_service._deliver_if_complete(
        session,
        feature_request_id=request_id,
        project_key="demo",
        bus_root=None,
        entry_name="index.html",
        now=NOW,
    )

    row = session.get(FeatureRequest, request_id)
    assert str(row.status) == "DELIVERED"


def test_pending_cards_do_not_sync_the_request_status(session):
    """反向对照：还有一张卡没交付时，不许提前把单写成 DELIVERED。"""
    request_id = seed(session, status="RUNNING")

    review_service._deliver_if_complete(
        session,
        feature_request_id=request_id,
        project_key="demo",
        bus_root=None,
        entry_name="index.html",
        now=NOW,
    )

    row = session.get(FeatureRequest, request_id)
    assert str(row.status) == "SPLIT"
