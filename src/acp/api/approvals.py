"""T055：审批 REST 面（FR-042｜R-26，`contracts/approval-api.md`）。

四条公开端点 + 一条**内部**端点，且"内部"这件事是结构上的而不是注释上的：
建单挂在 `internal_router` 上，装配方 MUST NOT 把它 `include_router` 进对外的 app
（`contracts/approval-api.md §1` 末行"由编排图内部建单（外部不直调）"）。
判据 = `tests/integration/test_approval_api.py::test_the_public_app_has_no_create_endpoint` ——
它读公开 app 的 OpenAPI 路径集，断 `POST /approvals` 不在里面，并**同时**断 `internal_router`
里确实有这一条（否则"不在"只是因为压根没实现，那是空对空）。

三处刻意的写法：

1. **状态码只从 `repository.HTTP_STATUS` 取**。错误码到 HTTP 状态的映射表在仓储层成文一次，
   这里只做翻译；两处各写一份就会漂成"用例里 409、文档里 403"。
2. **`principal` 从请求头 `X-ACP-Principal` 取，MVP-1 没有认证**。所以 403 `principal_not_allowed`
   挡住的是"披错身份误操作"，**挡不住冒充** —— 这条限制写在文档里、也写在用例名里，
   不留成一句没人读的注释（认证与审批者集合的归属归 M2，见 `tasks.md` 的 Phase 4 依赖段）。
   审批者集合本身是注入的（`app.state.approvers`），缺它即 503 而不是"谁都能批"。
3. **`resumed_from` 诚实回报，且只在"条件满足"时才报**。契约 §3 的成功响应里有这个字段，而"续跑图"
   需要装配方注入（`app.state.graph_resumer`，T123）。没注入时这里返回 `null` 而不是写死 `"ApprovalGate"` ——
   写死就是拿响应体冒充一条不存在的执行路径（同类实况见 Checkpoint 3 的"装配点不存在"那条）。
   **2026-10-22 起多两格**（owner Phase 15 裁决 ③）：① `serve.build_app()` 默认注入真续跑者，
   `null` 从此只出现在"装配方明确没注"的自建 app 上；② 只有**图上的那张单**（`kind == approval_gate.KIND`，
   `A_SPLIT`）且挂着需求单号时才谈续跑 —— 别类单据批了不碰图，报一个 `"approvalGate"` 会是假话。
   调用方式也改了：**`background.add_task(...)`，HTTP 先回、续跑后跑**（`_resume` 的 docstring 有理由）。

4. **落库边界在端点这一侧，且只有一处写法**（M2-W11）。仓储层与 `service` 只 `flush()`，把提交点留给
   调用方（`persistence/session.py::new_session`）；但 HTTP 这一侧过去**没有**调用方 —— `_session()` 直接
   `factory()`，既不提交也不还会话，于是 `applied=true / state=APPROVED` 的响应体后面是一条从没发到
   PostgreSQL 的事务（owner 2026-10-20 点火实测：`realrun --resume` 读到 `'PENDING'`）。现在的口径：
   读写都走 `_store(request)`（一次请求一个会话，出去一定 `close()`），而**落地由端点显式 `commit()`**，
   位置在 `repository.resolve` 返回之后、4xx 分支与 `_resume()` 之前 —— 两个"之前"各有理由，见那两个函数
   的 docstring。其余四个 REST 模块（`autonomy / requests / tasks / web`）同形缺陷归 M2-W13，本笔不顺手改。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from acp.approval import repository as repo
from acp.approval import service
from acp.orchestrate.nodes.approval_gate import GATE, KIND as GATE_KIND
from acp.persistence.enums import values_of
from acp.registry.risk_engine import RequestFacts

#: 提交者身份的请求头。名字里带 `ACP-` 前缀是防与网关/代理的既有头撞名。
PRINCIPAL_HEADER = "x-acp-principal"

#: 恢复动作的节点名（`contracts/approval-api.md §3` 的响应字段值）。唯一载体是图里那个节点常量。
RESUMED_FROM = GATE

_STATES = set(values_of("approval_state"))


class BatchTicket(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticket_id: uuid.UUID
    version: int = Field(ge=0)


class BatchResolveBody(BaseModel):
    """`POST /approvals/batch` 的请求体：一批单一次结论（2026-10-28，批量批准）。

    每张单**必须带自己的 version**：或签的 CAS 是逐单的（`repository.resolve` 那条
    `WHERE version = ?`），拿一个版本号套一批单等于把"我看到的是哪一版"这件事抹掉。
    前端从 `/approvals?state=PENDING` 读到的正是这些版本号，照抄即可。
    """

    model_config = ConfigDict(extra="forbid")

    tickets: list[BatchTicket] = Field(min_length=1)
    verdict: str
    reason_or_ask: str = ""
    channel: str


class BatchItemResult(BaseModel):
    """逐单结果。**失败的单也占一行** —— 批量操作最要紧的是"哪几张没成、为什么"。"""

    ticket_id: uuid.UUID
    ok: bool
    state: str | None = None
    applied: bool | None = None
    code: str | None = None
    status_code: int = 200
    resumed_from: str | None = None


class BatchResolveResponse(BaseModel):
    results: list[BatchItemResult]
    #: 批成的张数 / 没成的张数。两个数分开给，读的人才不必自己数 `results`。
    applied: int
    failed: int


class ResolveBody(BaseModel):
    """`POST /approvals/{id}/resolve` 的请求体，形状照契约 §3（`channel` 取 EN-10 的域值）。"""

    model_config = ConfigDict(extra="forbid")

    verdict: str
    reason_or_ask: str = ""
    version: int = Field(ge=0)
    channel: str


class TicketSummary(BaseModel):
    ticket_id: uuid.UUID
    kind: str
    state: str
    version: int
    priority: int
    task_card_id: str | None
    feature_request_id: uuid.UUID | None
    risk_exempt_eligible: bool
    #: 裁决 ②：§1 写"无超时"的三类这里序列化成 `null`（列本身可空），不是某个极远的时刻。
    timeout_at: str | None
    #: P1-4：这张单**建出来**的时刻。无超时的单靠它算"挂了多久" —— `timeout_at` 为 null 时
    #: 它是唯一的起点，看板据此标红"已挂起 N 天"。
    created_at: str


class TicketDetail(TicketSummary):
    payload: dict[str, Any]
    decided_by_principal: str | None
    decided_at: str | None
    event_count: int


class ResolveResponse(BaseModel):
    ticket_id: uuid.UUID
    state: str
    applied: bool
    version: int
    logged_as: str | None = None
    resumed_from: str | None = None


class EventRow(BaseModel):
    event_id: uuid.UUID
    arrived_at: str
    channel: str
    verdict: str
    applied: bool
    reason_text: str


router = APIRouter()
internal_router = APIRouter()


def _factory(request: Request) -> Callable[[], Session]:
    """注入口的读取点。503 的话术只在这一处（其余四个 REST 模块各自的"缺注入"话术归 M2-W13 并轨）。"""
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=503, detail="store_not_configured：审批单读写需要注入 app.state.session_factory"
        )
    return factory


@contextmanager
def _store(request: Request) -> Iterator[Session]:
    """一次请求一个会话，出去一定**还**（`close()`）。

    为什么不套 `persistence/session.py::new_session`：那一支在异常路径上 `session.rollback()`，而 PG 档
    夹具（`tests/integration/test_approval_api.py::session`）把会话绑在一条**已 begin 的外层事务**上 ——
    实测 SQLAlchemy 2.1.4 下这次 rollback 连外层事务一起回滚，前一步建的单据凭空消失，于是每个走 4xx
    的判据都红成"找不到单据"。HTTP 这侧的"丢弃未提交"由 `close()` 承担：真连接（生产）里 `close()`
    把当前事务回滚并把连接还给池子（实测 `pg_stat_activity` 里不留 `idle in transaction`），
    而借外层事务（测试）时 `close()` 不动外层。**落地哪一笔不靠这里推断，由端点显式 `commit()`。**
    """
    session = _factory(request)()
    try:
        yield session
    finally:
        session.close()


def _approvers(request: Request) -> frozenset[str]:
    """审批者集合。**没配就一律拒**（503），不当成"不限制"。

    这一条的方向不能反：把"集合为空"读成"谁都能批"会让一次漏配置变成一道敞开的门，
    而那扇门后面的动作是直接改任务状态（FR-040）。
    """
    approvers = getattr(request.app.state, "approvers", None)
    if not approvers:
        raise HTTPException(
            status_code=503,
            detail="approvers_not_configured：MVP-1 至少要有一名审批者，注入 app.state.approvers",
        )
    return frozenset(approvers)


def _principal(request: Request) -> str:
    principal = request.headers.get(PRINCIPAL_HEADER, "")
    if not principal:
        raise HTTPException(status_code=403, detail=f"principal_not_allowed：请求头 {PRINCIPAL_HEADER} 缺失")
    return principal


def _summary(row) -> TicketSummary:
    return TicketSummary(
        ticket_id=row.id,
        kind=str(row.kind),
        state=str(row.state),
        version=row.version,
        priority=repo.priority_key(str(row.kind)),
        task_card_id=row.task_card_id,
        feature_request_id=row.feature_request_id,
        risk_exempt_eligible=row.risk_exempt_eligible,
        timeout_at=row.timeout_at.isoformat() if row.timeout_at is not None else None,
        created_at=row.created_at.isoformat(),
    )


@router.get("/approvals", response_model=list[TicketSummary])
def list_approvals(request: Request, state: str = "PENDING") -> list[TicketSummary]:
    """列单，按 FR-044 的优先级排（`C/D > B > A`）。排序键的唯一实现在仓储层。"""
    if state not in _STATES:
        raise HTTPException(
            status_code=422, detail=f"state 不在 approval_state 域里：{sorted(_STATES)}"
        )
    with _store(request) as session:
        return [_summary(row) for row in repo.list_tickets(session, state)]


@router.get("/approvals/{ticket_id}", response_model=TicketDetail)
def get_approval(ticket_id: uuid.UUID, request: Request) -> TicketDetail:
    with _store(request) as session:
        row = session.get(repo.ApprovalTicket, ticket_id)
        if row is None:
            raise HTTPException(status_code=404, detail="ticket_not_found")
        return TicketDetail(
            **_summary(row).model_dump(),
            payload=dict(row.payload),
            decided_by_principal=row.decided_by_principal,
            decided_at=row.decided_at.isoformat() if row.decided_at else None,
            event_count=len(repo.events(session, ticket_id)),
        )


@router.get("/approvals/{ticket_id}/events", response_model=list[EventRow])
def list_events(ticket_id: uuid.UUID, request: Request) -> list[EventRow]:
    """到达流水：`applied=false` 的行就是或签里"后到的那一份"（FR-040 的 duplicate 唯一载体）。"""
    with _store(request) as session:
        if session.get(repo.ApprovalTicket, ticket_id) is None:
            raise HTTPException(status_code=404, detail="ticket_not_found")
        return [
            EventRow(
                event_id=event.id,
                arrived_at=event.arrived_at.isoformat(),
                channel=str(event.channel),
                verdict=str(event.verdict),
                applied=event.applied,
                reason_text=event.reason_text,
            )
            for event in repo.events(session, ticket_id)
        ]


@router.post("/approvals/{ticket_id}/resolve", response_model=ResolveResponse)
def resolve_approval(
    ticket_id: uuid.UUID,
    body: ResolveBody,
    request: Request,
    background: BackgroundTasks,
) -> ResolveResponse:
    """提交结论。看板与 CLI 打的就是这一个端点（FR-042），CAS 在 `repository.resolve`。

    `session.commit()` 的位置在这里是**承重的**，两个"之前"各挡一种造假：

    - 在 `if outcome.code` **之前** ⇒ 409 那两支（或签后到的 duplicate、CAS 撞车的 stale version）
      在仓储里各写了一行 `applied=false` 的到达流水，那是 FR-040 要的审计事实；提交点若挪到成功分支里，
      "这次结论被拒了"就同时变成"这一行流水没发生过"。
    - 在 `_resume()` **之前** ⇒ 续跑者从**另一条连接**读单，未提交的 APPROVED 对它不可见，它会拿
      `'PENDING'` 当结论（owner 2026-10-20 点火实测到的 `approval_not_decisive` 就是这个形状）。
    """
    if body.channel not in repo.HUMAN_CHANNELS:
        # 不是"域里没有"而是"域里有但不许人提交"：`AUTO` 由裁决 ④ 进域，专供规则表免确认写流水。
        # 这条消息 MUST NOT 写成"不在 resolution_channel 域里" —— 它在域里，那样说是在撒谎。
        raise HTTPException(
            status_code=422,
            detail=(
                f"channel {body.channel!r} 不由人提交：可写的只有 {sorted(repo.HUMAN_CHANNELS)}，"
                "AUTO 由规则表免确认独占（见 repository.HUMAN_CHANNELS）"
            ),
        )
    with _store(request) as session:
        outcome = repo.resolve(
            session,
            ticket_id=ticket_id,
            verdict=body.verdict,
            version=body.version,
            channel=body.channel,
            principal=_principal(request),
            approvers=_approvers(request),
            reason_or_ask=body.reason_or_ask,
        )
        session.commit()
        if outcome.code:
            detail = {"ticket_id": str(ticket_id), "applied": outcome.applied, "state": outcome.state}
            if outcome.logged_as:
                detail["logged_as"] = outcome.logged_as
            raise HTTPException(status_code=outcome.status_code, detail={**detail, "code": outcome.code})

        resumed_from: str | None = None
        if outcome.applied and outcome.state in {repo.APPROVED, repo.REJECTED}:
            resumed_from = _resume(request, session, ticket_id, outcome, background)
        return ResolveResponse(
            ticket_id=outcome.ticket_id,
            state=outcome.state,
            applied=outcome.applied,
            version=outcome.version,
            logged_as=outcome.logged_as,
            resumed_from=resumed_from,
        )


@router.post("/approvals/batch", response_model=BatchResolveResponse)
def resolve_batch(
    body: BatchResolveBody,
    request: Request,
    background: BackgroundTasks,
) -> BatchResolveResponse:
    """一批单一次结论（2026-10-28，`[✅ 批准此批次全部任务]` 的后端）。

    **它不是第二条审批路径**，这一点由形状保证而不是由注释保证：
    - 每张单都走同一个 `repository.resolve`（同一份 CAS、同一份或签、同一份到达流水）；
    - 身份与审批者校验复用 `_principal` / `_approvers`（缺配置照样 503，不是"批量就放宽"）；
    - `AUTO` 通道照样不许人提交（同一句 422）。

    **逐单一个事务**，不整批回滚。理由是现场：一批十张单里第 7 张被人抢先批过了
    （或签后到 ⇒ 409），整批回滚就等于把前 6 张已经成立的批准抹掉，而它们本来已经生效了。
    所以结果是"批成的有几张、没成的是哪几张、各自为什么"。

    `verdict` 只收 `approve` / `reject`（域校验在端点里先做，域外结论**一张单都不碰**）。
    **理由不是必填** —— 单张端点也不要求，批量不发明一条更严的规则；`reason_or_ask` 原样进 EN-10
    到达流水。这一点在 `test_a_reject_batch_carries_the_reason_into_the_arrival_log` 里如实钉着，
    而不是靠这里一句"必须带理由"的注释（2026-10-28 写这段时我以为仓储会拒，实测它不拒）。
    """
    if body.channel not in repo.HUMAN_CHANNELS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"channel {body.channel!r} 不由人提交：可写的只有 {sorted(repo.HUMAN_CHANNELS)}，"
                "AUTO 由规则表免确认独占（见 repository.HUMAN_CHANNELS）"
            ),
        )
    if body.verdict not in repo.VERDICTS:
        # 一个域外的结论套在十张单上，只会在第 1 张那里抛 `ApprovalMisuse` ⇒ 整批 500，
        # 而真正的原因（"结论写错了"）被埋在一个堆栈里。先拒，且一张单都不碰。
        raise HTTPException(
            status_code=422,
            detail=f"verdict {body.verdict!r} 不在 resolution_verdict 域里：{sorted(repo.VERDICTS)}",
        )
    principal = _principal(request)
    approvers = _approvers(request)
    results: list[BatchItemResult] = []
    for item in body.tickets:
        # 每张单一个会话：一张单的失败（409 / 校验）不该把别的单的到达流水一起回滚。
        try:
            with _store(request) as session:
                outcome = repo.resolve(
                    session,
                    ticket_id=item.ticket_id,
                    verdict=body.verdict,
                    version=item.version,
                    channel=body.channel,
                    principal=principal,
                    approvers=approvers,
                    reason_or_ask=body.reason_or_ask,
                )
                session.commit()
                if outcome.code:
                    results.append(
                        BatchItemResult(
                            ticket_id=item.ticket_id,
                            ok=False,
                            state=outcome.state,
                            applied=outcome.applied,
                            code=outcome.code,
                            status_code=outcome.status_code,
                        )
                    )
                    continue
                resumed_from: str | None = None
                if outcome.applied and outcome.state in {repo.APPROVED, repo.REJECTED}:
                    resumed_from = _resume(request, session, item.ticket_id, outcome, background)
                results.append(
                    BatchItemResult(
                        ticket_id=item.ticket_id,
                        ok=True,
                        state=outcome.state,
                        applied=outcome.applied,
                        resumed_from=resumed_from,
                    )
                )
        except HTTPException:
            # 会话工厂没注入（503）这类**整批都不成立**的错：不能退化成"每张单都失败"，
            # 那会让一次装配事故看起来像十张单各自坏了。
            raise
        except Exception as exc:  # noqa: BLE001 - 一张单的意外不许吃掉整批的结果单
            # 这里**不是**把失败吞成成功：这一行 `ok=False` + 异常类名，前 6 张已经提交的批准照样成立，
            # 而 owner 拿到的是"第 7 张没成、为什么"，不是一句 500。
            results.append(
                BatchItemResult(
                    ticket_id=item.ticket_id,
                    ok=False,
                    code=type(exc).__name__,
                    status_code=500,
                )
            )
    return BatchResolveResponse(
        results=results,
        applied=sum(1 for row in results if row.ok),
        failed=sum(1 for row in results if not row.ok),
    )


def _resume(
    request: Request,
    session,
    ticket_id: uuid.UUID,
    outcome,
    background: BackgroundTasks,
) -> str | None:
    """把结论交回图里续跑（FR-041）。装配方没注入 resumer 时**如实返回 None**。

    这不是"忘了接"：装配点归 T123，而它存在与否必须能被读出来 —— 一个恒为 `"approvalGate"`
    的响应字段会让"结论已落库但图还挂着"这种状态看起来像已经跑下去了。

    进来的时候结论**已经提交过了**（调用点在 `resolve_approval` 的 `commit()` 之后）。
    这里再 `session.get()` 一次不是多此一举：要的是"另一条连接也读得到"的那一份状态。

    **两格"条件满足"都是 2026-10-22 加的**（owner Phase 15 裁决 ③）：

    - `background.add_task(...)`：续跑会一直跑到厂商返回（默认 900 秒），内联调它就是让看板点一下
      转两分钟。响应先回、续跑在后台线程里跑，`resumed_from` 报的是"已交给谁"而不是"已经跑完"。
      **结论在交给它之前已经提交**这一点不变 —— 后台线程拿到的是另一条连接，看得见 APPROVED。
    - 只有 `A_SPLIT`（图上的 `ApprovalGate`）且挂着需求单号时才调度：图上的中断点只有这一个，
      别类单据批了既不恢复图、也读不出 thread ⇒ 报 `"approvalGate"` 是假话。
    """
    resumer: Callable[..., Any] | None = getattr(request.app.state, "graph_resumer", None)
    if resumer is None:
        return None
    row = session.get(repo.ApprovalTicket, ticket_id)
    if row is None or row.feature_request_id is None or str(row.kind) != GATE_KIND:
        return None
    conclusion = "approve" if outcome.state == repo.APPROVED else "reject"
    background.add_task(resumer, feature_request_id=row.feature_request_id, conclusion=conclusion)
    return RESUMED_FROM


@internal_router.post("/approvals", status_code=201, response_model=TicketSummary)
def create_approval(payload: Mapping[str, Any], request: Request) -> TicketSummary:
    """内部建单端点（契约 §1 表末行）。**MUST NOT 挂进对外 app**；编排图直接调 `service`，
    这个端点存在是为了装配方（T123）与将来的自动化测试有一个同形状的入口。

    一个请求**只借一个会话**：建单与建完之后回读那一行用的是同一个 `session`
    （旧写法在这里调了两次 `_session()`，两个会话都既不提交也不还 ⇒ 单据生不出来，连接还不回去）。
    """
    kind = payload.get("kind")
    if not kind:
        raise HTTPException(status_code=400, detail="payload_incomplete：缺键 ['kind']")
    scope = {key: payload[key] for key in ("task_card_id", "feature_request_id") if payload.get(key)}
    if not scope:
        raise HTTPException(
            status_code=400,
            detail="payload_incomplete：建单必须挂到一张卡或一张需求单上（FR-045 的合并键）",
        )
    facts_body = payload.get("facts")
    with _store(request) as session:
        try:
            ticket = service.create_approval(
                session,
                kind=kind,
                payload=payload.get("payload") or {},
                task_card_id=scope.get("task_card_id"),
                feature_request_id=uuid.UUID(str(scope["feature_request_id"]))
                if "feature_request_id" in scope
                else None,
                facts=RequestFacts(**facts_body) if isinstance(facts_body, Mapping) else None,
            )
        except service.PayloadIncomplete as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except service.KindNotEnabled as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except repo.ApprovalMisuse as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        session.commit()
        return _summary(session.get(repo.ApprovalTicket, ticket.ticket_id))
