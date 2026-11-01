"""T056 + T064：文本通道 `python -m acp.approve` 的判据（FR-042 双通道｜R-26）。

**这档真连 PG**（缺 `ACP_DB_DSN` 整档 skip）。两件事只能在库里验：

1. **双通道打的是同一条 CAS**。看板先批、CLI 后到 ⇒ CLI 拿到 409 且库里不二次迁移，
   用 dict 假服务器永远验不出"两条通道共用一份状态"这件事。
2. **T064 的并发**：两个**真连接、真事务**同时 resolve 同一张单。
   `UPDATE ... WHERE version = :v` 的命中/未命中由 PG 的行锁给答案；
   单连接里"先 A 后 B"只是顺序调用，不叫并发，那条判据会恒绿。

CLI 侧的其余判据（命令词映射、必填补充参数、不许替用户挑单、退出码分档、不回显地址）
走注入的 `client`，不起真端口 —— 起真 uvicorn 会撞上 owner 在用的服务，那是纪律里明令不碰的。
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import datetime as dt
import io
import json
import os
import threading
import types
import uuid
from collections.abc import Callable

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from acp.api import approvals as api
from acp.approval import repository as repo
from acp.approval import service
from acp.approval.timeouts import KIND_BY_LETTER, parse_timeout_table
from acp.approve import CHANNEL, COMMANDS, CliUsage, REQUIRES, main, resolve_body
from acp.config.loader import DSN_ENV, db_url
from acp.persistence.enums import values_of
from acp.persistence.models_approval import ApprovalTicket, ResolutionEvent
from acp.persistence.models_core import FeatureRequest, TaskCard
from acp.persistence.models_registry import Snapshot

pytestmark = pytest.mark.skipif(
    not os.environ.get(DSN_ENV),
    reason=f"{DSN_ENV} 未注入 ⇒ 本文件需要 PG",
)

NOW = dt.datetime(2026, 10, 13, 18, 0, 0, tzinfo=dt.timezone.utc)
APPROVER = "owner-1"
TABLE = parse_timeout_table()


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
def scope(session):
    request_id = uuid.uuid4()
    session.add(
        FeatureRequest(
            id=request_id, project_key="demo", raw_input="做个介绍公司产品的静态网站",
            summary_digest={"自行补全项": [], "澄清问题": []}, clarification_waived=True,
            status="SPLIT", created_at=NOW,
        )
    )
    snapshot_id = uuid.uuid4()
    session.add(
        Snapshot(id=snapshot_id, round_no=1, phase="PRE_WORK", task_card_ids=["T01", "T02"],
                 ref=f"local:probe-{snapshot_id}", rolled_back_at=None)
    )
    session.flush()
    for card_id in ("T01", "T02"):
        session.add(
            TaskCard(
                id=card_id, feature_request_id=request_id, title=f"卡 {card_id}", difficulty=2,
                skill_tags=["html"], depends_on=[], artifact_floor_min_bytes=512,
                artifact_floor_min_lines=12, artifact_floor_format="html",
                acceptance_criteria=["首屏有标题"], handoff_count=0, review_round=0,
                status="PENDING_APPROVAL", snapshot_id=snapshot_id,
            )
        )
    session.flush()
    return types.SimpleNamespace(request_id=request_id, snapshot_id=snapshot_id)


def payload(letter: str) -> dict:
    return service.build_payload(
        kind=KIND_BY_LETTER[letter], pending_item=f"{letter} 类待批内容",
        context_digest="上下文摘要", suggested_verdict="approve",
        impact={"downstream_task_ids": ["T01"], "estimated_extra_usage": {}},
    )


def make_ticket(session, scope, letter="A", card_id=None, ticket_id=None) -> uuid.UUID:
    created = repo.create_or_merge(
        session, kind=KIND_BY_LETTER[letter], payload=payload(letter), risk_exempt_eligible=False,
        timeout_policy=TABLE[KIND_BY_LETTER[letter]], task_card_id=card_id,
        feature_request_id=scope.request_id, now=NOW, ticket_id=ticket_id,
    )
    session.flush()
    return created.ticket_id


def client_for(session) -> TestClient:
    app = FastAPI()
    app.include_router(api.router)
    app.state.session_factory = lambda: session
    app.state.approvers = frozenset({APPROVER})
    return TestClient(app)


def run_cli(client, *argv, principal=APPROVER) -> tuple[int, dict | None, str]:
    """跑一次 CLI，返回 `(退出码, 那一行 JSON, 原始 stdout)`。"""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = main([*argv, "--principal", principal], client=client)
    out = buffer.getvalue()
    line = out.strip().splitlines()[-1] if out.strip() else ""
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError:
        parsed = None
    return code, parsed, out


# ---------------------------------------------------------------- 命令词映射（纯函数面）

def test_the_three_command_words_map_to_the_domain_verdicts_and_channel_is_cli():
    assert COMMANDS == {"approve": "approve", "reject": "reject", "needinfo": "need_info"}
    assert REQUIRES == {"reject": "reason", "needinfo": "ask"}
    # CLI 硬编码的那个通道名 MUST 是 EN-10 域里的值：写错一个字母就是一次"看着跑通了"的脏写。
    assert CHANNEL in set(values_of("resolution_channel")), f"{CHANNEL} 不在域里"
    body = resolve_body("approve", "", "", 3)
    assert (body["verdict"], body["channel"], body["version"]) == ("approve", CHANNEL, 3)
    # 前导斜杠是契约 §1 的写法（`/approve T01`），大小写两种都要落到同一个域值上。
    for spelling in ("/approve", "APPROVE"):
        assert resolve_body(spelling, "", "", 0)["verdict"] == "approve"


def test_reject_without_a_reason_and_needinfo_without_an_ask_are_usage_errors():
    """FR-040 的两条"要留字"：缺补充参数就在命令行上拒，不许发一个空 reason 去污染流水。"""
    with pytest.raises(CliUsage, match="--reason"):
        resolve_body("reject", "", "", 0)
    with pytest.raises(CliUsage, match="--ask"):
        resolve_body("needinfo", "", "", 0)
    assert resolve_body("reject", "理由", "", 0)["reason_or_ask"] == "理由"
    assert resolve_body("needinfo", "", "追问", 0)["reason_or_ask"] == "追问"


def test_an_unknown_command_is_refused_before_any_request_leaves_the_client():
    with pytest.raises(CliUsage, match="不认识的动作"):
        resolve_body("force", "", "", 0)


# ---------------------------------------------------------------- 端到端：CLI 打真端点

def test_cli_approves_by_ticket_id_and_the_database_moves(session, scope):
    ticket_id = make_ticket(session, scope, "A")
    code, report, _ = run_cli(client_for(session), "approve", str(ticket_id))
    assert code == 0, report
    assert (report["applied"], report["state"], report["http"]) == (True, "APPROVED", 200)
    row = session.get(ApprovalTicket, ticket_id)
    session.expire(row)
    assert (str(row.state), row.version, row.decided_by_principal) == ("APPROVED", 1, APPROVER)


def test_cli_approves_by_card_id_reading_the_version_from_the_server(session, scope):
    """卡号写法（契约 §1 示例 `/approve T01`）：CLI 先按卡号找到那张未决单，再带上它的版本去撞 CAS。"""
    ticket_id = make_ticket(session, scope, "D", card_id="T01")
    code, report, _ = run_cli(client_for(session), "approve", "T01")
    assert code == 0, report
    assert report["ticket_id"] == str(ticket_id)
    assert report["applied"] is True and report["state"] == "APPROVED"


def test_cli_rejects_with_a_reason_and_the_reason_lands_in_the_ledger(session, scope):
    ticket_id = make_ticket(session, scope, "D", card_id="T01")
    code, report, _ = run_cli(client_for(session), "reject", "T01", "--reason", "会动生产库")
    assert code == 0 and report["state"] == "REJECTED"
    events = repo.events(session, ticket_id)
    assert [(str(e.verdict), e.reason_text, e.applied) for e in events] == [("reject", "会动生产库", True)]


def test_a_card_with_no_pending_ticket_is_refused_and_no_other_ticket_is_picked(session, scope):
    """越权形状：T02 上有一张待批单，用户对 T01 敲 `/approve` ⇒ CLI 不许"挑一张"替用户批。"""
    other = make_ticket(session, scope, "D", card_id="T02")
    code, report, out = run_cli(client_for(session), "approve", "T01")
    assert code == 2 and report is None
    assert "没有未决审批单" in out
    assert str(session.get(ApprovalTicket, other).state) == "PENDING", "拒了却还是把别的单批了 = 越权"


def test_two_pending_tickets_on_one_card_make_the_cli_refuse_to_guess(session, scope):
    """FR-045 的读侧防呆：索引只管同 (卡, 类)，两张**不同类**的未决单可以同时挂在一张卡上（库侧残余），
    那种形状下 CLI 必须报错，不许挑优先级高的那张。"""
    make_ticket(session, scope, "D", card_id="T01")
    with session.begin_nested():
        session.add(
            ApprovalTicket(
                id=uuid.uuid4(), task_card_id="T01", feature_request_id=scope.request_id,
                kind=KIND_BY_LETTER["B"], state="PENDING", payload=payload("B"),
                risk_exempt_eligible=False, version=0, timeout_at=NOW + dt.timedelta(hours=2),
            )
        )
    session.flush()
    code, _, out = run_cli(client_for(session), "approve", "T01")
    assert code == 2
    assert "同一任务同时只允许一个未决卡点" in out or "2 张未决单" in out


def test_a_late_cli_conclusion_after_a_dashboard_one_is_logged_as_duplicate(session, scope):
    """T064 的顺序面：看板先批 ⇒ CLI 后到拿 409、`logged_as=duplicate`、库里不二次迁移。"""
    ticket_id = make_ticket(session, scope, "A")
    client = client_for(session)
    dashboard = client.post(
        f"/approvals/{ticket_id}/resolve",
        json={"verdict": "approve", "version": 0, "channel": "DASHBOARD", "reason_or_ask": ""},
        headers={api.PRINCIPAL_HEADER: APPROVER},
    )
    assert dashboard.status_code == 200

    code, report, _ = run_cli(client, "reject", str(ticket_id), "--reason", "晚到的驳回")
    assert code == 1
    assert (report["applied"], report["state"], report["logged_as"], report["code"]) == (
        False, "APPROVED", "duplicate", "already_decided"
    )
    row = session.get(ApprovalTicket, ticket_id)
    session.expire(row)
    assert (str(row.state), row.version, row.decided_by_principal) == ("APPROVED", 1, APPROVER)


def test_a_stale_version_from_the_operator_is_reported_not_guessed(session, scope):
    """`--version` 是"我就是要拿这个号去撞"的用法：CLI 不许偷偷改成服务端当前版本。"""
    ticket_id = make_ticket(session, scope, "A")
    client = client_for(session)
    client.post(
        f"/approvals/{ticket_id}/resolve",
        json={"verdict": "need_info", "version": 0, "channel": "DASHBOARD",
              "reason_or_ask": "先问一句"},
        headers={api.PRINCIPAL_HEADER: APPROVER},
    )
    # 追问不动版本，所以显式抬一次版本号，把 stale 的形状造出来（与 ticket 档同一手法）。
    session.execute(text("update approval_ticket set version = 9 where id=:i"), {"i": ticket_id})
    session.flush()
    code, report, _ = run_cli(client, "approve", str(ticket_id), "--version", "0")
    assert code == 1
    assert (report["applied"], report["code"], report["logged_as"]) == (False, "stale_version", None)


def test_a_non_approver_principal_exits_1_and_writes_nothing(session, scope):
    ticket_id = make_ticket(session, scope, "A")
    code, report, _ = run_cli(client_for(session), "approve", str(ticket_id), principal="someone-else")
    assert code == 1
    assert (report["applied"], report["code"], report["http"]) == (False, "principal_not_allowed", 403)
    row = session.get(ApprovalTicket, ticket_id)
    assert str(row.state) == "PENDING" and row.version == 0
    assert repo.events(session, ticket_id) == []


def test_needinfo_keeps_the_ticket_pending_and_the_ask_in_the_ledger(session, scope):
    ticket_id = make_ticket(session, scope, "A")
    code, report, _ = run_cli(client_for(session), "needinfo", str(ticket_id), "--ask", "用邮箱还是表单？")
    assert code == 0
    assert (report["applied"], report["state"], report["logged_as"]) == (True, "PENDING", None)
    assert [e.reason_text for e in repo.events(session, ticket_id)] == ["用邮箱还是表单？"]


# ---------------------------------------------------------------- 退出码与不回显

def test_no_base_url_and_no_injected_client_is_a_usage_error_that_names_no_address():
    """本模块不猜后端地址：缺 `--base-url` ⇒ 退出 2，且那一行里不许出现任何 URL 形状。"""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = main(["approve", str(uuid.uuid4()), "--principal", APPROVER])
    out = buffer.getvalue()
    assert code == 2
    assert "--base-url" in out
    assert "http" not in out.lower(), f"错误行里出现了地址：{out}"


class _BadTransport(httpx.BaseTransport):
    """"这次请求会怎么坏"由用例给定 ⇒ 本机网络栈不再参与判据（owner 2026-10-14 裁决 P2-4）。

    为什么必须有这一层：原写法靠 `http://leak-probe.invalid:1` **真的连不上**来触发
    `httpx.TransportError`，而 WorkBuddy 的 Phase 6 审查实测到 —— 沙箱里设了
    `HTTP_PROXY / HTTPS_PROXY`，那条"注定失败"的请求被代理接走并回了 502 ⇒
    同一条判据在带代理的环境红、在旁路环境绿（他那一趟 `4 failed / 989 passed` 的第 4 条就是这个）。
    判据的绿/红由**运行环境的代理**决定，而不是由代码性质决定，这是环境耦合不是缺陷。
    换成注入式坏传输后，异常类型、异常正文、以及"正文里带不带地址"全部由用例控制。
    """

    def __init__(self, make_error: Callable[[httpx.Request], Exception]) -> None:
        self._make_error = make_error

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        raise self._make_error(request)


def _client_that_fails(make_error: Callable[[httpx.Request], Exception]) -> httpx.Client:
    """`base_url` 里故意留一个可识别的主机名 —— 它同时是"不回显"那条断言的探针。"""
    return httpx.Client(base_url="http://leak-probe.invalid:1", transport=_BadTransport(make_error))


def test_a_transport_failure_exits_3_and_echoes_neither_host_nor_scheme():
    """后端连不上时只报异常类名。httpx 的 ConnectError 正文里带完整 URL（可能含 userinfo），
    裸抛就是把它抄进终端；本例把这种正文**原样造出来**，所以"不回显"是有牙的断言。

    **owner 2026-10-14 裁决 P2-1（不断具体子类）**：原先写死 `ConnectError`，而"这次连不上是
    DNS 失败还是拒连"由本机网络栈决定；判据改成"打印出来的类名必须是 `httpx.TransportError` 的子类"。
    **owner 2026-10-14 裁决 P2-4（不押网络栈）**：坏法改为注入，见 `_BadTransport` 那段。

    下面那条反向（同一条 CLI、换一个**不属于** `TransportError` 那一支的 httpx 异常）钉的是
    退出码 3 与退出码 2 的分界 —— 少了它，把两个 `except` 顺序写反也不会红。
    """
    probe_body = "ConnectTimeout: failed establishing a connection to 'http://leak-probe.invalid:1'"

    def connect_error(request: httpx.Request) -> Exception:
        return httpx.ConnectError(probe_body, request=request)

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = main(
            ["approve", str(uuid.uuid4()), "--principal", APPROVER],
            client=_client_that_fails(connect_error),
        )
    out = buffer.getvalue()
    assert code == 3, out
    assert "transport error" in out
    line = next(entry for entry in out.splitlines() if entry.startswith("transport error:"))
    reported = line.split("（", 1)[0].split(":", 1)[1].strip()
    assert reported in dir(httpx), f"打印的类名 {reported!r} 不是 httpx 的异常类 ⇒ 那一行不是从异常对象取的"
    assert issubclass(getattr(httpx, reported), httpx.TransportError), (
        f"打印的是 {reported!r}，不属于 httpx.TransportError 那一支 ⇒ 退出码 3 与退出码 2 的分支混了"
    )
    assert "leak-probe" not in out, f"主机名被回显了：{out}"
    assert "://" not in out, f"地址形状出现在输出里：{out}"


def test_a_non_transport_http_error_exits_2_and_still_echoes_neither_host_nor_scheme():
    """上一条的反向：`HTTPStatusError` **不属于** `TransportError` ⇒ 走退出码 2 那一支。

    两支在源码里是相邻的两个 `except`，写反顺序（`HTTPError` 在前）会把连接失败也读成 2，
    而"3 = 连不上、2 = 用法/协议之外的调用错"是 `contracts/approval-api.md` 给终端用户的分档承诺。
    """
    def status_error(request: httpx.Request) -> Exception:
        return httpx.HTTPStatusError(
            "Server error 'http://leak-probe.invalid:1' 500 Internal Server Error",
            request=request,
            response=httpx.Response(500, request=request),
        )

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = main(
            ["approve", str(uuid.uuid4()), "--principal", APPROVER],
            client=_client_that_fails(status_error),
        )
    out = buffer.getvalue()
    assert code == 2, out
    line = next(entry for entry in out.splitlines() if entry.startswith("transport error:"))
    reported = line.split("（", 1)[0].split(":", 1)[1].strip()
    assert not issubclass(getattr(httpx, reported), httpx.TransportError), reported
    assert "leak-probe" not in out and "://" not in out, out


def test_missing_principal_is_an_argparse_error_with_exit_2(session, scope):
    with pytest.raises(SystemExit) as excinfo:
        main(["approve", str(uuid.uuid4())])
    assert excinfo.value.code == 2


def test_a_missing_store_surfaces_as_503_not_a_silent_zero(session, scope):
    """503 走的是错误分支：CLI 不许把"后端没配存储"读成"没有待批单"然后安静退出 0。"""
    ticket_id = make_ticket(session, scope, "A")
    app = FastAPI()
    app.include_router(api.router)
    app.state.approvers = frozenset({APPROVER})
    code, report, _ = run_cli(TestClient(app), "approve", str(ticket_id))
    assert code == 1
    assert report["http"] == 503


# ---------------------------------------------------------------- T064：真并发

def test_two_concurrent_connections_on_one_ticket_produce_exactly_one_effect(engine):
    """T064 的核心判据：**两个真连接、真事务**同时 resolve 同一张单。

    这一条为什么不能在单连接里跑：`UPDATE ... WHERE version = 0` 第二次未命中，
    在同一个事务里只说明"我自己把版本号推前了"，那是顺序效应不是并发；只有两条独立事务
    才会让 PG 用**行锁**把它们串起来，落后那条真实地看到 `version=1` 后 CAS 落空。

    本用例**会提交**（并发要互相看得见就必须提交），所以结尾显式删掉这张单、它的流水与需求单。
    """
    ticket_id = uuid.uuid4()
    request_id = uuid.uuid4()
    admin = Session(engine)
    try:
        admin.add(
            FeatureRequest(
                id=request_id, project_key="demo", raw_input="并发用例专用",
                summary_digest={"自行补全项": [], "澄清问题": []}, clarification_waived=True,
                status="SPLIT", created_at=NOW,
            )
        )
        admin.flush()
        repo.create_or_merge(
            admin, kind=KIND_BY_LETTER["A"], payload=payload("A"), risk_exempt_eligible=False,
            timeout_policy=TABLE[KIND_BY_LETTER["A"]], feature_request_id=request_id,
            now=NOW, ticket_id=ticket_id,
        )
        admin.commit()

        gate = threading.Barrier(2)
        results: list[tuple[str, bool, str | None, str]] = []

        def submit(channel: str, principal: str) -> None:
            with engine.connect() as conn:
                thread_session = Session(bind=conn)
                try:
                    gate.wait(15)
                    outcome = repo.resolve(
                        thread_session, ticket_id=ticket_id, verdict="approve", version=0,
                        channel=channel, principal=principal,
                        approvers=frozenset({principal, "other-owner"}),
                    )
                    thread_session.commit()
                    results.append((channel, outcome.applied, outcome.code, str(outcome.state)))
                finally:
                    thread_session.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(submit, repo.DASHBOARD, "owner-1"),
                       pool.submit(submit, repo.CLI, "owner-2")]
            concurrent.futures.wait(futures)
            for future in futures:
                future.result()  # 线程里抛了异常就让它冒出来，不许退化成"results 少了一条"

        assert len(results) == 2, f"两条线程没都跑完：{results}"
        applied = [row for row in results if row[1]]
        refused = [row for row in results if not row[1]]
        assert (len(applied), len(refused)) == (1, 1), f"或签并发不是「恰好一份生效」：{results}"
        assert refused[0][2] in {"already_decided", "stale_version"}, (
            f"败者没有可解释的错误码：{refused}"
        )

        final = admin.execute(
            select(ApprovalTicket.state, ApprovalTicket.version, ApprovalTicket.decided_by_principal)
            .where(ApprovalTicket.id == ticket_id)
        ).one()
        assert (str(final[0]), final[1]) == ("APPROVED", 1), f"并发把版本推了两次：{final}"
        assert final[2] == ("owner-1" if applied[0][0] == repo.DASHBOARD else "owner-2"), (
            f"署名与生效那条通道不符：{final} / {applied}"
        )

        ledger = admin.execute(
            select(ResolutionEvent.applied, ResolutionEvent.channel)
            .where(ResolutionEvent.approval_ticket_id == ticket_id)
        ).all()
        assert len(ledger) == 2, f"两条到达都该有流水，实到 {len(ledger)} 行"
        assert [(bool(row[0]), str(row[1])) for row in ledger].count((True, applied[0][0])) == 1, (
            f"生效行的通道不是赢家那条：{ledger} / {applied}"
        )
        assert [(bool(row[0]), str(row[1])) for row in ledger].count((False, refused[0][0])) == 1, (
            f"败者没留下 duplicate 行：{ledger}"
        )
    finally:
        admin.execute(
            text("delete from resolution_event where approval_ticket_id = :i"), {"i": ticket_id}
        )
        admin.execute(text("delete from approval_ticket where id = :i"), {"i": ticket_id})
        admin.execute(text("delete from feature_request where id = :i"), {"i": request_id})
        admin.commit()
        admin.close()
