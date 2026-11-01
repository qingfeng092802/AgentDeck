"""`ACP_REVIEW_MODE=auto`：门禁代签（2026-10-28 立）。

现场口径是"每张卡都要点一次，一单六张卡就是六次点击" —— owner 变成了盖章机器。
这一档的语义只有一句：**产物过门禁即视为通过，卡不进人工评审、也不调模型**。

本文件问五件事，各自只有一种坏法能把它撞红：

1. **它是取值域里的第三格，不是"猜出来的"**：`auto` 进 `REVIEW_MODES`，别的写法照旧启动期拒。
2. **它不造审查跑者**：`resolve_review_runner("auto")` 必须是 `None`，而且**不碰凭据**
   （删掉 `DEEPSEEK_API_KEY` 也不许抛）。这一条守的是"同一张卡被签两次"：
   跑卡事务里签一次、锁释放后 runner 再签一次，是这条路唯一的结构性风险。
3. **代签发生在 `valid` 那一支、只发生一次**：AST 数 `run_card_segment` 里 `_sign_by_gate` 的调用点
   （恰好 1 个，且被 `review_mode == AUTO` 与 `validated == VALID` 同时圈住）。
   `invalid` 的出口是 `POST /tasks/{id}/artifact-retry`（人工决定再跑一次），不是"签个字就过去"。
4. **签字的主语是门禁，不是编排器**：`principal=GATE_REVIEWER`，不是 `run.ACTOR_AGENT_ID`。
   写 `orchestrator` 会让审计里"谁判的"读成"编排器判的"，而它没有审查职责。
5. **三种验收来源的 mark 两两不同**：人工 / 模型 / 门禁。借错一个，审计里就会长出一份
   没发生过的审查结论。

反向对照（缺了就等于判据恒红）：
- `auto` 不建跑者 ↔ `deepseek` 建（同一函数、同一个入参形状）；
- 门禁能签 `REVIEWING` 的卡 ↔ 签不动 `ARTIFACT_INVALID` 的卡（后者会撞 `state_not_reviewing`）；
- `run_card_segment` 的默认模式仍是 `manual`（不传就是 Phase 16 的行为，逐字不变）。
"""

from __future__ import annotations

import ast
import datetime as dt
import inspect
import os
import pathlib
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from acp import realrun
from acp.api import autonomy as autonomy_api
from acp.config.loader import DSN_ENV, db_url
from acp.orchestrate.nodes import assign_guards as ag
from acp.persistence.models_approval import AcceptanceMap, ReviewVerdict
from acp.persistence.models_core import AgentUnit, Artifact, FeatureRequest, TaskCard
from acp.persistence.models_registry import EventLog, Snapshot
from acp.registry import roles as role_registry
from acp.review import policy
from acp.review import runner as runner_mod
from acp.review import service as review_service

needs_pg = pytest.mark.skipif(
    not os.environ.get(DSN_ENV), reason=f"缺 {DSN_ENV} ⇒ 本条要真 PG，整条 skip（不是红）"
)

NOW = dt.datetime(2026, 10, 28, 12, 0, tzinfo=dt.timezone.utc)
CRITERIA = ["页面有输入框", "刷新后列表仍在"]
MAP_ITEMS = ["页面能打开", "列表可用"]
EXECUTOR = "qoder"
REVIEWER = "doubao"
ACCEPTOR = "workbuddy"


# ------------------------------------------------------------------ 一、取值域与装配（纯读表）


def test_auto_is_the_third_value_and_unknown_words_still_fail_at_startup() -> None:
    """`auto` 在封闭集合里；别的写法照旧启动期拒 —— 不降级、不猜。"""
    assert policy.REVIEW_MODE_AUTO == "auto"
    assert policy.REVIEW_MODES == ("manual", "deepseek", "auto")
    assert policy.resolve_review_mode(" AUTO ") == "auto"
    for bad in ("automatic", "off", "true", ""):
        with pytest.raises(policy.ReviewModeInvalid):
            policy.resolve_review_mode(bad)


def test_auto_builds_no_review_runner_and_does_not_touch_credentials(monkeypatch) -> None:
    """`auto` 不造跑者：它根本不在"审查跑者"那一层动手（签在跑卡事务里）。

    **反向对照**：同一个函数在 `deepseek` 下必须抛（缺 key），否则"auto 不碰凭据"这句话
    可能只是因为"这个函数本来就不读凭据"而恒绿 —— 空对空。
    """
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    kwargs = {"session_factory": lambda: None, "bus_root": pathlib.Path(".")}
    assert policy.resolve_review_mode("auto") == policy.REVIEW_MODE_AUTO
    assert _resolve("auto", **kwargs) is None
    assert _resolve("manual", **kwargs) is None
    with pytest.raises(Exception) as caught:
        _resolve("deepseek", **kwargs)
    assert "DEEPSEEK_API_KEY" in str(caught.value), "缺 key 时只报变量名，且不许静默变 manual"


def _resolve(mode: str, **kwargs):
    from acp import serve

    return serve.resolve_review_runner(mode, **kwargs)


def test_the_three_acceptance_marks_are_pairwise_distinct() -> None:
    """人工 / 模型 / 门禁三份 mark 两两不同。借错一个，审计里就多一份没发生过的结论。"""
    human = review_service.HumanPassAcceptance("owner-1").MARK
    model = runner_mod.AutoReviewPassAcceptance("deepseek-flash").MARK
    gate = review_service.GatePassAcceptance.MARK
    assert len({human, model, gate}) == 3, (human, model, gate)
    assert human.startswith("human-review-pass:")
    assert model.startswith("review-auto-pass:")
    assert gate.startswith("gate-review-pass:")


def test_the_gate_never_borrows_the_model_mark_or_the_human_one() -> None:
    """门禁那一条的证据串里不许出现另外两条 mark 的前缀（它是"没有审查"这件事的载体）。"""

    class _Card:
        id = "T01"

    verdicts = review_service.GatePassAcceptance().verdicts_for(_Card(), CRITERIA)
    assert [item.met for item in verdicts] == [True, True]
    text = " ".join(item.evidence for item in verdicts)
    assert review_service.GatePassAcceptance.MARK in text
    assert "human-review-pass" not in text
    assert "review-auto-pass" not in text
    assert "不是独立验收判断" in text, "代填这件事必须写在证据里，不能让人读成真的逐条验过"


def test_auto_is_not_reviewer_ready_on_the_panel(monkeypatch) -> None:
    """胶囊按钮那一格在 `auto` 下如实报 `False` + 模式名 —— 它确实不参与那条判定。"""
    monkeypatch.setenv(policy.REVIEW_MODE_ENV, "auto")
    assert autonomy_api._review_mode() == "auto"
    assert autonomy_api._reviewer_ready() is False

    monkeypatch.setenv(policy.REVIEW_MODE_ENV, "deepseek")
    assert autonomy_api._review_mode() == "deepseek"
    assert autonomy_api._reviewer_ready() is True

    monkeypatch.setenv(policy.REVIEW_MODE_ENV, "手动")  # 不认识 ⇒ 如实报 manual，不猜
    assert autonomy_api._review_mode() == "manual"


# ------------------------------------------------------------------ 二、结构（AST）


def _function(name: str) -> ast.FunctionDef:
    tree = ast.parse(pathlib.Path(realrun.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"`{name}` 不在 {realrun.__file__} 里 ⇒ 判据的锚点失效了")


def _calls_to(node: ast.AST, name: str) -> list[ast.Call]:
    return [
        call
        for call in ast.walk(node)
        if isinstance(call, ast.Call) and getattr(call.func, "id", getattr(call.func, "attr", None)) == name
    ]


def test_the_gate_signing_has_exactly_one_call_point_and_it_is_the_valid_branch() -> None:
    """`run_card_segment` 里只有一个 `_sign_by_gate` 调用点，且同时被两个条件圈住。

    为什么要 AST 而不是行为：这条边的坏法是"多了一个触发点"（例如有人在 `run_real` 里又调一次），
    那种坏法在任何单个用例里都看不出来 —— 它只在真跑时表现为"同一张卡两条 EN-08"。
    """
    segment = _function("run_card_segment")
    calls = _calls_to(segment, "_sign_by_gate")
    assert len(calls) == 1, f"`_sign_by_gate` 在 run_card_segment 里被调了 {len(calls)} 次"

    guards = [
        node
        for node in ast.walk(segment)
        if isinstance(node, ast.If) and any(call is calls[0] for call in ast.walk(node))
    ]
    assert len(guards) == 1, "那个调用不在任何 if 里 ⇒ 它对 invalid 的卡也会签"
    condition = ast.dump(guards[0].test)
    assert "REVIEW_MODE_AUTO" in condition, condition
    assert "VALID" in condition, condition


def test_the_gate_signs_as_the_gate_not_as_the_orchestrator() -> None:
    """`_sign_by_gate` 的签字主语是 `GATE_REVIEWER`、结论是 `APPROVE`、验收来源是门禁那一份。"""
    kwargs = {
        keyword.arg: keyword.value
        for call in _calls_to(_function("_sign_by_gate"), "submit_review")
        for keyword in call.keywords
    }
    assert ast.dump(kwargs["principal"]) == "Attribute(value=Name(id='review_policy', ctx=Load()), attr='GATE_REVIEWER', ctx=Load())", (
        ast.dump(kwargs["principal"])
    )
    assert ast.dump(kwargs["verdict"]).endswith("attr='APPROVE', ctx=Load())")
    assert ast.dump(kwargs["accept_for"]).startswith("Call(func=Attribute(")
    assert "GatePassAcceptance" in ast.dump(kwargs["accept_for"])


def test_the_two_sweeps_share_one_auto_guard() -> None:
    """补签与补重跑都在**同一个** `review_mode == AUTO` 分支里 —— `manual` / `deepseek` 一格不动。

    两者回答的是同一个问题："切到 auto 之后，这张单里还有哪些卡在等人？"
    拆成两个 guard 就会有一条路被漏掉（改了一处、另一处还按老口径跑）。
    """
    run_real = _function("run_real")
    sign = _calls_to(run_real, "sign_waiting_reviews")
    retry = _calls_to(run_real, "retry_invalid_cards")
    assert len(sign) == 1 and len(retry) == 1, (len(sign), len(retry))
    guards = [
        node
        for node in ast.walk(run_real)
        if isinstance(node, ast.If)
        and any(call is sign[0] for call in ast.walk(node))
        and any(call is retry[0] for call in ast.walk(node))
    ]
    assert len(guards) == 1, "两个扫不在同一个 if 里 ⇒ 迟早有一处被改漏"
    assert "REVIEW_MODE_AUTO" in ast.dump(guards[0].test)


def test_the_auto_retry_is_bounded_to_one_per_card_per_run() -> None:
    """`run_real` 里那次自动重跑同时被三个条件圈住：`auto` 模式、产物不达标、**这张卡这一圈还没重跑过**。

    缺第三个条件就是"一支一直交不出产物的执行器会让这一圈转到 `MAX_CHAIN_ROUNDS`" ——
    那时谁也说不清它是慢还是坏。缺第一个条件则是 `manual` 也会自己重跑（人工那条路必须能停住）。
    """
    run_real = _function("run_real")
    calls = _calls_to(run_real, "_retry_invalid")
    assert len(calls) == 1, f"`_retry_invalid` 在 run_real 里被调了 {len(calls)} 次"
    guards = [
        node
        for node in ast.walk(run_real)
        if isinstance(node, ast.If) and any(call is calls[0] for call in ast.walk(node))
    ]
    assert len(guards) == 1, "重跑不在任何 if 里 ⇒ 每张卡都会无限重跑"
    condition = ast.dump(guards[0].test)
    assert "REVIEW_MODE_AUTO" in condition, condition
    assert "INVALID" in condition, condition
    assert "retried" in condition, condition


def test_the_sweep_runs_only_in_auto_mode() -> None:
    """`run_real` 里那次"补签历史遗留"只在 `auto` 下发生 —— `manual` / `deepseek` 一格不动。

    这条守的是 Phase 17 D6：`deepseek` 不追溯已在 `REVIEWING` 的卡（那是换审查员，补签等于让它
    审一批没给它看过的产物）。`auto` 不追追溯同样成立不了 —— 它根本没有审查员。
    """
    run_real = _function("run_real")
    calls = _calls_to(run_real, "sign_waiting_reviews")
    assert len(calls) == 1, f"补签在 run_real 里被调了 {len(calls)} 次"
    guards = [
        node
        for node in ast.walk(run_real)
        if isinstance(node, ast.If) and any(call is calls[0] for call in ast.walk(node))
    ]
    assert len(guards) == 1, "补签不在任何 if 里 ⇒ manual 模式也会去签历史卡"
    assert "REVIEW_MODE_AUTO" in ast.dump(guards[0].test)


def test_the_default_mode_is_still_manual_so_cli_and_tests_are_untouched() -> None:
    """不传 `review_mode` 就是 `manual` —— CLI 与既有判据逐字不变，这一档是显式选择。"""
    for func in (realrun.run_card_segment, realrun.run_real):
        default = inspect.signature(func).parameters["review_mode"].default
        assert default == policy.REVIEW_MODE_MANUAL, (func.__name__, default)


# ------------------------------------------------------------------ 三、真 PG


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
def bus_root(tmp_path):
    root = tmp_path / "bus"
    (root / "demo" / "qoder-workspace").mkdir(parents=True)
    (root / "demo" / "qoder-workspace" / "index.html").write_bytes(
        b"<html><body><input id='x'></body></html>"
    )
    return root


def _seed(session: Session, *, status: str = "REVIEWING", reviewer: str | None = REVIEWER) -> uuid.UUID:
    """一张停在 `REVIEWING` 的卡 + 一条 valid 产物 + 验收映射。

    `reviewer=None` 那一支用来量"三岗没配齐时门禁也签不动"（`record_verdict` 会撞 `reviewer_mismatch`）。
    """
    for agent_id in {EXECUTOR, ACCEPTOR, REVIEWER}:
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
    session.add(
        TaskCard(
            id="T01",
            feature_request_id=request.id,
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
            status=status,
            snapshot_id=snapshot_id,
            executor_agent_id=EXECUTOR,
            reviewer_agent_id=reviewer,
            acceptor_agent_id=ACCEPTOR,
        )
    )
    session.flush()
    session.add(
        Artifact(
            id=uuid.uuid4(),
            feature_request_id=request.id,
            task_card_id="T01",
            writer_agent_id=EXECUTOR,
            rel_path="qoder-workspace/index.html",
            bytes=64,
            lines=1,
            format="html",
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
    session.flush()
    return request.id


def _verdicts(session: Session) -> list[ReviewVerdict]:
    return list(session.scalars(select(ReviewVerdict)).all())


def _events(session: Session, action: str) -> list[EventLog]:
    return [
        row
        for row in session.scalars(select(EventLog)).all()
        if isinstance(row.payload, dict) and row.payload.get("action") == action
    ]


@needs_pg
def test_the_gate_signs_a_valid_card_straight_to_delivered(session, bus_root):
    """主路径：门禁签掉 `REVIEWING` 的卡 ⇒ `DELIVERED`，EN-08 一行、主语是门禁、验收 mark 是门禁那份。"""
    fid = _seed(session)
    status = realrun._sign_by_gate(
        session,
        feature_request_id=fid,
        task_id="T01",
        bus_root=bus_root,
        now=NOW,
        emit_to=lambda *_a, **_k: None,
    )
    session.expire_all()

    card = session.get(TaskCard, (fid, "T01"))
    assert status == "DELIVERED", status
    assert str(card.status) == "DELIVERED", "门禁签过就该直接到交付，不该在 ACCEPTING 停着"

    rows = _verdicts(session)
    assert len(rows) == 1, "一次门禁代签只许一条结论"
    assert str(rows[0].verdict) == "APPROVE"
    assert rows[0].reviewer_agent_id == REVIEWER, "审查岗还是原来那个人 —— 门禁没换人，只是替这一轮收了结论"
    assert all(item["met"] is True for item in rows[0].item_findings)

    proxy = _events(session, "acceptance_items_proxy")
    assert len(proxy) == 1, "代填必须留痕"
    assert proxy[0].payload["provider_mark"] == review_service.GatePassAcceptance.MARK
    assert "human-review-pass" not in str(proxy[0].payload)
    assert "review-auto-pass" not in str(proxy[0].payload), "不许借模型那条 mark"

    # 签字的主语落在 EN-14 那条留痕的 actor 上：审计里"谁判的"这一格必须是门禁。
    # 这一列不在 EN-08 的行上（结论行记的是"哪个岗位这一轮给了结论"），所以读留痕，不读结论表。
    signed = [row for row in _events(session, "verdict_recorded")]
    assert len(signed) == 1, [row.payload for row in signed]
    assert signed[0].actor_agent_id == policy.GATE_REVIEWER, signed[0].actor_agent_id
    assert signed[0].payload["verdict"] == "APPROVE"


@needs_pg
def test_the_gate_cannot_sign_a_card_that_is_not_waiting_for_review(session, bus_root):
    """反向对照：`ARTIFACT_INVALID` 的卡签不动（`state_not_reviewing`）。

    这一条守的是"门禁只签它自己判过的那一格"：`invalid` 的出口是
    `POST /tasks/{id}/artifact-retry`（人工决定再跑一次），不是"签个字就过去"。
    所以 `run_card_segment` 里那个 `validated == VALID` 的条件不是装饰。
    """
    fid = _seed(session, status="ARTIFACT_INVALID")
    status = realrun._sign_by_gate(
        session,
        feature_request_id=fid,
        task_id="T01",
        bus_root=bus_root,
        now=NOW,
        emit_to=lambda *_a, **_k: None,
    )
    session.expire_all()
    assert status.startswith("gate_sign_failed:"), status
    assert str(session.get(TaskCard, (fid, "T01")).status) == "ARTIFACT_INVALID", "签不掉就不许动卡态"
    assert _verdicts(session) == [], "签不掉就不许留结论行"
    failures = _events(session, "review_gate_failed")
    assert len(failures) == 1, "签不掉必须留一条痕说明为什么，不许静默"
    assert "state_not_reviewing" in failures[0].payload["detail"]


@needs_pg
def test_the_sweep_signs_only_the_cards_that_are_waiting(session, bus_root):
    """补签只碰 `REVIEWING` 的卡：已经交付的那张不许被再签一次（同一张卡两条结论是硬缺陷）。"""
    fid = _seed(session)
    session.add(
        TaskCard(
            id="T02",
            feature_request_id=fid,
            title="卡 T02",
            difficulty=2,
            skill_tags=["frontend_coding"],
            depends_on=[],
            artifact_floor_min_bytes=1,
            artifact_floor_min_lines=1,
            artifact_floor_format="html",
            acceptance_criteria=list(CRITERIA),
            handoff_count=0,
            review_round=1,
            status="DELIVERED",
            snapshot_id=session.get(TaskCard, (fid, "T01")).snapshot_id,
            executor_agent_id=EXECUTOR,
            reviewer_agent_id=REVIEWER,
            acceptor_agent_id=ACCEPTOR,
        )
    )
    session.flush()

    signed = realrun.sign_waiting_reviews(
        session,
        feature_request_id=fid,
        bus_root=bus_root,
        now=NOW,
        emit_to=lambda *_a, **_k: None,
    )
    session.expire_all()

    assert signed == ["T01"], signed
    assert str(session.get(TaskCard, (fid, "T01")).status) == "DELIVERED"
    assert str(session.get(TaskCard, (fid, "T02")).status) == "DELIVERED"
    assert len(_verdicts(session)) == 1, "已交付的那张不许被再签一次"


@needs_pg
def test_auto_retries_an_invalid_card_without_changing_who_runs_it(session):
    """产物不达标 ⇒ 自动原地重跑一次：卡回到可派发态，执行者与换人次数一个都不动。

    这条不是"把不合格的东西签过去"：`ARTIFACT_INVALID` 说的是"这一趟没交出合格的产物"，
    该做的决定是"再跑一次"。它走的是人工出口用的**同一个** `resolve_artifact_retry()`，
    所以两条路不会漂成两种判定。
    """
    fid = _seed(session, status="ARTIFACT_INVALID")
    status = realrun._retry_invalid(
        session,
        feature_request_id=fid,
        task_id="T01",
        now=NOW,
        emit_to=lambda *_a, **_k: None,
    )
    session.expire_all()

    card = session.get(TaskCard, (fid, "T01"))
    assert status == "HANDED_OFF", status
    assert str(card.status) == "HANDED_OFF", "要回到能被派发的那一格，否则重跑等于没跑"
    assert card.executor_agent_id == EXECUTOR, "原地重跑不是换人"
    assert int(card.handoff_count) == 0, "顺手 +1 会吃掉 FR-027 那唯一一次换人额度"
    assert _verdicts(session) == [], "重跑不是结论，不许留 EN-08"

    rows = [
        row
        for row in session.scalars(select(EventLog)).all()
        if isinstance(row.payload, dict) and row.payload.get("action") == "artifact_retried"
    ]
    assert len(rows) == 1, "重跑必须留痕"
    assert rows[0].actor_agent_id == policy.GATE_REVIEWER, "这个决定是门禁下的，不是'有人判过'"


@needs_pg
def test_the_retry_sweep_picks_up_cards_that_were_already_stuck(session):
    """补重跑只扫 `ARTIFACT_INVALID`：已交付的那张不许被再跑一次（那是白烧一次厂商调用）。"""
    fid = _seed(session, status="ARTIFACT_INVALID")
    session.add(
        TaskCard(
            id="T02",
            feature_request_id=fid,
            title="卡 T02",
            difficulty=2,
            skill_tags=["frontend_coding"],
            depends_on=[],
            artifact_floor_min_bytes=1,
            artifact_floor_min_lines=1,
            artifact_floor_format="html",
            acceptance_criteria=list(CRITERIA),
            handoff_count=0,
            review_round=1,
            status="DELIVERED",
            snapshot_id=session.get(TaskCard, (fid, "T01")).snapshot_id,
            executor_agent_id=EXECUTOR,
            reviewer_agent_id=REVIEWER,
            acceptor_agent_id=ACCEPTOR,
        )
    )
    session.flush()

    retried = realrun.retry_invalid_cards(
        session, feature_request_id=fid, now=NOW, emit_to=lambda *_a, **_k: None
    )
    session.expire_all()

    assert retried == ["T01"], retried
    assert str(session.get(TaskCard, (fid, "T01")).status) == "HANDED_OFF"
    assert str(session.get(TaskCard, (fid, "T02")).status) == "DELIVERED", "已交付的卡不许被扫进来"
    assert realrun.retry_invalid_cards(
        session, feature_request_id=fid, now=NOW, emit_to=lambda *_a, **_k: None
    ) == [], "已经放回去的卡不再是 ARTIFACT_INVALID ⇒ 第二次扫必须是空的"


@needs_pg
def test_the_quarantine_moves_only_settled_other_requests(session, bus_root, monkeypatch):
    """工作区残留：只挪**别的、已结束的**单的产物；本单的、以及还在跑的那单的文件一格不动。

    现场（2026-10-28，两单都栽在这里）：`qoder-workspace/` 是同一个 `project_key` 下所有需求单
    共用的。上一单的 `server.py` 还在，qoder 一看"已经有了"就不写，收集侧只登记本趟写过的文件
    ⇒ 零产物 ⇒ `ARTIFACT_INVALID`。重跑多少次都一样 —— 那份文件一直在。
    """
    fid = _seed(session)
    workspace = pathlib.Path(bus_root) / "demo" / "qoder-workspace"
    mine = workspace / "index.html"
    assert mine.is_file(), "本单的产物"

    # 上一单（已交付）留下的 server.py：这一份就是要被挪走的
    (workspace / "server.py").write_text("print('hi')\n", encoding="utf-8")
    # 另一单还在跑，它的产物不许动
    (workspace / "live.css").write_text(":root{}\n", encoding="utf-8")

    for status, name, rel in (
        ("DELIVERED", "旧单", "qoder-workspace/server.py"),
        ("RUNNING", "在跑的单", "qoder-workspace/live.css"),
    ):
        other = FeatureRequest(
            id=uuid.uuid4(),
            project_key="demo",
            raw_input=f"{name}的原话",
            summary_digest={"自行补全项": [], "澄清问题": []},
            clarification_waived=True,
            status=status,
            created_at=NOW,
        )
        session.add(other)
        session.flush()
        # 产物行挂复合外键 `(feature_request_id, task_card_id)` ⇒ 每张单都得有自己的卡。
        session.add(
            TaskCard(
                id="T01",
                feature_request_id=other.id,
                title=f"{name}的卡",
                difficulty=2,
                skill_tags=["frontend_coding"],
                depends_on=[],
                artifact_floor_min_bytes=1,
                artifact_floor_min_lines=1,
                artifact_floor_format="py" if rel.endswith(".py") else "css",
                acceptance_criteria=["能跑"],
                handoff_count=0,
                review_round=0,
                status="DELIVERED" if status == "DELIVERED" else "RUNNING",
                snapshot_id=session.get(TaskCard, (fid, "T01")).snapshot_id,
                executor_agent_id=EXECUTOR,
                reviewer_agent_id=REVIEWER,
                acceptor_agent_id=ACCEPTOR,
            )
        )
        session.flush()
        session.add(
            Artifact(
                id=uuid.uuid4(),
                feature_request_id=other.id,
                task_card_id="T01",
                writer_agent_id=EXECUTOR,
                rel_path=rel,
                bytes=10,
                lines=1,
                format="py" if rel.endswith(".py") else "css",
                self_reported_status="success",
                validated_status="valid",
                invalid_reason=None,
                created_at=NOW,
            )
        )
    session.flush()

    monkeypatch.setattr(
        realrun.archive, "require_archive_root", lambda *a, **k: pathlib.Path(bus_root) / "demo"
    )
    moved = realrun.quarantine_foreign_artifacts(
        session,
        feature_request_id=fid,
        project_key="demo",
        bus_root=pathlib.Path(bus_root),
        now=NOW,
        emit_to=lambda *_a, **_k: None,
    )

    assert moved == ["qoder-workspace/server.py"], moved
    assert not (workspace / "server.py").exists(), "上一单的残留必须挪走，否则执行器会以为已经写过了"
    # `_prev/` 落在**存档根**（`qoder-workspace/` 的兄弟），不落在执行器的工作区里 ——
    # 落进去的话模型还是看得见，等于没挪。
    assert list((pathlib.Path(bus_root) / "demo" / "_prev").glob("*/qoder-workspace/server.py")), (
        "挪不是删：那是上一单的交付证据"
    )
    assert not (workspace / "_prev").exists(), "不许把隔离区放进执行器能看见的那一级"
    assert mine.is_file(), "本单的产物不许动（依赖链靠它们）"
    assert (workspace / "live.css").is_file(), "还在跑的那一单的文件不许动"


@needs_pg
def test_auto_keeps_the_same_reviewer_pool_as_manual(session):
    """`auto` 的选人池与 `manual` 逐字相同 —— 它不引入新的审查身份，也不把 `deepseek-reviewer` 放进来。

    为什么重要：`deepseek-reviewer` 进池的前提是"真的有一个模型在审"。`auto` 下没有人审，
    把它放进候选池会让审查岗空挂一个从不发言的身份，而三岗互斥还要为它让位。
    """
    role_registry.seed_builtin_roles(session)
    auto_ids = [item.agent_id for item in ag.pool_from_role_specs(session, mode=policy.REVIEW_MODE_AUTO)]
    manual_ids = [item.agent_id for item in ag.pool_from_role_specs(session, mode=policy.REVIEW_MODE_MANUAL)]
    assert auto_ids == manual_ids, (auto_ids, manual_ids)
    assert policy.DEEPSEEK_REVIEWER not in auto_ids
