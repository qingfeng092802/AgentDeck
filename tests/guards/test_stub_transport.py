"""T123 第 1 步的判据：仿真 transport 的形状、选中纪律、回收面与 R-07 的产物构成。

档位：`tests/guards/`，**全档不连库**（`BusGateway` 只碰 `tmp_path`，注册表读 `config/agents.yaml`）
⇒ 没有 DSN 时这一份是真跑而不是 skip，这是把它放在 guards 而不是 integration 的理由。

四条例外最容易写歪的口径，逐条点名：

① **"必须显式"要落在函数上而不是 CLI 上**。写在 `argparse` 的 `default=` 上，下一个装配点复制参数
就会带出一个默认值；落在 `resolve_transport_flag()` 上，任何调用方想绕都得先造一个非空的假值。
② **`usage` 必须是空列表**。这一发零真实调用 ⇒ 没有用量。空列表与"造一份看起来合理的用量"在
EN-13 里长得一样（都是行），所以这条只能在这里钉死（口径同宪法「不造数」）。
③ **实测值来自磁盘而不是自报**（FR-019）。这一支与三支真适配器共用同一支 `gateway.write_and_measure`，
所以这里量的不是网关，而是"这一支有没有绕开它去自己拼三个数"。
④ **`PAGE_BODIES` 就是 R-07 那一发的产物构成**（3 HTML + 1 CSS），且页内相对链接不得指向不存在的文件。
这是死链判据的**静态半边**（T124 的浏览器那一半边还没接）；静态这一半现在就能钉，不欠着。
"""

from __future__ import annotations

import ast
import inspect
import re
from decimal import Decimal
from pathlib import Path

import pytest

from acp.adapters import AgentAdapter, stub
from acp.bus import acl, layout
from acp.bus.gateway import BusGateway
from acp.protocol.messages import ArtifactFloor, Task

SRC = Path(__file__).resolve().parents[2] / "src" / "acp"
STUB_REL = "adapters/stub.py"

#: 卡上声明的默认下限（与 `tests/guards/test_artifact_gate.py` 同一组数，但这里是**判据自己的期望值**：
#: 它量的是"正文够不够一张典型卡的下限"，不是"门禁会不会放行"）。
FLOOR_BYTES = 512
FLOOR_LINES = 12


def make_task(
    *, task_id: str = "T01", expected_output: str = "index.html 与共享样式 styles.css", agent_id: str = "qoder"
) -> Task:
    return Task(
        task_id=task_id,
        title="首页",
        difficulty=2,
        skill_tags=["frontend_coding"],
        depends_on=[],
        project_key="demo",
        assignee={"agent_id": agent_id, "role_id": "executor"},
        objective="做一个首页",
        inputs=[],
        expected_output=expected_output,
        artifact_floor=ArtifactFloor(min_bytes=FLOOR_BYTES, min_lines=FLOOR_LINES, format="html"),
        acceptance_criteria=["首屏有标题", "有到关于页的链接"],
        token_budget=Decimal("1000"),
    )


@pytest.fixture
def gateway(tmp_path: Path) -> BusGateway:
    root = tmp_path / "AI项目台" / "demo"
    root.mkdir(parents=True)
    return BusGateway(root)


def build(agent_id: str, gateway: BusGateway) -> stub.StubAdapter:
    return stub.StubAdapter(agent_id=agent_id, gateway=gateway)


def import_edges(rel_path: str) -> list[str]:
    tree = ast.parse((SRC / rel_path).read_text(encoding="utf-8"))
    edges: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            edges.append(node.module)
        elif isinstance(node, ast.Import):
            edges += [alias.name for alias in node.names]
    return edges


# ---------------------------------------------------------------- 一、形状：它是一支适配器，不是别的东西


def test_the_stub_satisfies_the_same_protocol_as_the_three_real_adapters(gateway):
    """跨实现符合性：`isinstance(实例, AgentAdapter)` 必须为真。

    契约 §4 的围栏比对只吃 `adapters/base.py` 那一份协议声明，**不吃实现** ⇒ 这一支如果少写一支方法，
    围栏那条判据照样绿。这条补的就是"实现侧也要被同一次形状检查过一遍"（口径同 `test_all_adapters.py`
    那组"同一套判据在三支上都成立"）。
    """
    adapter = build("qoder", gateway)
    assert isinstance(adapter, AgentAdapter), "仿真侧不再是四支同形 ⇒ 装配点拿到的形状变了"
    assert adapter.agent_id == "qoder"
    members = {name for name, value in inspect.getmembers(adapter) if callable(value)}
    assert {"describe", "submit", "collect", "cancel"} <= members, sorted(members)


def test_the_stub_package_surface_still_exports_only_the_protocol():
    """`acp.adapters` 的对外面不许多出仿真实现。

    与 `test_adapter_protocol.py::test_adapters_package_exports_only_the_protocol` 是**同一件事的两侧**：
    那条钉协议声明的形状，这条钉"加了 `stub.py` 之后导出面没跟着变"。少了这条，
    下一轮"顺手在 `__init__` 里重导出一个 `StubAdapter`"就会让编排层拿到具体实现（宪法 II 的导入面）。
    """
    from acp import adapters as adapters_pkg

    assert adapters_pkg.__all__ == ["AgentAdapter"], adapters_pkg.__all__
    assert not hasattr(adapters_pkg, "StubAdapter")


def test_the_stub_imports_no_vendor_adapter():
    """静态面：`adapters/stub.py` 不许 import 任何厂商适配器目录。

    `test_all_adapters.py::test_no_adapter_module_imports_a_sibling_adapter` 遍历的是**那三个目录名**，
    所以放在包根的 `stub.py` 天然不在它的扫描面里（"扫描面自己漏掉新位置"是这一族判据的真实失效模式）。
    这一条把那个口子补上：仿真侧一旦 import 了某支厂商 transport，"删目录仍能跑"就变成空话。
    """
    offenders = [
        edge for edge in import_edges(STUB_REL) if edge.startswith("acp.adapters.") or edge.endswith("transport")
    ]
    assert offenders == [], f"仿真侧引到了厂商实现：{offenders}"


# ---------------------------------------------------------------- 二、必须显式选中（"不给默认"的门）


@pytest.mark.parametrize("raw, code", [(None, stub.FLAG_MISSING), ("", stub.FLAG_MISSING)])
def test_a_missing_flag_is_refused_with_its_own_code(raw, code):
    """漏敲与打错**必须是两个代号**：同一句"配置不对"运维分不出是忘了还是打错。"""
    with pytest.raises(stub.StubMisuse) as caught:
        stub.resolve_transport_flag(raw)
    assert caught.value.code == code


@pytest.mark.parametrize("raw", ["qoder", "workbuddy", "doubao", "STUB", "Stub", "stub "])
def test_any_other_value_is_refused_rather_than_falling_back(raw):
    """`STUB` / `Stub` / `"stub "` 一律拒 ⇒ 不做大小写折叠、不 strip。

    排除掉的那三支名字尤其要紧：**真适配器的名字不能从这个旗标进**，否则一次敲错就是一次真实外部调用。
    """
    with pytest.raises(stub.StubMisuse) as caught:
        stub.resolve_transport_flag(raw)
    assert caught.value.code == stub.FLAG_UNKNOWN


def test_the_explicit_stub_value_is_the_only_one_that_passes():
    assert stub.resolve_transport_flag(stub.TRANSPORT_FLAG) == "stub"
    # 反向对照的另一半：门常量与"场内那三支"的名字必须不重叠，否则上一条的拒绝名单是巧合而不是设计。
    assert stub.TRANSPORT_FLAG not in {"qoder", "workbuddy", "doubao"}


# ---------------------------------------------------------------- 三、回收面：实测值、落点、用量


def test_collect_writes_into_its_own_workspace_and_measures_from_disk(gateway):
    adapter = build("qoder", gateway)
    task = make_task()
    result = adapter.collect(adapter.submit(task))

    assert len(result.artifacts) == 2, result.artifacts
    for ref in result.artifacts:
        assert ref.rel_path.startswith(f"qoder{layout.WORKSPACE_SUFFIX}/"), ref.rel_path
        assert ref.bytes > 0 and ref.lines > 0
        assert ref.format == ref.rel_path.rsplit(".", 1)[-1]
        on_disk = (gateway.root / ref.rel_path).read_bytes()
        assert ref.bytes == len(on_disk), "字节数不是从盘上量的"
        assert ref.lines == len(on_disk.decode("utf-8").splitlines()), "行数不是从盘上量的"


def test_the_stub_reports_no_usage_because_it_made_no_call(gateway):
    """②：`usage` 必须是空列表。这一条红了就等于有人在仿真那一发里造了用量。"""
    adapter = build("qoder", gateway)
    result = adapter.collect(adapter.submit(make_task()))
    assert result.usage == [], result.usage
    assert result.issues == [], result.issues
    assert result.log_excerpt == stub.STUB_LOG_LINE
    assert "stub" in result.log_excerpt.lower()


def test_the_log_line_carries_no_credential_shape(gateway):
    """留痕面只带固定文案：判据用形态而不是字面量（口径同 `feedback-secret-leak-gates`）。"""
    adapter = build("qoder", gateway)
    text = adapter.collect(adapter.submit(make_task())).log_excerpt
    patterns = [r"ghp_[A-Za-z0-9]{6,}", r"[A-Za-z0-9+/_-]{38,}={0,2}", r"sk-[A-Za-z0-9]{6,}", r"Bearer\s+\S+"]
    hits = [pattern for pattern in patterns if re.search(pattern, text)]
    assert hits == [], f"固定文案里出现了凭据形态：{hits}"


def test_a_card_that_names_no_file_stops_instead_of_picking_a_default(gateway):
    """卡上没点名交付物 ⇒ 停，并写出是哪张卡；**不交一份没人要的默认页**。

    这条是从"回落成 `task_id.html` 更省事"那个念头里退回来的：回落会让一张没写清交付物的卡
    照样"跑通"，于是"产物 = 卡上声明的那几份"这件事在链路上再也没人管。
    现在它红在提交后的第一拍，且 `written` 必须是空（停在写盘之前）。
    """
    adapter = build("qoder", gateway)
    task = make_task(task_id="T07", expected_output="一个能看的首页")
    handle = adapter.submit(task)
    with pytest.raises(stub.StubMisuse) as caught:
        adapter.collect(handle)
    assert caught.value.code == stub.UNNAMED_DELIVERABLE, caught.value.code
    assert "T07" in str(caught.value)
    assert adapter.written == [], "停在写盘之前才算停，写了一半再报错就是留脏产物"


def test_a_card_naming_an_undeliverable_file_is_refused_before_any_write(gateway):
    """点名了、但仿真侧没有那份正文 ⇒ 同样停在写盘之前（两个代号分列：`BODY_MISSING` vs `UNNAMED`）。"""
    adapter = build("qoder", gateway)
    handle = adapter.submit(make_task(task_id="T08", expected_output="pricing.html"))
    with pytest.raises(stub.StubMisuse) as caught:
        adapter.collect(handle)
    assert caught.value.code == stub.BODY_MISSING, caught.value.code
    assert adapter.written == []


def test_the_same_card_twice_yields_the_same_file_list(gateway):
    """确定性：同一张卡交出的文件名集合必须一模一样（交付包"唯一两份"的判据要能复算）。"""
    task = make_task(task_id="T01", expected_output="styles.css 与 index.html")
    first = build("qoder", gateway)
    second = build("qoder", gateway)
    assert first is not second
    paths = [adapter.collect(adapter.submit(task)).artifacts for adapter in (first, second)]
    assert [ref.rel_path for ref in paths[0]] == [ref.rel_path for ref in paths[1]]
    # 顺序取的是卡上点名的顺序，不是排序后的：`styles.css` 在前就必须在前。
    assert paths[0][0].rel_path.endswith("styles.css"), paths[0]


def test_an_unknown_handle_is_refused_instead_of_inventing_content(gateway):
    """**同一个身份**的第二个实例收到旧回执 ⇒ 拒（"跑在了别的进程里"那一支）。

    用同一个 `agent_id` 造第二个实例是这条的要点：如果换成别的工具，撞的就是下面那条
    `AGENT_MISMATCH`，这一支的分支一次都没被走过（防御分支零用例是这一族判据最常见的空转形状）。
    """
    adapter = build("qoder", gateway)
    orphan = adapter.submit(make_task(task_id="T01"))
    other_process = build("qoder", gateway)
    with pytest.raises(stub.StubMisuse) as caught:
        other_process.collect(orphan)
    assert caught.value.code == stub.HANDLE_UNKNOWN
    assert other_process.written == []


def test_a_handle_from_another_agent_is_refused_with_a_different_code(gateway):
    """身份串了与"没下发过"必须是**两个代号**（`AGENT_MISMATCH` 与 `HANDLE_UNKNOWN` 分列）。

    共用一个代号的话，装配点出错时"回执被换了一支工具"与"进程换了"这两种根因分不开，
    而它们的修法完全不同（前者是编排层传错了实例，后者是跨进程恢复没接上）。
    """
    adapter = build("qoder", gateway)
    handle = adapter.submit(make_task())
    other_agent = build("workbuddy", gateway)
    with pytest.raises(stub.StubMisuse) as caught:
        other_agent.collect(handle)
    assert caught.value.code == stub.AGENT_MISMATCH
    assert other_agent.written == []
    assert caught.value.code != stub.HANDLE_UNKNOWN


def test_a_cancelled_card_delivers_nothing(gateway):
    """撤单之后再回收必须交不出东西（FR-022 那一族在仿真侧的同形约束）。"""
    adapter = build("qoder", gateway)
    handle = adapter.submit(make_task())
    adapter.cancel(handle)
    with pytest.raises(stub.StubMisuse) as caught:
        adapter.collect(handle)
    assert caught.value.code == "stub_cancelled"
    assert adapter.written == []


def test_a_second_collect_writes_a_second_copy_of_the_files(gateway):
    """**已知边界**，钉成用例而不是藏着：`collect()` 可重入，同一张卡收两次就落两份同名文件（覆盖写）。

    为什么允许：交付包那一侧的去重靠 EN-06 的行而不是靠"网关拒绝第二次写"，
    而仿真侧重复回收就是测试里的手滑，不是生产形态。这一条读起来像"没防御"，
    但它立的是"这里没有隐藏的幂等承诺"——将来要真做幂等，这条会红，那时再裁。
    """
    adapter = build("qoder", gateway)
    handle = adapter.submit(make_task(expected_output="index.html"))
    first = adapter.collect(handle)
    second = adapter.collect(handle)
    assert [ref.rel_path for ref in first.artifacts] == [ref.rel_path for ref in second.artifacts]
    assert adapter.collects == 2, adapter.collects
    assert len(adapter.written) == 2, adapter.written


# ---------------------------------------------------------------- 四、R-07 的产物构成（静态那一半）


def test_the_page_set_is_exactly_three_html_plus_one_css():
    names = sorted(stub.PAGE_BODIES)
    html = [name for name in names if name.endswith(".html")]
    css = [name for name in names if name.endswith(".css")]
    assert (len(html), len(css)) == (3, 1), names
    assert "index.html" in html, "R-07 要的那份可预览首页必须在"


@pytest.mark.parametrize("name", sorted(stub.PAGE_BODIES))
def test_every_page_clears_a_typical_card_floor(name):
    body = stub.PAGE_BODIES[name]
    data = body.encode("utf-8")
    assert len(data) >= FLOOR_BYTES, f"{name} 只有 {len(data)} 字节"
    assert len(body.splitlines()) >= FLOOR_LINES, f"{name} 只有 {len(body.splitlines())} 行"


def test_no_relative_link_points_at_a_file_that_is_not_delivered():
    """死链数 = 0 的**静态半边**：页内每条站内引用都要指向这一发真会交付的文件。

    三种目标三种判法，各写清楚：
    - `http:` / `https:` / `//` ⇒ **一律拒**。这一发零外部调用，而 `<a href="https://…">` 在浏览器里
      也算"链接可点"，会把一次真实网络请求放进 R-07 的验收面 —— 那是验收口径被偷换，不是链接质量。
    - `#锚点` 与 `mailto:` ⇒ 允许且不联网。邮件链接是"可点"的（FR 侧要的就是这个），点开的是本机邮件客户端。
    - 其余（站内相对文件名）⇒ 必须在交付集合里，且**每页至少有一条**：一条站内链接都没有的页面
      在这条判据下会"零死链"地绿，而 R-07 那句"链接可点"根本没被演到。
    """
    delivered = set(stub.PAGE_BODIES)
    links = re.compile(r"""href="([^"]+)"|src="([^"]+)"|url\(([^)]+)\)""")
    for name, body in stub.PAGE_BODIES.items():
        internal = 0
        for groups in links.finditer(body):
            target = next((group for group in groups.groups() if group), "")
            if target.startswith(("http:", "https:", "//")):
                raise AssertionError(f"{name} 里有联网引用 {target!r} ⇒ 这一发必须零外部请求")
            if target.startswith(("#", "mailto:")):
                continue
            assert target in delivered, f"{name} 指向 {target!r}，但 {target} 不在交付集合 {sorted(delivered)} 里"
            internal += 1
        if name.endswith(".html"):
            assert internal >= 1, f"{name} 没有任何站内引用 ⇒ “链接可点”在这一发里没被演到"


def test_body_for_refuses_to_invent_a_page():
    """④ 的直接面：`body_for` 是唯一的正文入口，它自己就得拒掉没写过的页（上面那条走的是全链路）。"""
    with pytest.raises(stub.StubMisuse) as caught:
        stub.body_for("pricing.html")
    assert caught.value.code == stub.BODY_MISSING


def test_an_empty_agent_id_is_refused_before_any_write(gateway):
    """身份为空会把文件写进别人的 workspace 目录名里 —— 构造期就拒，不等网关判。"""
    with pytest.raises(stub.StubMisuse) as caught:
        stub.StubAdapter(agent_id="", gateway=gateway)
    assert caught.value.code == "stub_agent_id_missing"
    assert list(gateway.root.rglob("*")) == []


def test_the_actor_identity_the_stub_builds_is_the_adapter_one(gateway):
    """`describe()` 之外的第二处身份：`Actor.actor_id` 必须是 `adapter:<agent_id>` 且岗位是那句实话。

    这条不是复述 `acl` 的规则，钉的是"仿真侧没有自造身份"：它若报成一个编排者身份，
    公共区那道 403 就被绕开了（`can_write` 的第 3 条），而这一发恰好需要公共区写权限的是交付包，
    不是 transport。
    """
    expected_actor = acl.Actor(
        actor_id=f"{acl.ADAPTER_OWNER_PREFIX}qoder", role_id=acl.NO_ROLE_DECLARED
    )
    adapter = build("qoder", gateway)
    adapter.collect(adapter.submit(make_task(expected_output="index.html")))
    assert acl.declared_owner(f"qoder{layout.WORKSPACE_SUFFIX}/index.html") == expected_actor.actor_id
    decision = acl.can_write(
        expected_actor,
        f"qoder{layout.WORKSPACE_SUFFIX}/index.html",
        acl.Capability(agent_id="qoder", task_id="T01"),
    )
    assert decision is acl.ALLOW, decision
