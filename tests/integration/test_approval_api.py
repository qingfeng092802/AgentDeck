"""T055 的 HTTP 面判据：审批 REST 端点（FR-042｜R-26，`contracts/approval-api.md`）。

**本文件真连 PG**（缺 `ACP_DB_DSN` 时整档 skip，口径同 `test_approval_ticket.py`）：
端点只是翻译层，状态码背后的"这一份到底生不生效"是数据库给的 CAS 答案，
拿内存替身跑就会把"应用层以为自己拦住了"当成"接口拦住了"。

四条刻意的写法：
1. **"内部端点不对外"是读 OpenAPI 路径集断的**，不是读注释、也不是读文件名。
   同一条用例里**同时**断 `internal_router` 里确实存在这一条 —— 少了这半边，
   "不在公开 app 里"会因为"压根没实现"而恒绿（空对空）。
2. **`resumed_from` 两个分支都跑**：没注入 resumer ⇒ `null`；注入了 ⇒ 节点名 **且** resumer
   真被调到了、参数是那一张单的需求单号与结论。只断响应字段不断调用，等于允许"回了个漂亮 JSON
   但图还挂着"。
3. **审批者集合缺失 ⇒ 503** 与 **配上之后 200** 成对写：前者证明门是关的，后者证明门不是焊死的
   （纪律 2 要求那条反向对照保持绿）。
4. **每一次 HTTP 写之后都回库里读**，不看响应体自述：响应说 `applied=true` 而库里 `version` 没动，
   才是真正的缺陷形态。**但本文件的"回库"读的是同一条连接**（夹具靠外层事务回滚做隔离）——
   它量的是"仓储与端点有没有把这一笔写进会话"，**量不到"这一笔有没有发到 PostgreSQL"**：
   绑定在已 begin 的连接上时，`commit()` 发不到库、`rollback()` 也轮不到 PG。实锤落地的是
   `test_approval_commit_boundary.py`（第二条连接读 + `FOR UPDATE NOWAIT`），两个文件各管一段，
   别把这里读成"落库已验"。（这句不是免责声明：M2-W11 那个缺陷正是本文件整档全绿着过去的。）
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import threading
import types
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from acp.api import approvals as api
from acp.approval import repository as repo
from acp.approval import service
from acp.approval.timeouts import KIND_BY_LETTER, TimeoutPolicy, parse_timeout_table
from acp.config.loader import DSN_ENV, db_url
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
HEADERS = {api.PRINCIPAL_HEADER: APPROVER}


def policy(letter: str) -> TimeoutPolicy:
    return TABLE[KIND_BY_LETTER[letter]]


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
    """一张需求单 + 一条 snapshot + 三张卡（T01/T02/T03），够建 A/B/C/D 四类各自的单。"""
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
        Snapshot(id=snapshot_id, round_no=1, phase="PRE_WORK", task_card_ids=["T01", "T02", "T03"],
                 ref=f"local:probe-{snapshot_id}", rolled_back_at=None)
    )
    session.flush()
    for card_id in ("T01", "T02", "T03"):
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


def make_ticket(session, scope, letter: str = "A", card_id: str | None = None) -> uuid.UUID:
    created = repo.create_or_merge(
        session, kind=KIND_BY_LETTER[letter], payload=payload(letter), risk_exempt_eligible=False,
        timeout_policy=policy(letter), task_card_id=card_id,
        feature_request_id=scope.request_id, now=NOW,
    )
    session.flush()
    return created.ticket_id


def payload(letter: str) -> dict:
    return service.build_payload(
        kind=KIND_BY_LETTER[letter],
        pending_item=f"{letter} 类待批内容",
        context_digest="上下文摘要",
        suggested_verdict="approve",
        impact={"downstream_task_ids": ["T01"], "estimated_extra_usage": {}},
    )


def app_for(session, *, approvers=APPROVER, resumer=None, with_store=True, internal=False) -> TestClient:
    app = FastAPI()
    app.include_router(api.router)
    if internal:
        app.include_router(api.internal_router)
    if with_store:
        # 每次"请求"在**同一条连接**上新建一个会话，而不是把夹具那个会话本身交出去。
        # 理由是端点现在一定会 `close()` 它借到的会话（`api/approvals.py::_store`），而实测
        # SQLAlchemy 2.1.4 下 `close()` 会把那个会话与外层事务解绑 —— 交出去的话，第一发请求之后
        # 本文件的回读判据就读不到自己前一步建的单据了。同连接上的新会话照样读得到未提交的行，
        # 而隔离仍由夹具结束时那一次外层 `rollback()` 提供（`commit()` 在这里发不到 PostgreSQL）。
        app.state.session_factory = lambda: Session(bind=session.get_bind())
    if approvers is not None:
        app.state.approvers = frozenset({approvers}) if isinstance(approvers, str) else approvers
    if resumer is not None:
        app.state.graph_resumer = resumer
    return TestClient(app)


def resolve_body(version: int = 0, verdict: str = "approve", channel: str = "DASHBOARD", **kw) -> dict:
    return {"verdict": verdict, "version": version, "channel": channel, **kw}


# ---------------------------------------------------------------- 作用域：内部端点不对外

def test_the_public_app_has_no_create_endpoint_but_the_internal_router_does():
    """FR-042 的"建单端点仅内部"：读公开 app 的 OpenAPI 路径集，并同时证明那条路由真的存在。"""
    public = FastAPI()
    public.include_router(api.router)
    paths = {
        (method.upper(), path)
        for path, ops in TestClient(public).get("/openapi.json").json()["paths"].items()
        for method in ops
    }
    assert ("POST", "/approvals") not in paths, f"对外 app 里出现了建单端点：{sorted(paths)}"
    assert {("GET", "/approvals"), ("GET", "/approvals/{ticket_id}"),
            ("GET", "/approvals/{ticket_id}/events"), ("POST", "/approvals/{ticket_id}/resolve")} <= paths

    internal = FastAPI()
    internal.include_router(api.internal_router)
    internal_paths = {
        (method.upper(), path)
        for path, ops in TestClient(internal).get("/openapi.json").json()["paths"].items()
        for method in ops
    }
    assert ("POST", "/approvals") in internal_paths, (
        "internal_router 里根本没有建单端点 ⇒ 上面那条「不在公开 app 里」是空对空"
    )


def test_the_two_routers_are_distinct_objects_so_assembly_cannot_accidentally_merge_them():
    """`router is internal_router` 必须为假：一旦有人图省事把它们合成一个，上一条用例就失去意义。"""
    assert api.router is not api.internal_router


# ---------------------------------------------------------------- 列单与详情

def test_listing_returns_tickets_in_fr044_priority_order(session, scope):
    """C/D 同档且都"无超时" ⇒ 二者之间的次序由 `str(id)` 定序，那是 UUID，不许写进期望值里当精确。
    所以这里断的是**关系**：priority 非降、前两名恰是 C/D、后两名恰是 B 然后 A。"""
    for letter, card_id in (("A", None), ("B", "T02"), ("C", "T03"), ("D", "T01")):
        make_ticket(session, scope, letter, card_id)
    body = app_for(session).get("/approvals").json()
    assert len(body) == 4
    assert [row["priority"] for row in body] == [0, 0, 1, 2]
    kinds = [row["kind"] for row in body]
    assert set(kinds[:2]) == {"C_BUDGET", "D_DANGER"}, f"最高档不是 C/D：{kinds}"
    assert kinds[2:] == ["B_ACCEPTANCE", "A_SPLIT"], f"2/3 名次序错：{kinds}"
    # 裁决 ②：C/D 是"无超时" ⇒ 响应里是 `null`，不是某个极远的时刻字符串。
    assert [row["timeout_at"] for row in body[:2]] == [None, None]


def test_listing_an_unknown_state_is_422_and_a_known_one_is_200(session, scope):
    """422 那一支证明状态参数是按域校验的；200 那一支是它的反向对照（别把域校验写成焊死）。"""
    ticket_id = make_ticket(session, scope, "A")
    client = app_for(session)
    assert client.get("/approvals", params={"state": "PENDING"}).status_code == 200
    bad = client.get("/approvals", params={"state": "ALMOST_DONE"})
    assert bad.status_code == 422
    assert "approval_state" in bad.json()["detail"]
    assert client.get("/approvals", params={"state": "APPROVED"}).json() == []
    assert [row["ticket_id"] for row in client.get("/approvals", params={"state": "PENDING"}).json()] == [
        str(ticket_id)
    ]


def test_detail_carries_the_five_elements_and_the_event_count(session, scope):
    ticket_id = make_ticket(session, scope, "A")
    client = app_for(session)
    resolve_result = client.post(
        f"/approvals/{ticket_id}/resolve", json=resolve_body(), headers=HEADERS
    )
    assert resolve_result.status_code == 200
    detail = client.get(f"/approvals/{ticket_id}").json()
    assert set(detail["payload"]) == set(service._FIVE), "五要素少一项，审批者点的就不是那张单"
    assert detail["state"] == "APPROVED" and detail["version"] == 1
    assert detail["decided_by_principal"] == APPROVER
    assert detail["event_count"] == 1


def test_an_unknown_ticket_is_404_on_all_three_read_endpoints(session):
    missing = str(uuid.uuid4())
    client = app_for(session)
    assert client.get(f"/approvals/{missing}").status_code == 404
    assert client.get(f"/approvals/{missing}/events").status_code == 404
    assert client.post(
        f"/approvals/{missing}/resolve", json=resolve_body(), headers=HEADERS
    ).status_code == 404


# ---------------------------------------------------------------- 或签走 HTTP

def test_two_channels_over_http_the_first_conclusion_wins(session, scope):
    """FR-040 的 HTTP 版：看板先批、CLI 后驳回 ⇒ 409 + `logged_as=duplicate` + 库里仍是 APPROVED。"""
    ticket_id = make_ticket(session, scope, "A")
    client = app_for(session)
    first = client.post(f"/approvals/{ticket_id}/resolve", json=resolve_body(0), headers=HEADERS)
    assert first.status_code == 200
    assert (first.json()["applied"], first.json()["state"], first.json()["version"]) == (
        True, "APPROVED", 1
    )

    second = client.post(
        f"/approvals/{ticket_id}/resolve",
        json=resolve_body(0, "reject", "CLI", reason_or_ask="晚到的驳回"),
        headers=HEADERS,
    )
    assert second.status_code == 409
    detail = second.json()["detail"]
    assert (detail["applied"], detail["code"], detail["logged_as"], detail["state"]) == (
        False, "already_decided", "duplicate", "APPROVED"
    )

    row = session.get(ApprovalTicket, ticket_id)
    session.expire(row)
    assert (str(row.state), row.version) == ("APPROVED", 1), "响应说没生效，库里也得真的没生效"
    events = client.get(f"/approvals/{ticket_id}/events").json()
    assert [(e["applied"], e["verdict"], e["channel"]) for e in events] == [
        (True, "approve", "DASHBOARD"),
        (False, "reject", "CLI"),
    ]


def test_a_wrong_version_is_409_stale_version_and_leaves_the_ticket_pending(session, scope):
    ticket_id = make_ticket(session, scope, "A")
    session.execute(text("update approval_ticket set version = 5 where id=:i"), {"i": ticket_id})
    session.flush()
    result = app_for(session).post(
        f"/approvals/{ticket_id}/resolve", json=resolve_body(0), headers=HEADERS
    )
    assert result.status_code == 409
    assert result.json()["detail"]["code"] == "stale_version"
    row = session.get(ApprovalTicket, ticket_id)
    session.expire(row)
    assert (str(row.state), row.version) == ("PENDING", 5)


def test_need_info_keeps_the_ticket_pending_over_http(session, scope):
    """追问：200 + `applied=true` + 状态仍是 PENDING，且随后批准仍然生效（CAS 锁没被追问顶住）。"""
    ticket_id = make_ticket(session, scope, "A")
    client = app_for(session)
    asked = client.post(
        f"/approvals/{ticket_id}/resolve",
        json=resolve_body(0, "need_info", "DASHBOARD", reason_or_ask="联系方式用邮箱还是表单？"),
        headers=HEADERS,
    )
    body = asked.json()
    assert (asked.status_code, body["applied"], body["state"], body["version"]) == (
        200, True, "PENDING", 0
    )
    assert client.post(
        f"/approvals/{ticket_id}/resolve", json=resolve_body(0), headers=HEADERS
    ).json()["state"] == "APPROVED"


def test_an_out_of_domain_channel_is_422_before_any_db_touch(session, scope):
    ticket_id = make_ticket(session, scope, "A")
    result = app_for(session).post(
        f"/approvals/{ticket_id}/resolve",
        json=resolve_body(0, "approve", "EMAIL"),
        headers=HEADERS,
    )
    assert result.status_code == 422
    assert "EMAIL" in result.json()["detail"], "422 该点名被拒的那个通道"
    assert "AUTO 由规则表免确认独占" in result.json()["detail"]
    assert str(session.get(ApprovalTicket, ticket_id).state) == "PENDING"


def test_an_unknown_field_in_the_resolve_body_is_rejected(session, scope):
    """契约里的 body 是 `extra=forbid`：多一个 `admin_override` 就该 422，而不是被静丢掉。"""
    ticket_id = make_ticket(session, scope, "A")
    result = app_for(session).post(
        f"/approvals/{ticket_id}/resolve",
        json={**resolve_body(), "admin_override": True},
        headers=HEADERS,
    )
    assert result.status_code == 422


# ---------------------------------------------------------------- 身份与缺配置

def test_a_principal_outside_the_approver_set_gets_403(session, scope):
    ticket_id = make_ticket(session, scope, "A")
    result = app_for(session).post(
        f"/approvals/{ticket_id}/resolve", json=resolve_body(),
        headers={api.PRINCIPAL_HEADER: "someone-else"},
    )
    assert result.status_code == 403
    assert result.json()["detail"]["code"] == "principal_not_allowed"


def test_a_request_without_the_principal_header_gets_403(session, scope):
    """MVP-1 没有认证：这一条挡的是"忘了带身份"，挡不住冒充（认证归 M2，已在文档里点名）。"""
    ticket_id = make_ticket(session, scope, "A")
    result = app_for(session).post(f"/approvals/{ticket_id}/resolve", json=resolve_body())
    assert result.status_code == 403
    assert api.PRINCIPAL_HEADER in result.json()["detail"]


def test_missing_approvers_denies_everyone_and_configuring_one_lets_it_through(session, scope):
    """503 fail-closed 与 200 成对：前者证明漏配置不会变成敞开的门，后者证明门不是焊死的。"""
    ticket_id = make_ticket(session, scope, "A")
    closed = app_for(session, approvers=None).post(
        f"/approvals/{ticket_id}/resolve", json=resolve_body(), headers=HEADERS
    )
    assert closed.status_code == 503
    assert "approvers_not_configured" in closed.json()["detail"]
    assert str(session.get(ApprovalTicket, ticket_id).state) == "PENDING"

    opened = app_for(session, approvers=APPROVER).post(
        f"/approvals/{ticket_id}/resolve", json=resolve_body(), headers=HEADERS
    )
    assert opened.status_code == 200, "反向对照必须是绿的：503 不许是把所有解析都焊死了"


def test_a_missing_store_is_503_not_an_empty_list(session, scope):
    make_ticket(session, scope, "A")
    assert app_for(session, with_store=False).get("/approvals").status_code == 503


# ---------------------------------------------------------------- resumed_from

def test_resumed_from_is_null_when_no_resumer_is_injected(session, scope):
    """装配点归 T123。没注入时**必须**报 null —— 写死节点名就是拿响应体冒充一条不存在的执行路径。"""
    ticket_id = make_ticket(session, scope, "A")
    body = app_for(session).post(
        f"/approvals/{ticket_id}/resolve", json=resolve_body(), headers=HEADERS
    ).json()
    assert body["applied"] is True and body["resumed_from"] is None


def test_an_injected_resumer_is_called_with_the_ticket_scope_and_returns_the_node_name(session, scope):
    calls = []
    ticket_id = make_ticket(session, scope, "A")
    body = app_for(session, resumer=lambda **kw: calls.append(kw)).post(
        f"/approvals/{ticket_id}/resolve", json=resolve_body(), headers=HEADERS
    ).json()
    assert body["resumed_from"] == api.RESUMED_FROM
    assert len(calls) == 1, f"续跑动作只该发生一次，实到 {calls}"
    assert calls[0] == {"feature_request_id": scope.request_id, "conclusion": "approve"}


def test_a_rejected_conclusion_resumes_with_reject_and_a_need_info_resumes_with_nothing(session, scope):
    ticket_id = make_ticket(session, scope, "A")
    calls = []
    client = app_for(session, resumer=lambda **kw: calls.append(kw))
    client.post(
        f"/approvals/{ticket_id}/resolve", json=resolve_body(0, "need_info"), headers=HEADERS
    )
    assert calls == [], "追问不结束待决 ⇒ 图不许被唤醒"
    rejected = client.post(
        f"/approvals/{ticket_id}/resolve", json=resolve_body(0, "reject"), headers=HEADERS
    )
    assert rejected.json()["resumed_from"] == api.RESUMED_FROM
    assert calls == [{"feature_request_id": scope.request_id, "conclusion": "reject"}]


def test_resumed_from_stays_null_when_the_conclusion_did_not_apply(session, scope):
    """CAS 没命中时不许报"已续跑"：那是把一次被拒的写入说成一条执行过的路径。"""
    ticket_id = make_ticket(session, scope, "A")
    calls = []
    client = app_for(session, resumer=lambda **kw: calls.append(kw))
    client.post(f"/approvals/{ticket_id}/resolve", json=resolve_body(0), headers=HEADERS)
    calls.clear()
    second = client.post(
        f"/approvals/{ticket_id}/resolve", json=resolve_body(0, "reject", "CLI"), headers=HEADERS
    )
    assert second.status_code == 409
    assert "resumed_from" not in second.json()["detail"], "被拒的结论不许带一个「已续跑」字段"
    assert calls == []


# ---------------------------------------------------------------- 内部建单端点

def test_the_internal_create_endpoint_accepts_a_legal_a_split_ticket(session, scope):
    client = app_for(session, internal=True)
    result = client.post(
        "/approvals",
        json={"kind": KIND_BY_LETTER["A"], "feature_request_id": str(scope.request_id),
              "payload": payload("A")},
    )
    assert result.status_code == 201, result.text
    assert result.json()["kind"] == "A_SPLIT" and result.json()["state"] == "PENDING"
    assert session.scalar(text("select count(*) from approval_ticket")) == 1


def test_the_internal_create_endpoint_refuses_a_missing_five_element_with_400(session, scope):
    incomplete = payload("A")
    del incomplete["impact"]
    result = app_for(session, internal=True).post(
        "/approvals",
        json={"kind": KIND_BY_LETTER["A"], "feature_request_id": str(scope.request_id),
              "payload": incomplete},
    )
    assert result.status_code == 400
    assert "payload_incomplete" in result.json()["detail"]
    assert session.scalar(text("select count(*) from approval_ticket")) == 0, (
        "五要素不齐的单不许留下半行"
    )


def test_the_internal_create_endpoint_refuses_a_kind_not_enabled_in_mvp1(session, scope):
    result = app_for(session, internal=True).post(
        "/approvals",
        json={"kind": KIND_BY_LETTER["C"], "feature_request_id": str(scope.request_id),
              "payload": payload("C")},
    )
    assert result.status_code == 422
    detail = result.json()["detail"]
    assert "C_BUDGET" in detail and "未启用" in detail, f"422 说的是别的东西：{detail}"
    assert "A / D" in detail, f"拒建理由没点名 MVP-1 启用了哪两类：{detail}"
    assert session.scalar(text("select count(*) from approval_ticket")) == 0


def test_the_internal_create_endpoint_requires_a_scope(session, scope):
    result = app_for(session, internal=True).post(
        "/approvals", json={"kind": KIND_BY_LETTER["A"], "payload": payload("A")}
    )
    assert result.status_code == 400
    assert "payload_incomplete" in result.json()["detail"]


def test_the_internal_create_endpoint_refuses_a_card_without_a_request_with_422(session, scope):
    """422 而不是 500：那条 MATCH SIMPLE 残余的兜底在责任面上有名字，状态码是它的对外口径。"""
    result = app_for(session, internal=True).post(
        "/approvals",
        json={"kind": KIND_BY_LETTER["A"], "task_card_id": "T01", "payload": payload("A")},
    )
    assert result.status_code == 422
    assert "没挂需求单" in result.json()["detail"]


def test_the_internal_create_endpoint_never_times_out_an_a_split_ticket(session, scope):
    """裁决 ②：A 类"无超时"在 HTTP 上读作 `timeout_at: null`，库里那一列也真的是 NULL。"""
    result = app_for(session, internal=True).post(
        "/approvals",
        json={"kind": KIND_BY_LETTER["A"], "feature_request_id": str(scope.request_id),
              "payload": payload("A")},
    )
    ticket_id = uuid.UUID(result.json()["ticket_id"])
    assert result.json()["timeout_at"] is None
    assert session.get(ApprovalTicket, ticket_id).timeout_at is None


def test_the_http_surface_rejects_the_auto_channel_with_422(session, scope):
    """裁决 ④ 的对外口径：`AUTO` 在域里，但**人不许用它提交**（HTTP 与仓储两侧各拦一道）。

    两侧都拦是刻意的：HTTP 那道给一个说得出理由的 422，仓储那道保证绕过 HTTP 直调仓储也越不过去。
    """
    ticket_id = make_ticket(session, scope, "A")
    result = app_for(session).post(
        f"/approvals/{ticket_id}/resolve",
        json=resolve_body(0, "approve", "AUTO"),
        headers=HEADERS,
    )
    assert result.status_code == 422
    detail = result.json()["detail"]
    assert "不由人提交" in detail and "AUTO" in detail
    assert "不在 resolution_channel 域里" not in detail, "它在域里，那样说是在撒谎"
    assert str(session.get(ApprovalTicket, ticket_id).state) == "PENDING"
    assert app_for(session).get(f"/approvals/{ticket_id}/events").json() == []

    # 反向对照：同一张单换 DASHBOARD 就过（证明 422 量的是通道不是这张单）。
    assert app_for(session).post(
        f"/approvals/{ticket_id}/resolve", json=resolve_body(0), headers=HEADERS
    ).status_code == 200


# ---------------------------------------------------------------- 续跑：后台而非内联（Phase 15 裁决 ③）


def test_the_response_is_sent_before_the_resumer_finishes(session, scope):
    """点批准那一下**不能等厂商**：响应先回，续跑在后台线程里跑（owner 2026-10-22 裁决 ③）。

    量法不用 TestClient：它会在返回前把后台任务跑完，两种实现都"绿"。这里直接驱动 ASGI ——
    收到响应体结束那一帧之后，续跑线程还卡在闸门上（`release` 未放行）⇒ 响应没有等它。
    **反向鉴别力**：把端点改回内联调用，响应体那一帧要等续跑跑完才发 ⇒ `wait_for(..., 2)` 超时而红。
    """
    started = threading.Event()
    release = threading.Event()

    def resumer(**kwargs) -> None:
        started.set()
        assert release.wait(5), "续跑没被放行 ⇒ 测试自己挂住了"

    client = app_for(session, resumer=resumer)
    app = client.app
    ticket_id = make_ticket(session, scope, "A")
    body = json.dumps(resolve_body()).encode()
    asgi_scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.1"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": f"/approvals/{ticket_id}/resolve",
        "raw_path": None,
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (api.PRINCIPAL_HEADER.encode(), APPROVER.encode()),
        ],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
        "app": app,
    }

    async def drive() -> None:
        sent: list[dict] = []
        delivered = asyncio.Event()
        fed = {"done": False}

        async def receive() -> dict:
            if not fed["done"]:
                fed["done"] = True
                return {"type": "http.request", "body": body, "more_body": False}
            await asyncio.Event().wait()  # 不再有请求体；应用收尾后由 asyncio.run 取消
            return {"type": "http.disconnect"}

        async def send(message: dict) -> None:
            sent.append(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                delivered.set()

        task = asyncio.create_task(app(asgi_scope, receive, send))
        await asyncio.wait_for(delivered.wait(), 2.0)
        assert any(m["type"] == "http.response.start" and m["status"] == 200 for m in sent), sent
        # 这里不能同步 `started.wait()`：那会堵住事件循环线程，应用 coroutine 根本走不到
        # "跑后台任务"那一行 —— 红的是判据自己的写法，不是实现。
        assert await asyncio.to_thread(started.wait, 2.0), "后台续跑没启动 ⇒ 这条判据没量到东西"
        assert not task.done(), "HTTP 收尾等到了续跑结束（内联调用的形状）"
        release.set()
        await asyncio.wait_for(task, 5.0)

    asyncio.run(drive())
    assert release.is_set()


def test_only_the_gate_ticket_wakes_the_graph(session, scope):
    """图上的中断点只有 `A_SPLIT` 那一张 ⇒ 别的类别的批准**不调度**，`resumed_from` 也必须是 null。

    报一个 `"approvalGate"` 会让看板写出"已交回图里续跑"，而实际上没有那条 thread。
    """
    calls: list[dict] = []
    client = app_for(session, resumer=lambda **kw: calls.append(kw))
    ticket_id = make_ticket(session, scope, "B")
    body = client.post(f"/approvals/{ticket_id}/resolve", json=resolve_body(), headers=HEADERS).json()
    assert body["applied"] is True and body["resumed_from"] is None, body
    assert calls == [], f"B 类单据批了也去续跑图：{calls}"

    gate = make_ticket(session, scope, "A")
    body = client.post(f"/approvals/{gate}/resolve", json=resolve_body(), headers=HEADERS).json()
    assert body["resumed_from"] == api.RESUMED_FROM, "反向对照：A 类那一张必须调度（否则上一条只是因为全都不调度）"
    assert calls == [{"feature_request_id": scope.request_id, "conclusion": "approve"}]


# ---------------------------------------------------------------- 批量批准（2026-10-28）

# 背景：owner 那句"审批卡点太密集，我成了盖章机器"。面板上多了一个 `[✅ 批准此批次全部任务]`，
# 后端就是下面这个端点。判据只关心一件事：**它不是第二条审批路径** ——
# 每张单走同一个 `repository.resolve`（同一份 CAS、同一份或签、同一份到达流水），
# 只是把"N 次点击"收成"一次点击"。所以下面每一条都拿单张端点的行为当参照。

def batch_body(*pairs, verdict="approve", channel="DASHBOARD", **kw) -> dict:
    return {
        "tickets": [{"ticket_id": str(tid), "version": version} for tid, version in pairs],
        "verdict": verdict,
        "channel": channel,
        **kw,
    }


def ticket_row(session, ticket_id) -> ApprovalTicket:
    session.expire_all()
    return session.get(ApprovalTicket, ticket_id)


def test_a_batch_applies_every_ticket_and_reports_one_row_each(session, scope):
    """三张 A 类单一次批完：三行结果、三张 `APPROVED`、`version` 各 +1。"""
    # 三张必须挂**不同任务卡**：`create_or_merge` 的合并键是"同一需求单 + 同一张卡"（FR-045），
    # 三张都挂空卡就会并成一张，于是"一批三张"实际只有一张 —— 用例会以"第 2、3 张 already_decided"红掉。
    first, second, third = (
        make_ticket(session, scope, "A", card_id) for card_id in ("T01", "T02", "T03")
    )
    client = app_for(session)
    response = client.post(
        "/approvals/batch",
        json=batch_body((first, 0), (second, 0), (third, 0)),
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text[:300]
    body = response.json()
    assert (body["applied"], body["failed"]) == (3, 0), body
    assert [row["ok"] for row in body["results"]] == [True, True, True]
    for tid in (first, second, third):
        row = ticket_row(session, tid)
        assert str(row.state) == "APPROVED", f"{tid} 批了但没生效"
        assert row.version == 1
        assert row.decided_by_principal == APPROVER, "代签不许丢身份：这批是谁批的必须落在行上"


def test_a_batch_keeps_going_when_one_ticket_was_already_decided(session, scope):
    """第 2 张被人抢先批过（或签后到）⇒ 它记一行失败，另外两张照样成立。

    这条是"逐单一个事务"的判据。整批回滚的话，第 1、3 张已经生效的批准会被一起抹掉 ——
    而它们本来就已经批了，抹掉等于把 owner 刚做的决定偷偷撤销。
    """
    # 三张必须挂**不同任务卡**：`create_or_merge` 的合并键是"同一需求单 + 同一张卡"（FR-045），
    # 三张都挂空卡就会并成一张，于是"一批三张"实际只有一张 —— 用例会以"第 2、3 张 already_decided"红掉。
    first, second, third = (
        make_ticket(session, scope, "A", card_id) for card_id in ("T01", "T02", "T03")
    )
    client = app_for(session)
    client.post(f"/approvals/{second}/resolve", json=resolve_body(), headers=HEADERS)

    body = client.post(
        "/approvals/batch",
        json=batch_body((first, 0), (second, 0), (third, 0)),
        headers=HEADERS,
    ).json()
    assert (body["applied"], body["failed"]) == (2, 1), body
    rows = body["results"]
    assert [row["ok"] for row in rows] == [True, False, True]
    assert rows[1]["code"] == repo.ALREADY_DECIDED, rows[1]
    assert rows[1]["status_code"] == 409, "失败那一行要带上单张端点会给的状态码，前端才能照着说人话"
    assert str(ticket_row(session, first).state) == "APPROVED"
    assert str(ticket_row(session, third).state) == "APPROVED"


def test_a_batch_with_a_stale_version_reports_stale_and_leaves_that_one_pending(session, scope):
    """版本号对不上 ⇒ 那张单记 `stale_version` 且**仍是待决**（CAS 没命中就不许动它）。"""
    ticket = make_ticket(session, scope, "B")
    client = app_for(session)
    body = client.post("/approvals/batch", json=batch_body((ticket, 7)), headers=HEADERS).json()
    assert body["failed"] == 1 and body["results"][0]["code"] == repo.STALE_VERSION, body
    row = ticket_row(session, ticket)
    assert str(row.state) == "PENDING" and row.version == 0


def test_an_unknown_ticket_in_a_batch_is_a_failed_row_not_a_whole_batch_failure(session, scope):
    """一个不存在的单号混在批次里：它一行 404，别的照常。批量操作最要紧的是"哪几张没成"。"""
    good = make_ticket(session, scope, "A")
    ghost = uuid.uuid4()
    client = app_for(session)
    body = client.post(
        "/approvals/batch", json=batch_body((ghost, 0), (good, 0)), headers=HEADERS
    ).json()
    assert (body["applied"], body["failed"]) == (1, 1), body
    assert body["results"][0]["code"] == repo.TICKET_NOT_FOUND
    assert body["results"][0]["status_code"] == 404
    assert str(ticket_row(session, good).state) == "APPROVED"


def test_a_batch_refuses_the_auto_channel_and_an_out_of_domain_verdict_before_any_db_touch(session, scope):
    """两条 422 都是**先拒、且一张单都不碰**：域外结论套在十张单上只会让整批 500。"""
    ticket = make_ticket(session, scope, "A")
    client = app_for(session)

    auto = client.post(
        "/approvals/batch", json=batch_body((ticket, 0), channel="AUTO"), headers=HEADERS
    )
    assert auto.status_code == 422, auto.text[:300]

    nonsense = client.post(
        "/approvals/batch", json=batch_body((ticket, 0), verdict="lgtm"), headers=HEADERS
    )
    assert nonsense.status_code == 422, nonsense.text[:300]
    assert "resolution_verdict" in nonsense.json()["detail"]

    row = ticket_row(session, ticket)
    assert str(row.state) == "PENDING" and row.version == 0, "被拒的那两发不许留下任何迁移"


def test_a_batch_needs_a_principal_and_a_non_empty_ticket_list(session, scope):
    """身份头缺失 403；空批次 422（`min_length=1`）—— 两者都在动库之前。"""
    ticket = make_ticket(session, scope, "A")
    client = app_for(session)
    assert client.post("/approvals/batch", json=batch_body((ticket, 0))).status_code == 403
    empty = client.post(
        "/approvals/batch",
        json={"tickets": [], "verdict": "approve", "channel": "DASHBOARD"},
        headers=HEADERS,
    )
    assert empty.status_code == 422, empty.text[:300]
    assert str(ticket_row(session, ticket).state) == "PENDING"


def test_a_batch_also_refuses_a_missing_approver_set(session, scope):
    """审批者集合没配 ⇒ 503（与单张端点同一条 fail-closed），不是"批量就放宽"。"""
    ticket = make_ticket(session, scope, "A")
    client = app_for(session, approvers=None)
    response = client.post("/approvals/batch", json=batch_body((ticket, 0)), headers=HEADERS)
    assert response.status_code == 503, response.text[:300]


def test_the_gate_ticket_in_a_batch_still_wakes_the_graph(session, scope):
    """A 类那一张批完照样续跑图（`resumed_from` 落在它自己那一行），别的类不调度。"""
    calls: list[dict] = []
    client = app_for(session, resumer=lambda **kw: calls.append(kw))
    gate = make_ticket(session, scope, "A", "T01")
    other = make_ticket(session, scope, "B", "T02")
    body = client.post(
        "/approvals/batch", json=batch_body((gate, 0), (other, 0)), headers=HEADERS
    ).json()
    assert body["applied"] == 2, body
    assert body["results"][0]["resumed_from"] == api.RESUMED_FROM
    assert body["results"][1]["resumed_from"] is None, "B 类批了也不许声称续跑"
    assert calls == [{"feature_request_id": scope.request_id, "conclusion": "approve"}]


def test_a_reject_batch_carries_the_reason_into_the_arrival_log(session, scope):
    """批量驳回：理由原样进 EN-10 到达流水（`reason_text`），状态迁到 `REJECTED`。

    这里**不**断"空理由被拒" —— 单张端点也不要求理由，批量不发明一条更严的规则。
    要断的是"理由没丢"：一批驳回十张、理由却落成空串，事后没人说得清当初为什么驳回。
    """
    ticket = make_ticket(session, scope, "A")
    client = app_for(session)
    body = client.post(
        "/approvals/batch",
        json=batch_body((ticket, 0), verdict="reject", reason_or_ask="验收标准第 2 条没做到"),
        headers=HEADERS,
    ).json()
    assert body["applied"] == 1, body
    assert str(ticket_row(session, ticket).state) == "REJECTED"
    session.expire_all()
    arrivals = list(
        session.scalars(
            select(ResolutionEvent)
            .where(ResolutionEvent.approval_ticket_id == ticket)
            .order_by(ResolutionEvent.arrived_at)
        ).all()
    )
    assert [str(row.verdict) for row in arrivals] == ["reject"]
    assert arrivals[0].reason_text == "验收标准第 2 条没做到", "理由必须原样落到流水上"
    assert arrivals[0].applied is True


def test_a_summary_carries_the_ticket_created_at_not_the_response_time(session, scope):
    """P1-4：A/C/D 三类的 `timeout_at` 是 `null`（无超时），看板要算"挂了多久"只有 `created_at` 一个起点。

    所以这条判据断的是**这一列**，不是"响应的那一刻"：把库里的值改成一个确定的过去时刻，
    端点必须原样吐回来。若它自己 `now()` 一下，界面上"已挂起 N 天"就会永远是 0 天 ——
    一条永远绿、永远没用的告警。
    """
    ticket_id = make_ticket(session, scope, "A")
    born = dt.datetime(2026, 10, 1, 3, 0, tzinfo=dt.timezone.utc)
    session.execute(
        text("update approval_ticket set created_at = :born where id = :tid"),
        {"born": born, "tid": ticket_id},
    )
    session.flush()

    row = app_for(session).get("/approvals").json()[0]
    assert dt.datetime.fromisoformat(row["created_at"]) == born
    # 同时钉住"无超时"那一档没有被这一列悄悄改掉：A 类仍然是 null。
    assert row["timeout_at"] is None
