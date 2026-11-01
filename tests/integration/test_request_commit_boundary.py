"""Phase 15：`POST /requests` 的提交边界实锤 —— 201 之后**另一条连接**读得到那张需求单。

**为什么单开一个文件**（形状照 `test_approval_commit_boundary.py`，那一档是笔 U 撞出来的同一格）：
`test_request_intake.py` 整档靠"夹具开外层事务、结束时 `rollback()`"做隔离，而 SQLAlchemy 2.1.4 在
"会话绑到已 begin 的连接"这个形状下 `commit()` 发不到 PostgreSQL、`close()` 不动外层。
于是**摘掉 `_persist` 里那句 `commit()`，那一档照样全绿** —— 它量的是"发到连接了没有"，
量不到"发到库里了没有"。owner 在生产面上撞的正是后者：`serve` 给的是生产工厂（一次请求一个会话），
端点既不提交也不收尾 ⇒ 201 回了 `request_id`，PG 里 0 行，需求单从此不存在，看板与 `realrun` 全读不到。

本文件换形状：被测应用用**生产会话工厂**（`make_session_factory(app_engine)`，自己的池、真的提交），
断言一律走**另一个 engine 的另一条连接**。两个后果都认：

1. 写进库的行是**真提交**的 ⇒ 收尾必须按 `project_key` 删干净，否则下一发全量的 PG 档入口守卫
   （`tests/conftest.py` 的十张表）会红。
2. 它量的是"提交与借还"这一件事，量不到摘要形状、503 fail-closed、`clarification_waived` 三态 ——
   那些仍在 `test_request_intake.py`，两边不许互相冒充。

**本文件证明不到的**（明写，不留成"看起来全验了"）：
- 不证明补全是模型给的。这里注的是 `LocalPassthroughCompleter`，它一件都不补；
  FR-002 的实证仍归适配器那一支（响应里的 `completer_id` 就是为了让这一格在读数上可分）。
- 不证明 uvicorn 起的那个进程用的是新代码 —— 这里用 TestClient，`serve` 要重启才换实现。
- 不测 WSL 那一档（口径同全仓：只在 Windows 上跑过 PG 档）。
"""

from __future__ import annotations

import ast
import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from acp import serve
from acp.api.requests import router
from acp.config.loader import DSN_ENV, db_url
from acp.orchestrate.local_completer import COMPLETER_ID, LocalPassthroughCompleter
from acp.persistence.models_core import FeatureRequest
from acp.persistence.session import make_session_factory

pytestmark = pytest.mark.skipif(
    not os.environ.get(DSN_ENV), reason=f"{DSN_ENV} 未注入 ⇒ 本文件需要 PG（缺凭据时 skip 而不是假绿）"
)

#: 连接打这个标签，`pg_stat_activity` 才只数本文件的连接 —— 数全局会把别人的现场读成我们的泄漏
#: （本轮实测：owner 那发 `serve` 正挂着一条 `idle in transaction`）。
TAG = "acp-test-request-commit"
RAW = "做个介绍公司产品的静态网站，配色你看着办"

ENTRY_SOURCE = Path(__file__).resolve().parents[2] / "src" / "acp" / "api" / "requests.py"


@pytest.fixture(scope="session")
def engines() -> Iterator[tuple[Engine, Engine]]:
    """两个 engine：一个给被测应用（小池 + 短超时，连接不还就在 2 秒内暴露），一个只用来读。

    `max_overflow=0` 是有意的：默认"池 5 + 溢出 10"会把"每发请求漏一条连接"这种缺陷推到第 16 发
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
    read_engine = create_engine(db_url(), future=True, connect_args={"application_name": f"{TAG}-read"})
    try:
        yield app_engine, read_engine
    finally:
        app_engine.dispose()
        read_engine.dispose()


@pytest.fixture
def clean(engines):
    """每个用例一组**独占的** `project_key`；收尾按键删，并自证删干净了。

    清理挂在**读端那个 engine** 上，不挂应用那个：应用侧的池被故意做小，变异对照里"连接不还"
    那一发会把清理一起卡死（笔 U 实测留过 3 行在库里）。清理不能依赖被测对象的健康度。
    """
    _app_engine, read_engine = engines
    keys: list[str] = []

    def new_key(label: str) -> str:
        key = f"acp-p15-{label}-{uuid.uuid4().hex[:8]}"
        keys.append(key)
        return key

    yield new_key

    factory = make_session_factory(read_engine)
    with factory() as session:
        for key in keys:
            session.execute(text("delete from feature_request where project_key = :k"), {"k": key})
        session.commit()
    with read_engine.connect() as conn:
        left = sum(
            int(
                conn.execute(
                    text("select count(*) from feature_request where project_key = :k"), {"k": key}
                ).scalar()
            )
            for key in keys
        )
    assert left == 0, f"本用例留下了 {left} 行需求单（按 project_key 圈定删不干净 = 判据自己有缺陷）"


def app_for(app_engine: Engine, *, completer: object = ...) -> FastAPI:
    """生产形状的应用：会话工厂走 `make_session_factory`（真池、真提交），补全器给本机旁路。"""
    app = FastAPI()
    app.include_router(router)
    app.state.session_factory = make_session_factory(app_engine)
    app.state.completer = LocalPassthroughCompleter() if completer is ... else completer
    return app


def intake(client: TestClient, project_key: str, *, raw_input: str = RAW):
    return client.post("/requests", json={"project_key": project_key, "raw_input": raw_input})


def read_row(read_engine: Engine, project_key: str) -> FeatureRequest | None:
    """**另一条连接**上的现值。查不到就回 `None`，让断言报出差异而不是抛 IndexError。"""
    with Session(read_engine) as session:
        return session.query(FeatureRequest).filter_by(project_key=project_key).one_or_none()


def idle_in_transaction(read_engine: Engine) -> list[tuple[str, str]]:
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


# ---------------------------------------------------------------- 一、实锤：另一条连接读得到 EN-01


def test_the_committed_request_is_visible_on_a_second_connection(engines, clean):
    """owner 工作单第 3 条的后半（"确保落库 commit"）：201 之后换一条连接去查，行必须在。

    四个读数按顺序钉，缺一格就放过了别的缺陷形态：
    ① 响应 201 且带回 `request_id`；② 另一条连接**读得到这一行**（摘掉 commit 就在这里红）；
    ③ 库里那一行的原文与请求**逐字**一致（入口不许顺手"归一化"）；④ 响应体自述的 `status`
    与库里读到的是同一个值 —— 响应不能自述一个库里没有的态。
    """
    app_engine, read_engine = engines
    key = clean("single")
    body = intake(TestClient(app_for(app_engine)), key).json()

    row = read_row(read_engine, key)
    assert row is not None, "接口回了 201 而另一条连接读不到 ⇒ 提交边界断了（owner 撞的原缺陷形状）"
    assert str(row.id) == body["request_id"]
    assert row.raw_input == RAW, "库里存的已经不是使用者说的那句话"
    assert str(row.status) == body["status"] == "INTAKE"
    assert row.summary_digest == {"自行补全项": [], "澄清问题": []}


def test_the_endpoint_returns_the_connection_it_borrowed(engines, clean):
    """四发起单走 `pool_size=2 / max_overflow=0` 的池 ⇒ 每一发都得把连接还回去，且不留未结束事务。

    为什么不只测一发：漏掉收尾时第一发照样成功，缺陷表现为"池子逐发少一条"。
    为什么数到 4 而不是恰好第 3 发：让"还了一条但没还干净"这种半修也过不去。

    **这条的限度（2026-10-22 变异实测，别把它能挡的东西说大）**：摘掉 `_persist` 的
    `finally: session.close()` 那一发，**这一条在本机没红** —— `_persist` 返回后那个会话没有别的引用，
    CPython 的引用计数当场回收并释放连接，于是"连接没还"这个失效形态在行为面上**不可复现**
    （换个不立刻回收的运行时就会红）。真正挡住 M2 的是另外两条：本文件的形状看守
    `test_the_commit_boundary_lives_in_the_persister_and_nowhere_else` 与 fast 档的
    `test_assembly.py::test_the_endpoint_and_the_intake_helper_build_the_same_row`（假会话数 `close()` 的次数）。
    摘掉 `commit()` 那一发倒是这条与上一条**各红一次**（M1 实测）—— 两个失效模式各有各的那一发。
    """
    app_engine, read_engine = engines
    client = TestClient(app_for(app_engine))
    keys = [clean(f"pool{index}") for index in range(4)]

    codes = [intake(client, key).status_code for key in keys]
    assert codes == [201] * 4, f"2 条连接撑不住 4 发起单 ⇒ 有连接没还：{codes}"
    assert idle_in_transaction(read_engine) == [], "端点返回后仍有未结束的事务挂在池子里"
    assert [read_row(read_engine, key) for key in keys].count(None) == 0, "池子撑住了但行没落库"


# ---------------------------------------------------------------- 二、装配点：旁路是谁注进来的


def test_the_launcher_injects_the_local_completer_and_can_be_overridden(tmp_path):
    """`serve.build_app` 默认注本机旁路，而**传对象就用传的** —— 两半各钉一件事。

    只断"默认有注"会在有人把旁路写进 `plan.py` 当节点默认值时也绿（那条禁令正是 `plan.py`
    模块头点名不许犯的）；只断"能覆盖"则放过了"生产面永远 503"这一格。
    这一发不起服务、不发请求：`build_app` 会建一个真引擎，所以收尾必须 `dispose()`，
    否则解释器退出时 psycopg 的 `__del__` 会按 GC 时序抖出 ResourceWarning（口径同各 PG 档夹具）。
    """
    app = serve.build_app(dist=None, bus_root=tmp_path)
    try:
        completer = getattr(app.state, "completer", None)
        assert isinstance(completer, LocalPassthroughCompleter), (
            "装配点没注补全器 ⇒ `POST /requests` 在生产面上还是 503（owner 要修的就是这一格）"
        )
        assert completer.completer_id == COMPLETER_ID, "补全器自报的身份与常量不一致"
    finally:
        app.state.session_factory.kw["bind"].dispose()

    class Other:
        completer_id = "other-completer"

        def complete(self, raw_input):  # pragma: no cover - 只验装配，不跑这一发
            raise AssertionError("本用例不发请求")

    overridden = serve.build_app(dist=None, bus_root=tmp_path, completer=Other())
    try:
        assert overridden.state.completer.__class__.__name__ == "Other", "默认值把注入顶掉了"
    finally:
        overridden.state.session_factory.kw["bind"].dispose()


# ---------------------------------------------------------------- 三、结构性看守：边界只在这一处


def _call_attr_names(tree: ast.Module, function: str, attr: str) -> int:
    node = next(item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == function)
    return sum(
        1
        for inner in ast.walk(node)
        if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute) and inner.func.attr == attr
    )


def _finally_calls(tree: ast.Module, function: str) -> list[str]:
    node = next(item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == function)
    called: list[str] = []
    for item in ast.walk(node):
        if not isinstance(item, ast.Try) or not item.finalbody:
            continue
        for stmt in item.finalbody:
            for inner in ast.walk(stmt):
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
                    called.append(inner.func.attr)
    return called


def test_the_commit_boundary_lives_in_the_persister_and_nowhere_else():
    """提交边界只许在 `_persist` 里那一个点：一次 `commit`、异常路径一次 `rollback`、`finally` 一次 `close`。

    这是**形状**看守，与上面那两发行为判据配成一对：行为判据量"这一笔落没落库"，
    这条量"有没有人把边界挪走、或者在别处又加了一次提交"。第二个提交点出现的典型形态是
    "顺手在 `create_request` 里也 commit 一下" —— 那时 fail-closed 的 503 分支也可能已经把半行发出去。
    """
    tree = ast.parse(ENTRY_SOURCE.read_text(encoding="utf-8"))
    assert _call_attr_names(tree, "_persist", "commit") == 1, "提交点不该是 0 也不该是 2"
    assert _call_attr_names(tree, "_persist", "rollback") == 1, "半途失败不回滚 = 脏会话还池子"
    assert _finally_calls(tree, "_persist") == ["close"], (
        f"`_persist` 的 finally 里只该有 close() 这一句：{_finally_calls(tree, '_persist')}"
    )
    assert _call_attr_names(tree, "create_request", "commit") == 0, "第二个提交点出现在 handler 里"
