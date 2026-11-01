"""T045：三形态的**同一套**判据 + "删掉一个适配器目录，图仍可构建"的零耦合证明（FR-012 / SC-001 / 契约 §4）。

为什么值得单独一份文件：另三份 `test_*_transport.py` 各自只能证明"这一支自己是对的"。
本文件证的是两件跨支的事：
① **同一份任务卡、同一个网关、同一套断言**在三种形态上都跑得通（"双向"= 协议 payload 进得去、
   `TaskResult` 出得来，出来的那份还能被协议模型再校验一次）；
   口径统一的那一条也在这里（`acceptance_criteria` 三支都发，owner 2026-10-13 裁决 2）——
   逐支单测各钉各的方向，管不了"三支是不是同一个口径"。
② 三支之间**没有任何一条 import 边** ⇒ 拔掉一支，注册表仍可装载、图仍可 compile、
   另两支仍可被 `resolve()` 现import 现跑。这一条是 `SC-001` 与"新增 Agent 只增一个目录"（契约 §1）的
   共同判据，也是 `contracts/agent-protocol.md §4` 那句"厂商差异关在 transport 里"的可执行版本。

**零真实出网、零子进程、零 API 费**（成本纪律 5）：workbuddy / doubao 走注入的 `httpx.MockTransport`，
qoder 走实现 `CliRunner` 协议的假 runner。拔目录用 `sys.meta_path` 上的挡子模拟（不动盘上文件，
不碰任何人在用的工作树），挡子的写法见 `_blocked`。

**三支的实质差别明写**（不当成"同一件事"）：产物来源与 `usage` 面在三种形态里是三套 ——
workbuddy 由厂商报路径 + 内容我们写；qoder 由外部进程自己写、我们验；doubao 的响应里根本没有"文件"
这个概念，产物是我们把结论落成 `reviews/*.md` 再实测。所以本文件断言的是**共同不变量**
（归属在自己的 workspace、三个数读自盘上、格式在域内），差别那几处逐支配期望值。
"""

from __future__ import annotations

import ast
import contextlib
import importlib
import json
import sys
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from acp.bus.gateway import ARTIFACT_FORMATS, BusGateway
from acp.growth import store as _growth
from acp.orchestrate.graph import OrchestrateDeps, build_graph
from acp.protocol import Handle, Task, TaskResult
from acp.registry.loader import RegistryError, load_registrations

def _load_sibling(name: str):
    """按路径加载同目录的用例（它们不是包，不能 import）。口径与另三份 transport 用例一致。

    `sys.modules` 必须先登记再 `exec_module`：sibling 里有 `@dataclass`，而 dataclasses 会按
    `cls.__module__` 回查模块取 `__dict__` —— 没登记就是 `AttributeError: 'NoneType' object has no attribute '__dict__'`。
    """
    path = Path(__file__).with_name(f"{name}.py")
    spec = importlib.util.spec_from_file_location(f"acp_all_adapters_fixture.{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_messages = _load_sibling("test_protocol")
_workbuddy = _load_sibling("test_workbuddy_transport")
_qoder = _load_sibling("test_qoder_transport")
_doubao = _load_sibling("test_doubao_transport")

ADAPTER_ROOT = Path(__file__).resolve().parents[2] / "src" / "acp" / "adapters"
FAKE_KEY = "all-adapters-fixture-secret"


class _Blocker:
    """让指定模块（及其子模块）import 不进来 —— 等价于"那个目录不存在"，但不碰盘上文件。"""

    def __init__(self, names: tuple[str, ...]) -> None:
        self.names = names

    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == n or fullname.startswith(n + ".") for n in self.names):
            raise ModuleNotFoundError(f"No module named {fullname!r}", name=fullname)
        return None


@contextlib.contextmanager
def _blocked(*names: str):
    """插挡子 + **把已加载的模块从 sys.modules 里摘掉**。

    只做前者不够：这些模块在本文件 import 时就已经进去了，`import` 会直接命中缓存、挡子根本不会被问起
    ⇒ "拔掉了"其实是"没拔但也没人需要"。摘掉的范围只限适配器自己（`acp.adapters.*`），
    父包留着，因为 `acp.adapters` 自己没有内容可 import。
    """
    saved = {name: sys.modules.pop(name) for name in list(sys.modules) if name.startswith("acp.adapters.") and name != "acp.adapters"}
    blocker = _Blocker(names)
    sys.meta_path.insert(0, blocker)
    try:
        yield
    finally:
        sys.meta_path.remove(blocker)
        sys.modules.update(saved)


@pytest.fixture
def bus_root(tmp_path: Path) -> Path:
    root = tmp_path / "AI项目台" / "demo"
    root.mkdir(parents=True)
    return root


@pytest.fixture
def gateway(bus_root: Path) -> BusGateway:
    return BusGateway(bus_root)


def card(agent_id: str, role_id: str) -> Task:
    payload = _messages.task_payload(**{"assignee": {"agent_id": agent_id, "role_id": role_id}})
    return Task(**payload)


# —— 三支各自的"厂商替身"装配：返回 (transport, 副作用计数器, 期望的产物与用量) ——


def build_workbuddy(gateway: BusGateway, bus_root: Path, *, env=None):
    responses = [
        _workbuddy.created(),
        _workbuddy.finished(),
    ]
    transport, calls = _workbuddy.build_transport(
        gateway, responses, env=_workbuddy.make_env() if env is None else env
    )
    return (
        transport,
        lambda: len(calls),
        "workbuddy-workspace/index.html",
        "html",
        {"CALL"},
    )


def build_qoder(gateway: BusGateway, bus_root: Path, *, env=None):
    """qoder 的"厂商替身"= 实现 `CliRunner` 协议的 `FakeRunner`（零子进程，owner 裁决 2 的注入点）。

    产物由"外部进程"写在盘上 ⇒ 这里代表那个进程先写一次，transport 只做验证与实测登记。
    """
    stdout = _qoder.stdout_of(
        _qoder.tool_use("Write", "qoder-workspace/index.html"),
        _qoder.assistant("页面写好了"),
        _qoder.result_event(result="已产出 index.html"),
    )
    _qoder.write_artifact(bus_root, "qoder-workspace/index.html", "<html>\n<p>hi</p>\n</html>\n")
    transport, runner = _qoder.build(gateway, [_qoder.outcome(stdout=stdout)], env=_qoder.make_env() if env is None else env)
    return transport, (lambda: len(runner.calls)), "qoder-workspace/index.html", "html", set()


def build_doubao(gateway: BusGateway, bus_root: Path, *, env=None):
    transport, calls = _doubao.build(
        gateway, [(200, _doubao.completion_body())], env={_doubao.API_KEY_ENV: FAKE_KEY} if env is None else env
    )
    return (
        transport,
        lambda: len(calls),
        None,  # doubao 的产物名带 native_ref，见下面的 case_by_case 断言
        "md",
        {"INPUT", "OUTPUT"},
    )


CASES = {
    "workbuddy": ("architect", build_workbuddy),
    # 岗取 `architect`：owner 2026-10-13 裁决 2 把 qoder 的申报改成三个前置岗，而其中
    # `reviewer` / `acceptor` 按 FR-033 是**只读岗** ⇒ 一支要写产物的适配器挂在只读岗上会自相矛盾
    # （这处冲突已登记在 `tasks.md` 的收口段，不在这里替 owner 解决）。
    "qoder": ("architect", build_qoder),
    "doubao": ("reviewer", build_doubao),
}
AGENT_IDS = sorted(CASES)


def build(agent_id: str, gateway: BusGateway, bus_root: Path, **kwargs):
    role, factory = CASES[agent_id]
    return factory(gateway, bus_root, **kwargs)


def round_trip(agent_id: str, gateway: BusGateway, bus_root: Path, **kwargs):
    transport, side_effects, expected_rel, expected_format, expected_usage_kinds = build(
        agent_id, gateway, bus_root, **kwargs
    )
    role = CASES[agent_id][0]
    receipt = transport.submit(card(agent_id, role))
    result = transport.collect(receipt)
    return transport, receipt, result, side_effects, expected_rel, expected_format, expected_usage_kinds


# —— 一、注册面：三形态各有一支能跑的适配器 ——


def test_registry_declares_three_forms_each_backed_by_a_working_adapter():
    registrations = load_registrations()
    assert {r.manifest.form for r in registrations} == {"IN_PLATFORM", "SDK", "API"}
    execution = [r for r in registrations if r.agent_id in CASES]
    assert sorted(r.agent_id for r in execution) == AGENT_IDS, "执行适配器仍是那三支"
    for registration in execution:
        cls = registration.resolve()
        # 类层只有**注解**没有属性（`agent_id: str` 是给契约 §4 的围栏读的，值在实例上）
        # ⇒ 这里读 `__annotations__`，值那一面交给下面的 round-trip 用例。
        assert "agent_id" in cls.__annotations__, "契约 §4 的围栏比对读类层注解，缺它就等于第五支"
        assert {name for name in ("describe", "submit", "collect", "cancel")} <= {
            name for name in dir(cls)
        }, "四支协议成员缺一支就不是适配器"

    # 第四支（Phase 17 P3）是**审查身份**，不是执行适配器：它的入口是标记类，
    # 恰好没有那四个协议成员 —— 这正是"把它误接进执行派发时会当场炸"的结构保证。
    review_only = next(r for r in registrations if r.agent_id == "deepseek-reviewer")
    entry = review_only.resolve()
    assert entry.__name__ == "ReviewEntry", entry
    assert not {name for name in ("describe", "submit", "collect", "cancel")} & set(dir(entry)), (
        "审查身份被实现成了执行适配器 ⇒ 它就能被派发去干活了"
    )


def test_registry_entry_resolves_to_the_class_the_adapter_package_exports():
    """入口字符串（`acp.adapters.doubao:Transport`）与包再导出的那个名字 MUST 是同一个对象。"""
    for registration in load_registrations():
        if registration.agent_id not in CASES:
            continue
        package = importlib.import_module(registration.adapter_entry.partition(":")[0])
        assert registration.resolve() is getattr(package, "Transport")
    # 审查身份的同一条对读：包导出的 `ReviewEntry` 就是注册表解析到的类。
    review_only = next(r for r in load_registrations() if r.agent_id == "deepseek-reviewer")
    package = importlib.import_module(review_only.adapter_entry.partition(":")[0])
    assert review_only.resolve() is package.ReviewEntry


# —— 二、同一套判据在三支上都成立（双向的两半）——


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_same_task_card_round_trips_and_the_artifact_is_measured_from_disk(agent_id, gateway, bus_root):
    """共同不变量：回执可重查、产物落在自己的 workspace、三个数读自盘上字节、格式在域内。"""
    _, receipt, result, side_effects, expected_rel, expected_format, usage_kinds = round_trip(
        agent_id, gateway, bus_root
    )
    assert isinstance(receipt, Handle) and receipt.agent_id == agent_id
    assert receipt.native_ref and receipt.submitted_at.tzinfo is not None
    assert isinstance(result, TaskResult) and result.task_id == receipt.task_id
    assert side_effects() >= 1, "下发确实动了厂商面（没动就是 Mock 在自说自话）"

    (ref,) = result.artifacts
    on_disk = (bus_root / ref.rel_path).read_bytes()
    assert ref.bytes == len(on_disk)
    assert ref.lines == len(on_disk.decode("utf-8").splitlines())
    assert ref.format == expected_format and ref.format in ARTIFACT_FORMATS
    assert ref.rel_path.startswith(f"{agent_id}-workspace/"), "产物必须在它自己的 workspace 内（FR-013）"
    if expected_rel is not None:
        assert ref.rel_path == expected_rel
    assert {notice.usage_kind for notice in result.usage} == usage_kinds, (
        "用量面是三形态的实质差别：doubao 报 token 两维、workbuddy 报一条 CALL、qoder 的 usage 全 0 因此不落账"
    )


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_the_produced_task_result_passes_the_protocol_again(agent_id, gateway, bus_root):
    """"双向"的后一半：适配器交回来的对象，用协议模型再校验一次必须仍然成立（不是私有类型）。"""
    _, _, result, *_ = round_trip(agent_id, gateway, bus_root)
    again = TaskResult.model_validate(result.model_dump(mode="json"))
    assert again == result
    assert isinstance(again.artifacts[0].bytes, int)


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_foreign_task_card_is_refused_by_every_adapter(agent_id, gateway, bus_root):
    other = next(name for name in AGENT_IDS if name != agent_id)
    transport, *_ = build(agent_id, gateway, bus_root)
    with pytest.raises(Exception) as caught:
        transport.submit(card(other, CASES[other][0]))
    assert other in str(caught.value), "拒发要点名是谁的卡"


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_missing_credential_stops_every_adapter_before_any_side_effect(agent_id, gateway, bus_root):
    transport, side_effects, *_ = build(agent_id, gateway, bus_root, env={})
    with pytest.raises(Exception) as caught:
        transport.submit(card(agent_id, CASES[agent_id][0]))
    assert type(caught.value).__name__ in (
        "CredentialMissing",
        "QoderTransportError",
        "DoubaoTransportError",
        "WorkBuddyTransportError",
    ), f"缺凭据要抛的是这一族的，收到 {type(caught.value).__name__}"
    assert side_effects() == 0, "缺凭据时一次厂商动作都不许发"


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_describe_issues_no_side_effect_and_returns_the_registry_manifest(agent_id, gateway, bus_root):
    transport, side_effects, *_ = build(agent_id, gateway, bus_root)
    manifest = transport.describe()
    declared = next(r.manifest for r in load_registrations() if r.agent_id == agent_id)
    # 相等而不是 `is`：`load_registrations()` 每次调用都重新装载一份对象，跨调用比同一性是恒假的判据。
    assert manifest == declared, "describe() 不许生产新事实，只许交回注册表那份"
    assert side_effects() == 0


def submit_face(agent_id: str, gateway: BusGateway, bus_root: Path, *, growth_root=...):
    """下发一次，取出"真正发出去的那份报文"。三种形态三种取法，**不归一**。

    workbuddy / doubao 是 HTTP body，qoder 是子进程 stdin 那一行 JSON。取法不同正是这条要防的东西。

    `growth_root` 只有"没传"与"显式传"两态，用 `...`（Ellipsis）当哨兵而不是 `None`：
    `None` 在代码侧的含义正是"走 §11 登记的默认根"，拿它当"没传"的哨兵就会让默认根的档案
    在判据里被指到位，"只改了一侧"那种漂移反而看不见。
    """
    role = CASES[agent_id][0]
    if agent_id == "qoder":
        transport, runner = _qoder.build(
            gateway, [_qoder.outcome(stdout=_qoder.stdout_of(_qoder.result_event(result="ok")))],
            env=_qoder.make_env(),
        )
    elif agent_id == "doubao":
        transport, calls = _doubao.build(
            gateway, [(200, _doubao.completion_body())], env={_doubao.API_KEY_ENV: FAKE_KEY}
        )
    else:
        transport, calls = _workbuddy.build_transport(
            gateway, [_workbuddy.created()], env=_workbuddy.make_env()
        )
    if growth_root is not ...:
        transport.growth_root = growth_root
    if agent_id == "qoder":
        transport.submit(card(agent_id, role))
        return runner.calls[0].stdin_text
    if agent_id == "doubao":
        transport.submit(card(agent_id, role))
        return json.dumps(calls[0]["body"], ensure_ascii=False)
    transport.submit(card(agent_id, role))
    return json.dumps(calls[0]["body"], ensure_ascii=False)


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_every_submit_carries_the_criteria(agent_id, gateway, bus_root):
    """裁决 2 的跨支口径（FR-029）：**每条**验收标准都出现在发出去的报文里。"""
    face = submit_face(agent_id, gateway, bus_root)
    for criterion in card(agent_id, CASES[agent_id][0]).acceptance_criteria:
        assert criterion in face, f"{agent_id} 的报文里没有 {criterion!r} ⇒ 审查方手里没有标准可比"


@pytest.fixture
def growth_dir(tmp_path: Path) -> Path:
    """一棵只属于本发的档案根：两份文件、内容各自带一个**单行**独有标记。

    标记必须不含换行：报文是 JSON 串，`\n` 会被转义成两字符，用多行标记去 `in face`
    就会永远不命中，而那条判据看起来"测了全文"其实一次都没测到。
    """
    directory = tmp_path / "growth"
    directory.mkdir()
    (directory / "my_profile.md").write_text(
        "档案首行 MARK-PROFILE-HEAD\n" + ("填充分支用于把长度推到判据无法靠截断蒙过去。" * 400) + "\n档案末行 MARK-PROFILE-TAIL",
        encoding="utf-8",
    )
    (directory / "mistakes.md").write_text("错题末行 MARK-MISTAKES-TAIL\n", encoding="utf-8")
    return directory


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_every_submit_carries_the_archives_in_full(agent_id, gateway, bus_root, growth_dir):
    """owner 2026-10-19 第三步 ① 的跨支口径：**两份档案全文、无条件、在任务面之前**进场。

    三支的落点各有形状（qoder = stdin 的 `message.content`、doubao = `messages[0].content`、
    workbuddy = `Objective`），所以"三支同不同口径"这件事只有放在同一发的参数化里才量得出 ——
    逐支单测各测各的，漏掉一支也不会红。
    """
    face = submit_face(agent_id, gateway, bus_root, growth_root=growth_dir)
    assert _growth.PROFILE_HEADER in face and _growth.MISTAKES_HEADER in face, f"{agent_id} 没带档案标题"
    assert "MARK-PROFILE-HEAD" in face and "MARK-PROFILE-TAIL" in face, f"{agent_id} 丢了档案首尾之一"
    assert "MARK-MISTAKES-TAIL" in face, f"{agent_id} 的错题本没进报文"
    # 顺序：档案在任务面之前（三支同一条顺序，否则前端与厂商侧看到的"第一段"就不是同一件事）。
    objective = card(agent_id, CASES[agent_id][0]).objective
    assert face.index(_growth.PROFILE_HEADER) < face.index(objective), f"{agent_id} 的档案排在任务面之后"


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_a_missing_archive_stops_the_submit_instead_of_sending_without_it(agent_id, gateway, bus_root, tmp_path):
    """档案根里没有那两份文件 ⇒ 下发**失败**，不许"少带一段照发"。

    这一发是上一条的对照面：上一条证"带着全文才叫发了"，这一条证"没档案就不发"。
    只写上一条的话，"档案读不到就当没有、静默省略"这种实现是完全合法的 —— 而它正好违背
    owner 那句"无条件、全量"。
    """
    empty = tmp_path / "no-archives"
    empty.mkdir()
    with pytest.raises(_growth.GrowthError):
        submit_face(agent_id, gateway, bus_root, growth_root=empty)


# —— 三、零耦合：拔掉一支，图仍可构建、另两支仍可现 import 现跑 ——


def _import_edges(node: ast.AST) -> list[str]:
    """把一条 import 语句展开成候选点路径。

    三种写法都要吃到（第一版只吃了前两种，N06b 因此没红）：
    `import acp.adapters.qoder` / `from acp.adapters.doubao.transport import Transport`
    / **`from acp.adapters import qoder`** —— 最后那种，兄弟名在 `alias.name` 里而不在 `node.module` 里。
    """
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        head = node.module or ""
        edges = [head] if head else []
        edges += [f"{head}.{alias.name}" if head else alias.name for alias in node.names]
        return edges
    return []


def test_no_adapter_module_imports_a_sibling_adapter():
    """静态面：三支互不 import（否则共享常量会把两支焊在一起，"删目录"就变成"两个厂商互相依赖"）。

    走 AST 而不是正则：docstring 里写一句 `acp.adapters.qoder/transport.py` 这种占位说明
    不是 import 边（T038 那一轮就是这么定的口径）。扫的是目录里的**每个 `.py`**（不只 transport.py）：
    `__init__.py` 那层同样能把兄弟拉进来，N06b 就是把违规放进 `__init__.py` 才逼出这条宽度的。
    """
    offenders = {}
    for own in ("workbuddy", "qoder", "doubao"):
        others = {name for name in ("workbuddy", "qoder", "doubao") if name != own}
        directory = ADAPTER_ROOT / own
        assert directory.is_dir(), f"适配器目录不见了：{directory}"
        for path in sorted(directory.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                for edge in _import_edges(node):
                    segments = edge.split(".")
                    absolute_sibling = (
                        len(segments) >= 3 and segments[0] == "acp" and segments[1] == "adapters" and segments[2] != own
                    )
                    relative = isinstance(node, ast.ImportFrom) and node.level and segments[-1] in others
                    if absolute_sibling or relative:
                        offenders.setdefault(f"{own}/{path.name}", []).append(edge)
    assert offenders == {}, f"适配器之间出现了 import 边：{offenders}"


@pytest.mark.parametrize("removed", AGENT_IDS)
def test_the_graph_still_builds_and_survivors_still_resolve_with_one_adapter_removed(removed, gateway, bus_root):
    """FR-012 的"注册即接入"要能在缺一支时不塌：注册表装载不受影响、图仍可 compile、
    被拔掉的那支 `resolve()` 明确报错（不是静默返回 None）。"""
    with _blocked(f"acp.adapters.{removed}"):
        registrations = load_registrations()
        assert len(registrations) == 4, "注册表是声明面，拔目录不该改变它读得到几条"

        # Phase 4 起图上有了挂起点 ⇒ compile 必须带 checkpointer（`graph.py` 的
        # `CheckpointerNotConfigured`）。这里给内存版：本条判据问的是"拔一支适配器图还装不装得起来"，
        # 与 checkpoint 落在哪个后端无关；真实 PG 挂起 / 恢复归 `tests/guards/test_approval_resume.py`。
        graph = build_graph(
            OrchestrateDeps(session=object(), splitter=None, checkpointer=InMemorySaver())
        )
        assert graph is not None, "图的装配不许依赖任何一支适配器可导入"

        for registration in registrations:
            if registration.agent_id == removed:
                with pytest.raises(RegistryError):
                    registration.resolve()
            else:
                assert registration.resolve() is not None


@pytest.mark.parametrize("removed", AGENT_IDS)
def test_the_two_surviving_adapters_still_round_trip_with_one_removed(removed, bus_root):
    """真正跑一遍另两支：类对象是**在挡子生效之后**由注册表现 import 拿到的，不是本文件顶部的旧引用。"""
    survivors = [name for name in AGENT_IDS if name != removed]
    with _blocked(f"acp.adapters.{removed}"):
        for agent_id in survivors:
            gateway = BusGateway(bus_root)
            entry = next(r for r in load_registrations() if r.agent_id == agent_id)
            cls = entry.resolve()
            transport, side_effects, *_ = build(agent_id, gateway, bus_root)
            # 比**类名**而不是比对象：挡子生效时这些模块被我刻意从 `sys.modules` 摘掉，
            # `resolve()` 现 import 出来的是一个新类对象，与 sibling 用例里那个必然不是同一个。
            # 这里要证的是"没有那一支也能 import 到正确的类并跑通"，同一性反而是被我们自己打破的前提。
            assert type(transport).__name__ == cls.__name__, "注册表入口解析出的类与实际跑的那一支同名同形"
            assert transport.agent_id == agent_id
            receipt = transport.submit(card(agent_id, CASES[agent_id][0]))
            assert isinstance(transport.collect(receipt), TaskResult)
            assert side_effects() >= 1


def test_blocking_an_unrelated_module_changes_nothing():
    """反向对照（纪律 2 要求至少一条）：挡一个不存在的模块 ⇒ 一切照旧。

    没有这条，"拔掉一支仍全绿"可能只是挡子根本没生效。
    """
    with _blocked("acp.adapters.this_one_does_not_exist"):
        assert len(load_registrations()) == 4
        for registration in load_registrations():
            assert registration.resolve() is not None


def test_the_blocker_really_blocks():
    """前置动作（不是断言的替代品）：挡子对**在册**模块确实会抛 —— 否则上面那两条红不出任何东西。"""
    with _blocked("acp.adapters.doubao"), pytest.raises(ModuleNotFoundError):
        importlib.import_module("acp.adapters.doubao")
