"""T038：`AgentAdapter` 的形状，以及围着它的三条静态边界（FR-009 / FR-011 / D-7｜R-01）。

本文件落 `tests/contract/`（分档由目录决定，见 `tests/conftest.py`，不需要装饰器）。
三条边界都写成**扫描器**而不是运行时断言，理由在各自那条上。扫描逻辑为什么在这里而不在
`src/acp/guard/`：`plan.md` 的目录树把 `guard/` 的成员钉死为 `triple / readonly / autonomy / naming`
四支，再造第五支属新增模块（要先在 Complexity Tracking 登记才可动），而这三条判据眼下只有测试在消费。
Phase 11（FR-066 ~ FR-070 自证）要把它们收进统一门禁时，再按登记流程搬。

**读这些绿之前要先知道的**：对 `src/acp/orchestrate/` 的现跑基线是 **0 命中**，而"0 命中"自己
不构成保证 —— 每条扫描都配**合成正对照**（真违例样本 MUST 被抓）与**合成负对照**（合法写法 MUST 不被抓），
否则它就是死判据。`test_orchestrate_sources_are_actually_read` 钉的是第三种失效：扫了个空目录。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import acp.adapters as adapters_pkg
from acp.adapters import AgentAdapter

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT = REPO_ROOT / "specs" / "001-multi-agent-collab-platform" / "contracts" / "agent-protocol.md"
ORCHESTRATE_ROOT = REPO_ROOT / "src" / "acp" / "orchestrate"
SECTION_4 = "## 4. `AgentAdapter` 接口"
SECTION_1_2 = "### 1.2 `PATCH /tasks/{task_id}`"

VENDOR_LITERALS = frozenset({"workbuddy", "qoder", "doubao"})
FORM_LITERALS = frozenset({"IN_PLATFORM", "SDK", "API", "CLI", "GUI"})
DISCRIMINATOR_NAMES = frozenset({"vendor", "form"})
VENDOR_SDK_MODULES = frozenset({"qoder_agent_sdk", "openai"})

ATTR_LINE = re.compile(r"^ {4}(?P<name>\w+): (?P<annotation>\S.*?)$")
METHOD_LINE = re.compile(r"^ {4}def (?P<name>\w+)\((?P<params>.*?)\) -> (?P<returns>\S.*?): \.\.\.$")


def section_fence(text: str, heading: str) -> str:
    """取契约某小节内**唯一**的 ```python 围栏正文。

    围栏数不等于 1 就抛：那一节出现了第二份形状说法，挑一个来比就等于自证。
    """
    start = text.index(heading)
    rest = text[start + len(heading) :]
    next_section = re.search(r"^## \d", rest, re.M)
    section = rest[: next_section.start()] if next_section else rest
    fences = re.findall(r"```python\n(.*?)```", section, re.S)
    if len(fences) != 1:
        raise AssertionError(f"{heading} 的 python 围栏数 = {len(fences)}，期望 1")
    return fences[0]


def canon(annotation: object) -> str:
    """注解取一个可比对的规范名。

    两头都要归一：契约围栏里的是**源码文本**（`'CapabilityManifest'`），实现里若是
    `from __future__ import annotations` 则也是字符串，若不是则是**真对象**。不归一的话，
    形状比对就会把"有人删掉了那行 future import"这种与形状无关的改动判成违例（假阳性），
    而那恰恰是让门禁长期被绕开的典型理由。`None` 特殊处理：它是值不是类型，`__name__` 取不到。
    """
    if annotation is None:
        return "None"
    if isinstance(annotation, str):
        return re.sub(r"\s+", "", annotation)
    return getattr(annotation, "__name__", str(annotation))


def parse_fence(fence: str) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """围栏 → (数据成员 {名: 注解}, 方法 {名: {参数名: 注解, 'return': 返回注解}})。"""
    attrs: dict[str, str] = {}
    methods: dict[str, dict[str, str]] = {}
    for line in fence.splitlines():
        method = METHOD_LINE.match(line)
        if method:
            annotations: dict[str, str] = {}
            for raw in method["params"].split(","):
                piece = raw.strip()
                if not piece or piece == "self":
                    continue
                name, _, annotation = piece.partition(":")
                annotations[name.strip()] = canon(annotation.strip())
            annotations["return"] = canon(method["returns"].strip())
            methods[method["name"]] = annotations
            continue
        attr = ATTR_LINE.match(line)
        if attr:
            attrs[attr["name"]] = canon(attr["annotation"])
    return attrs, methods


def protocol_shape(cls) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """运行期取同一个形状：类 `__annotations__` 是数据成员，函数 `__annotations__` 是签名。

    下划线开头的都跳过：Protocol 的元类会往 `vars(cls)` 里塞 `_abc_impl` / `_is_runtime_protocol`
    这类内部件，把它们算进"协议成员"就永远与契约不同形（这是自指失效，不是产品缺陷）。
    """
    attrs = {name: canon(value) for name, value in cls.__annotations__.items()}
    methods = {}
    for name, member in vars(cls).items():
        if name.startswith("_") or not callable(member):
            continue
        methods[name] = {
            key: canon(value) for key, value in getattr(member, "__annotations__", {}).items()
        }
    return attrs, methods


def shape_diff(left, right) -> list[str]:
    """两侧形状的逐条差异；空列表 ⇔ 名字、参数名、注解文本全同。"""
    l_attrs, l_methods = left
    r_attrs, r_methods = right
    out: list[str] = []
    for name in sorted(set(l_attrs) ^ set(r_attrs)):
        out.append(f"数据成员 {name} 只在一侧出现")
    for name in sorted(set(l_attrs) & set(r_attrs)):
        if l_attrs[name] != r_attrs[name]:
            out.append(f"数据成员 {name} 注解不同：实现={l_attrs[name]} 契约={r_attrs[name]}")
    for name in sorted(set(l_methods) ^ set(r_methods)):
        out.append(f"方法 {name} 只在一侧出现")
    for name in sorted(set(l_methods) & set(r_methods)):
        l_ann, r_ann = l_methods[name], r_methods[name]
        for key in sorted(set(l_ann) ^ set(r_ann)):
            out.append(f"方法 {name} 的 {key} 只在一侧声明")
        for key in sorted(set(l_ann) & set(r_ann)):
            if l_ann[key] != r_ann[key]:
                out.append(f"方法 {name} 的 {key} 注解不同：实现={l_ann[key]} 契约={r_ann[key]}")
    return out


def bare_dict_hits(shape) -> list[str]:
    """D-7：任何签名（数据成员 / 参数 / 返回）都不许出现裸 `dict` 这个词。"""
    attrs, methods = shape
    table = dict(attrs)
    for method, annotations in methods.items():
        for key, annotation in annotations.items():
            table[f"{method}.{key}"] = annotation
    return sorted(name for name, annotation in table.items() if re.search(r"\bdict\b", annotation))


def branching_hits(source: str) -> list[str]:
    """编排层不许按厂商 / 形态分支（宪法 II）。

    只检 `ast.Compare` 的操作数 ⇒ 文档字符串与注释天然不参与（`nodes/plan.py` 的 docstring 里就写着
    `adapters/<vendor>/transport.py`，按文本匹配会误报，按 AST 不会）。
    """
    hits: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Compare):
            continue
        for operand in [node.left, *node.comparators]:
            if isinstance(operand, ast.Name) and operand.id in DISCRIMINATOR_NAMES:
                hits.append(f"{node.lineno}: 判别变量 {operand.id} 进了比较")
            if isinstance(operand, ast.Constant) and isinstance(operand.value, str):
                if operand.value.lower() in VENDOR_LITERALS:
                    hits.append(f"{node.lineno}: 与厂商字面量 {operand.value!r} 比较")
                elif operand.value in FORM_LITERALS:
                    hits.append(f"{node.lineno}: 与形态字面量 {operand.value!r} 比较")
    return hits


def vendor_import_hits(source: str) -> list[str]:
    """编排层不许 import 具体 transport 或厂商 SDK，只许见协议（FR-012：新增 Agent 不改编排图）。"""
    hits: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.split(".")[0] in VENDOR_SDK_MODULES:
                hits.append(f"{node.lineno}: 从厂商 SDK 导入 {module}")
            elif re.match(r"^acp\.adapters\.\w", module) and module != "acp.adapters.base":
                hits.append(f"{node.lineno}: 从具体适配器导入 {module}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in VENDOR_SDK_MODULES:
                    hits.append(f"{node.lineno}: 导入了厂商 SDK {alias.name}")
    return hits


def orchestrate_sources() -> list[Path]:
    """只收 `.py` 且跳过 `__pycache__` —— 那是 gitignore 里的编译产物，算进扫描面就是读旧字节码。"""
    return [
        path
        for path in sorted(ORCHESTRATE_ROOT.rglob("*.py"))
        if "__pycache__" not in path.parts
    ]


def contract_fence() -> str:
    return section_fence(CONTRACT.read_text(encoding="utf-8"), SECTION_4)


def own_members(cls) -> set[str]:
    """沿 MRO 取"成员名"集合（`vars()` 只看自己那层，子类加的第五支会看不见 ⇒ 必须走 MRO）。"""
    return {
        name
        for base in cls.__mro__
        for name, member in vars(base).items()
        if not name.startswith("_") and callable(member)
    }


# —— 一、形状：实现 ↔ 契约 §4 的围栏 ——


def test_agent_adapter_shape_equals_contract_section_4():
    diff = shape_diff(protocol_shape(AgentAdapter), parse_fence(contract_fence()))
    assert diff == [], "AgentAdapter 与契约 §4 不同形：\n" + "\n".join(diff)


@pytest.mark.parametrize(
    ("old", "new", "expected_fragment", "expected_lines"),
    [
        (
            "    def cancel(self, handle: Handle) -> None: ...",
            "    def stream(self, handle: Handle) -> None: ...",
            "方法 cancel 只在一侧出现",
            2,
        ),
        (
            "    def submit(self, task: Task) -> Handle: ...",
            "    def submit(self, task: Task) -> dict: ...",
            "方法 submit 的 return 注解不同",
            1,
        ),
        ("    agent_id: str\n", "", "数据成员 agent_id 只在一侧出现", 1),
        (
            "    def collect(self, handle: Handle) -> TaskResult: ...",
            "    def collect(self, slot: Handle) -> TaskResult: ...",
            "方法 collect 的 slot 只在一侧声明",
            2,
        ),
    ],
    ids=["rename-cancel", "submit-returns-dict", "drop-agent_id", "rename-param"],
)
def test_shape_comparator_fires_on_each_single_point_mutation(
    old, new, expected_fragment, expected_lines
):
    """比较器不恒绿：契约围栏各改**一处** ⇒ 差异 MUST 抓到这一处。

    `expected_lines` 不是凑数：**改名类变异必然产生两条**（旧名只在一侧 + 新名只在另一侧），
    改注解与删成员才是一条。把它写成"1 条"会把一个正确的比较器判成脆的 —— 这是我自己先踩的一次，
    所以这里把两侧的分类讲清楚，而不是只留一个数。
    """
    mutated = contract_fence().replace(old, new)
    assert mutated != contract_fence(), f"变异没开火：锚点 {old!r} 不在围栏里"
    diff = shape_diff(protocol_shape(AgentAdapter), parse_fence(mutated))
    assert any(expected_fragment in line for line in diff), diff
    assert len(diff) == expected_lines, f"{expected_lines} 条应为：{diff}"


def test_section_fence_refuses_a_section_without_exactly_one_python_fence():
    """`section_fence` 的"围栏数必须是 1"不是摆设，两个方向都开过火。

    真实数据那一支用 §1.2：它的两个围栏是 ```text（机读白名单），**python 围栏数 = 0** ⇒ MUST 抛，
    不许"取不到就当空串"往下比。合成那一支给两支 python 围栏 ⇒ MUST 抛"= 2"。
    """
    text = CONTRACT.read_text(encoding="utf-8")
    with pytest.raises(AssertionError, match="围栏数 = 0"):
        section_fence(text, SECTION_1_2)
    synthetic = "## 9. X\n```python\na = 1\n```\n正文\n```python\nb = 2\n```\n"
    with pytest.raises(AssertionError, match="围栏数 = 2"):
        section_fence(synthetic, "## 9. X")
    assert section_fence("## 9. X\n```python\nc = 3\n```\n", "## 9. X") == "c = 3\n"


def test_no_signature_accepts_or_returns_a_bare_dict():
    fence_shape = parse_fence(contract_fence())
    live_shape = protocol_shape(AgentAdapter)
    assert bare_dict_hits(live_shape) == [], bare_dict_hits(live_shape)
    assert bare_dict_hits(fence_shape) == [], bare_dict_hits(fence_shape)
    # 正对照：同样两个落点写进 `dict` 就必须被抓（否则上面两条是恒真的空扫）
    poisoned = {"agent_id": "dict[str, str]"}, {"submit": {"task": "Task", "return": "dict"}}
    assert bare_dict_hits(poisoned) == ["agent_id", "submit.return"]


# —— 二、结构检查的能与不能（runtime_checkable 的边界）——


class FullStub:
    agent_id: str = "stub"

    def describe(self) -> object:
        return None

    def submit(self, task) -> object:
        return None

    def collect(self, handle) -> object:
        return None

    def cancel(self, handle) -> object:
        return None


@pytest.mark.parametrize("dropped", ["describe", "submit", "collect", "cancel"])
def test_protocol_rejects_a_stub_missing_exactly_one_method(dropped):
    """四支缺一支就不再是适配器：逐支单独摘掉（单点变异），`isinstance` MUST 转 False。"""
    assert isinstance(FullStub(), AgentAdapter), "完整 stub 应先通过，否则下面的 False 不算证据"
    namespace = {k: v for k, v in vars(FullStub).items() if k != dropped}
    assert not isinstance(type("Stripped", (object,), namespace)(), AgentAdapter)


def test_runtime_checkable_cannot_refuse_extra_members_so_the_shape_gate_is_the_real_one():
    """`runtime_checkable` 只查"有没有"，不查"多没多" ⇒ 第五支靠名字集合比对拦，不靠 isinstance。"""
    five = type("FiveMethods", (FullStub,), {"stream": lambda self, handle: None})
    assert isinstance(five(), AgentAdapter), "isinstance 不该因多一支而 False（这是它的已知边界）"
    fence_methods = set(parse_fence(contract_fence())[1])
    members = own_members(five)
    assert members - fence_methods == {"stream"}, members
    assert fence_methods - members == set(), members


def test_issubclass_raises_because_the_protocol_has_a_data_member():
    """`agent_id: str` 是非方法成员 ⇒ `issubclass` 抛 TypeError。

    写下来防的是"有人把门禁改成 `issubclass(Stub, AgentAdapter)`"——那会直接炸，而不是判否。
    """
    with pytest.raises(TypeError):
        issubclass(FullStub, AgentAdapter)


def test_protocol_declares_no_scheduler_or_streaming_members():
    """宪法 II / 契约 §4：协议里没有 plan / select / route，也没有 `stream`。

    `stream()` 出现在 `plan.md` 设计项 6 与本清单 T038 行，但契约 §4 没有它，spec 的 FR 全集里也没有
    任何一条要求流式回传（现跑 `grep -rni stream spec.md` = **0 命中**）⇒ 本轮按契约四支落地，
    那两处与契约的差异**已登记待 owner 裁**（T038 行下），不在此发明第五支。
    """
    members = set(protocol_shape(AgentAdapter)[1])
    assert members == {"describe", "submit", "collect", "cancel"}, members
    assert not members & {"plan", "select", "route", "stream", "capability"}


def test_adapters_package_exports_only_the_protocol():
    """`acp.adapters` 的对外面只有协议本体：导不到具体 transport，也就导不到厂商字段。"""
    assert adapters_pkg.__all__ == ["AgentAdapter"]
    assert adapters_pkg.AgentAdapter is AgentAdapter


# —— 三、编排层的两条静态边界（现跑 + 合成对照）——


def test_orchestrate_sources_are_actually_read():
    """扫描面非空：目录扫空会让下面两条 0 命中判据变成自证。"""
    sources = orchestrate_sources()
    assert sources, f"{ORCHESTRATE_ROOT} 下没扫到 .py"
    assert all(path.suffix == ".py" for path in sources)
    assert not any("__pycache__" in path.parts for path in sources)


def test_orchestrate_has_no_vendor_or_form_branching():
    offenders = {
        path.relative_to(REPO_ROOT).as_posix(): hits
        for path in orchestrate_sources()
        if (hits := branching_hits(path.read_text(encoding="utf-8")))
    }
    assert offenders == {}, offenders


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('if vendor == "qoder":\n    pass\n', 2),
        ('if form == "SDK":\n    pass\n', 2),
        ('if handle.agent_id == "workbuddy":\n    pass\n', 1),
        ('if "doubao" != vendor:\n    pass\n', 2),
    ],
    ids=["vendor-eq", "form-eq", "agent-id-literal", "vendor-ne"],
)
def test_branching_scanner_fires_on_synthetic_violations(source, expected):
    hits = branching_hits(source)
    assert len(hits) == expected, hits


def test_branching_scanner_leaves_legal_comparisons_alone():
    """负对照：合法的 id 对 id 比较、以及 docstring 里的 `<vendor>` 占位都不许被算成违例。"""
    legal = (
        '"""见 adapters/<vendor>/transport.py。"""\n'
        "if handle.task_id == task.task_id:\n"
        "    return handle.agent_id != reviewer_agent_id\n"
    )
    assert branching_hits(legal) == []
    plan_node = (REPO_ROOT / "src" / "acp" / "orchestrate" / "nodes" / "plan.py").read_text(
        encoding="utf-8"
    )
    assert branching_hits(plan_node) == [], "nodes/plan.py 的 docstring 含 <vendor>，AST 扫描不应误报"


def test_orchestrate_imports_no_concrete_adapter_or_vendor_sdk():
    offenders = {
        path.relative_to(REPO_ROOT).as_posix(): hits
        for path in orchestrate_sources()
        if (hits := vendor_import_hits(path.read_text(encoding="utf-8")))
    }
    assert offenders == {}, offenders


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("from acp.adapters.workbuddy import transport\n", 1),
        ("import qoder_agent_sdk\n", 1),
        ("from openai import OpenAI\n", 1),
    ],
    ids=["concrete-adapter", "qoder-sdk", "openai-sdk"],
)
def test_vendor_import_scanner_fires_on_synthetic_violations(source, expected):
    assert len(vendor_import_hits(source)) == expected


def test_vendor_import_scanner_allows_the_protocol_import():
    """负对照：`from acp.adapters import AgentAdapter` 与 `acp.adapters.base` 是合法路径。"""
    legal = (
        "from acp.adapters import AgentAdapter\n"
        "from acp.adapters.base import AgentAdapter as Base\n"
        "from acp.protocol import Task\n"
    )
    assert vendor_import_hits(legal) == []
