"""T123 装配点（其一）：HTTP 面 `create_app` 与 EN-01 建行处的判据。

**这一文件为什么不连库**：装配面要证的都是"形状"——挂了什么、没挂什么、缺注入时拒不拒、
建行只有一处。这些全在 import 期与 AST 期量得出来。真跑 PG 的那几条归 `tests/e2e/`（R-07 那一发）。
缺 `ACP_DB_DSN` 时本文件**照跑**，这也是它存在的理由之一：装配面的回归不该等数据库。

五节分工（一节一件事，别把两节的断言混成一条）：

- 节 一 = 挂载面：六支公开 router 全在、那一条内部端点不在、签名里没有能挂它的参数、
  `include_router` 在 src 里恰有一处，`src/acp/api/` 里新增 router 而挂载面没跟上要红。
- 节 二 = 空注入仍 fail-closed：给了才落 `app.state`，没给就由各端点自己 503 并点名变量名。
- 节 三 = EN-01 建行只有一处，且 HTTP 与 CLI 两支构造同形。
- 节 四 = 那一行的初始态必须等于图所认的前置态（跨实现符合性，两处常量各写一份就会漂）。
- 节 五 = 适配器层的依赖方向：`adapters/**` 里没有任何一份 import `acp.orchestrate`
  （owner 2026-10-16 补充约束 1，与 T045 的零耦合证明同源）。

③ 反向对照贯穿全文件：**纯措辞改动不许红**（节 一 那条 `include_router` 计数与节 四 那条同值断言
都是为它准备的对照面 —— 它红了就是判据自己在盯正文措辞，那是自指，不是判据）。
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from acp import app as app_module
from acp.api import approvals
from acp.orchestrate import intake
from acp.orchestrate import graph as orchestrate_graph
from acp.orchestrate.nodes.plan import (
    CLARIFICATION_KEY,
    SELF_COMPLETED_KEY,
    CompleterOutcome,
    is_clarification_waived,
)
from acp.persistence.models_core import FeatureRequest

SRC_ROOT = pathlib.Path(__file__).resolve().parents[2] / "src" / "acp"
API_DIR = SRC_ROOT / "api"

RAW = "做个介绍公司产品的静态网站，配色你看着办"
COMPLETIONS = ("页面数量取 1 个首页",)
CLARIFICATIONS = ("联系方式用邮箱还是表单？",)


class RecordingCompleter:
    """注入用的补全器：只回声，不判定。它的存在就是"这里没有真总管"的证据，不是"规则版够用"。"""

    def __init__(self, outcome: CompleterOutcome | None = None) -> None:
        self.outcome = outcome or CompleterOutcome(COMPLETIONS, CLARIFICATIONS)
        self.seen: list[str] = []

    def complete(self, raw_input: str) -> CompleterOutcome:
        self.seen.append(raw_input)
        return self.outcome


def source_of(rel_path: pathlib.Path) -> ast.Module:
    return ast.parse(rel_path.read_text(encoding="utf-8"))


def calls_of_name(tree: ast.Module, name: str) -> list[ast.Call]:
    """按**被调用者的名字**取 Call 节点。类定义那一行不是 Call，所以不会混进来。"""
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == name)
            or (isinstance(node.func, ast.Attribute) and node.func.attr == name)
        )
    ]


# ---------------------------------------------------------------- 一、挂载面（公开四支、内部零支）


def test_the_factory_mounts_every_router_defined_under_the_api_package():
    """挂载面 = `src/acp/api/` 里所有**公开** `router` 对象，一个不多一个不少。

    数量与名字都是从文件系统 + AST 现读的，不是我抄的常量：新增一支 `api/*.py` 而没进
    `PUBLIC_ROUTERS` 就会在这里红（那正是"路由写了但没挂"的形态，端点测试全绿而对外不存在）。
    """
    found: dict[str, str] = {}
    for path in sorted(API_DIR.glob("*.py")):
        tree = source_of(path)
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "router":
                        found[path.stem] = path.stem
    mounted = dict(app_module.PUBLIC_ROUTERS)
    assert set(mounted) == set(found), f"挂载面与 api/ 里的公开 router 不齐：{sorted(set(found) ^ set(mounted))}"
    assert len(mounted) == 6, (
        f"公开 router 从 6 支变成 {len(mounted)} 支 ⇒ 要么加了面要么掉了面：{sorted(mounted)}"
        "（这一格是**故意的绝对数**：上面那条集合相等已经保证'加了文件就得同时加挂载'，"
        "这个数管的是'对外面变宽'这件事必须以一次显式的改判据为代价发生，不能顺手带过）"
    )


def test_the_openapi_surface_has_no_public_ticket_creation_path():
    """对外的 OpenAPI 路径集里没有 `POST /approvals`，而 `internal_router` 里确实有这一条。

    两半各钉一件事（口径同 `test_approval_api.py` 那条同名判据）：只断"公开面没有"，
    会在"建单端点压根没实现"时也成立；补上"内部 router 里有"才把两件事分开。
    """
    paths = app_module.create_app().openapi()["paths"]
    assert "post" not in paths.get("/approvals", {}), f"内部建单端点漏进了对外面：{sorted(paths)}"
    internal_paths = {r.path for r in approvals.internal_router.routes}
    assert "/approvals" in internal_paths, f"内部 router 里没有建单 ⇒ 上面那句'不在'就成了空对空：{sorted(internal_paths)}"


def test_no_argument_of_the_factory_can_mount_the_internal_router():
    """签名里没有布尔参数、没有任何名字含 `internal` 的参数。

    这条钉的是"结构而不是默认值"：给了一个 `expose_internal=False` 的开关，
    `contracts/approval-api.md §1` 那句"外部不直调"就从不可表示降级成"默认没按"。
    """
    signature = inspect.signature(app_module.create_app)
    names = list(signature.parameters)
    assert set(names) == set(app_module.INJECTED_KEYS), f"注入面多了东西或少了东西：{sorted(names)}"
    assert all(p.kind == p.KEYWORD_ONLY for p in signature.parameters.values()), "注入项必须全是关键字参数"
    assert not [n for n in names if "internal" in n.lower()], f"签名里出现了能碰内部面的参数：{names}"
    assert not [
        n
        for n, p in signature.parameters.items()
        if p.annotation is bool or p.default is True or p.default is False
    ], f"装配面带布尔开关 ⇒ 禁令从结构降级成默认值：{names}"


def test_include_router_appears_exactly_once_in_the_production_tree():
    """`include_router` 在整个 `src/acp/` 里恰有一处调用（就是本工厂那一行）。

    两处装配点 = 两支 app，而"对外挂了哪几支"这件事就会有两个答案 —— 判据读到的是其中一个，
    另一个在跑。这条也是反向对照的锚：**改 app.py 的措辞（不动这一行）不许红**。
    """
    hits = [
        path.relative_to(SRC_ROOT).as_posix()
        for path in sorted(SRC_ROOT.rglob("*.py"))
        if calls_of_name(source_of(path), "include_router")
    ]
    assert hits == ["app.py"], f"装配点不止一处（或本工厂不再装配）：{hits}"


# ---------------------------------------------------------------- 二、空注入仍然 fail-closed


def test_a_bare_app_refuses_by_naming_the_missing_injection():
    """一个注入都不给 ⇒ 各端点 503 并点名要注入的键，而不是 200 或 500。

    参数化把**需要库的那几支** router 各自那个"最先撞上缺省"的端点都跑一遍：少跑一支就是"那支没人验过"。
    第六支 `growth` 不在这张表里，因为它不碰库 —— 它"没给身份就拒"那条形状归
    `tests/guards/test_growth_api.py`，硬塞进这里只会把两件事读成一件事。
    `/events/stream` 那一发验的是"**503 在流开始之前**"——流一旦开始，状态码就发出去了，
    那时才发现缺注入只能留一条 200 的空流给客户端（`api/web.py` 模块头第 3 条）。
    """
    client = TestClient(app_module.create_app())
    cases = [
        ("post", "/requests", {"project_key": "demo", "raw_input": RAW}, "completer_not_configured"),
        ("get", "/approvals", None, "store_not_configured"),
        ("get", "/events", None, "store_not_configured"),
        ("get", "/events/stream", None, "store_not_configured"),
    ]
    for method, url, body, key in cases:
        response = client.request(method, url, json=body) if body else client.request(method, url)
        assert response.status_code == 503, f"{method} {url} 读到 {response.status_code} ⇒ 缺注入不该变成成功"
        assert key in response.text, f"{method} {url} 的 503 没点名 {key}：{response.text[:120]}"


def test_injection_lands_on_app_state_only_when_given():
    """给了才落 `app.state`，没给就**没有这个属性** —— 不是落成 `None`。

    落成 None 会让各端点那句 `getattr(state, key, None)` 之外的写法（直接属性访问）读到 None
    而不是 AttributeError，也就把"没装配"读成"装配了一个空值"。
    """
    bare = app_module.create_app()
    assert [k for k in app_module.INJECTED_KEYS if hasattr(bare.state, k)] == []
    marker = object()
    wired = app_module.create_app(session_factory=marker)
    assert hasattr(wired.state, "session_factory")
    assert [k for k in app_module.INJECTED_KEYS if hasattr(wired.state, k)] == ["session_factory"]


class _CountedSession:
    """只会记"被关了几次"的假会话。

    本条判据要的那一格是审批者缺省，会话本身用不上；但 M2-W11 之后端点 MUST `close()` 它借到的
    每一个会话（`api/approvals.py::_store`），旧写法 `session_factory=lambda: None` 会炸在
    `NoneType.close()` ⇒ 把 503 顶成 500。给一个接得住 `close()` 的替身，并把"被关过"拿来当第二条断言。
    """

    def __init__(self) -> None:
        self.closes = 0

    def close(self) -> None:
        self.closes += 1


def test_an_injected_store_without_approvers_still_refuses_the_resolve_call():
    """注了会话不等于审批面开门：审批者集合仍缺 ⇒ 503 `approvers_not_configured`。

    这条防的是"装配点顺手给个空集合"：`_approvers` 把"集合为空"一律拒（503）而不是读成"谁都能批"。
    顺序是实测的：`resolve_approval` 先进 `_store`（借会话）、再在实参求值序里 `_principal`（缺头 403）、
    最后 `_approvers`（503）⇒ 这里带上请求头，让那一发真的走到审批者这一格。

    第二轮加的是收尾那半边：**被拒的那一发也要把会话还回去** —— 4xx 不是不泄漏连接的理由。
    打两发而不是只数 `closes == 1`：一发里"同一个会话被关两次"和"两发各关自己那次"读出来是同一个数，
    必须断对象互不相同才分得开。
    """
    held: list[_CountedSession] = []

    def factory() -> _CountedSession:
        borrowed = _CountedSession()
        held.append(borrowed)
        return borrowed

    client = TestClient(app_module.create_app(session_factory=factory))
    bodies = []
    for _ in range(2):
        bodies.append(
            client.post(
                f"/approvals/{uuid.uuid4()}/resolve",
                json={"verdict": "approve", "version": 0, "channel": "DASHBOARD"},
                headers={"X-ACP-Principal": "operator"},
            )
        )
    for response in bodies:
        assert response.status_code == 503, (
            f"读到 {response.status_code} ⇒ 审批者缺省被读成了放行：{response.text[:160]}"
        )
        assert "approvers_not_configured" in response.text, response.text[:160]

    assert len(held) == 2, f"两发请求借到的会话数不是 2：{len(held)}"
    assert held[0] is not held[1], "两发共用了同一个会话 ⇒ 一次请求一个会话没有成立"
    assert [session.closes for session in held] == [1, 1], (
        f"各会话被关次数（应为每发恰好一次）：{[session.closes for session in held]}"
    )


# ---------------------------------------------------------------- 三、EN-01 建行只有一处


def test_the_request_row_is_constructed_in_exactly_one_production_module():
    """`FeatureRequest(...)` 的构造调用在全 src 里恰有一处，且它在 `orchestrate/intake.py`。

    类定义那一行（`class FeatureRequest(Base)`）不是 Call 节点，所以不会误计 —— 这条判据
    量的确实是"建行"。owner 2026-10-16 裁"允许抽取"的那一动的看守就是它。
    """
    hits = [
        path.relative_to(SRC_ROOT).as_posix()
        for path in sorted(SRC_ROOT.rglob("*.py"))
        if calls_of_name(source_of(path), "FeatureRequest")
    ]
    assert hits == ["orchestrate/intake.py"], f"EN-01 的建行不止一处：{hits}"


def test_the_endpoint_and_the_intake_helper_build_the_same_row():
    """同一条需求走 HTTP 端点与走 `intake.new_request()`，两个构造在四列上一致。

    这是"抽取之后行为一字不变"那条前置的**符合性**部分：HTTP 那侧的响应只回三个字段，
    抽出来之后如果 status / waived / digest 有任何一列算法变了，只有这条能发现（各测各的都不红）。
    顺手把**边界动作**也数了（`commit` 恰一次、`close` 恰一次）：这一档不带 DSN 也跑，
    所以"有人把提交点摘掉"这种改法在没有 PG 的机器上照样会红，而不是等到 PG 档才发现。
    """
    app = FastAPI()
    app.include_router(_requests_router())
    app.state.completer = RecordingCompleter()
    captured: list[FeatureRequest] = []
    sessions: list[_RecordingSession] = []

    def factory() -> _RecordingSession:
        session = _RecordingSession(captured)
        sessions.append(session)
        return session

    app.state.session_factory = factory
    response = TestClient(app).post("/requests", json={"project_key": "demo", "raw_input": RAW})
    assert response.status_code == 201, response.text[:160]
    from_http = captured[0]
    assert len(sessions) == 1, "一次请求借了不止一个会话"
    assert (sessions[0].commits, sessions[0].closes) == (1, 1), (
        f"提交与收尾各该恰一次，实际 commit={sessions[0].commits} close={sessions[0].closes}"
    )

    from_intake = intake.new_request(
        project_key="demo",
        raw_input=RAW,
        summary_digest=from_http.summary_digest,
        clarification_answers=[],
    )
    assert str(from_http.status) == str(from_intake.status)
    assert from_http.clarification_waived == from_intake.clarification_waived
    assert from_http.summary_digest == from_intake.summary_digest
    assert from_http.project_key == from_intake.project_key


def test_a_failure_at_the_commit_point_rolls_back_and_still_closes():
    """提交那一拍失败 ⇒ `rollback` 一次、`close` 一次、响应不是 201。

    这是 `_persist` 的**异常分支**：防御分支零用例等于没钉。摘掉 `except: rollback` 时这条红
    （脏会话还回池子，下一发请求在那团脏状态上继续写）；摘掉 `finally: close` 时同样红。
    `TestClient` 默认把服务端异常原样抛出，所以这里用 `pytest.raises` 而不是断状态码。
    """
    app = FastAPI()
    app.include_router(_requests_router())
    app.state.completer = RecordingCompleter()
    sessions: list[_RecordingSession] = []

    def factory() -> _RecordingSession:
        session = _RecordingSession([], fail_on_commit=True)
        sessions.append(session)
        return session

    app.state.session_factory = factory
    with pytest.raises(RuntimeError):
        TestClient(app).post("/requests", json={"project_key": "demo", "raw_input": RAW})
    assert len(sessions) == 1
    assert (sessions[0].commits, sessions[0].rollbacks, sessions[0].closes) == (1, 1, 1)


def test_the_waiver_is_read_from_the_plan_module_not_recomputed():
    """`clarification_waived` 只可能来自 `plan.is_clarification_waived` —— 建行处不复写第二份规则。

    两侧各取一个真值：有澄清问题且没答 ⇒ 放行标志为真（FR-003 的不阻塞），
    没有澄清问题 ⇒ 也是真但理由不同，这两形态在 `plan.py` 里已被钉过，这里只核"intake 用的是它"。
    """
    digest = {SELF_COMPLETED_KEY: list(COMPLETIONS), CLARIFICATION_KEY: list(CLARIFICATIONS)}
    row = intake.new_request(project_key="demo", raw_input=RAW, summary_digest=digest)
    assert row.clarification_waived == is_clarification_waived(digest, [])
    answered = intake.new_request(
        project_key="demo", raw_input=RAW, summary_digest=digest, clarification_answers=["邮箱"]
    )
    assert row.clarification_waived is not None
    assert answered.clarification_waived == is_clarification_waived(digest, ["邮箱"])


def test_the_row_is_not_written_to_any_session_by_the_helper():
    """`new_request()` 只造对象：它不该出现在任何会话方法上（提交点归装配方）。"""
    row = intake.new_request(project_key="demo", raw_input=RAW, summary_digest={})
    assert isinstance(row, FeatureRequest)
    assert inspect.signature(intake.new_request).parameters["request_id"].default is None
    fresh = intake.new_request(project_key="demo", raw_input=RAW, summary_digest={})
    assert row.id != fresh.id, "不给 id 时每次调用都该得到新行（幂等归 id，不归缓存）"


# ---------------------------------------------------------------- 四、跨实现的初始态一致


def test_the_initial_status_is_the_one_the_graph_advances():
    """建行处的初始态 MUST 等于图认的那个可推进态，否则建出来的单推不动。

    两个常量分处两个模块（`intake.INITIAL_STATUS` / `graph.ADVANCEABLE`），这条是它们之间
    唯一的连线：任何一侧被单独改掉，这里先红 —— 而不是等到端到端那一发才发现"库里有一行没人接"。
    """
    assert intake.INITIAL_STATUS == orchestrate_graph.ADVANCEABLE, (
        f"建行写 {intake.INITIAL_STATUS}、图只推进 {orchestrate_graph.ADVANCEABLE} ⇒ 造出推不动的需求单"
    )


def test_the_supplied_id_and_clock_are_honoured_not_overridden():
    """给了 `request_id` / `created_at` 就用给的（重放类判据要靠它才能钉住同一发）。"""
    fixed_id = uuid.uuid4()
    import datetime as dt

    fixed = dt.datetime(2026, 10, 16, 12, 0, tzinfo=dt.timezone.utc)
    row = intake.new_request(
        project_key="demo", raw_input=RAW, summary_digest={}, request_id=fixed_id, created_at=fixed
    )
    assert row.id == fixed_id
    assert row.created_at == fixed


# ---------------------------------------------------------------- 五、适配器层的依赖方向（owner 2026-10-16 补充约束 1）


def test_no_adapter_module_imports_the_orchestrate_layer():
    """`src/acp/adapters/**` 里任何一份都不许 import `acp.orchestrate`。

    owner 2026-10-16 裁"仿真总管放装配点 `run.py`、`adapters/stub.py` 只装 transport"时点名要钉这条，
    理由是**今天没违反**（现跑扫全目录：adapters 的导入面只有 `acp.bus.*` / `acp.protocol*` /
    `acp.registry.loader` / `acp.config.loader` / `acp.adapters.base`），而"没违反"不等于"不许违反" ——
    一旦有人把仿真补全/拆单挪进 `adapters/`，就会出现**第一条** adapters → orchestrate 边，
    而 T045 那套"删掉一支目录，编排图仍可构建"的零耦合证明默认的就是这个方向不存在。

    判据**扫整个目录而不只看 `stub.py`**：只钉新来的那一份，等于给旧文件开了"以后也能引"的先例；
    而且目录里将来多一个文件时，这条不用跟着改（关系式而不是清单）。
    """
    offenders = {}
    for path in sorted((SRC_ROOT / "adapters").rglob("*.py")):
        edges = [
            node.module
            for node in ast.walk(source_of(path))
            if isinstance(node, ast.ImportFrom) and node.module
        ] + [
            alias.name
            for node in ast.walk(source_of(path))
            for alias in getattr(node, "names", [])
            if isinstance(node, ast.Import)
        ]
        hits = [edge for edge in edges if edge == "acp.orchestrate" or edge.startswith("acp.orchestrate.")]
        if hits:
            offenders[path.relative_to(SRC_ROOT).as_posix()] = hits
    assert offenders == {}, f"适配器层反向依赖了编排层：{offenders}"


# ---------------------------------------------------------------- 小工具


def _requests_router():
    """从 `api/requests.py` 取那支 router（不 import 挂载面，避免与本文件第一条判据互相顶替）。"""
    from acp.api import requests as requests_api

    return requests_api.router


class _RecordingSession:
    """假会话：接住行、把**边界动作计数**留住，但一个字都不碰 PG。

    它存在的目的只有一个：让"端点把建行交给了注入的会话、并且按 `_persist` 的口径收尾"这一件事
    在**没有 DSN 的机器上**也量得出来。口径（Phase 15 起）是 `add` → `commit` → `close`，
    异常路径 `rollback`。这里记的是**调用发生了没有、几次**，不是"PG 上真落了一行" ——
    后者只有真连接能判，归 `tests/integration/test_request_commit_boundary.py`（同一条边界、
    两档各量各的：那一档量"另一条连接读得到"，这一档量"代码面上提交点没被人摘掉"）。
    假会话**不许静默吞掉**这些动作：吞掉就等于把"端点忘了 commit"这种改法放过去。
    """

    def __init__(self, sink: list[FeatureRequest], *, fail_on_commit: bool = False) -> None:
        self.sink = sink
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0
        self.flushes = 0
        self.fail_on_commit = fail_on_commit

    def add(self, row: FeatureRequest) -> None:
        self.sink.append(row)

    def flush(self) -> None:
        self.flushes += 1

    def commit(self) -> None:
        self.commits += 1
        if self.fail_on_commit:
            raise RuntimeError("假装 PG 在提交那一拍断了")

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closes += 1
