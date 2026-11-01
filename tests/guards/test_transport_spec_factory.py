"""MVP-2 笔 H 的判据：`config/runtime.yaml` 里那个数怎么走到厂商报文（owner 2026-10-19 裁决 2）。

**与契约档的分工**（这一格先说清，免得两档量同一件事）：

- `tests/contract/test_doubao_transport.py:255` 证的是一截：**给了** `spec.max_tokens` ⇒ 报文里有这个键，
  没给 ⇒ 没有。它的入参是手搓的 spec。
- 本文件证的是另外三截：`runtime.yaml` → `spec.max_tokens` → 报文（同一发里跑完，见节 一）、
  构造点**唯一**且不许旁路（节 二）、缺配置时**拒**而不是猜（节 三）、面板那栏的读数与这些事实的关系
  （节 四，含"spec 的构造点仍只在工厂自己那一个文件里"那格绝对读数；真装配的调用方读数随笔 P 搬去
  `tests/guards/test_realrun_driver.py`，改口的去向写在那一格的 docstring 里）。
  **【2026-10-19 B 计划 第一步追加】** 还有节 五：真装配面（造出真 transport、凭据名来自厂商模块、
  超时只有一个载体、三种网络死法各成一格、`IN_PLATFORM` 拒、`acp.run` 那道 stub 门一格没放宽）。
  节 三 里原来那条"工厂只读一支环境"被本节改口 —— 改口的理由与两半读数搬到哪里，写在那一格的 docstring 里。

**跨档借用点名**：`task()` / `completion_body()` / `build()` 是从
`tests/contract/test_doubao_transport.py` 按文件路径 load 进来用的（机制与该文件自己 load 兄弟档同源）。
理由只有一条 —— `Task` 的 13 项必填在本仓只许有一份抄本，在这里抄第二份就是亲手制造
"两份 fixture 各自漂开"那种缺陷；借用则保证本文件量的是 transport 的真报文面，不是我另造的形状。

全程零出网：对端是 `httpx.MockTransport`（成本纪律 5）。用例写的模型 ID 是一个
`factory-fixture-model` 形态的假值，真凭据与真厂商 ID 一次都不出现。
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import importlib.util
import inspect
import os
import pathlib
import re

import httpx
import pytest
import yaml
from pydantic import ValidationError

from acp.adapters import doubao as doubao_pkg
from acp.adapters import spec_factory
from acp.adapters.doubao.transport import API_KEY_ENV, DoubaoApiSpec, DoubaoTransport, DoubaoTransportError
from acp.adapters.qoder.transport import (
    CN_PAT_ENV,
    CREDENTIAL_ENV_NAMES,
    INTL_PAT_ENV,
    SERVICE_ACCOUNT_ENV,
    QoderCliSpec,
    QoderTransport,
)
from acp.adapters.stub import StubMisuse, StubAdapter, TRANSPORT_FLAG, resolve_transport_flag
from acp.adapters.workbuddy.transport import WorkBuddyApiSpec
from acp.api import web
from acp.bus.gateway import BusGateway
from acp.config import runtime
from acp.registry.loader import load_registrations

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "acp"
REAL_CARRIER = REPO_ROOT / "config" / "runtime.yaml"

#: 只用于构造 env 注入的假值。刻意不成任何真实形态（口径同契约档那句注释）。
FAKE_MODEL = "factory-fixture-model"
FAKE_KEY = "spec-fixture-ark-key"

#: 直接构造 `QoderTransport` 时用的哑 runner：`_require_credential()` 只读环境、不碰这个字段，
#: 所以用例永远不会因为这一发而真起 `qodercli` 子进程（成本纪律 5 的另一面：本机进程也不许随手起）。
_quiet_runner = object()

#: 三支厂商 spec 的类名字面集合 —— 节 二那条"不许绕开工厂"要按这三名字扫，一份都不能漏。
#: 从类本身取（`__name__`），不在这里手抄字符串：手抄的那一份会跟着重命名漂开。
VENDOR_SPEC_CLASSES = (DoubaoApiSpec, QoderCliSpec, WorkBuddyApiSpec)
VENDOR_SPEC_NAMES = frozenset(cls.__name__ for cls in VENDOR_SPEC_CLASSES)


def _load_contract_fixture():
    path = REPO_ROOT / "tests" / "contract" / "test_doubao_transport.py"
    spec = importlib.util.spec_from_file_location("spec_factory_fixture", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_doubao = _load_contract_fixture()


def carrier(tmp_path: pathlib.Path, monkeypatch, max_tokens) -> pathlib.Path:
    """把 §12 那份载体临时挪到 `tmp_path`，写入给定的 `max_tokens`。

    挪的是 `runtime.RUNTIME_PATH` 这一个模块属性，**不是**改工厂的签名去收路径：加载器模块头明写
    "生产调用方 MUST 不传 path"，用例也不能给它开那个口子（否则"面板写的"与"生效的"可以指向两份文件）。
    真仓库那份文件在本发前后指纹必须相同 —— 那条断言写在节 一，量的是"用例没碰用户的数据"。
    """
    target = tmp_path / "runtime.yaml"
    # 第三格 `auto_accept_low_risk`（2026-10-28 加）必须一并写上：加载器按"缺必填键"拒，
    # 少写一格就整条报错 —— 本文件量的是"那个数怎么走到报文"，与这一格无关，但不能因此写坏文件。
    text = (
        f"max_tokens: {'null' if max_tokens is None else max_tokens}\n"
        "fragment_merge_ms: 500\nauto_accept_low_risk: null\n"
        "artifact_floors:\n  html: [512, 12]\n  css: [512, 12]\n  py: [400, 10]\n"
        "  js: [400, 10]\n  md: [200, 6]\n  json: [120, 5]\n"
    )
    target.write_text(text, encoding="utf-8", newline="\n")
    monkeypatch.setattr(runtime, "RUNTIME_PATH", target)
    return target


def build_spec(**env_override):
    env = {spec_factory.MODEL_ENV: FAKE_MODEL, **env_override}
    return spec_factory.build_transport_spec("doubao", env=env)


def real_carrier_digest() -> str:
    return "sha256:" + hashlib.sha256(REAL_CARRIER.read_bytes()).hexdigest()


# ------------------------------------------------------------------ 节 一：载体 → spec → 报文


def test_a_changed_carrier_value_reaches_the_outgoing_body(tmp_path, monkeypatch):
    """改那份文件 ⇒ **线上报文里的数跟着改**。两轮，值不同 ⇒ 排除"读一次就缓存"与"写死一个数"。

    这正是裁决 2 要的那条"不是假绿"：工厂只把 `params.max_tokens` 抄进 spec 而没人调它，这一发不会红；
    所以这里从载体一路量到 `record[0]["body"]`，中间每一步都是真代码。
    """
    before = real_carrier_digest()
    seen: list[object] = []
    for value in (4321, 999):
        carrier(tmp_path, monkeypatch, value)
        spec = build_spec()
        bus_root = tmp_path / str(value) / "AI项目台" / "demo"
        bus_root.mkdir(parents=True)
        transport, record = _doubao.build(
            BusGateway(bus_root), [(200, _doubao.completion_body())], api=spec, env={API_KEY_ENV: FAKE_KEY}
        )
        transport.submit(_doubao.task())
        seen.append(spec)
        assert record[0]["body"]["max_tokens"] == value, f"第 {value} 轮报文里没有这个数"
        assert record[0]["body"]["model"] == FAKE_MODEL, "模型 ID 没从环境走到报文"
    assert [item.max_tokens for item in seen] == [4321, 999], "两次构造拿到了同一个值 ⇒ 有缓存"
    assert seen[0] is not seen[1], "两次构造返回同一个对象 ⇒ 第二份配置根本没被读"
    assert real_carrier_digest() == before, "用例改到了仓库里那份真载体"


def test_a_null_carrier_still_omits_the_key_on_the_wire(tmp_path, monkeypatch):
    """`null` 是一个合法取值 = **不发这个键**，不是"发 0"也不是"回落成某个默认上限"。

    `runtime.MAX_TOKENS_BOUND` 那一档把错形拦在加载器里，这一发管的是合法的另一支。
    """
    carrier(tmp_path, monkeypatch, None)
    spec = build_spec()
    assert spec.max_tokens is None
    bus_root = tmp_path / "null" / "AI项目台" / "demo"
    bus_root.mkdir(parents=True)
    transport, record = _doubao.build(
        BusGateway(bus_root), [(200, _doubao.completion_body())], api=spec, env={API_KEY_ENV: FAKE_KEY}
    )
    transport.submit(_doubao.task())
    assert "max_tokens" not in record[0]["body"], record[0]["body"].keys()


def test_the_factory_signature_cannot_be_handed_a_number_directly():
    """签名里只有 `agent_id` / `env` / `registry_path`，**没有 `max_tokens` 这一项**。

    给长度开一个形参 = 开第二条来源：面板写的是一份文件、调用方传的是另一个数，而两者不会报错，
    只会行为不同。这条把"长度只有一个来源"钉成结构而不是习惯。
    """
    parameters = inspect.signature(spec_factory.build_transport_spec).parameters
    assert set(parameters) == {"agent_id", "env", "registry_path"}, sorted(parameters)
    assert not [name for name in parameters if "token" in name.lower()], "长度可以从签名塞进来了"
    assert all(
        item.kind is item.KEYWORD_ONLY for name, item in parameters.items() if name != "agent_id"
    ), "注入项必须全是关键字参数（位置参数最容易在调用点上错位）"


# ------------------------------------------------------------------ 节 二：构造点唯一、不许旁路


def _call_sites(name: str, root: pathlib.Path) -> list[str]:
    """`root` 下按**被调用者名字**取 Call 的那些文件（相对 POSIX 路径）。"""
    hits = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            if (isinstance(target, ast.Name) and target.id == name) or (
                isinstance(target, ast.Attribute) and target.attr == name
            ):
                hits.append(path.relative_to(root).as_posix())
                break
    return hits


def test_no_production_module_constructs_a_vendor_spec_bypassing_the_factory(tmp_path):
    """工厂认领的那几支 spec，`src/acp/` 里没有任何一处直接构造 —— 带长度的构造只能经工厂。

    工厂本身也**不出现** `DoubaoApiSpec(...)` 这个字面调用：它按 `SPEC_SHAPES` 定位类再实例化。
    所以这一条量的不是"工厂里有一行构造"，而是"有没有第二个地方绕开那张表、自己造一份能吃
    `max_tokens` 的 spec"。没有 `tmp_path` 那份正对照，这条就是"扫描面塌成空集也算过"的那种绿。

    **覆盖面限制点名**（这一格不是通配，是现读出来的差别）：扫的类名集合 = `SPEC_SHAPES` 的值，
    不是"全部厂商 spec"。差别在 `adapters/qoder/transport.py:339` 那一行
    `self.spec = spec or QoderCliSpec()` —— 那是 qoder 的**自默认**（线协议字面量本来就集中在那个
    dataclass 的默认值里），而它的字段名里根本没有长度这一项（下一条判据量的就是这件事），
    所以它不构成 §12 那个数的第二个来源。把它算进违例会把一条真判据变成恒红，把它写进这张表才是说谎。
    """
    scanned = sorted(class_ for _, class_ in spec_factory.SPEC_SHAPES.values())
    assert scanned and set(scanned) <= VENDOR_SPEC_NAMES, f"表里的类名不在本文件认得的三支里：{scanned}"
    offenders: dict[str, list[str]] = {}
    for class_name in scanned:
        if hits := _call_sites(class_name, SRC_ROOT):
            offenders[class_name] = hits
    assert offenders == {}, f"厂商 spec 在工厂之外被直接构造：{offenders}"

    decoy = tmp_path / "bypass.py"
    decoy.write_text(
        f"from acp.adapters.doubao.transport import {DoubaoApiSpec.__name__}\n\n"
        f"def f():\n    return {DoubaoApiSpec.__name__}(model='m')\n",
        encoding="utf-8",
        newline="\n",
    )
    assert _call_sites(DoubaoApiSpec.__name__, tmp_path) == ["bypass.py"], (
        "扫描器没看见这份违例 ⇒ 上面那条是空对空"
    )


def test_the_table_only_lists_specs_that_can_take_those_two_arguments():
    """`SPEC_SHAPES` 里每一支的 spec 都必须真有 `model` 与 `max_tokens` 这两个字段。

    这条管的是"将来往表里加一支"的那个动作：加进去的类如果不吃这两个参数，构造点会以内联方式
    替厂商编字段 —— 而那正是本文件节 三倒数第二条要挡的形态。关系式，不是清单。
    """
    for module_name, (_, class_name) in spec_factory.SPEC_SHAPES.items():
        spec_class = getattr(importlib.import_module(f"{module_name}.transport"), class_name)
        fields = {field.name for field in dataclasses.fields(spec_class)}
        assert {"model", "max_tokens"} <= fields, f"{class_name} 的字段里没有这两个：{sorted(fields)}"


# ------------------------------------------------------------------ 节 三：缺配置就拒，不猜


def test_a_missing_model_id_refuses_and_names_the_variable_without_leaking_the_key(monkeypatch, tmp_path):
    """缺 `ACP_DOUBAO_MODEL` ⇒ `SpecRefused` 点名变量名；消息里不许带上环境里那把键的值。

    顺序也是这条的一部分：拒绝发生在**读参数文件之前**（节 三第一条的归因纪律）——
    拼错部署身份的人该读到"缺模型 ID"，不该读到"参数文件坏了"。
    """
    carrier(tmp_path, monkeypatch, 4321)
    with pytest.raises(spec_factory.SpecRefused) as caught:
        spec_factory.build_transport_spec("doubao", env={API_KEY_ENV: FAKE_KEY})
    message = str(caught.value)
    assert spec_factory.MODEL_ENV in message, message
    assert FAKE_KEY not in message, "报错消息把凭据值带出去了"
    assert "runtime" not in message.lower(), f"缺模型 ID 却报成参数问题，归因错位：{message}"


def test_a_blank_model_id_is_refused_like_a_missing_one(monkeypatch, tmp_path):
    """空白 ID 与缺失同一处置。留它会得到厂商的 404，而那次 404 看起来像"模型没开通"。"""
    carrier(tmp_path, monkeypatch, 4321)
    with pytest.raises(spec_factory.SpecRefused) as caught:
        spec_factory.build_transport_spec("doubao", env={spec_factory.MODEL_ENV: "   "})
    assert spec_factory.MODEL_ENV in str(caught.value)


def test_an_unregistered_agent_is_refused(monkeypatch, tmp_path):
    carrier(tmp_path, monkeypatch, 4321)
    with pytest.raises(spec_factory.SpecRefused) as caught:
        spec_factory.build_transport_spec("no-such-agent", env={spec_factory.MODEL_ENV: FAKE_MODEL})
    assert "no-such-agent" in str(caught.value)


def test_a_disabled_registration_is_refused(monkeypatch, tmp_path):
    """在册但 `enabled: false` ⇒ 拒。关掉的一支不该因为面板里有个数就又被造出来。"""
    carrier(tmp_path, monkeypatch, 4321)
    entries = yaml.safe_load(
        (SRC_ROOT / "registry" / "agents.yaml").read_text(encoding="utf-8")
    )
    for entry in entries:
        if entry["agent_id"] == "doubao":
            entry["enabled"] = False
    registry = tmp_path / "agents.yaml"
    registry.write_text(yaml.safe_dump(entries, allow_unicode=True), encoding="utf-8", newline="\n")
    with pytest.raises(spec_factory.SpecRefused) as caught:
        spec_factory.build_transport_spec(
            "doubao", env={spec_factory.MODEL_ENV: FAKE_MODEL}, registry_path=registry
        )
    assert "enabled" in str(caught.value), str(caught.value)


@pytest.mark.parametrize("agent_id", ["qoder", "workbuddy"])
def test_a_vendor_without_a_length_field_is_refused_not_invented(monkeypatch, tmp_path, agent_id):
    """另外两支**没有**长度这一项 ⇒ 工厂拒，而不是替它们编一个字段名。

    反向对照在同一发里：这两支的 spec 字段名里确实没有 `max_tokens`，而豆包那支有。
    少了这半句，"表里没有 qoder"可以被读成"我忘了加"；有了它，读数是"这一支没有这个可设项"。
    """
    carrier(tmp_path, monkeypatch, 4321)
    with pytest.raises(spec_factory.SpecRefused) as caught:
        spec_factory.build_transport_spec(agent_id, env={spec_factory.MODEL_ENV: FAKE_MODEL})
    assert "不在工厂能构造的表里" in str(caught.value), str(caught.value)
    assert "max_tokens" not in {f.name for f in dataclasses.fields(QoderCliSpec)}
    assert "max_tokens" not in {f.name for f in dataclasses.fields(WorkBuddyApiSpec)}
    assert "max_tokens" in {f.name for f in dataclasses.fields(DoubaoApiSpec)}


def test_a_broken_carrier_file_is_not_swallowed_by_the_factory(monkeypatch, tmp_path):
    """参数文件写坏 ⇒ 让 `RuntimeParamsError` 原样抛出去。

    工厂如果在这里 catch 成"没给就是 None"，就把"配置坏了"读成了"配置选了不发这个键"——
    两种形态在报文上一模一样，而只有后者是用户的意思。
    """
    target = tmp_path / "runtime.yaml"
    target.write_text(
        "max_tokens: many\nfragment_merge_ms: 500\nauto_accept_low_risk: null\n",
        encoding="utf-8",
        newline="\n",
    )
    monkeypatch.setattr(runtime, "RUNTIME_PATH", target)
    with pytest.raises(runtime.RuntimeParamsError) as caught:
        build_spec()
    assert "max_tokens" in str(caught.value)


def test_the_factory_never_hardcodes_a_credential_env_name(tmp_path, monkeypatch):
    """工厂读环境只有两处，而且**凭据名一个都不写死在它自己源码里**（B 计划 第一步 改口，见下面"口径变更"）。

    **口径变更（owner 2026-10-19 B 计划 第一步）**：本文件原来这一格写的是
    `test_the_factory_reads_exactly_one_environment_variable` —— 断言"工厂只读 `MODEL_ENV` 一支，
    凭据仍由 transport 在 `submit()` 那一刻取"。那条判据的形状与"缺 Key 必须在这里就拒、禁止静默降级 stub"
    直接互斥，所以本笔把它改口成现在这一条；原句的两半各去了新地方：
    ① "凭据值不许被构造品持有" ⇒ `tests/guards/test_credentials.py` 与下面第 3 格（值不进消息、不进 spec）；
    ② "只有一个读环境的形状" ⇒ 仍然成立但换了取值：名字来自**厂商模块的约定属性**，不是工厂里的字面量。

    量的形状点名：这里收的是**每个 `env_value(...)` 调用第一个实参的源码形状**（`ast.unparse`），
    不是它运行期取到的值。所以多一处调用、或把某个凭据变量名当字面量塞进去，都会改变这个集合 ⇒ 红。
    """
    tree = ast.parse((SRC_ROOT / "adapters" / "spec_factory.py").read_text(encoding="utf-8"))
    read = [
        ast.unparse(node.args[0])
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "env_value"
        and node.args
    ]
    assert sorted(read) == ["MODEL_ENV", "name"], f"工厂读环境的样子变了：{read}"
    literals = [
        ast.unparse(node.args[0])
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "env_value"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    ]
    assert literals == [], f"凭据变量名被写死进工厂了：{literals}"
    # 非文档串的字符串字面量里不许出现"某个环境变量名"的形态（文档串允许提名，门禁本来就允许散文引用）。
    doc_ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)):
            first = getattr(node, "body", [None])[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                doc_ids.add(id(first.value))
    env_shaped = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in doc_ids
        and re.fullmatch(r"[A-Z][A-Z0-9_]*_(KEY|TOKEN|SECRET|DSN)", node.value)
    ]
    # 取值域点名：这条管的是**凭据形态**的名字。`MODEL_ENV` 里那个 `ACP_DOUBAO_MODEL` 是工厂自己的载体
    # （模块头那张表就写着"模型 ID = 环境变量，经 `config.loader.env_value()` 读"），不在被禁之列。
    assert env_shaped == [], f"工厂里出现了写死的凭据变量名字面量：{env_shaped}"

    # **活性对照**：把 doubao 那一支声明的凭据名换成一个假名，工厂要的东西必须跟着换 ——
    # 换成假名后仍收真 ARK 键，才证明它真的在按厂商模块的属性走，而不是扫不到所以绿。
    # 打补丁要打在**工厂那一刻解析到的模块对象**上：`tests/contract/test_all_adapters.py` 的 `_blocked()`
    # 会把 `acp.adapters.*` 从 `sys.modules` 里拔掉，本文件顶部那个绑定因此可能是个孤儿对象 ⇒ 一律现取。
    carrier(tmp_path, monkeypatch, None)
    live = importlib.import_module(doubao_pkg.__name__)
    monkeypatch.setattr(live, "CREDENTIAL_ENV_NAMES", ("ACP_FAKE_ONLY_KEY",))
    with pytest.raises(spec_factory.RealWiringRefused) as caught:
        spec_factory.build_real_transport(
            "doubao", gateway=object(), env={spec_factory.MODEL_ENV: FAKE_MODEL, API_KEY_ENV: FAKE_KEY}
        )
    assert "ACP_FAKE_ONLY_KEY" in str(caught.value), str(caught.value)
    assert FAKE_KEY not in str(caught.value), "拒绝消息里带着凭据值"
    built = spec_factory.build_real_transport(
        "doubao", gateway=object(), env={spec_factory.MODEL_ENV: FAKE_MODEL, "ACP_FAKE_ONLY_KEY": FAKE_KEY}
    )
    # 按**名字 + 模块**认类，不按 `isinstance`：本文件的类绑定可能在契约档 `_blocked()` 拔模块之后成为孤儿，
    # 那种时候工厂现取到的是同一份源码的另一个类对象，`isinstance` 会假红（全量档实测就是这两格先红的）。
    assert (type(built).__name__, type(built).__module__) == (
        "DoubaoTransport",
        "acp.adapters.doubao.transport",
    ), (type(built).__name__, type(built).__module__)


def test_blank_credential_is_refused_exactly_like_a_missing_one(tmp_path, monkeypatch):
    """空字符串与"根本没有这个键"同一处置 ⇒ 同一类异常、同样不降级。

    为什么单独一格：`httpx` 会把 `Authorization: Bearer ` 原样发出去，厂商回 401 ——
    那比在这里拒一行难查十倍（口径同 `_require_model` 对空白模型 ID 的处置）。
    """
    carrier(tmp_path, monkeypatch, None)
    missing = {spec_factory.MODEL_ENV: FAKE_MODEL}
    blank = {spec_factory.MODEL_ENV: FAKE_MODEL, API_KEY_ENV: "   "}
    caught = []
    for env in (missing, blank):
        with pytest.raises(spec_factory.RealWiringRefused) as hit:
            spec_factory.build_real_transport("doubao", gateway=object(), env=env)
        caught.append(str(hit.value))
    assert FAKE_KEY not in caught[1], caught[1]
    assert API_KEY_ENV in caught[0] and API_KEY_ENV in caught[1], caught


def test_the_credential_rule_matches_the_vendors_own_gate(tmp_path, monkeypatch):
    """**跨实现符合性**：同一份 env 状态，工厂那道门与 transport 自己那道门必须同判（两份规则不许分叉）。

    这里两侧都取得到：qoder 的凭据门是 `_require_credential()`（只读环境、不起子进程、不发网络），
    工厂的是 `build_real_transport()`。四档 env 各比一次：只配 CN PAT / 只配 INTL PAT /
    只配 Service Account / 一个都没。名字集合从厂商模块现取（`CREDENTIAL_ENV_NAMES`）——
    2026-10-24 CN 切换后它是三个名字，本用例的"一个都没"那档必须把**三个都摘掉**，
    否则机器上导出了 CN 名时那一档会静默变成"只配了 CN PAT"（口径同本轮对旧判据的临时 home 注入）。
    工厂没有要求"都得有"，因为厂商模块自己的语义是 `CREDENTIAL_ENV_NAMES` 里**任一**有值即可 ——
    要求全部有会拒掉一个合法部署，也和 transport 不一致。
    """
    carrier(tmp_path, monkeypatch, None)
    base = {k: v for k, v in os.environ.items() if k not in CREDENTIAL_ENV_NAMES}
    gate = tmp_path / "AI项目台" / "demo"
    gate.mkdir(parents=True)
    for env in (
        {**base, CN_PAT_ENV: FAKE_KEY},
        {**base, INTL_PAT_ENV: FAKE_KEY},
        {**base, SERVICE_ACCOUNT_ENV: FAKE_KEY},
        dict(base),
    ):
        transport = None
        try:
            transport = spec_factory.build_real_transport(
                "qoder", gateway=BusGateway(gate), env=env, cwd=gate
            )
            factory_refused = False
        except spec_factory.RealWiringRefused:
            factory_refused = True
        if factory_refused:
            assert transport is None
            continue
        assert (type(transport).__name__, type(transport).__module__) == (
            "QoderTransport",
            "acp.adapters.qoder.transport",
        ), type(transport).__name__
        transport._require_credential()  # 同一份 env 也必须过厂商自己那道门（不抛 = 一致）
    # 反向那一档单独再量一次：一个都没有时，两侧都得拒，且消息里只有变量名。
    bare = dict(base)
    with pytest.raises(spec_factory.RealWiringRefused) as by_factory:
        spec_factory.build_real_transport("qoder", gateway=object(), env=bare, cwd=tmp_path)
    assert CN_PAT_ENV in str(by_factory.value) and INTL_PAT_ENV in str(by_factory.value)
    assert FAKE_KEY not in str(by_factory.value)
    # 同一份 env 直接喂厂商自己那道门（构造 runner 只是存起来，不起子进程、不发网络）。
    same_transport = QoderTransport(runner=_quiet_runner, gateway=object(), env=bare, cwd=tmp_path)
    with pytest.raises(RuntimeError) as by_vendor:
        same_transport._require_credential()
    assert CN_PAT_ENV in str(by_vendor.value) and INTL_PAT_ENV in str(by_vendor.value), str(
        by_vendor.value
    )


# ------------------------------------------------------------------ 节 四：面板那栏与诚实登记


def test_the_panel_names_the_construction_point_for_max_tokens(monkeypatch, tmp_path):
    """`GET /runtime` 的 `consumers["max_tokens"]` 现在必须**有名字**，且那个名字就是构造点。

    这一条同时钉两件事：
    ① 消费者从 `[]` 变成 `["adapters/spec_factory.py"]` —— 那是本笔唯一的"生效面变化"，
       它必须以一次显式的判据改动为代价（旧读数 `[]` 写在 `test_runtime_params.py` 里，同笔改掉）；
    ② 被点名的那个模块**真能造出带这个数的 spec**（下面那发实例化就是证据），
       不是随便一个读了键的函数。
    """
    assert web._runtime_consumers("max_tokens") == ["adapters/spec_factory.py"]
    carrier(tmp_path, monkeypatch, 2048)
    assert isinstance(build_spec(), DoubaoApiSpec)


def test_the_factory_still_has_no_production_caller(tmp_path):
    """诚实登记（W-8）：`src/acp/` 里今天还没有任何模块调用工厂 —— 这一格是**故意的绝对读数**。

    成因不是漏接：`run.py` 的执行段只构造 `StubAdapter`，`resolve_transport_flag()` 只放 `stub`
    （R-07："一次误敲就是一次真实外部调用"）。所以面板那栏从 `[]` 变成有名字，说的是
    "这一段就是它生效的代码"，**不是**"改完已经有线上效果"。
    把真跑那一发接进来的那一天，这一格必须同时改掉并改 `spec_factory` 模块头那一段 ——
    宁可让人手动改判据，也不许它静默变绿。`tmp_path` 那份正对照管的是"扫描器真能看见调用点"。
    """
    defining = (SRC_ROOT / "adapters" / "spec_factory.py").relative_to(SRC_ROOT).as_posix()
    callers = [hit for hit in _call_sites("build_transport_spec", SRC_ROOT) if hit != defining]
    assert callers == [], f"工厂已有生产调用点：{callers} ⇒ 同步改掉模块头那段'今天没有生产调用点'"

    decoy = tmp_path / "caller.py"
    decoy.write_text(
        "from acp.adapters.spec_factory import build_transport_spec\n\n"
        "def f():\n    return build_transport_spec('doubao')\n",
        encoding="utf-8",
    )
    assert _call_sites("build_transport_spec", tmp_path) == ["caller.py"], "扫描器看不见调用点 ⇒ 上面那条是空对空"


def test_the_registry_is_read_from_the_carrier_not_from_a_name_list():
    """工厂认得哪些 `agent_id` 是从注册表现读的，不是在工厂里另抄一份名单。

    取证方式：拿在册的三支逐个构造 —— 认表的证据 = 那三支里只有一支能通过，
    另外两支的拒绝消息里出现的是**入口字符串**（来自 yaml），不是我在工厂里写死的 agent 名。
    """
    registered = {item.agent_id: item.adapter_entry for item in load_registrations()}
    assert sorted(registered) == ["deepseek-reviewer", "doubao", "qoder", "workbuddy"]
    # 审查身份不在工厂能构造的表里 ⇒ 它连 spec 都造不出来，这是"只服务审查"的结构保证之一。
    assert "acp.adapters.deepseek" not in spec_factory.SPEC_SHAPES
    assert set(spec_factory.SPEC_SHAPES) <= {entry.partition(":")[0] for entry in registered.values()}


# ---------------------------------------------------- 节 五：真装配面（B 计划 第一步 / 第三步 1、2）


def test_the_constructed_objects_are_the_real_transports_not_the_stub(tmp_path, monkeypatch):
    """两支各自造出**真** transport，而且没有一条路会走到 `StubAdapter`（owner："禁止静默降级为 stub"）。

    这一格是形状断言不是行为断言：它证的是"类型对、模块对、stub 不是任何一种退路"。
    行为面（真请求）由 owner 亲自在终端开火，本轮零出网（成本纪律 5 + 他的纪律 4）。
    """
    carrier(tmp_path, monkeypatch, 2048)
    gate = tmp_path / "AI项目台" / "demo"
    gate.mkdir(parents=True)
    doubao = spec_factory.build_real_transport(
        "doubao",
        gateway=BusGateway(gate),
        env={spec_factory.MODEL_ENV: FAKE_MODEL, API_KEY_ENV: FAKE_KEY},
    )
    qoder = spec_factory.build_real_transport(
        "qoder",
        gateway=BusGateway(gate),
        env={"QODER_PERSONAL_ACCESS_TOKEN": FAKE_KEY},
        cwd=gate,
    )
    assert [(type(item).__name__, type(item).__module__) for item in (doubao, qoder)] == [
        ("DoubaoTransport", "acp.adapters.doubao.transport"),
        ("QoderTransport", "acp.adapters.qoder.transport"),
    ], [type(item).__name__ for item in (doubao, qoder)]
    assert [item.agent_id for item in (doubao, qoder)] == ["doubao", "qoder"], "身份不是由调用方指定的"
    for item in (doubao, qoder):
        assert type(item).__name__ != StubAdapter.__name__, item
        assert not str(type(item).__module__).startswith("acp.adapters.stub"), item


def test_the_request_timeout_comes_from_the_vendor_module_not_the_factory(tmp_path, monkeypatch):
    """超时那一格只有一个载体：httpx 那支在厂商模块的 `HTTP_TIMEOUT_S`，子进程那支在 `QoderCliSpec` 的默认值。

    两半都量：① AST —— 工厂源码里的**数值字面量集合**不含那两个超时数（写死一个就红）；
    ② 实例 —— 造出来的 http client 的 read 超时 == 模块里那个数 == 120.0，
    而且改模块那个数 ⇒ 客户端跟着改（这才证明读的是它，不是巧合同值）。
    qoder 那一支不走 httpx：它的数在 `QoderCliSpec.timeout_seconds` 的默认值上（**2026-10-20 裁决 = 900.0**，
    载体见 `config/params-ref.md §1` 的"非卡点执行超时"），工厂**没传** spec ⇒ 这里比的是"造出来的 spec
    与厂商模块那份默认 spec 整支相等"，而不是把 900 再抄一遍（抄了就变成第三个载体）。
    """
    source = (SRC_ROOT / "adapters" / "spec_factory.py").read_text(encoding="utf-8")
    numbers = {
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool)
    }
    for stolen in (120, 120.0, 900, 900.0):
        assert stolen not in numbers, f"工厂里自己写了一个超时数 {stolen}：{sorted(numbers)}"

    carrier(tmp_path, monkeypatch, None)
    live = importlib.import_module(doubao_pkg.__name__)
    built = spec_factory.build_real_transport(
        "doubao", gateway=object(), env={spec_factory.MODEL_ENV: FAKE_MODEL, API_KEY_ENV: FAKE_KEY}
    )
    assert built.http.timeout.read == live.HTTP_TIMEOUT_S == 120.0, built.http.timeout
    monkeypatch.setattr(live, "HTTP_TIMEOUT_S", 7.5)
    moved = spec_factory.build_real_transport(
        "doubao", gateway=object(), env={spec_factory.MODEL_ENV: FAKE_MODEL, API_KEY_ENV: FAKE_KEY}
    )
    assert moved.http.timeout.read == 7.5, "客户端里的数不是从模块来的"

    qoder = spec_factory.build_real_transport(
        "qoder", gateway=object(), env={"QODER_PERSONAL_ACCESS_TOKEN": FAKE_KEY}, cwd=tmp_path
    )
    assert qoder.spec == QoderCliSpec(), qoder.spec
    assert qoder.spec.timeout_seconds == QoderCliSpec().timeout_seconds == 900.0, qoder.spec
    assert "spec" not in inspect.signature(spec_factory.build_real_transport).parameters, "工厂不该替 SDK 形态开 spec 口子"


def test_the_factory_refuses_a_subprocess_form_that_was_not_given_a_home(tmp_path, monkeypatch):
    """子进程形态没交出 `cwd` ⇒ 拒（owner 2026-10-20 裁决 1）；API 形态收到它 ⇒ 照造、不转发。

    两半各挡一种坏法：
    ① 只有 transport 里那句"必填关键字参数"在管 ⇒ 工厂这一层一旦有人写 `cwd=None`，默认值就会重新变成
       "继承调用方的目录"（= 第四发点火的现场）。这一格把同一句话也在装配面说一遍，因为它才是生产路径。
    ② API 那一支不发子进程，所以它**没有**这一格可以收：转给 `DoubaoTransport` 会 `TypeError`，
       而驱动是按"不管指派给谁都要算一个家"的形状调的。所以这里钉的是"收到了、不适用、不转发"。
    凭据值不许出现在任何一条消息里（口径同节 三那两条防泄漏判据）。
    """
    carrier(tmp_path, monkeypatch, None)
    monkeypatch.setenv(spec_factory.MODEL_ENV, FAKE_MODEL)
    with pytest.raises(spec_factory.RealWiringRefused) as caught:
        spec_factory.build_real_transport(
            "qoder", gateway=object(), env={"QODER_PERSONAL_ACCESS_TOKEN": FAKE_KEY}
        )
    message = str(caught.value)
    assert "cwd" in message and "require_archive_root" in message, message
    assert FAKE_KEY not in message, "拒绝消息里带上了凭据值"

    api_built = spec_factory.build_real_transport(
        "doubao", gateway=object(), env={spec_factory.MODEL_ENV: FAKE_MODEL, API_KEY_ENV: FAKE_KEY}, cwd=tmp_path
    )
    assert type(api_built).__name__ == "DoubaoTransport", type(api_built).__name__
    assert not hasattr(api_built, "cwd"), "API 形态不该收到一个它用不上的格（转发了就是 TypeError 的前奏）"


def test_a_timeout_becomes_a_transport_error_and_lands_nothing_on_disk(tmp_path):
    """真跑会超时（owner 第三步 1）⇒ 必须变成一条 transport 失败并带着那一秒数，且不许留半成品落盘。

    注入方式：给 transport 一个**自己会抛 `httpx.TimeoutException`** 的 MockTransport；
    这是本轮唯一一处"演网络故障"的用例，全程零出网、零子进程。
    """
    root = tmp_path / "AI项目台" / "demo"
    root.mkdir(parents=True)
    calls: list = []

    def _boom(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        raise httpx.ReadTimeout("simulated: 对端在 120 秒内没有回", request=request)

    transport = DoubaoTransport(
        api=_doubao.spec(),
        http=httpx.Client(transport=httpx.MockTransport(_boom)),
        gateway=BusGateway(root),
        env={API_KEY_ENV: FAKE_KEY},
    )
    with pytest.raises(DoubaoTransportError) as caught:
        transport.submit(_doubao.task())
    note = str(caught.value)
    assert caught.value.retryable is True, caught.value.retryable
    assert "120.0" in note and "ReadTimeout" in note, note
    assert FAKE_KEY not in note and "Bearer" not in note, "超时消息里带着凭据形态的东西"
    assert len(calls) == 1, f"transport 在超时后自己重试了：{calls}"
    assert sorted(path.name for path in root.rglob("*") if path.is_file()) == [], "超时那一发不该落任何盘"


def test_a_network_error_becomes_a_transport_error_without_a_retryable_claim(tmp_path):
    """非超时的传输层失败 ⇒ 也是 transport 错误，但 `retryable` 留空 = "我们没有判定权"，不是"可以重试"。

    这一格与上一格是同一族的两种死法（owner 第三步"断网 / 超时 / 限流"里的断网那一支）。
    """
    root = tmp_path / "AI项目台" / "demo"
    root.mkdir(parents=True)

    def _boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated: 宿主连不上")

    transport = DoubaoTransport(
        api=_doubao.spec(),
        http=httpx.Client(transport=httpx.MockTransport(_boom)),
        gateway=BusGateway(root),
        env={API_KEY_ENV: FAKE_KEY},
    )
    with pytest.raises(DoubaoTransportError) as caught:
        transport.submit(_doubao.task())
    assert caught.value.retryable is None
    assert "ConnectError" in str(caught.value) and FAKE_KEY not in str(caught.value)


def test_a_malformed_body_raises_a_transport_error_not_a_pydantic_one(tmp_path):
    """坏报文 ⇒ `DoubaoTransportError`，不是 `ValidationError`，也不崩进程（owner 第三步 2 的 transport 半边）。

    这一格只量构造面之前那一截：200 + 一坨非 JSON。协议层那半（坏报文怎么停而不把整条链打死）随笔 P 落了，
    落点是 `tests/guards/test_realrun_driver.py::test_the_failure_policy_keeps_the_two_assembly_defects_loud`
    （收不收那张真值表）与同文件节 三那发真 PG（一张卡倒下、其余照跑、留痕一行）。
    """
    root = tmp_path / "AI项目台" / "demo"
    root.mkdir(parents=True)
    record: list = []
    transport = DoubaoTransport(
        api=_doubao.spec(),
        http=httpx.Client(transport=_doubao.handler_for([(200, "<html>网关返回的一坨非 JSON</html>")], record)),
        gateway=BusGateway(root),
        env={API_KEY_ENV: FAKE_KEY},
    )
    with pytest.raises(DoubaoTransportError) as caught:
        transport.submit(_doubao.task())
    assert "JSON" in str(caught.value), str(caught.value)
    assert not isinstance(caught.value, ValidationError), "协议错泄漏到了 transport 面上"


def test_workbuddy_is_refused_because_its_form_is_not_wired(tmp_path, monkeypatch):
    """`IN_PLATFORM` 不在工厂能装的表里 ⇒ 拒，并且消息点名的是注册表里那个形态词，不是猜出来的字段名。

    这一格管的是"别把第三支也算成已接入"：它的凭据是 CAM 的一对键加签名，形状与这一发不同。
    """
    carrier(tmp_path, monkeypatch, None)
    with pytest.raises(spec_factory.RealWiringRefused) as caught:
        spec_factory.build_real_transport("workbuddy", gateway=object(), env={"WORKBUDDY_SECRET_ID": FAKE_KEY})
    assert "IN_PLATFORM" in str(caught.value) and "API" in str(caught.value)
    assert sorted(spec_factory.WIRABLE_FORMS) == ["API", "SDK"]


def test_the_spec_constructor_still_has_no_outside_caller(tmp_path):
    """接线之后本文件只管这一格：`build_transport_spec()` 的调用点**仍然只在工厂自己那个文件里**。

    笔 O 那一版这条断的是"两个名字都还没有生产调用点"。真入口落了（笔 P），`build_real_transport` 那一半
    的前件已经不存在 ⇒ 改口而不是删掉：**改口的去向点名** =
    `tests/guards/test_realrun_driver.py::test_the_factory_has_exactly_one_production_caller_and_it_is_the_driver`
    量"真装配恰一个调用方且只能是驱动"，本文件继续量"没有人绕过工厂自己拼 spec"。
    两格合起来才是那句裁决的完整形状：**接了线，但构造点没有变多**。
    """
    defining = (SRC_ROOT / "adapters" / "spec_factory.py").relative_to(SRC_ROOT).as_posix()
    callers = [hit for hit in _call_sites("build_transport_spec", SRC_ROOT) if hit != defining]
    assert callers == [], f"有人绕过工厂自己构造厂商 spec：{callers}"

    decoy = tmp_path / "caller.py"
    decoy.write_text(
        "from acp.adapters.spec_factory import build_transport_spec\n\n"
        "def f():\n    return build_transport_spec('doubao')\n",
        encoding="utf-8",
    )
    assert _call_sites("build_transport_spec", tmp_path) == ["caller.py"], "扫描器看不见调用点 ⇒ 上面那条是空对空"


def test_wiring_the_factory_did_not_open_the_run_transport_door():
    """R-07 那道门**一格都没放宽**：`acp.run --transport` 仍然只放 `stub`，真厂商名照样拒。

    这是本轮最重要的一条负面对照。owner 的 B 计划把真装配放在**另一个入口**（`acp.realrun`）里，
    而 `resolve_transport_flag()` 的取值域一动，"一次误敲就是一次真实外部调用"那条成本纪律就没了。
    三支真厂商名 + 三种大小写/空白写法都在拒的那一侧，`stub` 自己在通过的那一侧。
    """
    for value in ("doubao", "qoder", "workbuddy", "STUB", "Stub", "stub "):
        with pytest.raises(StubMisuse) as caught:
            resolve_transport_flag(value)
        assert caught.value.code == "transport_flag_unknown", (value, caught.value.code)
    assert resolve_transport_flag(TRANSPORT_FLAG) == TRANSPORT_FLAG
    assert TRANSPORT_FLAG == "stub"

