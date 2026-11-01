"""`ACP_APPROVAL_MODE=auto`：拆单确认由规则免确认（2026-10-28 立）。

现场口径：`ACP_REVIEW_MODE=auto` 之后，逐张卡的评审没了，但**拆单确认**（A 类单据）还挂在那儿 ——
它是图上的中断点，不批就不往下走，`/requests/{id}/continue` 见到待批单还直接 409。
于是仍然是"每单点一次"，而那颗按钮长在右下那块浮层面板里。面板被 owner 要求整块删掉之后，
这道门必须由机器自己过，否则整条链在拆单那一步永久停住。

这一档的语义只有一句：**拆单方案落库即由规则表免确认，然后自己续跑**。

本文件问五件事，各自只有一种坏法能把它撞红：

1. **它是取值域里的第二格，不是"猜出来的"**：`auto` 进 `APPROVAL_MODES`，别的写法启动期拒。
2. **`WebIntakeRunner` 的 dataclass 默认值仍是 `manual`**（裸构造不自己开始免确认）；
   `build_app` 的缺省解析在 **2026-10-29 P0-6** 改为 `auto` —— live 档没有审批按钮，
   缺省 manual 会让新装环境在拆单那一步永久停住；显式 `ACP_APPROVAL_MODE=manual` 仍走人工门。
3. **签字的唯一写者是 `apply_auto_pass()`**：AST 数 `serve_intake` 里对审批表的 `update(`，
   必须为 0 —— 自己 UPDATE 一行状态就把 `channel=AUTO` 与 `DASHBOARD/CLI` 那条区分抹掉了。
4. **署名是规则名，不是 `system` / `None`**：`decided_by=SPLIT_AUTO_RULE`，且那条规则名里带开关名。
5. **续跑只发生在真签成之后**：`_resume_after_auto_pass` 的唯一调用点被
   `if self._auto_pass_split(...)` 圈住 —— 签不成还往下跑，等于让一张 PENDING 的单进执行段。

反向对照（缺了就等于判据恒红）：
- `auto` 签成 ↔ `manual` 一格不动（同一函数、同一个入参形状）；
- 签成之后再签一次 ⇒ `applied=False` 且**不多写一行** EN-10（幂等，不许伪造第二次批准）；
- 没注 `resumer` 时如实留痕 `resume_skipped`，不抛、不假装跑下去了。
"""

from __future__ import annotations

import ast
import contextlib
import datetime as dt
import inspect
import os
import pathlib
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from acp.approval import policy as approval_policy
from acp.approval import repository as approval_repo
from acp.approval import service as approval_service
from acp.approval.timeouts import KIND_BY_LETTER, policy_for
from acp.config.loader import DSN_ENV, db_url
from acp.persistence.models_approval import ApprovalTicket
from acp.persistence.models_core import FeatureRequest
from acp.persistence.models_registry import EventLog
from acp.serve import build_app
from acp.serve_intake import WebIntakeRunner
from acp.persistence import session as session_module

needs_pg = pytest.mark.skipif(
    not os.environ.get(DSN_ENV), reason=f"缺 {DSN_ENV} ⇒ 本条要真 PG，整条 skip（不是红）"
)

NOW = dt.datetime(2026, 10, 28, 12, 0, tzinfo=dt.timezone.utc)
SOURCE = pathlib.Path(__file__).resolve().parents[2] / "src" / "acp"


def _tree(name: str = "serve_intake.py") -> ast.Module:
    return ast.parse((SOURCE / name).read_text(encoding="utf-8"))


def _method(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"没找到 {name}")


def _calls_to(func: ast.AST, name: str) -> list[ast.Call]:
    found = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if isinstance(target, ast.Attribute) and target.attr == name:
            found.append(node)
        elif isinstance(target, ast.Name) and target.id == name:
            found.append(node)
    return found


# ------------------------------------------------------------------ 一、取值域与默认档


def test_auto_is_the_second_value_and_unknown_words_still_fail_at_startup() -> None:
    """`auto` 在封闭集合里；别的写法照旧启动期拒 —— 不降级、不猜。"""
    assert approval_policy.APPROVAL_MODE_AUTO == "auto"
    assert approval_policy.APPROVAL_MODES == ("manual", "auto")
    assert approval_policy.resolve_approval_mode(" AUTO ") == "auto"
    for bad in ("automatic", "on", "true", "", "deepseek"):
        with pytest.raises(approval_policy.ApprovalModeInvalid):
            approval_policy.resolve_approval_mode(bad)


def test_the_switch_only_fires_for_auto() -> None:
    """`auto_split_confirm` 只认 `auto` 一个值 —— 判据写成 `==` 而不是 `!= manual`。

    写成 `!= manual` 的话，将来加第三档（比如"只免文档类拆单"）会**默默继承**"全免"这个后果。
    """
    assert approval_policy.auto_split_confirm(approval_policy.APPROVAL_MODE_AUTO) is True
    assert approval_policy.auto_split_confirm(approval_policy.APPROVAL_MODE_MANUAL) is False
    for other in ("deepseek", "", "AUTO"):
        assert approval_policy.auto_split_confirm(other) is False


def test_the_rule_name_says_which_switch_let_it_through() -> None:
    """署名必须能一路查回 `.env` 那一行：`auto-split-confirm:ACP_APPROVAL_MODE=auto`。

    写成 `system` / `auto` 这类没有出处的词，等于把"谁决定的"退化成"某个东西决定的"。
    """
    assert approval_policy.SPLIT_AUTO_RULE == "auto-split-confirm:ACP_APPROVAL_MODE=auto"
    assert approval_policy.APPROVAL_MODE_ENV in approval_policy.SPLIT_AUTO_RULE
    assert approval_policy.APPROVAL_MODE_AUTO in approval_policy.SPLIT_AUTO_RULE


def test_the_runner_defaults_to_manual_and_has_no_resumer() -> None:
    """不传就是 `manual` + 没有续跑者 —— 判据里的裸 runner 不许自己开始免确认。"""
    assert inspect.signature(WebIntakeRunner).parameters["approval_mode"].default == approval_policy.APPROVAL_MODE_MANUAL
    assert inspect.signature(WebIntakeRunner).parameters["resumer"].default is None


def test_build_app_resolves_the_gate_from_its_own_env_and_hands_the_resumer_in() -> None:
    """两格各自解析、共用同一个续跑者对象。

    由 `ACP_REVIEW_MODE` 推导这一格是不行的：那会让"只免验收、拆单要看一眼"没有表达方式。
    判据读源码而不是读行为 —— `build_app` 里要真能看见 `APPROVAL_MODE_ENV` 与 `resumer=graph_resumer`。
    """
    code = (SOURCE / "serve.py").read_text(encoding="utf-8")
    assert "else APPROVAL_MODE_AUTO" in code
    assert "ACP_APPROVAL_MODE 未设置 ⇒ 默认 auto" in code
    assert "approval_mode=gate" in code
    assert "resumer=graph_resumer" in code


# ------------------------------------------------------------------ 二、写侧只有一条路


def test_the_intake_never_writes_the_ticket_state_itself() -> None:
    """`serve_intake` 里 MUST NOT 出现对 `ApprovalTicket` 的 `update(`。

    `apply_auto_pass()` 是本仓为"规则表免确认"留的唯一写者：它只允许 A 类、必须由规则署名、
    并且写一行 `channel=AUTO` 的 EN-10。自己 UPDATE 一行状态就把那条区分抹了 ——
    审计面从"规则批的"退化成"库里就这么写的"。
    """
    tree = _tree()
    updates = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "update"
    ]
    assert updates == [], "serve_intake 里出现了 update(...) ⇒ 它在自己改表"


def test_the_signature_is_the_rule_name_not_a_bare_literal() -> None:
    """`decided_by` 取 `approval_policy.SPLIT_AUTO_RULE`，不是字符串字面量、不是 `None`。"""
    method = _method(_tree(), "_auto_pass_split")
    calls = _calls_to(method, "apply_auto_pass")
    assert len(calls) == 1, f"apply_auto_pass 被调了 {len(calls)} 次"
    keywords = {kw.arg: kw.value for kw in calls[0].keywords}
    assert "decided_by" in keywords, "没传 decided_by ⇒ apply_auto_pass 会直接拒"
    value = keywords["decided_by"]
    assert isinstance(value, ast.Attribute) and value.attr == "SPLIT_AUTO_RULE", ast.dump(value)


def test_the_resume_happens_only_after_a_real_signature() -> None:
    """`_resume_after_auto_pass` 的唯一调用点被 `if self._auto_pass_split(...)` 圈住。

    签不成（单据已被人定过 / 不存在）还往下跑，等于让一张仍是 `PENDING` 的单进执行段 ——
    而执行段不会再看那张单据，它只看卡态。
    """
    run = _method(_tree(), "_run")
    calls = _calls_to(run, "_resume_after_auto_pass")
    assert len(calls) == 1, f"续跑被调了 {len(calls)} 次"
    guards = [
        node
        for node in ast.walk(run)
        if isinstance(node, ast.If) and any(call is calls[0] for call in ast.walk(node))
    ]
    assert len(guards) == 1, "续跑不在任何 if 里 ⇒ 免确认失败也会往下跑"
    dumped = ast.dump(guards[0].test)
    assert "_auto_pass_split" in dumped, dumped


def test_the_switch_is_checked_inside_the_signer() -> None:
    """档位判断长在 `_auto_pass_split` **里面**，而且在碰仓储之前就返回。

    放在调用方那边的话，"新加一个调用点忘了判"就是一次无人批过的批准；而这个方法是
    `serve_intake` 里唯一会动审批状态的地方（见上一条），前提就该由它自己守。
    判据要求那条 `if` 的 body 是 `return False` 且位置在 `apply_auto_pass` 之前 ——
    "判了但没拦住"（比如判完继续往下走）在这里会红。
    """
    method = _method(_tree(), "_auto_pass_split")
    guards = [
        node
        for node in ast.walk(method)
        if isinstance(node, ast.If) and "auto_split_confirm" in ast.dump(node.test)
    ]
    assert len(guards) == 1, "档位判断不在 _auto_pass_split 里（或被写了两次）"
    body = guards[0].body
    assert len(body) == 1 and isinstance(body[0], ast.Return), ast.dump(guards[0])
    assert isinstance(body[0].value, ast.Constant) and body[0].value.value is False
    signer = _calls_to(method, "apply_auto_pass")[0]
    assert guards[0].lineno < signer.lineno, "判断排在签字之后 ⇒ 拦不住"


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
def shared_session(monkeypatch, session):
    """让 runner 内部 `new_session(...)` 借**同一个会话**，但不提交。

    `_auto_pass_split` / `_trace` 都是"自己开会话、自己收尾"的写法（那是生产口径，不改）。
    判据这边换成不提交的上下文，于是整条用例仍然由外层那个 `transaction.rollback()` 收尾 ——
    否则它会真提交，`acp_test` 里就留下一张孤儿需求单。
    """

    @contextlib.contextmanager
    def _borrow(factory):  # noqa: ANN001 - 签名与 `new_session(factory)` 同形
        yield session

    monkeypatch.setattr(session_module, "new_session", _borrow)
    return session


def _request(session: Session) -> FeatureRequest:
    row = FeatureRequest(
        id=uuid.uuid4(),
        project_key="demo",
        raw_input="做一个手冲咖啡的静态官网",
        summary_digest={"自行补全项": [], "澄清问题": []},
        clarification_waived=True,
        status="SPLIT",
        created_at=NOW,
    )
    session.add(row)
    session.flush()
    return row


def _ticket(session: Session, request_id: uuid.UUID) -> uuid.UUID:
    """一张停在 `PENDING` 的 A 类单据（不传 facts ⇒ 走"等人批"那一支）。"""
    payload = approval_service.build_payload(
        kind=KIND_BY_LETTER["A"],
        pending_item="A 类待批内容",
        context_digest="上下文摘要",
        suggested_verdict="approve",
        impact={"downstream_task_ids": ["T01"], "estimated_extra_usage": {}},
    )
    ticket_id = uuid.uuid4()
    approval_repo.create_or_merge(
        session,
        kind=KIND_BY_LETTER["A"],
        payload=payload,
        risk_exempt_eligible=False,
        timeout_policy=policy_for(KIND_BY_LETTER["A"]),
        feature_request_id=request_id,
        now=NOW,
        ticket_id=ticket_id,
    )
    session.flush()
    return ticket_id


def _runner(session, **over) -> WebIntakeRunner:
    params = dict(session_factory=lambda: session, bus_root=pathlib.Path("/tmp/acp-probe"), approval_mode="auto")
    params.update(over)
    return WebIntakeRunner(**params)


@needs_pg
def test_auto_signs_the_ticket_with_the_rule_name_and_writes_an_auto_row(shared_session) -> None:
    """`auto`：单据进 `APPROVED`、署名是规则名、EN-10 多一行 `channel=AUTO`。"""
    request = _request(shared_session)
    ticket_id = _ticket(shared_session, request.id)
    runner = _runner(shared_session)
    summary: dict[str, object] = {}

    assert runner._auto_pass_split(request.id, ticket_id, summary) is True

    row = shared_session.get(ApprovalTicket, ticket_id)
    shared_session.refresh(row)
    assert str(row.state) == approval_repo.APPROVED
    assert row.decided_by_principal == approval_policy.SPLIT_AUTO_RULE
    assert row.version == 1
    assert summary["auto_pass"] == "applied"
    arrivals = approval_repo.events(shared_session, ticket_id)
    assert [(str(e.channel), str(e.verdict), e.applied, e.reason_text) for e in arrivals] == [
        ("AUTO", "approve", True, approval_policy.SPLIT_AUTO_RULE)
    ]


@needs_pg
def test_manual_leaves_the_ticket_pending(shared_session) -> None:
    """反向对照：`manual` 档一格不动 —— 单据仍 `PENDING`，EN-10 一行都没有。"""
    request = _request(shared_session)
    ticket_id = _ticket(shared_session, request.id)
    runner = _runner(shared_session, approval_mode="manual")
    summary: dict[str, object] = {}

    assert runner._auto_pass_split(request.id, ticket_id, summary) is False

    row = shared_session.get(ApprovalTicket, ticket_id)
    shared_session.refresh(row)
    assert str(row.state) == approval_repo.PENDING
    assert row.decided_by_principal is None
    assert approval_repo.events(shared_session, ticket_id) == []
    assert "auto_pass" not in summary


@needs_pg
def test_signing_twice_does_not_fake_a_second_approval(shared_session) -> None:
    """第二次签 ⇒ `applied=False`，并且**不许多写一行** EN-10。

    多写一行就是伪造一次"批准发生过"。`apply_auto_pass` 只在 `rowcount == 1` 时写流水，
    这条判据钉的就是那个条件 —— 它很容易在"顺手补一行日志"时被改坏。
    """
    request = _request(shared_session)
    ticket_id = _ticket(shared_session, request.id)
    runner = _runner(shared_session)
    assert runner._auto_pass_split(request.id, ticket_id, {}) is True

    summary: dict[str, object] = {}
    assert runner._auto_pass_split(request.id, ticket_id, summary) is False
    assert summary["auto_pass"] != "applied"
    assert len(approval_repo.events(shared_session, ticket_id)) == 1


@needs_pg
def test_the_resume_is_skipped_and_said_so_when_no_resumer_is_wired(shared_session) -> None:
    """没注续跑者：如实留痕 `resume_skipped`，不抛。

    单据是 `APPROVED` 而没人接着跑 —— 这个状态必须能被读出来，不能让它看起来像已经跑下去了。
    """
    request = _request(shared_session)
    runner = _runner(shared_session, resumer=None)
    runner._resume_after_auto_pass(request.id)

    rows = (
        shared_session.query(EventLog)
        .filter(EventLog.entity_id == str(request.id))
        .all()
    )
    skipped = [row for row in rows if (row.payload or {}).get("action") == "resume_skipped"]
    assert len(skipped) == 1, [row.payload for row in rows]
    assert skipped[0].payload["reason"] == "resumer_not_configured"


@needs_pg
def test_the_resume_is_handed_the_request_and_an_approve_conclusion(shared_session) -> None:
    """注了续跑者：被调**恰好一次**，`conclusion="approve"`，参数是这张单的号。

    续跑者与「批准」那条路是同一个对象、同一个入口 —— 这里换个 `conclusion` 就会让
    `realrun` 走到"继续执行"那一支（它会跳过恢复段），而单据刚被签掉，恢复段正是要跑的那一段。
    """
    request = _request(shared_session)
    seen: list[dict[str, object]] = []
    runner = _runner(shared_session, resumer=lambda **kw: seen.append(kw))
    runner._resume_after_auto_pass(request.id)

    assert seen == [{"feature_request_id": request.id, "conclusion": "approve"}]


@needs_pg
def test_a_broken_resumer_does_not_take_the_intake_down(shared_session) -> None:
    """续跑者违约（抛了）时只留痕 —— 它按契约永不抛，但这里不赌。"""
    request = _request(shared_session)

    def boom(**_kw):
        raise RuntimeError("厂商连接被拒")

    runner = _runner(shared_session, resumer=boom)
    runner._resume_after_auto_pass(request.id)

    rows = shared_session.query(EventLog).filter(EventLog.entity_id == str(request.id)).all()
    failed = [row for row in rows if (row.payload or {}).get("action") == "resume_failed"]
    assert len(failed) == 1
    assert failed[0].payload["code"] == "RuntimeError"
