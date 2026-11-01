"""卡跑挂了之后**必须还有下一步**（2026-10-28，现场 = `real_demo / 7800b3dd` 的 T05）。

那一天的真实读数：T01–T04 已交付、T05 停在 `RUNNING`、需求单停在 `SPLIT`、A 类工单早已 `APPROVED`。
于是三件事同时发生 —— 没有可派发的卡（`DISPATCHABLE` 里没有 `RUNNING`）、待办条里没有工单
（它不再是 `PENDING`）、屏上没有任何能点的东西。用户原话："一直卡在这个页面没办法往下进行"。

四组判据，各钉一条链：

1. **总线拒绝只算一张卡失败**（`_swallowable(BusError)`）：一件坏产物不该打断整条链。
2. **失败之后卡不能留在 `RUNNING`**：落到 `ESCALATED`，而 `ESCALATED ──人工──▶ READY` 是文档里
   唯一"停下、交人工、且有出边"的位置。只留痕不挪态，那张卡就再也没有入口。
3. **需求单状态跟着卡走**：`SPLIT → RUNNING → DELIVERED`，且不碰 `INTAKE` / `FAILED`。
4. **两个人工入口在 HTTP 面上真的存在**：`POST /requests/{id}/continue`（再推一次）
   与既有的 `POST /tasks/{id}/escalation`（放回待跑）。

零库、零出网、零厂商：会话与查询都是记账替身，`escalation.resolve_escalation` 那一条读的是
仓库里的 `data-model.md`（出口取值本来就是从原文解析的，不在这里另抄一份）。
"""

from __future__ import annotations

import datetime as dt
import inspect
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from acp import realrun, serve_resumer as sr
from acp import run as run_module
from acp.api import requests as requests_api
from acp.bus.gateway import BusError
from acp.orchestrate.nodes import dispatch as dispatch_node
from acp.orchestrate.nodes import escalation

NOW = dt.datetime(2026, 10, 28, 6, 0, tzinfo=dt.timezone.utc)
FID = uuid.UUID("7800b3dd-6882-40b5-b508-3b5b3e27c20b")
DID = uuid.UUID("7a1f0c2e-5d4b-4a9c-9f31-0c7d2b8e4a11")


# ---------------------------------------------------------------- 假件


class FakeQuery:
    """只认三件事：`filter` 之后还能 `all()` / `one_or_none()` / `count()`。

    不实现 SQL 语义是刻意的：这些判据量的是"调用方拿到行之后做了什么"，
    真按条件筛属于仓储层，那一条由集成档（PG）量。
    """

    def __init__(self, rows: list[object]) -> None:
        self._rows = list(rows)

    def filter(self, *args, **kwargs) -> "FakeQuery":
        return self

    def order_by(self, *args, **kwargs) -> "FakeQuery":
        return self

    def all(self) -> list[object]:
        return list(self._rows)

    def one_or_none(self):
        return self._rows[0] if self._rows else None

    def count(self) -> int:
        return len(self._rows)


class FakeSession:
    def __init__(self, *, request=None, cards=(), tickets=()) -> None:
        self.request = request
        self.cards = list(cards)
        self.tickets = list(tickets)
        self.added: list[object] = []
        self.flushes = 0
        self.commits = 0
        self.closed = False

    def query(self, model):
        name = getattr(model, "__name__", "")
        if name == "TaskCard":
            return FakeQuery(self.cards)
        if name == "ApprovalTicket":
            return FakeQuery(self.tickets)
        return FakeQuery([])

    def get(self, model, key):
        return self.request

    def add(self, row: object) -> None:
        self.added.append(row)

    def flush(self) -> None:
        self.flushes += 1

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


def card(card_id: str, status: str) -> SimpleNamespace:
    """`handoff_count` / `rework_count` / `executor_agent_id` 也要给：`escalation` 的留痕会读它们，
    而那三个数正是"升级不许顺手改人/改返工"的读数载体（FR-036）。"""
    return SimpleNamespace(
        id=card_id,
        status=status,
        feature_request_id=FID,
        title=f"卡 {card_id}",
        handoff_count=0,
        rework_count=0,
        executor_agent_id="qoder",
    )


def request_row(status: str) -> SimpleNamespace:
    return SimpleNamespace(id=FID, status=status, project_key="real_demo")


# ---------------------------------------------------------------- 1 总线拒绝


def test_a_bus_refusal_counts_as_one_card_failing() -> None:
    """`BusError` 是"这一件产物/这一次写入不成立"，不是"这个进程不会成功"。

    它原来不在 `SWALLOWABLE` 里（`BusError` 直接派生自 `Exception`），于是一件 `submit-contact.php`
    把整条链打断成 `resume_failed`，后面还能跑的卡一张都没跑。
    """
    refusal = BusError("unsupported_format", 400, "GUARD_VIOLATION", "后缀 'php' 不在取值域内")
    assert realrun._swallowable(refusal) is True


def test_a_process_level_refusal_is_still_not_swallowable() -> None:
    """`RealRunRefused`（缺凭据 / 不在册 / 存档根开不出来）一张卡撞上就说明每张都会撞上 —— 不许吞。"""
    refused = realrun.RealRunRefused("凭据不在环境变量里", code="credential_missing")
    assert realrun._swallowable(refused) is False


# ---------------------------------------------------------------- 2 卡不许停在 RUNNING


def test_a_failed_card_leaves_running_and_is_escalated() -> None:
    stuck = card("T05", dispatch_node.STARTED)
    session = FakeSession(request=request_row("SPLIT"), cards=[stuck])

    moved = realrun.escalate_card_after_failure(
        session,
        feature_request_id=FID,
        task_id="T05",
        code="unsupported_format",
        detail="产物 'qoder-workspace/submit-contact.php' 的后缀 'php' 不在取值域内",
        now=NOW,
    )

    assert moved is True
    assert stuck.status == realrun.FAILED_CARD_STATE == "ESCALATED"
    payload = session.added[-1].payload
    assert payload["action"] == realrun.FAILED_CARD_ACTION
    assert payload["prior_status"] == dispatch_node.STARTED
    assert payload["code"] == "unsupported_format"
    # 留痕里要有一句"下一步点哪儿"，否则读的人还得去翻代码。
    assert "escalation" in payload["exit_hint"]


def test_a_card_that_is_not_running_is_left_alone() -> None:
    """已经交付的卡不该被一次失败的收尾顺手改成升级态。"""
    delivered = card("T01", "DELIVERED")
    session = FakeSession(request=request_row("SPLIT"), cards=[delivered])

    assert (
        realrun.escalate_card_after_failure(
            session, feature_request_id=FID, task_id="T01", code="x", detail="x", now=NOW
        )
        is False
    )
    assert delivered.status == "DELIVERED"
    assert session.added == []


def test_the_escalated_card_can_be_handed_back_by_a_human() -> None:
    """升级态的出口是文档里那一行：`ESCALATED ──人工──▶ READY / REJECTED`。

    这条把"失败 → 升级 → 放回待跑"整条闭合掉：没有它，`ESCALATED` 就是第二条死路。
    """
    stuck = card("T05", "ESCALATED")
    session = FakeSession(request=request_row("SPLIT"), cards=[stuck])

    outcome = escalation.resolve_escalation(
        session,
        feature_request_id=FID,
        task_id="T05",
        decision="READY",
        principal="owner-1",
        reason="换个人再跑一次",
        created_at=NOW,
    )

    assert outcome.status_after == "READY"
    assert stuck.status == "READY"
    # 升级不是换人，也不是返工：这两列一个都不许动（FR-036 的读数要求）。
    # 读数载体是留痕那一行（`EscalationOutcome` 不带这两个计数），所以从 payload 里读。
    payload = session.added[-1].payload
    assert payload["handoff_count"] == 0
    assert stuck.rework_count == 0
    assert stuck.executor_agent_id == "qoder"


# ---------------------------------------------------------------- 3 需求单状态


def test_request_status_goes_running_while_any_card_is_unsettled() -> None:
    session = FakeSession(
        request=request_row("SPLIT"),
        cards=[card("T01", "DELIVERED"), card("T05", "ESCALATED")],
    )
    assert realrun.sync_request_status(session, FID, now=NOW) == "RUNNING"
    assert session.request.status == "RUNNING"


def test_request_status_goes_delivered_when_every_card_is_settled() -> None:
    session = FakeSession(
        request=request_row("RUNNING"),
        cards=[card("T01", "DELIVERED"), card("T02", "DELIVERED_WITH_ISSUES")],
    )
    assert realrun.sync_request_status(session, FID, now=NOW) == "DELIVERED"
    assert session.request.status == "DELIVERED"


@pytest.mark.parametrize("status", ["INTAKE", "FAILED", "DELIVERED"])
def test_request_status_does_not_resurrect_other_states(status: str) -> None:
    """`INTAKE` 归受理段、`FAILED` 是 U6 的现场、`DELIVERED` 是终态 —— 一次续跑都不许改写它们。"""
    session = FakeSession(request=request_row(status), cards=[card("T01", dispatch_node.STARTED)])
    assert realrun.sync_request_status(session, FID, now=NOW) is None
    assert session.request.status == status


def test_a_sync_that_changes_nothing_writes_no_event() -> None:
    """每张卡跑完都对一次状态，值没变就不许留痕 —— 否则活动流会被"状态没变"的假行淹掉。"""
    session = FakeSession(request=request_row("RUNNING"), cards=[card("T05", dispatch_node.STARTED)])
    assert realrun.sync_request_status(session, FID, now=NOW) is None
    assert session.added == []


def test_the_resumer_releases_stuck_cards_in_a_separate_session() -> None:
    """续跑失败之后由**另一个会话**把卡挪出 `RUNNING`。

    为什么必须另一个会话：失败路径上主工作单元一定 `rollback()`，在它里面改的卡态会被一起撤掉 ——
    2026-10-28 的现场正是这样：05:54 的失败之后 T05 在库里一直是 `RUNNING`，
    而之后三次续跑都读到"没有可派发的卡"，整单停摆六小时。
    """
    created: list[FakeSession] = []
    stuck = card("T05", dispatch_node.STARTED)

    def factory() -> FakeSession:
        session = FakeSession(request=request_row("SPLIT"), cards=[stuck])
        created.append(session)
        return session

    resumer = sr.WebResumer(
        session_factory=factory,
        bus_root=Path("."),
        clock=lambda: NOW,
        emit_to=lambda line: None,
    )

    moved = resumer._release_stuck_cards(FID, code="unsupported_format")

    assert moved == ("T05",)
    assert stuck.status == "ESCALATED"
    # 一个会话：自己开的、自己收的（`new_session` 出口提交 + close）。
    assert len(created) == 1
    assert created[0].commits == 1
    assert created[0].closed is True


def test_releasing_stuck_cards_never_raises() -> None:
    """挪不动卡只是"这次没救回来"，不该把归因换成一个更难读的错。"""

    def factory():
        raise RuntimeError("库连不上")

    resumer = sr.WebResumer(
        session_factory=factory, bus_root=Path("."), clock=lambda: NOW, emit_to=lambda line: None
    )
    assert resumer._release_stuck_cards(FID, code="unsupported_format") == ()


# ---------------------------------------------------------------- 4 两个人工入口


def _app(*, session: FakeSession, resumer=None) -> FastAPI:
    app = FastAPI()
    app.include_router(requests_api.router)
    app.state.session_factory = lambda: session
    if resumer is not None:
        app.state.graph_resumer = resumer
    return app


class Recorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs) -> None:
        self.calls.append(dict(kwargs))


def test_continue_schedules_the_resumer_for_a_half_done_request() -> None:
    resumer = Recorder()
    session = FakeSession(request=request_row("SPLIT"), cards=[card("T05", "ESCALATED")])
    client = TestClient(_app(session=session, resumer=resumer))

    response = client.post(f"/requests/{FID}/continue")

    assert response.status_code == 202
    assert response.json()["resume_scheduled"] is True
    assert resumer.calls == [{"feature_request_id": FID, "conclusion": "continue", "principal": None}]
    # 端点自己不写状态：推进归 `realrun.run_real` 那一侧（一次续跑一个事实）。
    assert session.commits == 0


def test_continue_hands_the_person_to_the_resumer() -> None:
    """带身份头就把"谁按的这一下"一路传下去 —— 只有它允许收回孤儿卡（`reclaim_orphaned_cards`）。

    缺头不拦（这一发本身不改卡态），所以那一格传的是 `None`，不是 `""`：空串会被下游当成"有人"，
    于是收回动作的留痕主语是一串空白，等于没人认领。
    """
    resumer = Recorder()
    session = FakeSession(request=request_row("RUNNING"), cards=[card("T01", dispatch_node.STARTED)])
    client = TestClient(_app(session=session, resumer=resumer))

    response = client.post(
        f"/requests/{FID}/continue", headers={requests_api.PRINCIPAL_HEADER: "owner-1"}
    )

    assert response.status_code == 202
    assert resumer.calls[-1]["principal"] == "owner-1"


def test_continue_refuses_while_an_approval_is_still_pending() -> None:
    """待批的单还没批就想继续 —— 那是绕过门禁，必须 409 并说清"先批"。"""
    resumer = Recorder()
    session = FakeSession(
        request=request_row("SPLIT"),
        tickets=[SimpleNamespace(state="PENDING", feature_request_id=FID)],
    )
    client = TestClient(_app(session=session, resumer=resumer))

    response = client.post(f"/requests/{FID}/continue")

    assert response.status_code == 409
    assert "pending_approval" in response.json()["detail"]
    assert resumer.calls == []


@pytest.mark.parametrize("status", ["INTAKE", "DELIVERED", "FAILED"])
def test_continue_refuses_states_it_cannot_push(status: str) -> None:
    resumer = Recorder()
    client = TestClient(_app(session=FakeSession(request=request_row(status)), resumer=resumer))

    response = client.post(f"/requests/{FID}/continue")

    assert response.status_code == 409
    assert "not_runnable" in response.json()["detail"]
    assert resumer.calls == []


def test_continue_reports_missing_wiring_instead_of_pretending() -> None:
    """没注续跑者就 503 —— "排上了"这句话不能凭空说（与 `intake_runner` 同一条口径）。"""
    client = TestClient(_app(session=FakeSession(request=request_row("SPLIT"))))
    response = client.post(f"/requests/{FID}/continue")
    assert response.status_code == 503
    assert "graph_resumer_missing" in response.json()["detail"]


def test_continue_on_an_unknown_request_is_404() -> None:
    client = TestClient(_app(session=FakeSession(request=None), resumer=Recorder()))
    response = client.post(f"/requests/{FID}/continue")
    assert response.status_code == 404
    assert "request_not_found" in response.json()["detail"]


# ---------------------------------------------------------------- 5 交付那一刻

def test_the_two_modules_spell_the_delivered_status_the_same_way() -> None:
    """`run` 与 `realrun` 各持一份 `DELIVERED` 字面量（不能互相 import：`realrun` 反向依赖 `run`）。

    两份一旦漂开，跑卡循环把状态推到的是一个词、组包点写的是另一个词 —— 而 EN-01 的枚举只有五档，
    多出来的那个词在界面上根本读不出来，表现回退成"状态永远不动"（正是这一轮要修的那件事）。
    """
    assert run_module.REQUEST_DELIVERED == realrun.REQUEST_DELIVERED == "DELIVERED"
    assert run_module.REQUEST_STATUS_SYNCED == "request_status_synced"


def test_assembling_the_package_turns_the_request_delivered() -> None:
    """组包成功 ⇒ EN-01 终态，并留一行带 `prior_status` 的痕。

    现场（`real_demo / 7800b3dd`，2026-10-28 08:05）：`results/delivery_package.json` 已经在盘上、
    十份产物都进了 manifest，而 EN-01 还写着 `RUNNING` —— 屏上那句"需求单状态"读的就是这一列，
    读者只能理解成"还在跑，等一会儿"，于是关掉页面走了。终态没人写，是因为最后一张卡是**人工评审**
    才到终态的，那条路径不经过跑卡循环。
    """
    session = FakeSession(request=request_row("RUNNING"))

    written = run_module.mark_request_delivered(session, FID, now=NOW, deliverable_id=DID)

    assert written == "DELIVERED"
    assert session.request.status == "DELIVERED"
    event = session.added[-1]
    assert event.entity == "feature_request" and event.entity_id == str(FID)
    assert event.payload["action"] == run_module.REQUEST_STATUS_SYNCED
    assert event.payload["prior_status"] == "RUNNING"
    assert event.payload["status_after"] == "DELIVERED"
    assert event.payload["deliverable_id"] == str(DID)


def test_a_second_delivery_writes_no_second_event() -> None:
    """已是终态就什么都不写：重试组包会走 `already_assembled` 早退，不该每次重试都多一行假留痕。"""
    session = FakeSession(request=request_row("DELIVERED"))
    assert run_module.mark_request_delivered(session, FID, now=NOW, deliverable_id=DID) is None
    assert session.added == []
    assert session.request.status == "DELIVERED"


def test_marking_a_missing_request_is_a_no_op() -> None:
    """单号读不到时不许抛 —— 交付包已经写进盘了，报错只会让调用方以为"包没成"。"""
    session = FakeSession(request=None)
    assert run_module.mark_request_delivered(session, FID, now=NOW, deliverable_id=DID) is None
    assert session.added == []


# ---------------------------------------------------------------- 6 没人推的 RUNNING 卡

def test_a_card_left_running_is_reclaimed_by_two_recorded_steps() -> None:
    """停在 `RUNNING` 却没人推的卡：先升到 `ESCALATED`（机器判），再由人放回 `READY`。

    现场是库里那两张单（`9e827757` / `070eeb46`）：T01 停在 `RUNNING`，T02–T04 还是 `READY`。
    `readiness()` 只收 `READY`，所以 T01 永远派不出去 ⇒「继续执行」点多少次，这一单都到不了 `DELIVERED`。
    """
    session = FakeSession(cards=[card("T01", dispatch_node.STARTED)])

    reclaimed = realrun.reclaim_orphaned_cards(session, feature_request_id=FID, principal="owner-1", now=NOW)

    assert reclaimed == ("T01",)
    assert session.cards[0].status == "READY"
    kinds = [row.payload["action"] for row in session.added]
    assert kinds == [realrun.FAILED_CARD_ACTION, "human_exit"], kinds
    # 两段的主语不同：机器判"没人推了"，人判"放回队列"。合成一步就没有人做过判断。
    assert session.added[0].actor_agent_id == run_module.ACTOR_AGENT_ID
    assert session.added[1].actor_agent_id == "owner-1"
    assert session.added[1].payload["prior_status"] == "ESCALATED"
    assert session.added[1].payload["card_status_after"] == "READY"


def test_reclaim_leaves_every_card_that_is_not_running_alone() -> None:
    """别的态一格都不碰：`READY` 本来就能派、`DELIVERED` 是终态、`ESCALATED` 等人处置。"""
    session = FakeSession(
        cards=[card("T01", "READY"), card("T02", "DELIVERED"), card("T03", "ESCALATED")]
    )

    assert realrun.reclaim_orphaned_cards(
        session, feature_request_id=FID, principal="owner-1", now=NOW
    ) == ()
    assert [row.status for row in session.cards] == ["READY", "DELIVERED", "ESCALATED"]
    assert session.added == []


def test_the_run_only_reclaims_when_a_person_asked() -> None:
    """`reclaim_principal` 为空 ⇒ 一格不动。CLI 与「批准」那两条路径的现场里可能有另一个进程握着卡，
    在那里自动收回就是把卡从别人手里抢走。"""
    signature = inspect.signature(realrun.run_real)
    assert signature.parameters["reclaim_principal"].default is None
    source = inspect.getsource(realrun.run_real)
    assert source.count("reclaim_orphaned_cards(") == 1
    guard = source.split("reclaim_orphaned_cards(")[0]
    assert "if reclaim_principal:" in guard, "收回那一格没有挂在'有人按过'这个条件上"
