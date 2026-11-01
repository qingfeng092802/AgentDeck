"""M2-W11 的实锤判据：审批 REST 面写完必须由**第二条连接**读得到（owner 2026-10-20 点火撞出的缺陷）。

**为什么单开一个文件**：`test_approval_api.py` 整档绿着放过了这个缺陷，原因写在它自己的 docstring 第 4 条
里 —— 它的"回库读"读的是**同一条连接**（夹具靠 `conn.begin()` + 结束时 `rollback()` 做隔离），
而 SQLAlchemy 2.1.4 在"会话绑到已 begin 的连接"这个形状下，`commit()` 发不到 PostgreSQL、
`rollback()` 也轮不到 PG。于是"端点压根没提交"这件事在那一档里**读不出来**：
`applied=true / state=APPROVED` 的响应体、同连接的"回库"读、外层事务里的行，三者可以同时成立，
而库里什么都没有。owner 撞上的就是这一格：CLI 拿到 200，`realrun --resume` 却读到 `'PENDING'`
（`code=approval_not_decisive`）。

本文件换一个形状：被测试的应用用**生产会话工厂**（`persistence/session.py::make_session_factory`，
自己的池、真的提交），断言一律走**另一个 engine 的另一条连接**。这带来两个后果，都认：

1. 它写进库的行是**真提交**的 ⇒ 收尾必须删干净，否则下一次全量的 PG 档入口守卫（`tests/conftest.py`
   的十张表）会红。删除按"这一发造出来的 `feature_request_id` 圈定"来做（口径同入口守卫的复原说明）。
2. 它量的是"提交边界与借还"这一件事，量不到 CAS 并发、或签顺序那些 —— 那些仍在
   `test_approval_api.py`，两边不许互相冒充。

**本文件证明不到的**（明写，不留成"看起来全验了"）：
- 不证明 uvicorn 起的那个进程用的是新代码 —— 这里用 TestClient，`serve` 要重启才换实现（归 quickstart §9）。
- 不证明 `graph_resumer` 真能续跑图（注入面归 T123）；这里只钉"续跑者被调到的那一刻结论已提交"。
- 不测 WSL 那一档（口径同全仓：本轮只在 Windows 上跑过 PG 档）。
"""

from __future__ import annotations

import ast
import datetime as dt
import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from acp.api import approvals as api
from acp.approval import repository as repo
from acp.approval import service
from acp.approval.timeouts import KIND_BY_LETTER
from acp.config.loader import DSN_ENV, db_url
from acp.persistence.models_core import FeatureRequest
from acp.persistence.session import make_session_factory

pytestmark = pytest.mark.skipif(
    not os.environ.get(DSN_ENV),
    reason=f"{DSN_ENV} 未注入 ⇒ 本文件需要 PG",
)

APPROVER = "owner-commit-boundary"
HEADERS = {api.PRINCIPAL_HEADER: APPROVER}
#: 连接打这个标签，`pg_stat_activity` 才只数本文件的连接 —— 数全局会把别人的现场读成我们的泄漏。
TAG = "acp-test-commit-boundary"
PROJECT_KEY = "acp-commit-boundary"
NOW = dt.datetime(2026, 10, 20, 1, 0, 0, tzinfo=dt.timezone.utc)

APP_SOURCE = Path(__file__).resolve().parents[2] / "src" / "acp" / "api" / "approvals.py"


def payload_a() -> dict:
    return service.build_payload(
        kind=KIND_BY_LETTER["A"],
        pending_item="笔 U 的 A 类待批内容",
        context_digest="上下文摘要",
        suggested_verdict="approve",
        impact={"downstream_task_ids": ["T90"], "estimated_extra_usage": {}},
    )


@pytest.fixture
def engines() -> Iterator[tuple[Engine, Engine]]:
    """两个 engine：一个给被测应用（小池 + 短超时，连接不还在 2 秒内就暴露），一个只用来读。

    `max_overflow=0` 是有意的：默认"池 5 + 溢出 10"会让"每发请求漏一条连接"这种缺陷跑到第 16 发
    才炸，而 2 条池子的第 3 发就炸 —— 变异对照要的是快的、可归因的那一个读数。
    """
    app_engine = create_engine(
        db_url(),
        future=True,
        pool_size=2,
        max_overflow=0,
        pool_timeout=2,
        connect_args={"application_name": f"{TAG}-app"},
    )
    read_engine = create_engine(
        db_url(), future=True, connect_args={"application_name": f"{TAG}-read"}
    )
    try:
        yield app_engine, read_engine
    finally:
        app_engine.dispose()
        read_engine.dispose()


@pytest.fixture
def seeded(engines) -> Iterator[uuid.UUID]:
    """一条**已提交**的需求单（本文件的每一张单据都挂它），用例结束按这个 id 圈定删除。

    起点走 `session.add + commit` 而不走 HTTP：本文件的主题是**结论**落不落库，
    起点必须是一条确定已在库里的单，否则"读不到"就有了第二个解释。

    建单与收尾都挂在**读端那个 engine** 上，不挂应用那个：应用侧的池被故意做小（2 条、不许溢出），
    变异对照里"连接不还"的形态会把清理那一步一起卡死 —— 实测 M-U3 就把 3 行留在了库里，
    靠 conftest 的入口守卫才被抓出来。清理不能依赖被测对象的健康度，所以走另一条池。
    """
    _app_engine, read_engine = engines
    request_id = uuid.uuid4()
    factory = make_session_factory(read_engine)
    with factory() as session:
        session.add(
            FeatureRequest(
                id=request_id,
                project_key=PROJECT_KEY,
                raw_input="笔 U 的提交边界判据",
                summary_digest={},
                clarification_waived=True,
                status="SPLIT",
                created_at=NOW,
            )
        )
        session.commit()
    try:
        yield request_id
    finally:
        # 删除与真跑分两笔事务；圈定条件只用 feature_request_id，不整表清空。
        with factory() as session:
            session.execute(
                text(
                    "delete from resolution_event where approval_ticket_id in "
                    "(select id from approval_ticket where feature_request_id = :r)"
                ),
                {"r": request_id},
            )
            session.execute(
                text("delete from approval_ticket where feature_request_id = :r"), {"r": request_id}
            )
            session.execute(text("delete from feature_request where id = :r"), {"r": request_id})
            session.commit()
        # 收尾的自证：留下了行就是污染了下一发的入口守卫，必须在这里就说清楚，而不是让别人去撞。
        # 只数 `feature_request` 就够：单据与流水是它的子行，外键在，母行删掉了子行就不可能还在。
        with read_engine.connect() as conn:
            left = int(
                conn.execute(
                    text("select count(*) from feature_request where project_key = :k"),
                    {"k": PROJECT_KEY},
                ).scalar()
            )
        assert left == 0, f"本用例留下了 {left} 行需求单（按 {PROJECT_KEY} 圈定删不干净 = 判据自己有缺陷）"


def app_for(app_engine: Engine, *, approvers: object = frozenset({APPROVER})) -> TestClient:
    """把**生产形状**的会话工厂挂上去：`make_session_factory(engine)`，一次请求一个会话、真的提交。"""
    app = FastAPI()
    app.include_router(api.router)
    app.include_router(api.internal_router)
    app.state.session_factory = make_session_factory(app_engine)
    if approvers is not None:
        app.state.approvers = approvers
    return TestClient(app)


def read_state(read_engine: Engine, ticket_id: uuid.UUID) -> tuple[str, int]:
    """另一条连接上的 (state, version)。查不到行返回 `("<缺行>", -1)`，让断言报出差异而不是抛 IndexError。"""
    with read_engine.connect() as conn:
        row = conn.execute(
            text("select state::text, version from approval_ticket where id = :t"),
            {"t": ticket_id},
        ).first()
    return ("<缺行>", -1) if row is None else (str(row[0]), int(row[1]))


def read_events(read_engine: Engine, ticket_id: uuid.UUID) -> list[tuple[str, bool]]:
    with read_engine.connect() as conn:
        return [
            (str(row[0]), bool(row[1]))
            for row in conn.execute(
                text(
                    "select verdict, applied from resolution_event "
                    "where approval_ticket_id = :t order by arrived_at, id"
                ),
                {"t": ticket_id},
            )
        ]


def create_ticket(client: TestClient, request_id: uuid.UUID) -> uuid.UUID:
    """走内部建单端点拿单据号（这一段也是被验的：它同样得提交，否则下一条连接连单据都读不到）。"""
    response = client.post(
        "/approvals",
        json={"kind": KIND_BY_LETTER["A"], "payload": payload_a(), "feature_request_id": str(request_id)},
    )
    assert response.status_code == 201, f"建单端点没成功：{response.status_code} {response.text[:200]}"
    return uuid.UUID(response.json()["ticket_id"])


def resolve(
    client: TestClient, ticket_id: uuid.UUID, *, version: int = 0, verdict: str = "approve"
) -> dict:
    """打一发结论，把 4xx 的 `detail` 摊平到同一层 —— 成功与冲突两份形状都要能读得到字段。"""
    response = client.post(
        f"/approvals/{ticket_id}/resolve",
        json={"verdict": verdict, "version": version, "channel": "CLI", "reason_or_ask": "笔 U"},
        headers=HEADERS,
    )
    body = response.json()
    if isinstance(body.get("detail"), dict):
        body = body["detail"]
    return {"status": response.status_code, **body}


def idle_in_transaction(read_engine: Engine) -> list[tuple[str, str]]:
    """本文件那两条连接里还挂着未结束事务的那些 `(application_name, state)`。

    未提交就还回池子的连接在 PG 侧读作 `idle in transaction` —— 它是 owner 那句"底层事务并没有真正
    commit"在库这一侧的样子，也是他机上那条挡住别人写同一行的事务的样子。按 `TAG` 前缀筛：
    数全局会把别人的现场读成我们的泄漏（本轮实测：owner 的 serve 正挂着一条）。
    """
    with read_engine.connect() as conn:
        return [
            (str(row[0]), str(row[1]))
            for row in conn.execute(
                text(
                    "select application_name, state from pg_stat_activity "
                    "where application_name like :p and state = 'idle in transaction'"
                ),
                {"p": f"{TAG}%"},
            )
        ]


# ---------------------------------------------------------------- 一、实锤：另一条连接读得到结论

def test_the_committed_approval_is_visible_on_a_second_connection(engines, seeded):
    """owner 的工作单第 2 条：调接口之后**由另一个连接**去查库，实锤 `state` 变成 `APPROVED`。

    四个读数按顺序钉，缺一格就放过了别的缺陷形态：
    ① 建单端点提交过（否则第二条连接连单据都没有）；② 批准前是 PENDING；
    ③ 批准后另一条连接读到 APPROVED 且 version 进 1；④ 响应体自述的 state 与库里读到的是同一个值。
    第 ④ 格防的正是"响应说 applied=true 而库里没动"这一形状 —— 也就是本文件存在的全部理由。
    """
    app_engine, read_engine = engines
    client = app_for(app_engine)

    ticket_id = create_ticket(client, seeded)
    assert read_state(read_engine, ticket_id) == ("PENDING", 0), "建单端点写的那一笔没发到库里"

    result = resolve(client, ticket_id)
    assert result["status"] == 200, result
    assert result["applied"] is True and result["state"] == repo.APPROVED, result

    assert read_state(read_engine, ticket_id) == ("APPROVED", 1), (
        "接口返回 APPROVED 而另一条连接读到的不是 ⇒ 提交边界又断了（M2-W11 原缺陷形状）"
    )
    assert result["state"] == read_state(read_engine, ticket_id)[0], "响应体与库不一致 = 响应在自述"
    assert read_events(read_engine, ticket_id) == [("approve", True)], "结论流水没跟着落地"


def test_the_row_is_not_left_locked_after_the_endpoint_returns(engines, seeded):
    """写完那一行必须**放锁**：第二条连接 `FOR UPDATE NOWAIT` 要拿得到，且不留未结束的事务。

    这一格量的不是 state 而是"有没有一条事务挂着不放"。`_store` 不 `close()` 时行已经被
    `commit()` 了、状态读起来是对的，但连接还在 `idle in transaction` 里挂着 —— 那正是 owner
    终端上"下一次写同一行要等下去"的成因（产出方自己也被挡过三分钟，实测读数在 `evidence/phase-15.md` §12.5）。
    """
    app_engine, read_engine = engines
    client = app_for(app_engine)
    ticket_id = create_ticket(client, seeded)

    assert resolve(client, ticket_id)["status"] == 200

    with read_engine.connect() as conn:
        got = conn.execute(
            text("select state::text from approval_ticket where id = :t for update nowait"),
            {"t": ticket_id},
        ).scalar_one()
        conn.rollback()
    assert str(got) == repo.APPROVED, f"NOWAIT 没拿到行锁（读到的还是 {got}）⇒ 写者没放锁"
    assert idle_in_transaction(read_engine) == [], "端点返回后仍有未结束的事务挂在池子里"


def test_the_read_endpoints_return_what_they_borrow(engines, seeded):
    """四批读请求（每批 3 发）走在 `pool_size=2 / max_overflow=0` 的池上 ⇒ 每一发都得把连接还回去。

    为什么不只测一发：漏掉 `close()` 时第一发照样成功，缺陷表现为"池子逐发少一条"。
    为什么数到 12 而不是恰好第 3 发：让"还了一条但没还干净"这种半修也过不去。
    去掉 `close()` 之后本文件有两处会红（这条与上一条），两个失效模式各有各的判据：
    一条量"挂着事务"，一条量"连接耗干"。
    """
    app_engine, read_engine = engines
    client = app_for(app_engine)
    ticket_id = create_ticket(client, seeded)

    codes = []
    for _ in range(4):
        codes.append(client.get("/approvals").status_code)
        codes.append(client.get(f"/approvals/{ticket_id}").status_code)
        codes.append(client.get(f"/approvals/{ticket_id}/events").status_code)

    assert codes == [200] * 12, f"2 条连接撑不住 12 发读请求 ⇒ 有连接没还：{codes}"
    assert idle_in_transaction(read_engine) == [], "读端点也漏连接：只修写路径的那种修法在这里会红"


# ---------------------------------------------------------------- 二、被拒的那一发：该留痕的留痕，不该落的不落

def test_a_rejected_duplicate_still_lands_its_audit_row(engines, seeded):
    """409 那一支**也要提交**：或签里后到的那一份得留一行 `applied=false` 的流水（FR-040）。

    提交点如果只写在成功分支里（"没批成就不算一次操作"这种直觉），这条就红。单据状态不许被
    第二次结论动过 —— 所以这里是"流水落地、状态不变"两半各断一次。
    """
    app_engine, read_engine = engines
    client = app_for(app_engine)
    ticket_id = create_ticket(client, seeded)

    first = resolve(client, ticket_id)
    assert first["status"] == 200, first

    again = resolve(client, ticket_id, version=0)
    assert again["status"] == 409, again
    assert again["code"] == repo.ALREADY_DECIDED, again

    assert read_state(read_engine, ticket_id) == ("APPROVED", 1), "被拒的第二次把已批准的单据改动了"
    assert read_events(read_engine, ticket_id) == [("approve", True), ("approve", False)], (
        "后到的那一份没留痕 ⇒ 审计面上读成只来了一次"
    )


def test_a_refused_principal_writes_nothing(engines, seeded):
    """反向对照（纪律 2）：不在审批者集合里的人打这一发 ⇒ 403，而库里**一个字都不许变**。

    上一条钉"该提交的提交了"，这一条钉"不该提交的没提交"。只写上一条的话，
    "每次调用都提交"这种修法也能全绿 —— 而它会把未授权的尝试写成事实。
    """
    app_engine, read_engine = engines
    client = app_for(app_engine, approvers=frozenset({APPROVER}))
    ticket_id = create_ticket(client, seeded)

    response = client.post(
        f"/approvals/{ticket_id}/resolve",
        json={"verdict": "approve", "version": 0, "channel": "CLI"},
        headers={api.PRINCIPAL_HEADER: "stranger-who-is-not-an-approver"},
    )
    assert response.status_code == 403, f"{response.status_code} {response.text[:200]}"
    assert read_state(read_engine, ticket_id) == ("PENDING", 0), "被 403 拒掉的那一发把单据改了"
    assert read_events(read_engine, ticket_id) == [], "被拒的尝试不该有流水"


def test_the_resume_callback_is_handed_a_row_that_is_already_committed(engines, seeded):
    """续跑者被调到的**那一刻**，另一条连接已经读得到 APPROVED（这是 owner 撞到的那一步的正解）。

    `graph_resumer` 是装配方注入的（T123）。这里注入一个"在回调里另开一条连接去读"的 resumer：
    提交点挪回 `_resume()` 之后，它按自己收到的 `feature_request_id` 读到的就是 PENDING ⇒ 本条红。
    断言放在回调之外：回调没被调到也算 `seen` 为空而红，不会静默通过。
    """
    app_engine, read_engine = engines
    client = app_for(app_engine)
    seen: list[object] = []

    def resumer(**kwargs) -> None:
        with read_engine.connect() as conn:
            row = conn.execute(
                text("select state::text, version from approval_ticket where feature_request_id = :r"),
                {"r": kwargs["feature_request_id"]},
            ).first()
        seen.append((kwargs["conclusion"], str(row[0]) if row else "<缺行>"))

    client.app.state.graph_resumer = resumer
    ticket_id = create_ticket(client, seeded)

    result = resolve(client, ticket_id)
    assert result["status"] == 200 and result["resumed_from"] == api.RESUMED_FROM, result
    assert seen == [("approve", "APPROVED")], f"续跑时另一条连接读到的不是已提交的结论：{seen}"


# ---------------------------------------------------------------- 三、结构性看守：边界只在这两处

def _commit_points(tree: ast.Module) -> dict[str, int]:
    """返回 {所在函数名: 该函数里 `.commit()` 的调用次数}。"""
    found: dict[str, int] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        count = sum(
            1
            for inner in ast.walk(node)
            if isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Attribute)
            and inner.func.attr == "commit"
        )
        if count:
            found[node.name] = count
    return found


def _finally_calls(tree: ast.Module, function: str) -> list[str]:
    """`function` 里每个 `try` 的 `finally` 块中调用的方法名（按出现顺序）。"""
    node = next(
        item
        for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == function
    )
    called: list[str] = []
    for item in ast.walk(node):
        if not isinstance(item, ast.Try) or not item.finalbody:
            continue
        for stmt in item.finalbody:
            for inner in ast.walk(stmt):
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
                    called.append(inner.func.attr)
    return called


def test_the_commit_points_are_exactly_three_and_the_store_always_closes():
    """提交边界只允许在 `resolve_approval` / `resolve_batch` / `create_approval` 三处，
    而 `_store` 出去一定 `close()`。

    这是一条**形状**看守，配合上面几发行为判据：行为判据量"这一笔落没落"，这条量"有没有人把边界
    挪走、或者给读端点也加一次提交"。读端点提交 = 把只读请求变成写事务的开始，那是 owner 那条
    `idle in transaction` 的另一半，所以也要拦。

    `resolve_batch`（2026-10-28 加）是**第三个**提交点，而它不是"新开一条写路径"：
    它逐单调用 `repository.resolve`（与单张端点同一个函数），每张单一个会话、一次提交 ——
    理由与 `resolve_approval` 一字不差（一批里第 7 张失败不该把前 6 张已成立的批准回滚掉）。
    数出第三处就等于确认了它没有偷懒复用 `create_approval` 的会话，也没有整批一个大事务。
    """
    tree = ast.parse(APP_SOURCE.read_text(encoding="utf-8"))
    commits = _commit_points(tree)
    assert commits == {"create_approval": 1, "resolve_approval": 1, "resolve_batch": 1}, (
        f"提交点的位置或次数变了：{commits}"
    )
    assert _finally_calls(tree, "_store") == ["close"], (
        f"_store 的 finally 里不该只有 close() 这一句：{_finally_calls(tree, '_store')}"
    )
