"""MVP-2 笔 J 的判据：owner 2026-10-19 两条裁决里的**启动面**那一面（W-9 选甲）。

**本文件零库、零出网**，只量"启动面把名单送到哪儿"这一件事。
状态码那一对（403 / 503）不在这里量，落点是 `tests/integration/test_autonomy_api.py` 的节 五 ——
不是省事，是量不了：`persistence/autonomy.py::_guard_updated_by` 的真实顺序是
`mode` 域检 → **先读 EN-03 的 agent 名单**（`_agent_ids(session)`）→ 才轮到 `machine_principals`
那一道 → 最后才 `session.get(...)`。所以"配了名单 ⇒ 403"这一发**必然要有一次库读**，
拿会话替身去跑只会读到替身自曝的那条 `AssertionError`（本轮实测过，见台账）。

分工（三处各量一件，互不重复）：

- `test_board_stream.py` 量 `serve.py` 的**绑址与静态面**（含 `--approver` 那一支的落位形状）；
- `test_transport_spec_factory.py` 量**工厂与 §12 那份载体**（"调用点恰一处 = 驱动"那一格已搬到
  `test_realrun_driver.py::test_the_factory_has_exactly_one_production_caller_and_it_is_the_driver`）；
- 本文件量**一件**：启动面的 `--machine-principal` 落到 `app.state`，且"没配"在 state 上保持为"没这个属性"
  （不是 `None`、不是空集合）。

**这一支曾经量的第二件事已经搬走了**：笔 J 那一版 `realrun.py` 是"打印 TODO 就返回 2"的骨架，
当时用四条 AST/退出码判据钉住"骨架没接线"。B 计划第二步（笔 P）把它换成真驱动之后，那四条的**前件没了** ——
逐条去向记在这里而不是删掉就完事：
`test_the_skeleton_says_the_owners_words_and_refuses` 与 `test_the_skeleton_needs_no_dsn`
→ 由 `tests/guards/test_realrun_driver.py` 的 `test_a_usage_error_leaves_nothing_on_disk` /
`test_the_missing_dsn_is_named_and_no_vendor_is_blamed` 接替（量的对象从"骨架不说谎"变成"入口归因不说谎"）；
`test_the_skeleton_wires_nothing_yet` 与 `test_the_factory_still_has_no_caller_after_the_skeleton`
→ 由同文件的 `test_the_driver_constructs_vendor_transports_only_through_the_factory` /
`test_the_factory_has_exactly_one_production_caller_and_it_is_the_driver` 接替，方向反过来：
**那时要证"没有调用点"，现在要证"调用点恰一处且只能是驱动"**。
R-07 那道门仍然在本文件之外被钉着（`test_realrun_driver.py::test_the_frozen_segments_still_pass_the_door...`）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from acp import serve
from acp.api import approvals

MODE_URL = "/autonomy"
MISSING = object()

#: P1-12：`serve.main` 现在给 app 装了 `TrustedHostMiddleware`（只放环回 Host）。
#: `TestClient(app)` 的默认 base_url 是 `http://testserver`，那一发会被 Host 校验挡在 400，
#: 本文件量的"旗标送到哪儿"就永远读不到。改成环回地址，让请求与生产面同一个 Host。
#: **不往白名单里加 `testserver`**：生产名单上多一个测试用主机名，守卫就从"只放环回"变成"只放环回 + 一个测试名"。
LOOPBACK = "http://127.0.0.1"


class RegistryRead(Exception):
    """替身自曝用的哨兵：会话被拿去读 EN-03 名单时抛它，而不是抛 `AssertionError`。

    为什么要单独一个类型：本节三条判据里，"拒在任何库读之前"（503）与"已经走到库读那一步"
    （配了名单）是两种相反的形状，而它们在 HTTP 面上都能读成"这发出错了"。用一个只在
    `scalars()` 上抛的哨兵，两种形状就分得开，而且不必假装替身能回答"库里有哪些 agent"。
    """


class FakeSession:
    """一枚只回答"你有没有拿我查过库"的会话替身。

    它**不**回答任何数据 —— 回答了就等于在 guards 档伪造出 EN-03 的内容，那既是假绿，
    又把 403 这一发变成了"替身说了算"。所以 `close()` 照记、`scalars()` 照抛。
    """

    def __init__(self) -> None:
        self.queries: list[str] = []
        self.closed = False

    def scalars(self, *args: object, **kwargs: object):
        self.queries.append("scalars")
        raise RegistryRead("会话被拿去读 EN-03 的 agent 名单")

    def get(self, *args: object, **kwargs: object):
        self.queries.append("get")
        raise RegistryRead("会话被拿去读 EN-16 的档位行")

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def served(tmp_path, monkeypatch):
    """启动面替身：`serve.main` 走真参数解析，只把会话工厂换成 `FakeSession`。

    `--dist` 指一个空目录之外什么都不给 ⇒ 想验静态面就去 `test_board_stream.py`，这里只关心 `app.state`。
    `make_session_factory` 是 `serve.py` 里的模块属性，替换它而不是替换 `db_url()`：
    真工厂在没有 `ACP_DB_DSN` 时当场抛（本轮实测），而 guards 档不许连库。
    """
    made: list[FakeSession] = []

    def factory():
        session = FakeSession()
        made.append(session)
        return session

    monkeypatch.setattr(serve, "make_session_factory", lambda: factory)

    def launch(*flags: str):
        captured: dict[str, object] = {}

        def runner(app, **kwargs):
            captured["app"] = app
            return None

        code = serve.main(["--api-only", "--dist", str(tmp_path), "--port", "8799", *flags], runner=runner)
        assert code == 0 and "app" in captured, "启动面没把 app 交给 runner ⇒ 下面断的 state 不是生产装配出来的那份"
        return captured["app"], made

    return launch


# ------------------------------------------------------------------ 一、W-9 选甲：注入位与"没配"的形状


def test_the_launcher_flag_lands_on_app_state(served):
    """`--machine-principal` 可重复，落到 `app.state.machine_principals` 的是**去重后的集合**。

    笔 I 那发的实测就是栽在"这一格没人送"：`grep -c machine_principals src/acp/serve.py` 读 0，
    于是 `--as-agent` 打到真服务只读得到 503（`quickstart.md` §5 ③ 那格登记的 W-9）。
    这一条把"送到了"钉住；`create_app` 的"给了才放"语义由下一负责。
    """
    app, _ = served("--machine-principal", "qoder", "--machine-principal", "doubao")
    assert app.state.machine_principals == frozenset({"qoder", "doubao"})

    app, _ = served("--machine-principal", "qoder", "--machine-principal", "qoder")
    assert app.state.machine_principals == frozenset({"qoder"}), "同一个身份写两遍就变成两个 ⇒ 名单是集合不是计数"


def test_the_launcher_without_the_flag_leaves_no_attribute(served):
    """**不给旗标就没有这个属性** —— 不是 `None`、不是空集合。

    三种"没配"在端点上读起来都是 503（`_machine_principals` 那句 `if not declared`），
    但在 `app.state` 上是三种形状：属性缺失 = 启动的人没说过话；空集合 = 启动的人说了"一台机器都没有"。
    后者是真实可区分的（`--machine-principal` 写成一个空值那种），把它读成前者就会给漏配找一个替罪羊。
    """
    app, _ = served()
    assert getattr(app.state, "machine_principals", MISSING) is MISSING, "启动面擅自给了个默认 ⇒ 503 的归因从'漏配'变成'配了个空'"


def test_the_served_app_without_the_flag_refuses_before_any_query(served):
    """同一发请求打在**启动面装配的** app 上：503，且一次库读都没发生。

    这里是"生产装配点"三个字第一次可验的地方 —— 以前只有用例里手搓的 `FastAPI()` 能配这一项。
    断两半：状态码 + 错误码逐字，以及替身记到的查询列表为空（`fact refusal 早于任何 select`）。
    """
    app, made = served()
    client = TestClient(app, base_url=LOOPBACK)
    response = client.patch(
        MODE_URL,
        json={"mode": "FULL", "project_key": "probe-j"},
        headers={approvals.PRINCIPAL_HEADER: "qoder"},
    )
    assert response.status_code == 503, response.text[:200]
    assert "machine_principals_not_configured" in response.text, response.text[:200]
    assert [session.queries for session in made] == [[]], "这一发走到了库读 ⇒ 503 不再是'漏配'那一格"


def test_the_served_app_with_the_flag_reaches_the_guard(served):
    """反向半边：**带**该参数 ⇒ 同一发不再被"漏配"挡住，而是进到守卫里去查 EN-03 名单。

    这一条刻意不断 403（那要真库，归 `tests/integration/test_autonomy_api.py` 节 五），
    它只断"旗标改变了请求的前进深度"这一件事：没配 = 停在 `_machine_principals`，
    配了 = 走到 `_agent_ids`。少了这一发，上面那条 503 就只是"这条端点对谁都拒"的同义反复。
    """
    app, made = served("--machine-principal", "qoder")
    client = TestClient(app, base_url=LOOPBACK)
    with pytest.raises(RegistryRead):
        client.patch(
            MODE_URL,
            json={"mode": "FULL", "project_key": "probe-j"},
            headers={approvals.PRINCIPAL_HEADER: "qoder"},
        )
    assert [session.queries for session in made] == [["scalars"]], "名单变了却没换成一次真读 ⇒ 旗标只是装饰"
