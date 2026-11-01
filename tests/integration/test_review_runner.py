"""Phase 17 P2：`ReviewRunner` 的三条写路径（PASS / FAIL / NEED_HUMAN）。

**本文件要真 PG**（结论、单据、留痕都在库里），所以整份挂 `needs_pg`；不碰网络，
模型侧全部换成记账替身。三条路径各量四件事：EN-08 的结论、卡态、E 单（只在 NEED_HUMAN 时）、
以及"重复调用不会造第二张 E 单/第二条结论"。

**为什么不用 `submit_review` 的人工入口来测**：那会掩盖 runner 自己的选择——PASS 必须带
`AutoReviewPassAcceptance`（mark 里写 `deepseek-reviewer` 与响应模型），FAIL 必须落
`CHANGES_REQUESTED` 且不验收，NEED_HUMAN 必须把 `met=null` 原样保留并建 E 单。这三件事在
`submit_review` 里分别走不同分支，只有从 runner 这一侧调用才量得出来。
"""

from __future__ import annotations

import datetime as dt
import os
import pathlib
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from acp.adapters.deepseek import review as dr
from acp.config import auto_approval as auto_accept
from acp.config.loader import DSN_ENV, db_url
from acp.persistence.models_approval import AcceptanceMap, ApprovalTicket, ReviewVerdict
from acp.persistence.models_core import AgentUnit, Artifact, FeatureRequest, TaskCard
from acp.persistence.models_registry import EventLog, Snapshot
from acp.persistence.models_review import ReviewReport
from acp.registry import roles as role_registry
from acp.review import runner as rr

needs_pg = pytest.mark.skipif(
    not os.environ.get(DSN_ENV), reason=f"缺 {DSN_ENV} ⇒ 本条要真 PG，整条 skip（不是红）"
)

NOW = dt.datetime(2026, 10, 23, 9, 0, tzinfo=dt.timezone.utc)
CRITERIA = ["页面有输入框", "刷新后列表仍在"]
MAP_ITEMS = ["页面能打开", "列表可用"]
EXECUTOR = "qoder"
REVIEWER = rr.DEEPSEEK_REVIEWER
ACCEPTOR = "workbuddy"


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
def factory(session):
    """runner 的会话工厂：**共用外层连接**，让用例末尾的 rollback 能清干净。

    `Session(bind=已 begin 的连接).commit()` 在 SQLAlchemy 2.1.4 下发不到 PG（实测口径见
    `evidence/phase-14.md`），但 flush 会发生 ⇒ 同一连接上的断言读得到本轮写入；
    这不是生产形态，生产走 `persistence/session.py::new_session`。
    """
    return lambda: Session(bind=session.get_bind())


@pytest.fixture
def auto_on(monkeypatch):
    """把低风险自动验收的开关打开（2026-10-28 起的代签前提）。

    为什么用环境变量而不是改 `config/runtime.yaml`：那一格写 `null` 的意思是"跟随环境变量"，
    所以设变量就能打开，而**不用改仓里的文件** —— 用例去改配置文件，跑完之后仓里就多一处脏。
    """
    monkeypatch.setenv(auto_accept.AUTO_ACCEPT_ENV, "true")
    return True


@pytest.fixture
def bus_root(tmp_path):
    root = tmp_path / "bus"
    (root / "demo" / "qoder-workspace").mkdir(parents=True)
    (root / "demo" / "qoder-workspace" / "index.html").write_bytes(
        b"<input id='new-item'><button id='add'>add</button><script>localStorage.setItem('x','1')</script>"
    )
    return root


def _seed(
    session: Session,
    bus_root,
    *,
    status: str = "REVIEWING",
    reviewer: str = REVIEWER,
    extra_card: bool = False,
    fmt: str = "html",
):
    """造一张待审的卡。`fmt` = 这张卡的**产物类型**（`artifact_floor_format`）。

    它从 2026-10-28 起是**承重的**：低风险自动验收只对 `config/auto_accept.yaml` 里那几种后缀生效，
    所以"这张卡能不能被机器代签"完全由这一格决定。默认仍是 `html`（既有用例的现场不变），
    要测代签就显式传 `fmt="md"` —— 那一行读起来就是"这是一张文档卡"。
    """
    for agent_id in {EXECUTOR, ACCEPTOR, REVIEWER, reviewer}:
        if session.get(AgentUnit, agent_id) is None:
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
    role_registry.seed_builtin_roles(session)
    request = FeatureRequest(
        id=uuid.uuid4(),
        project_key="demo",
        raw_input="做一个待办清单前端",
        summary_digest={"自行补全项": [], "澄清问题": []},
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
    # 产物文件与卡的类型必须**同一种后缀**：低风险判定读的是卡的那一列，而审查预览读的是盘上那份。
    # 两者对不上时，用例看起来在测"md 卡"，实际喂给审查员的是一份 html —— 那种错不会报错，只会误导。
    artifact_rel = f"qoder-workspace/index.{fmt}"
    artifact_target = pathlib.Path(bus_root) / "demo" / artifact_rel
    artifact_target.parent.mkdir(parents=True, exist_ok=True)
    if not artifact_target.exists():
        artifact_target.write_bytes(b"# doc\n" if fmt == "md" else b"<html></html>")
    card = TaskCard(
        id="T01",
        feature_request_id=request.id,
        title="卡 T01",
        difficulty=2,
        skill_tags=["frontend_coding"],
        depends_on=[],
        artifact_floor_min_bytes=1,
        artifact_floor_min_lines=1,
        artifact_floor_format=fmt,
        acceptance_criteria=list(CRITERIA),
        handoff_count=0,
        review_round=0,
        status=status,
        snapshot_id=snapshot_id,
        executor_agent_id=EXECUTOR,
        reviewer_agent_id=reviewer,
        acceptor_agent_id=ACCEPTOR,
    )
    session.add(card)
    session.flush()
    session.add(
        Artifact(
            id=uuid.uuid4(),
            feature_request_id=request.id,
            task_card_id="T01",
            writer_agent_id=EXECUTOR,
            rel_path=artifact_rel,
            bytes=64,
            lines=1,
            format=fmt,
            self_reported_status="success",
            validated_status="valid",
            invalid_reason=None,
            created_at=NOW,
        )
    )
    for item in MAP_ITEMS:
        session.add(
            AcceptanceMap(
                id=uuid.uuid4(),
                feature_request_id=request.id,
                requirement_item="R-01",
                acceptance_item=item,
                task_card_id="T01",
                produced_by=EXECUTOR,
            )
        )
    if extra_card:
        session.add(
            TaskCard(
                id="T02",
                feature_request_id=request.id,
                title="卡 T02",
                difficulty=2,
                skill_tags=["frontend_coding"],
                depends_on=[],
                artifact_floor_min_bytes=1,
                artifact_floor_min_lines=1,
                artifact_floor_format="html",
                acceptance_criteria=list(CRITERIA),
                handoff_count=0,
                review_round=0,
                status="DRAFT",
                snapshot_id=snapshot_id,
                executor_agent_id=EXECUTOR,
                reviewer_agent_id=None,
                acceptor_agent_id=None,
            )
        )
    session.flush()
    return request.id


def _result(
    decision: str,
    findings: tuple[dr.Finding, ...],
    *,
    reason: str | None = None,
    issues: tuple[dict[str, str], ...] = (),
    model_id: str = "deepseek-flash",
) -> dr.ReviewResult:
    return dr.ReviewResult(
        decision=decision,
        findings=findings,
        issues=issues,
        reason=reason,
        reason_detail="unit-test",
        model_decision="FAIL" if decision == dr.FAIL else decision,
        model_id=model_id,
        attempts=1,
        http_status=200,
        latency_ms=1,
        prompt_sha256="a" * 64,
        raw_sha256="b" * 64,
        raw_chars=10,
    )


class FakeClient:
    def __init__(self, result: dr.ReviewResult | BaseException) -> None:
        self.result = result
        self.calls: list[dr.ReviewInput] = []

    def __call__(self, request: dr.ReviewInput) -> dr.ReviewResult:
        self.calls.append(request)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def _runner(factory, bus_root, client) -> rr.ReviewRunner:
    return rr.ReviewRunner(
        session_factory=factory,
        client=client,
        bus_root=bus_root,
        clock=lambda: NOW,
        emit_to=lambda line: None,
    )


def _verdicts(session: Session) -> list[ReviewVerdict]:
    return list(session.scalars(select(ReviewVerdict).order_by(ReviewVerdict.task_card_id)).all())


def _tickets(session: Session) -> list[ApprovalTicket]:
    return list(session.scalars(select(ApprovalTicket)).all())


def _reports(session: Session) -> list[ReviewReport]:
    return list(session.scalars(select(ReviewReport).order_by(ReviewReport.attempt_no)).all())


def _events(session: Session, action: str) -> list[EventLog]:
    return [
        row
        for row in session.scalars(select(EventLog)).all()
        if isinstance(row.payload, dict) and row.payload.get("action") == action
    ]


# ---------------------------------------------------------------- 一、PASS


@needs_pg
def test_auto_pass_records_en08_and_accepts_with_the_auto_mark(session, factory, bus_root, auto_on):
    """低风险卡（md）+ 开关开 ⇒ 机器代签：EN-08 APPROVE、验收代填、mark 与人工可区分。

    2026-10-28 起这条路的入口多了一道**风险闸**：`_seed(fmt="md")` 与 `auto_on` 缺一不可。
    少了 `auto_on` 就是 `switch_off`，少了 `fmt="md"` 就是 `not_low_risk` —— 两种都会退化成"只建议"，
    这条用例会红在 `card.status == DELIVERED` 那一行上（这正是它该红的地方）。
    """
    fid = _seed(session, bus_root, fmt="md")
    client = FakeClient(
        _result(dr.PASS, tuple(dr.Finding(criterion, True, "index.html") for criterion in CRITERIA))
    )
    batch = _runner(factory, bus_root, client)(feature_request_id=fid, card_ids=("T01",))
    session.expire_all()

    assert batch.processed == ("T01",), batch
    assert client.calls and client.calls[0].raw_input == "做一个待办清单前端"
    assert client.calls[0].acceptance_criteria == tuple(CRITERIA)
    assert client.calls[0].artifacts[0].rel_path == "qoder-workspace/index.md"

    card = session.get(TaskCard, (fid, "T01"))
    assert str(card.status) == "DELIVERED", "PASS 必须走验收；验收全过应到 DELIVERED"
    rows = _verdicts(session)
    assert len(rows) == 1
    assert str(rows[0].verdict) == "APPROVE"
    assert rows[0].reviewer_agent_id == REVIEWER
    assert all(item["met"] is True for item in rows[0].item_findings)
    assert _tickets(session) == [], "PASS 不得顺手建 E 单"

    proxy = _events(session, "acceptance_items_proxy")
    assert len(proxy) == 1, "自动验收必须留下代填留痕"
    assert proxy[0].payload["provider_mark"] == f"review-auto-pass:{REVIEWER}:deepseek-flash"
    assert proxy[0].payload["agent_id"] == REVIEWER
    assert "human-review-pass" not in str(proxy[0].payload)

    reports = _reports(session)
    assert len(reports) == 1, "PASS 必须写一条 EN-18 报告（与 EN-08 同一事务）"
    report = reports[0]
    assert report.decision == "PASS"
    assert report.attempt_no == 1
    assert report.verdict_id == rows[0].id
    assert report.verdict_round_no == 1 and report.approval_ticket_id is None
    assert report.model_id == "deepseek-flash"
    assert report.prompt_sha256 == "a" * 64 and report.raw_sha256 == "b" * 64
    assert {item["criterion"] for item in report.criteria} == set(CRITERIA)
    assert all(item["met"] is True for item in report.criteria)
    assert {"prompt", "response", "prompt_text", "raw"} & set(report.__table__.columns.keys()) == set(), (
        "产品表不得存原始 prompt/response 正文"
    )


# ---------------------------------------------------------------- 二、FAIL


@needs_pg
def test_auto_fail_records_changes_requested_without_acceptance_or_escalation(session, factory, bus_root):
    fid = _seed(session, bus_root)
    findings = (
        dr.Finding(CRITERIA[0], True, "index.html"),
        dr.Finding(CRITERIA[1], False, "index.html 里没有 localStorage"),
    )
    client = FakeClient(
        _result(
            dr.FAIL,
            findings,
            issues=({"criterion": CRITERIA[1], "issue": "没有 localStorage"},),
        )
    )
    batch = _runner(factory, bus_root, client)(feature_request_id=fid, card_ids=("T01",))
    session.expire_all()

    assert batch.processed == ("T01",), batch
    card = session.get(TaskCard, (fid, "T01"))
    assert str(card.status) == "CHANGES_REQUESTED"
    rows = _verdicts(session)
    assert len(rows) == 1
    assert str(rows[0].verdict) == "CHANGES_REQUESTED"
    assert _tickets(session) == [], "FAIL 是决定性结论，不得升级成 E 单"
    assert _events(session, "acceptance_items_proxy") == [], "FAIL 不得验收"

    reports = _reports(session)
    assert len(reports) == 1
    assert reports[0].decision == "FAIL"
    assert reports[0].verdict_id == rows[0].id
    assert reports[0].approval_ticket_id is None
    assert reports[0].issues == [{"criterion": CRITERIA[1], "issue": "没有 localStorage"}]
    assert reports[0].reason is None


# ---------------------------------------------------------------- 三、NEED_HUMAN


@needs_pg
def test_need_human_preserves_null_and_creates_one_e_ticket(session, factory, bus_root):
    fid = _seed(session, bus_root)
    findings = (
        dr.Finding(CRITERIA[0], True, "index.html"),
        dr.Finding(CRITERIA[1], None, "看不到 localStorage"),
    )
    client = FakeClient(_result(dr.NEED_HUMAN, findings, reason="model_uncertain"))
    runner = _runner(factory, bus_root, client)
    batch = runner(feature_request_id=fid, card_ids=("T01",))
    session.expire_all()

    assert batch.escalated == ("T01",), batch
    card = session.get(TaskCard, (fid, "T01"))
    assert str(card.status) == "REVIEWING", "NEED_HUMAN 不移动卡态，人工随后再判"
    rows = _verdicts(session)
    assert len(rows) == 1
    assert str(rows[0].verdict) == "NEED_HUMAN"
    assert rows[0].item_findings[1]["met"] is None, "模型判不了的那条 MUST NOT 被折成 true/false"
    tickets = _tickets(session)
    assert len(tickets) == 1
    assert str(tickets[0].kind) == "E_REVIEW_UNCERTAIN"
    assert str(tickets[0].state) == "PENDING"
    assert tickets[0].task_card_id == "T01"
    assert _events(session, "review_escalated")

    reports = _reports(session)
    assert len(reports) == 1, "NEED_HUMAN 必须写一条 EN-18 报告"
    report = reports[0]
    assert report.decision == "NEED_HUMAN"
    assert report.reason == "model_uncertain"
    assert report.verdict_id is None, "NEED_HUMAN 的报告走 E 单出口，XOR 的那一半为空"
    assert report.verdict_round_no == 1
    assert report.approval_ticket_id == tickets[0].id
    assert report.model_id == "deepseek-flash"
    assert [item["met"] for item in report.criteria] == [True, None], "逐条判定原样入报告"

    second = runner(feature_request_id=fid, card_ids=("T01",))
    session.expire_all()
    assert second.skipped == (("T01", "escalation_open"),), second
    assert len(client.calls) == 1, "已有 PENDING E 单时不得再调模型、不得再写第二条结论"
    assert len(_verdicts(session)) == 1
    assert len(_tickets(session)) == 1


@needs_pg
def test_need_human_with_all_true_findings_escalates_instead_of_approving(session, factory, bus_root):
    """模型说 NEED_HUMAN 但逐条都写 true 时，`record_verdict` 不能被"全 true"骗成 APPROVE。

    runner 自己也要把这条守死：它可能收到不经 `DeepSeekReviewClient` 的客户端（例如将来的另一支），
    而 `record_verdict` 只从 findings 推结论。全 true + NEED_HUMAN ⇒ 平台不采纳那批判定。
    """
    fid = _seed(session, bus_root)
    findings = tuple(dr.Finding(criterion, True, "ok") for criterion in CRITERIA)
    client = FakeClient(_result(dr.NEED_HUMAN, findings, reason="model_uncertain"))
    batch = _runner(factory, bus_root, client)(feature_request_id=fid, card_ids=("T01",))
    session.expire_all()

    assert batch.escalated == ("T01",), batch
    card = session.get(TaskCard, (fid, "T01"))
    assert str(card.status) == "REVIEWING"
    rows = _verdicts(session)
    assert len(rows) == 1
    assert str(rows[0].verdict) == "NEED_HUMAN"
    assert all(item["met"] is None for item in rows[0].item_findings)
    assert len(_tickets(session)) == 1


@needs_pg
def test_need_human_does_not_create_a_second_ticket_when_one_is_in_need_info(session, factory, bus_root):
    fid = _seed(session, bus_root)
    first = FakeClient(
        _result(
            dr.NEED_HUMAN,
            (dr.Finding(CRITERIA[0], True, "ok"), dr.Finding(CRITERIA[1], None, "看不到")),
            reason="model_uncertain",
        )
    )
    runner = _runner(factory, bus_root, first)
    runner(feature_request_id=fid, card_ids=("T01",))
    session.expire_all()
    ticket = _tickets(session)[0]
    ticket.state = "NEED_INFO"
    session.flush()

    second = runner(feature_request_id=fid, card_ids=("T01",))
    session.expire_all()
    assert second.skipped == (("T01", "escalation_open"),)
    assert len(_tickets(session)) == 1


# ---------------------------------------------------------------- 四、范围与失败


@needs_pg
def test_the_runner_skips_cards_not_assigned_to_deepseek_or_not_reviewing(session, factory, bus_root):
    fid = _seed(session, bus_root, reviewer="doubao")
    client = FakeClient(
        _result(dr.PASS, tuple(dr.Finding(criterion, True, "ok") for criterion in CRITERIA))
    )
    batch = _runner(factory, bus_root, client)(feature_request_id=fid, card_ids=("T01",))
    assert batch.processed == ()
    assert batch.skipped == (("T01", "reviewer_not_deepseek"),)
    assert client.calls == []


@needs_pg
def test_the_runner_never_raises_when_the_model_client_fails(session, factory, bus_root):
    fid = _seed(session, bus_root)
    client = FakeClient(RuntimeError("unit-test model exploded"))
    batch = _runner(factory, bus_root, client)(feature_request_id=fid, card_ids=("T01",))
    session.expire_all()

    assert batch.failed == (("T01", "RuntimeError"),), batch
    card = session.get(TaskCard, (fid, "T01"))
    assert str(card.status) == "REVIEWING", "客户端炸了必须把卡留在 REVIEWING 交人工"
    assert _verdicts(session) == []
    assert _events(session, "review_auto_failed")


@needs_pg
def test_a_pass_with_pending_cards_returns_the_feature_for_a_continue_run(session, factory, bus_root, auto_on):
    # 这里要的是"验收真的发生了、于是还差别的卡"这一支 ⇒ 同样得是低风险卡 + 开关开。
    fid = _seed(session, bus_root, extra_card=True, fmt="md")
    client = FakeClient(
        _result(dr.PASS, tuple(dr.Finding(criterion, True, "ok") for criterion in CRITERIA))
    )
    batch = _runner(factory, bus_root, client)(feature_request_id=fid, card_ids=("T01",))
    assert batch.continue_feature_ids == (fid,), batch


# ---------------------------------------------------------------- 一·五、风险闸（2026-10-28）


@needs_pg
def test_a_high_risk_pass_is_only_advice_and_leaves_the_card_for_a_human(session, factory, bus_root, auto_on):
    """开关开着，但这是一张 **html** 卡 ⇒ 不代签：只留建议，卡留在 `REVIEWING`。

    为什么这条最要紧：开关一旦打开，"开"这个动作本身不能变成"全放"。`html` 里可以有表单、
    外链、内嵌脚本 —— 2026-10-28 那次事故就是一张表单页要去写库。机器审过它可以，
    替人签它不行。
    """
    fid = _seed(session, bus_root)  # 默认 fmt="html"
    client = FakeClient(
        _result(dr.PASS, tuple(dr.Finding(criterion, True, "index.html") for criterion in CRITERIA))
    )
    batch = _runner(factory, bus_root, client)(feature_request_id=fid, card_ids=("T01",))
    session.expire_all()

    assert batch.processed == (), "被风险闸拦下的卡不算 processed"
    assert batch.skipped == (("T01", f"auto_accept_{auto_accept.NOT_LOW_RISK}"),), batch
    card = session.get(TaskCard, (fid, "T01"))
    assert str(card.status) == "REVIEWING", "机器不代签 ⇒ 卡必须还在等人"
    assert _verdicts(session) == [], "建议不是结论：EN-08 一行都不许写"
    assert _tickets(session) == [], "也不许顺手建 E 单"
    advisory = _events(session, rr.ADVISORY_ACTION)
    assert len(advisory) == 1, "机器审过了这件事必须留痕，否则屏上分不清'没审'与'审了没签'"
    assert advisory[0].payload["code"] == auto_accept.NOT_LOW_RISK
    assert advisory[0].payload["risk_class"] == auto_accept.NEEDS_HUMAN


@needs_pg
def test_the_switch_off_keeps_even_a_low_risk_card_for_a_human(session, factory, bus_root):
    """低风险卡但开关没开（默认态）⇒ 同样只建议。**默认必须是关的**：代签要人主动打开。"""
    fid = _seed(session, bus_root, fmt="md")
    client = FakeClient(
        _result(dr.PASS, tuple(dr.Finding(criterion, True, "index.md") for criterion in CRITERIA))
    )
    batch = _runner(factory, bus_root, client)(feature_request_id=fid, card_ids=("T01",))
    session.expire_all()

    assert batch.skipped == (("T01", f"auto_accept_{auto_accept.SWITCH_OFF}"),), batch
    assert str(session.get(TaskCard, (fid, "T01")).status) == "REVIEWING"
    assert _verdicts(session) == []


@needs_pg
def test_a_broken_triple_stops_the_machine_signature(session, factory, bus_root, auto_on):
    """三岗互斥不成立 ⇒ 不代签。这条是"机器代签也必须建立在三岗互斥之上"的判据。

    现场造法：把 acceptor 改成与 reviewer 同一支工具（`deepseek-reviewer` 同时占审查与验收）。
    这正是"同一支工具自己审自己、自己签自己"的形状，风险档再低也不许签。
    """
    fid = _seed(session, bus_root, fmt="md")
    card = session.get(TaskCard, (fid, "T01"))
    card.acceptor_agent_id = REVIEWER
    session.flush()

    client = FakeClient(
        _result(dr.PASS, tuple(dr.Finding(criterion, True, "index.md") for criterion in CRITERIA))
    )
    batch = _runner(factory, bus_root, client)(feature_request_id=fid, card_ids=("T01",))
    session.expire_all()

    assert batch.skipped == (("T01", f"auto_accept_{auto_accept.TRIPLE_BROKEN}"),), batch
    assert str(session.get(TaskCard, (fid, "T01")).status) == "REVIEWING"
    assert _verdicts(session) == []
