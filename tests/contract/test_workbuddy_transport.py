"""T040：WorkBuddy transport 的契约测试（下发 → 执行 → 回收闭环，全程 Mock HTTP）。

**不真调 WorkBuddy API**（owner 2026-10-13 成本纪律）。网络端用 `httpx.MockTransport`：
transport 的请求构造、签名、响应解析、落盘与量取全是真代码，只有对端是脚本化的应答。
⇒ 本文件证明的是"我们的构造与解析自洽且不可旁路"，**不是**"腾讯服务端接受这套请求"
（后者归真凭据轮次，`tasks.md` 的 T040 申报里已按 `NEEDS VERIFICATION` 挂账）。

"双向"落在两处，方向不同、缺一半就不算：
- **契约 → 实现**：把 `contracts/agent-protocol.md §4` 的 ```python 围栏与 `Transport` 的运行期
  形状做差集（用 `test_adapter_protocol.py` 里**同一套**解析与比对函数，不复制第二份比较器）；
- **实现 → 契约**：`registry/agents.yaml` 里那条入口 `acp.adapters.workbuddy:Transport` 经
  `Registration.resolve()` 必须取回**这个类本身** ⇒ 注册表、包重导出、实现三者对得上。
"""

from __future__ import annotations

import importlib.util
import inspect
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from acp.adapters.base import AgentAdapter
from acp.adapters.workbuddy import Transport
from acp.adapters.workbuddy.transport import (
    API_VERSION,
    ENDPOINT,
    NO_ROLE_DECLARED,
    SECRET_ID_ENV,
    SECRET_KEY_ENV,
    USAGE_KINDS,
    WorkBuddyApiSpec,
    WorkBuddyTransportError,
)
from acp.bus.acl import WRITE_FORBIDDEN
from acp.bus.gateway import BusError, BusGateway
from acp.config.loader import CredentialMissing
from acp.protocol import Handle, Task, TaskResult
from acp.protocol.messages import ArtifactRef
from acp.registry.loader import load_registrations

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_sibling(name: str):
    """按路径加载同目录的另一份用例，取它已有的解析器。

    为什么不用 `import test_adapter_protocol`：`tests/` 下没有包标记，那条写法要赌 pytest 的
    import 模式与 sys.path；按文件路径显式加载与模式无关，且**共用同一个比较器**比复制一份
    `protocol_shape()` 更诚实（复制就等于承认"两份会漂开"）。
    """
    path = Path(__file__).with_name(f"{name}.py")
    spec = importlib.util.spec_from_file_location(f"acp_contract_fixture.{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_protocol_fixture = _load_sibling("test_adapter_protocol")
_messages = _load_sibling("test_protocol")

#: 测试用假凭据。刻意不构成任何真实形态（`sk-` 那一支在下面单独造，用来验脱敏而不是用来当凭据）。
FAKE_ID = "AKID-fixture-id"
FAKE_KEY = "fixture-secret-key-value"
#: 凭据**形态**用的串（与 `tests/guards/test_credentials.py` 同一个写法：拼接构造，
#: 免得整串落进源码被任何密钥扫描读成"仓库里有真键"）。
LEAK_FORM = "sk-" + "wbmocknotreal01234567"

FIXED_TIMESTAMP = 1_760_000_000  # 2025-10-08T…：签名作用域里那个日期由此固定，用例才可断言签名值


def make_env(**override) -> dict[str, str]:
    env = {SECRET_ID_ENV: FAKE_ID, SECRET_KEY_ENV: FAKE_KEY}
    env.update(override)
    for key, value in list(env.items()):
        if value is None:
            del env[key]
    return env


def handler_for(responses: list[dict], record: list) -> httpx.MockTransport:
    """按调用次序返回 `responses`，并把每次请求记进 `record`（`dict`：action / body / headers）。"""

    def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        record.append({"action": body.get("Action"), "body": body, "headers": dict(request.headers)})
        if not responses:
            raise AssertionError("transport 发出了比用例预期更多的请求（不许偷偷重试）")
        payload = responses.pop(0)
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(_handler)


@pytest.fixture
def bus_root(tmp_path: Path) -> Path:
    root = tmp_path / "AI项目台" / "demo"
    root.mkdir(parents=True)
    return root


@pytest.fixture
def gateway(bus_root: Path) -> BusGateway:
    return BusGateway(bus_root)


def build_transport(gateway: BusGateway, responses: list[dict], **kwargs) -> tuple[Transport, list]:
    """造一个走 Mock 的 transport；返回 (transport, 请求流水)。"""
    record: list = []
    api = kwargs.pop("api", None) or WorkBuddyApiSpec(service="fixture-service")
    transport = Transport(
        api=api,
        http=httpx.Client(transport=handler_for(responses, record)),
        gateway=gateway,
        env=kwargs.pop("env", make_env()),
        clock=kwargs.pop("clock", lambda: FIXED_TIMESTAMP),
        **kwargs,
    )
    return transport, record


def task(**override) -> Task:
    """借用 `test_protocol.py` 的那份 payload（13 项必填只有一份抄本）。

    只改一件事：默认指派必须是 workbuddy —— 原 fixture 指给 `qoder/frontend`，而本 transport
    会拒收别人的任务卡（`submit()` 的身份检查），照抄就变成每条用例都在验那条拒绝。
    岗位取 `architect` = FR-071"架构师由其兼任"与 `agents.yaml` 里 `role_mounts: [architect]` 同一写法。
    """
    payload = _messages.task_payload(
        **{"assignee": {"agent_id": "workbuddy", "role_id": "architect"}, **override}
    )
    return Task(**payload)


def handle() -> Handle:
    return Handle(
        task_id="T01",
        agent_id="workbuddy",
        native_ref="wb-native-77",
        submitted_at=datetime(2026, 10, 13, 3, 0, tzinfo=timezone.utc),
    )


def created(task_id: str = "wb-native-77") -> dict:
    return {"Response": {"TaskId": task_id, "RequestId": "req-1"}}


def finished(**override) -> dict:
    body = {
        "TaskState": "SUCCESS",
        "LogExcerpt": "已生成首页",
        "SelfAssessment": "按三行做完",
        "Artifacts": [
            {"Path": "workbuddy-workspace/index.html", "Content": "<html>\n<body>hi</body>\n</html>\n"}
        ],
        "Usage": [{"Kind": "CALL", "Quantity": "3", "Unit": "credit"}],
    }
    body.update(override)
    return {"Response": body}


# —— 一、形状：契约 → 实现 ——


def test_transport_shape_equals_contract_section_4():
    """`Transport` 与契约 §4 的围栏零差异（用的是 T038 那套比较器，不是这里另写的）。"""
    fence = _protocol_fixture.contract_fence()
    shape = _protocol_fixture.protocol_shape(Transport)
    assert _protocol_fixture.shape_diff(shape, _protocol_fixture.parse_fence(fence)) == []


def test_protocol_and_transport_agree_on_the_same_fence():
    """双向的左半边：协议基类与实现**各自**与围栏同形 ⇒ 二者不可能彼此漂开。"""
    fence = _protocol_fixture.parse_fence(_protocol_fixture.contract_fence())
    assert _protocol_fixture.shape_diff(_protocol_fixture.protocol_shape(AgentAdapter), fence) == []
    assert _protocol_fixture.shape_diff(_protocol_fixture.protocol_shape(Transport), fence) == []


def test_transport_is_structurally_an_agent_adapter():
    """`isinstance` 走通（契约 §4 的 `runtime_checkable` 就是为 T045 的零耦合证明留的）。"""
    transport, _ = build_transport(BusGateway(Path(".")), [])
    assert isinstance(transport, AgentAdapter)


def test_transport_signatures_carry_no_bare_dict():
    """D-7 的签名面：实现交出去的四个签名里不许出现裸 `dict`。"""
    assert _protocol_fixture.bare_dict_hits(_protocol_fixture.protocol_shape(Transport)) == []


def test_registry_entry_resolves_to_this_transport_class():
    """双向的右半边：注册表那条入口字符串必须取回**这个类本身**。

    这条同时钉住 `adapters/workbuddy/__init__.py` 的重导出：少了它，`getattr(模块, 'Transport')`
    会抛，而 `loader.resolve()` 的报错形态是"入口解析失败"—— 也就是说这份实现会在装配点才炸。
    """
    registrations = {item.agent_id: item for item in load_registrations()}
    assert registrations["workbuddy"].adapter_entry == "acp.adapters.workbuddy:Transport"
    assert registrations["workbuddy"].resolve() is Transport


# —— 二、describe：数据只从注册表来 ——


def test_describe_returns_the_registry_manifest_itself():
    gateway = BusGateway(Path("."))
    transport, record = build_transport(gateway, [])
    manifest = transport.describe()
    registered = {item.agent_id: item.manifest for item in load_registrations()}["workbuddy"]
    assert manifest == registered
    assert manifest.agent_id == transport.agent_id == "workbuddy"
    assert record == []  # describe 不发请求：它是申报单的回读，不是一次调用


def test_transport_source_does_not_restate_the_declaration():
    """`transport.py` 的源码文本里不许出现注册表里那几项申报值 ⇒ 证明它真去读注册表而不是抄了一份。

    抄一份的后果是 FR-012 的反面：新工具改档位要动两个文件，而改漏的那个不会被任何用例发现。
    """
    text = Path(inspect.getfile(Transport)).read_text(encoding="utf-8")
    manifest = {item.agent_id: item.manifest for item in load_registrations()}["workbuddy"]
    tokens = list(manifest.capabilities) + list(manifest.role_mounts) + [manifest.cost_band]
    offenders = [token for token in tokens if token in text]
    assert offenders == [], offenders


# —— 三、下发 ——


def test_submit_builds_a_handle_from_the_native_ref(gateway):
    transport, record = build_transport(gateway, [created()])
    receipt = transport.submit(task())
    assert isinstance(receipt, Handle)
    assert receipt.native_ref == "wb-native-77"
    assert receipt.agent_id == "workbuddy" and receipt.task_id == "T01"
    assert receipt.submitted_at.tzinfo is not None, "契约 §4.1：`submitted_at` 带时区"
    assert [item["action"] for item in record] == [transport.api.create_action]


def test_submit_sends_the_visible_faces_and_the_criteria(gateway):
    """裁决 2（2026-10-13）在消息面的落点：标准随任务下发，键名 `AcceptanceCriteria`。

    前身 `test_submit_sends_only_the_three_visible_lines` 钉的是"细则不发"，方向已反转。
    键名是**未验假定**（`NEEDS VERIFICATION`，验收点 = Phase 12 真凭据冒烟第一次不被拒）。
    """
    transport, record = build_transport(gateway, [created()])
    transport.submit(task())
    body = record[0]["body"]
    assert set(body) == {
        "TaskId",
        "ProjectKey",
        "Objective",
        "Inputs",
        "ExpectedOutput",
        "AcceptanceCriteria",
        "Action",
        "Version",
    }, sorted(body)
    assert body["Version"] == API_VERSION
    assert body["Inputs"] == task().inputs
    assert body["AcceptanceCriteria"] == list(task().acceptance_criteria)


def test_submit_refuses_a_task_assigned_to_another_agent(gateway):
    """拿别人的身份下发 ⇒ workspace 归属比对（`acl.can_write` 的 `adapter:<agent_id>`）就失去意义。"""
    transport, record = build_transport(gateway, [])
    with pytest.raises(WorkBuddyTransportError, match="拒发"):
        transport.submit(task(assignee={"agent_id": "qoder", "role_id": "developer"}))
    assert record == []


@pytest.mark.parametrize("missing", [SECRET_ID_ENV, SECRET_KEY_ENV])
def test_missing_credential_stops_before_any_request(gateway, missing):
    """凭据缺失 ⇒ 连一个请求都不该发出（"先试一下再看错误"会把未签名的请求送到公网）。"""
    env = make_env()
    env.pop(missing)
    transport, record = build_transport(gateway, [], env=env)
    with pytest.raises(CredentialMissing) as caught:
        transport.submit(task())
    assert missing in str(caught.value)
    assert record == []


# —— 四、回收：实测值与归属 ——


def test_collect_measures_artifacts_from_disk_not_from_the_response(gateway, bus_root):
    """FR-019 的落点：`bytes` / `lines` 读自盘上那份字节。

    内容里放了多字节中文 ⇒ `len(str)` 与 `len(bytes)` 必然不等；若实现取的是字符串长度，
    这条就红。这也是"厂商/工具自报数不进场"最直接的反证：本用例根本不提供任何自报字段。
    """
    content = "<html>\n你好世界\n</html>\n"
    transport, _ = build_transport(gateway, [created(), finished(Artifacts=[{"Path": "workbuddy-workspace/index.html", "Content": content}])])
    transport.submit(task())
    result = transport.collect(handle())
    assert isinstance(result, TaskResult)
    assert result.self_reported_status == "success"
    (ref,) = result.artifacts
    on_disk = (bus_root / "workbuddy-workspace" / "index.html").read_bytes()
    assert ref.bytes == len(on_disk) == len(content.encode("utf-8"))
    assert ref.bytes != len(content), "取到字符串长度 = 没读盘"
    assert ref.lines == len(on_disk.decode("utf-8").splitlines()) == 3
    assert ref.format == "html" and ref.rel_path == "workbuddy-workspace/index.html"


@pytest.mark.parametrize(("state", "expected"), [("SUCCESS", "success"), ("FAILED", "failed")])
def test_collect_maps_only_the_two_declared_states(gateway, state, expected):
    transport, _ = build_transport(gateway, [created(), finished(TaskState=state, Artifacts=[])])
    transport.submit(task())
    result = transport.collect(handle())
    assert result.self_reported_status == expected


def test_collect_refuses_an_unknown_state_instead_of_guessing(gateway):
    """状态取值域外的值 ⇒ 抛。"认不出来就当失败"会伪造一次失败回收（成本与状态机都会跟着错）。"""
    transport, _ = build_transport(gateway, [created(), finished(TaskState="TIMEOUT")])
    transport.submit(task())
    with pytest.raises(WorkBuddyTransportError, match="TIMEOUT"):
        transport.collect(handle())


def test_non_numeric_quantity_becomes_a_transport_error_not_a_pydantic_one(gateway):
    """`Kind` 合法但 `Quantity` 不是数 ⇒ 抛 `WorkBuddyTransportError`，不把 pydantic 的报错形态漏给编排层。

    为什么单独一条：`_collect_usage` 里有两支失败路径 —— 取值域外（上一条用例）与**构造期不合法**（本条）。
    只测前一支的话，"把 ValidationError 吞掉继续跑"这种改法就没人抓，而它的后果正是本任务最怕的那件事：
    一条读不懂的用量静默消失，账面上少一笔、状态机照常往下走。
    """
    transport, _ = build_transport(
        gateway, [created(), finished(Usage=[{"Kind": "CALL", "Quantity": "banana", "Unit": "credit"}])]
    )
    transport.submit(task())
    with pytest.raises(WorkBuddyTransportError, match="用量条目不合法"):
        transport.collect(handle())


def test_collect_reports_the_state_field_it_actually_read(gateway):
    """缺字段时报出**读了哪个字段、还有哪些键**：否则排查只能靠猜厂商回了什么。"""
    transport, _ = build_transport(gateway, [created(), {"Response": {"RequestId": "req-2"}}])
    transport.submit(task())
    with pytest.raises(WorkBuddyTransportError, match="TaskState") as caught:
        transport.collect(handle())
    assert "RequestId" in str(caught.value)


def test_collect_refuses_a_foreign_workspace_and_writes_nothing(gateway, bus_root):
    """FR-013：跨 workspace 写一律拒，且**不许留下半个文件**。"""
    transport, _ = build_transport(
        gateway,
        [created(), finished(Artifacts=[{"Path": "qoder-workspace/stolen.html", "Content": "x\n"}])],
    )
    transport.submit(task())
    with pytest.raises(BusError) as caught:
        transport.collect(handle())
    assert caught.value.code == WRITE_FORBIDDEN
    assert not (bus_root / "qoder-workspace").exists()
    assert sorted(p.name for p in bus_root.iterdir()) == []


def test_collect_refuses_public_area_writes(gateway, bus_root):
    """公共区唯一写入者是 `orchestrator_writer`（`results/**` 由 Phase 1 的写入方负责）。"""
    transport, _ = build_transport(
        gateway,
        [created(), finished(Artifacts=[{"Path": "results/index.md", "Content": "# 产物索引\n"}])],
    )
    transport.submit(task())
    with pytest.raises(BusError) as caught:
        transport.collect(handle())
    assert caught.value.code == WRITE_FORBIDDEN
    assert not (bus_root / "results" / "index.md").exists()


def test_collect_refuses_an_off_domain_format_before_writing_it(gateway, bus_root):
    """域外后缀必须**落盘前**拒：留在盘上就等于给下一次读工作区的人留了一份未声明的产物。"""
    transport, _ = build_transport(
        gateway,
        [created(), finished(Artifacts=[{"Path": "workbuddy-workspace/payload.ps1", "Content": "Get-Process\n"}])],
    )
    transport.submit(task())
    with pytest.raises(BusError) as caught:
        transport.collect(handle())
    assert caught.value.code == "unsupported_format"
    assert not (bus_root / "workbuddy-workspace" / "payload.ps1").exists()


def test_uppercase_suffix_is_accepted_as_the_same_format(gateway, bus_root):
    """`INDEX.HTML` 与 `index.html` 是同一个格式：取值域比较在**归一之后**做。

    不归一的话，厂商回一个大写后缀就"格式不符"拒收 ⇒ 一份完全合规的产物因为文件名大小写被丢掉，
    而拒收的形态（`unsupported_format`）看起来像安全门禁在生效，没人会去查它其实是大小写。
    """
    transport, _ = build_transport(
        gateway,
        [created(), finished(Artifacts=[{"Path": "workbuddy-workspace/INDEX.HTML", "Content": "<html>a</html>\n"}])],
    )
    transport.submit(task())
    result = transport.collect(handle())
    (ref,) = result.artifacts
    assert ref.format == "html" and ref.rel_path == "workbuddy-workspace/INDEX.HTML"
    assert (bus_root / "workbuddy-workspace" / "INDEX.HTML").exists()


def test_write_actor_and_capability_come_from_the_handle(gateway, bus_root):
    """两条边界一次验，因为它们都是"回收路径上没人核对就会静默错"的那类：

    ① **不编岗位**：`Handle`（契约 §4.1）只有四字段、不带 `role_id` ⇒ transport 若自己填一个岗，
       审计行里就多出一条没人批准过的岗位挂载（绕过 EN-04 的生效口径）。这里必须看到那个显式常量。
       `read_only` 必须是 False —— 执行者不是只读岗，谎报只读会让该写的一行都写不下去。
    ② **凭证取回执里的任务号**：进程重启后 `collect()` 手上只有 `Handle`，若实现取的是 `submit()`
       留在内存里的那个 task，重启后写盘用的就是**另一张卡**的凭证（`acl.can_write` 用它反推
       目录唯一写入者，任务号错位不会崩，只会在审计里留下错的对账关系）。
    """
    seen: list = []

    class Recorder(BusGateway):
        def write_and_measure(self, actor, path, payload, capability=None):
            seen.append((actor, capability, path))
            return ArtifactRef(
                rel_path=path,
                bytes=len(payload.encode("utf-8")),
                lines=len(payload.splitlines()),
                format="html",
            )

    transport, _ = build_transport(Recorder(bus_root), [created(), finished()])
    transport.submit(task())  # 这张卡是 T01
    result = transport.collect(
        Handle(
            task_id="T99",
            agent_id="workbuddy",
            native_ref="wb-native-77",
            submitted_at=datetime(2026, 10, 13, 3, 0, tzinfo=timezone.utc),
        )
    )
    assert result.task_id == "T99"
    (actor, capability, path), = seen
    assert actor.actor_id == "adapter:workbuddy" and actor.read_only is False
    assert actor.role_id == NO_ROLE_DECLARED, "不许给写盘动作编一个岗位"
    assert capability.agent_id == "workbuddy"
    assert capability.task_id == "T99", "凭证必须是回执里那张卡，不是 submit() 时内存里那张"
    assert path == "workbuddy-workspace/index.html"


def test_collect_refuses_a_handle_from_another_agent(gateway):
    transport, _ = build_transport(gateway, [created(), finished()])
    with pytest.raises(WorkBuddyTransportError, match="agent_id"):
        transport.collect(Handle(task_id="T01", agent_id="qoder", native_ref="x", submitted_at=datetime.now(timezone.utc)))


# —— 五、用量与自证边界 ——


def test_usage_notices_carry_the_basis_but_never_an_amount(gateway):
    """FR-061 / EN-13：WorkBuddy 走 `SEAT_CREDIT` 口径，金额由 `cost/ledger.py` 依 §6 换算。"""
    transport, _ = build_transport(gateway, [created(), finished()])
    transport.submit(task())
    result = transport.collect(handle())
    (notice,) = result.usage
    assert notice.billing_basis == "SEAT_CREDIT"
    assert notice.usage_kind in USAGE_KINDS and str(notice.quantity) == "3"
    assert notice.occurred_at.tzinfo is not None
    for forbidden in ("cost_amount", "currency", "price", "usd"):
        assert not hasattr(notice, forbidden), forbidden


def test_unknown_usage_kind_is_refused_not_dropped(gateway):
    """静默丢一条用量 = 用量少计（FR-059 失真）⇒ 必须抛，并把允许取值说清楚。"""
    transport, _ = build_transport(
        gateway, [created(), finished(Usage=[{"Kind": "MINUTES", "Quantity": "1", "Unit": "x"}])]
    )
    transport.submit(task())
    with pytest.raises(WorkBuddyTransportError, match="MINUTES") as caught:
        transport.collect(handle())
    assert "CALL" in str(caught.value)


# —— 六、cancel ——


def test_cancel_calls_once_and_writes_nothing(gateway, bus_root):
    transport, record = build_transport(gateway, [created(), {"Response": {"RequestId": "req-3"}}])
    transport.submit(task())
    assert transport.cancel(handle()) is None
    assert [item["action"] for item in record] == [transport.api.create_action, transport.api.cancel_action]
    assert record[1]["body"][transport.api.ref_field] == "wb-native-77"
    assert sorted(p.name for p in bus_root.iterdir()) == []


def test_cancel_does_not_reuse_the_create_action(gateway):
    """三条 Action 各自独立：若 cancel 复用了 create，撤单会变成再下发一次（成本翻倍且状态机不动）。"""
    transport, _ = build_transport(gateway, [created(), {"Response": {}}])
    assert len({transport.api.create_action, transport.api.status_action, transport.api.cancel_action}) == 3
    transport.submit(task())
    transport.cancel(handle())


# —— 七、签名与凭据面 ——


def test_authorization_header_shape_and_deterministic_signature(gateway):
    """TC3 头的结构逐项点名；固定时间戳 ⇒ 同一输入必须得同一签名（可复算才有对照意义）。"""
    transport, record = build_transport(gateway, [created(), created("wb-2")])
    transport.submit(task())
    first = record[0]["headers"]["authorization"]
    assert first.startswith("TC3-HMAC-SHA256 ")
    assert f"Credential={FAKE_ID}/" in first
    assert "/fixture-service/tc3_request" in first
    assert "SignedHeaders=content-type;host" in first
    signature = first.split("Signature=")[1]
    assert len(signature) == 64 and all(char in "0123456789abcdef" for char in signature)
    transport.submit(task(task_id="T02"))
    assert record[1]["headers"]["authorization"].split("Signature=")[1] != signature


def test_secret_key_never_leaves_the_process(gateway):
    """签名用 SecretKey，但它 MUST NOT 出现在请求头或任何异常消息里。"""
    transport, record = build_transport(gateway, [created()])
    transport.submit(task())
    headers = record[0]["headers"]
    assert FAKE_KEY not in json.dumps(headers)
    assert FAKE_ID in headers["authorization"]  # SecretId 是公开面（它进 Credential 段）


def test_error_message_is_washed_even_on_the_failure_path(gateway):
    """失败路径也过脱敏：厂商的 `Message` 可能回显请求内容，而异常最终进日志（宪法「二」两个面）。"""
    echoed = {"Response": {"Error": {"Code": "AuthFailure", "Message": f"bad key {LEAK_FORM}"}, "RequestId": "req-e"}}
    record: list = []

    def _handler(request: httpx.Request) -> httpx.Response:
        record.append(request)
        return httpx.Response(400, json=echoed)

    transport = Transport(
        api=WorkBuddyApiSpec(service="fixture-service"),
        http=httpx.Client(transport=httpx.MockTransport(_handler)),
        gateway=gateway,
        env=make_env(),
        clock=lambda: FIXED_TIMESTAMP,
    )
    with pytest.raises(WorkBuddyTransportError) as caught:
        transport.submit(task())
    message = str(caught.value)
    assert LEAK_FORM not in message and "REDACTED" in message
    assert "AuthFailure" in message and "req-e" in message


def test_service_has_no_default_value(gateway):
    """`service` 不给默认：`params-ref §4` 没列它 ⇒ 猜一个就是造一个没人能证伪的事实源。"""
    with pytest.raises(ValueError, match="service"):
        WorkBuddyApiSpec(service="")


# —— 八、注入式 spec：证明字段名没焊在逻辑里 ——


def test_synthetic_spec_changes_every_wire_field_the_transport_reads(gateway, bus_root):
    """换一份 spec（Action、字段名、状态取值全改）⇒ 请求与解析都跟着变。

    这条是"集中成可注入对象"的唯一实质理由：如果实现里还残留硬编码字段名，注入后就会读不到值。
    """
    api = WorkBuddyApiSpec(
        service="other-service",
        create_action="DoCreate",
        status_action="DoStatus",
        cancel_action="DoCancel",
        ref_field="JobNo",
        state_field="Phase",
        succeeded_states=frozenset({"DONE_OK"}),
        failed_states=frozenset({"DONE_BAD"}),
        artifacts_field="Files",
        artifact_path_field="To",
        artifact_content_field="Text",
        log_field="Trace",
        assessment_field="Note",
        usage_field="Meters",
        usage_kind_field="K",
        usage_quantity_field="Q",
        usage_unit_field="U",
    )
    transport, record = build_transport(
        gateway,
        [
            {"Response": {"JobNo": "job-9"}},
            {
                "Response": {
                    "Phase": "DONE_OK",
                    "Trace": "完成",
                    "Note": "自评",
                    "Files": [{"To": "workbuddy-workspace/page.md", "Text": "# 标题\n正文"}],
                    "Meters": [{"K": "CALL", "Q": "2", "U": "credit"}],
                }
            },
        ],
        api=api,
    )
    receipt = transport.submit(task())
    assert receipt.native_ref == "job-9"
    assert record[0]["action"] == "DoCreate"
    result = transport.collect(
        Handle(
            task_id="T01",
            agent_id="workbuddy",
            native_ref="job-9",
            submitted_at=datetime(2026, 10, 13, 3, 0, tzinfo=timezone.utc),
        )
    )
    assert result.self_reported_status == "success"
    assert result.log_excerpt == "完成" and result.self_assessment == "自评"
    (ref,) = result.artifacts
    assert ref.rel_path == "workbuddy-workspace/page.md" and ref.format == "md" and ref.lines == 2
    assert (bus_root / "workbuddy-workspace" / "page.md").exists()


def test_endpoint_and_version_match_params_ref_section_4():
    """端点与版本是 `params-ref §4` 的运行期抄本 ⇒ 两侧漂开必须有用例红（一事实一载体的另一面）。

    两侧都按**字面**比：§4 那张表里写的就是 `workbuddyenterprise.tencentcloudapi.com` 与
    `Version=2026-07-09`。写成"或含版本串"的宽松判断等于没判（任何日期都能凑上）。
    """
    text = (REPO_ROOT / "config" / "params-ref.md").read_text(encoding="utf-8")
    host = ENDPOINT.split("://", 1)[1]
    assert host in text, host
    assert f"Version={API_VERSION}" in text, API_VERSION


# —— 九、契约里那条"未闭"的显式承接 ——


def test_log_excerpt_is_not_washed_by_the_transport(gateway):
    """消息面的脱敏**不在本层**（契约 §2 明写接线点是写 EN-14 的那一步）⇒ 本用例钉的是"现在没洗"。

    为什么钉一条"缺陷没修"的用例：契约 §2 已点名"这一条现在未闭合，MUST NOT 读成消息字段已脱敏"。
    没有这条，接线的用例写出来时没人知道是哪一天变的；有了这条，接线那一轮 MUST 看到它转红并改断言，
    而不是把一个静默的行为变化留到 Phase 12。产物面相反 —— 它已经过网关洗（下面那条验它）。
    """
    transport, _ = build_transport(
        gateway, [created(), finished(LogExcerpt=f"key {LEAK_FORM}", Artifacts=[])]
    )
    transport.submit(task())
    result = transport.collect(handle())
    assert LEAK_FORM in result.log_excerpt, "若哪天在 transport 里加洗，先看契约 §2 的接线点是否已改"


def test_artifact_face_is_washed_by_the_gateway(gateway, bus_root):
    """产物面（已闭的那一面）：脱敏发生在落盘之前，所以盘上的那份才是判定用的那份。"""
    content = f"<html>{LEAK_FORM}</html>\n"
    transport, _ = build_transport(
        gateway,
        [created(), finished(Artifacts=[{"Path": "workbuddy-workspace/index.html", "Content": content}])],
    )
    transport.submit(task())
    transport.collect(handle())
    on_disk = (bus_root / "workbuddy-workspace" / "index.html").read_text(encoding="utf-8")
    assert LEAK_FORM not in on_disk and "[REDACTED]" in on_disk
