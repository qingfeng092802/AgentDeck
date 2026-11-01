"""T042：豆包（方舟 OpenAI 兼容 HTTP）transport 的契约测试（下发 → 执行 → 回收闭环，全程 Mock HTTP）。

**不真调方舟 API**（owner 成本纪律 5）：注入 `httpx.Client` + `httpx.MockTransport`（与 T040 同源）。
请求构造、鉴权头、响应解析、留档落盘、产物实测与用量口径全是真代码，只有对端是脚本化的应答
⇒ 本文件证明的是"我们的构造与解析自洽且不可旁路"，**不是**"方舟接受这套请求"
（后者的传输层已由 D-1 那两轮真机 200 证到，但 `id` 的形态与错误体字段名仍未证 ⇒ 挂 `NEEDS VERIFICATION`）。

"双向"同样两处：
- **契约 → 实现**：`contracts/agent-protocol.md §4` 的 ```python 围栏与 `Transport` 的运行期形状做差集
  （复用 `test_adapter_protocol.py` 那套解析与比对函数，不复制第二份比较器）；
- **实现 → 契约**：`registry/agents.yaml` 里 `acp.adapters.doubao:Transport` 经 `Registration.resolve()`
  必须取回**这个类本身**。

本支与前两支最实质的差别，三分之一的用例在为它服务：豆包是**一次性补全**，
既没有"再去查状态"的接口（T040），也没有外部进程自己写盘（T041）
⇒ 留档是我们写的、产物也是我们把响应正文经网关落盘后实测的，而 `acceptance_criteria`
在这一支是**审查方的输入**（FR-029），方向与 T040/T041 相反。
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
from acp.adapters.doubao import Transport
from acp.adapters.doubao import transport as doubao_module
from acp.adapters.doubao.transport import (
    API_KEY_ENV,
    ARK_BASE_URL,
    CHAT_COMPLETIONS_PATH,
    KNOWN_FINISH_REASONS,
    NON_RETRYABLE_STATUS,
    RETRYABLE_STATUS,
    RESPONSES_SUBDIR,
    REVIEWS_SUBDIR,
    TOKEN_UNIT,
    DoubaoApiSpec,
    DoubaoTransportError,
)
from acp.bus.gateway import BusGateway
from acp.config.loader import CredentialMissing
from acp.protocol import Handle, Task, TaskResult
from acp.registry.loader import load_registrations

REPO_ROOT = Path(__file__).resolve().parents[2]
PARAMS_REF = REPO_ROOT / "config" / "params-ref.md"
ADAPTER_DIR = REPO_ROOT / "src" / "acp" / "adapters"

WS = "doubao-workspace"
MODEL = "fixture-model-id"

#: 假凭据。刻意不构成任何真实形态；`sk-` 那一支单独造，用来验脱敏而不是用来当凭据。
FAKE_KEY = "ark-fixture-key-value"
LEAK_FORM = "sk-" + "dobmocknotreal01234567"


def _load_sibling(name: str):
    path = Path(__file__).with_name(f"{name}.py")
    spec = importlib.util.spec_from_file_location(f"acp_contract_fixture.{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_protocol_fixture = _load_sibling("test_adapter_protocol")
_messages = _load_sibling("test_protocol")


def spec(**override) -> DoubaoApiSpec:
    return DoubaoApiSpec(model=MODEL, **override)


def handler_for(responses: list[tuple[int, dict | str]], record: list) -> httpx.MockTransport:
    """按调用次序返回 `(status, body)`，并把每次请求记进 `record`。队列空了还来 ⇒ 抛（不许偷偷重试）。"""

    def _handler(request: httpx.Request) -> httpx.Response:
        record.append(
            {
                "url": str(request.url),
                "headers": dict(request.headers),
                "body": json.loads(request.content.decode("utf-8")),
            }
        )
        if not responses:
            raise AssertionError("transport 发出了比用例预期更多的请求（不许偷偷重试）")
        status, payload = responses.pop(0)
        if isinstance(payload, str):
            return httpx.Response(status, content=payload.encode("utf-8"), headers={"Content-Type": "application/json"})
        return httpx.Response(status, json=payload)

    return httpx.MockTransport(_handler)


@pytest.fixture
def bus_root(tmp_path: Path) -> Path:
    root = tmp_path / "AI项目台" / "demo"
    root.mkdir(parents=True)
    return root


@pytest.fixture
def gateway(bus_root: Path) -> BusGateway:
    return BusGateway(bus_root)


def build(gateway: BusGateway, responses: list, **kwargs):
    """造一个走 Mock 的 transport；返回 (transport, 请求流水)。"""
    record: list = []
    transport = Transport(
        api=kwargs.pop("api", None) or spec(),
        http=httpx.Client(transport=handler_for(responses, record)),
        gateway=gateway,
        env=kwargs.pop("env", {API_KEY_ENV: FAKE_KEY}),
        **kwargs,
    )
    return transport, record


def task(**override) -> Task:
    """借用 `test_protocol.py` 那份 payload（13 项必填只有一份抄本）。

    指派改两件事：`agent_id=doubao`（本 transport 拒收别人的卡），
    `role_id=reviewer` = `agents.yaml` 里 doubao 唯一申报的挂载（原 fixture 写的 `frontend` 没申报过，
    照抄会让每条用例都先撞上岗位那道拒绝）。
    """
    payload = _messages.task_payload(
        **{"assignee": {"agent_id": "doubao", "role_id": "reviewer"}, **override}
    )
    return Task(**payload)


def completion_body(**override) -> dict:
    """形状取自甲档那两轮真机 200 的读数 + 乙档 `openai==3.28.0` 的类型定义。"""
    body = {
        "id": "chatcmpl-fixture-0001",
        "object": "chat.completion",
        "created": 1_760_000_000,
        "model": MODEL,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "两条标准都满足，结论：通过", "reasoning_content": "先看第一条"},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 52,
            "completion_tokens": 30,
            "total_tokens": 82,
            "completion_tokens_details": {"reasoning_tokens": 29},
        },
    }
    return {**body, **override}


def handle_of(body: dict) -> Handle:
    """按"厂商报的那个 id"造一张回执 —— 用来喂 `collect()`，不重新下发。"""
    return Handle(
        task_id="T01",
        agent_id="doubao",
        native_ref=body["id"],
        submitted_at=datetime(2026, 10, 13, 6, 0, tzinfo=timezone.utc),
    )


def persisted(gateway: BusGateway, native_ref: str = "chatcmpl-fixture-0001") -> Path:
    return gateway.root / WS / RESPONSES_SUBDIR / f"{native_ref}.json"


# —— 一、形状：契约 ↔ 实现 ↔ 注册表 ——


def test_transport_shape_equals_contract_section_4():
    fence = _protocol_fixture.contract_fence()
    shape = _protocol_fixture.protocol_shape(Transport)
    assert _protocol_fixture.shape_diff(shape, _protocol_fixture.parse_fence(fence)) == []


def test_protocol_and_transport_agree_on_the_same_fence():
    """双向的左半边：协议基类与实现**各自**与围栏同形 ⇒ 二者不可能彼此漂开。"""
    fence = _protocol_fixture.parse_fence(_protocol_fixture.contract_fence())
    assert _protocol_fixture.shape_diff(_protocol_fixture.protocol_shape(AgentAdapter), fence) == []
    assert _protocol_fixture.shape_diff(_protocol_fixture.protocol_shape(Transport), fence) == []


def test_transport_is_structurally_an_agent_adapter(gateway):
    transport, _ = build(gateway, [])
    assert isinstance(transport, AgentAdapter)


def test_transport_signatures_carry_no_bare_dict():
    assert _protocol_fixture.bare_dict_hits(_protocol_fixture.protocol_shape(Transport)) == []


def test_registry_entry_resolves_to_this_transport_class():
    """双向的右半边：入口字符串必须取回**这个类本身**（顺带钉住 `__init__.py` 的重导出）。"""
    registrations = {item.agent_id: item for item in load_registrations()}
    assert registrations["doubao"].adapter_entry == "acp.adapters.doubao:Transport"
    assert registrations["doubao"].resolve() is Transport


def test_describe_returns_the_registry_manifest_itself(gateway):
    transport, record = build(gateway, [])
    manifest = transport.describe()
    registered = {item.agent_id: item.manifest for item in load_registrations()}["doubao"]
    assert manifest == registered
    assert manifest.agent_id == transport.agent_id == "doubao"
    assert record == [], "describe 不发请求：它是申报单的回读，不是一次调用"


def test_transport_source_does_not_restate_the_declaration():
    """能力集合与成本档的字面量不许出现在源码里 ⇒ 证明 `describe()` 真去读注册表。

    **覆盖面限制点名**：这条扫的是 `capabilities` 与 `cost_band`，不含 `role_mounts` ——
    裁决 2 删掉 `Audience` 注入位之后，`transport.py` 里已经没有任何岗名（`grep -n reviewer` 0 行），
    岗位申报的**来源**仍由 `test_role_mount_check_reads_the_registry_not_a_hardcoded_tuple` 单独钉。
    """
    text = Path(inspect.getfile(Transport)).read_text(encoding="utf-8")
    manifest = {item.agent_id: item.manifest for item in load_registrations()}["doubao"]
    offenders = [token for token in list(manifest.capabilities) + [manifest.cost_band] if token in text]
    assert offenders == [], offenders


# —— 二、下发：URL / 鉴权 / 报文面 ——


def test_submit_builds_a_handle_from_the_vendor_id(gateway):
    transport, record = build(gateway, [(200, completion_body())])
    receipt = transport.submit(task())
    assert isinstance(receipt, Handle)
    assert receipt.native_ref == "chatcmpl-fixture-0001"
    assert receipt.task_id == "T01" and receipt.agent_id == "doubao"
    assert receipt.submitted_at.tzinfo is not None, "契约 §4.1：`submitted_at` 带时区"
    assert len(record) == 1


def test_submit_posts_the_completions_path_with_bearer_auth(gateway):
    transport, record = build(gateway, [(200, completion_body())])
    transport.submit(task())
    # 字面量写死在这条断言里：读 `{ARK_BASE_URL}{CHAT_COMPLETIONS_PATH}` 等于拿实现的常量验实现，
    # 路径被换掉它照样绿（本轮变异 M05 就是这么暴露的）。
    assert record[0]["url"] == "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
    assert record[0]["headers"]["authorization"] == f"Bearer {FAKE_KEY}"
    assert record[0]["headers"]["content-type"] == "application/json"


def test_body_carries_only_model_and_messages_unless_max_tokens_is_set(gateway):
    """默认**不发** `max_tokens`：甲档那次带的是 16（最小探针值），不是平台参数。"""
    transport, record = build(gateway, [(200, completion_body())])
    transport.submit(task())
    assert set(record[0]["body"]) == {"model", "messages"}
    assert record[0]["body"]["model"] == MODEL

    transport2, record2 = build(gateway, [(200, completion_body())], api=spec(max_tokens=2048))
    transport2.submit(task())
    assert record2[0]["body"]["max_tokens"] == 2048


def test_prompt_carries_the_visible_faces_plus_the_criteria(gateway):
    """FR-029 的方向（owner 2026-10-13 裁决 2 定为三支统一口径）：验收标准**必须**随任务发过去。

    原名带 `for_a_reviewer`：那时本支的报文面按注入位 `audience` 分岗位筛，标准只发给审查岗。
    口径统一后那个条件不存在了 ⇒ 改名，名字不许替实现讲一份已经不发生的条件。
    """
    transport, record = build(gateway, [(200, completion_body())])
    transport.submit(task())
    content = record[0]["body"]["messages"][0]["content"]
    card = task()
    assert card.objective in content and card.expected_output in content
    assert card.artifact_floor.format in content and str(card.artifact_floor.min_bytes) in content
    for index, criterion in enumerate(card.acceptance_criteria, 1):
        assert f"{index}. {criterion}" in content, "逐条编号送出：不编号就分不清哪条对哪条"
    assert "---" not in content, "frontmatter 不进消息面"


def test_the_criteria_face_is_read_from_the_card_not_a_hardcoded_tuple(gateway):
    """反向对照（这条必须**绿**）：换一张卡上的标准，报文就跟着换 —— 证明发的是 `Task` 的字段。

    前身 `test_prompt_for_an_executor_audience_omits_the_criteria` 钉的是注入位 `audience` 的有效性；
    裁决 2 统一口径后那个注入位已删（一个不改变行为的注入位会在 API 面上说谎），
    这里补的是同一条"判据不是恒真"的位置：只往卡上多塞一条标准，报文里就必须多那一条。
    """
    extra = "首屏必须给出算力口径的来源"
    transport, record = build(gateway, [(200, completion_body())])
    transport.submit(task(acceptance_criteria=[*task().acceptance_criteria, extra]))
    content = record[0]["body"]["messages"][0]["content"]
    for index, criterion in enumerate([*task().acceptance_criteria, extra], 1):
        assert f"{index}. {criterion}" in content
    assert len(record) == 1, "换标准不换请求数：多加的那条只进正文，不该多打一次 API"


def test_the_static_role_mount_ban_is_gone(gateway, monkeypatch):
    """路线 C（2026-10-31）：适配器不再按 `agents.yaml` 的挂载拦角色。

    现场：以前给一份"只挂 architect"的假注册表，`reviewer` 的卡会被拒。现在这道拦截删除，
    谁能审谁不能审归 `guard/triple.py` 的席位实例互斥与通用审查 Runner —— 适配器只负责发报文。
    """
    assert not hasattr(Transport, "_refuse_unmounted_role")

    class Remounted:
        agent_id = "doubao"

        def __init__(self, mounts: list[str]) -> None:
            self.manifest = type("M", (), {"role_mounts": mounts})()

    monkeypatch.setattr(doubao_module, "load_registrations", lambda: [Remounted(["architect"])])
    transport, record = build(gateway, [(200, completion_body())])
    receipt = transport.submit(task())
    assert receipt.agent_id == "doubao" and len(record) == 1, "岗位挂载不该再挡住下发"

def test_model_id_has_no_default(gateway):
    """把某个 Model ID 写成默认值 = 把部署事实伪装成代码事实（§4 明写"以控制台实际 ID 为准"）。"""
    with pytest.raises(TypeError):
        DoubaoApiSpec()  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="§4"):
        DoubaoApiSpec(model="   ")
    with pytest.raises(ValueError, match="https"):
        DoubaoApiSpec(model=MODEL, base_url="http://ark.example.com/api/v3")


def test_missing_credential_stops_before_any_request(gateway):
    transport, record = build(gateway, [], env={})
    with pytest.raises(CredentialMissing) as caught:
        transport.submit(task())
    assert API_KEY_ENV in str(caught.value)
    assert record == []


def test_credential_value_never_enters_the_body_the_url_or_the_archive(gateway):
    transport, record = build(gateway, [(200, completion_body())])
    receipt = transport.submit(task())
    surfaces = [
        record[0]["url"],
        json.dumps(record[0]["body"], ensure_ascii=False),
        persisted(gateway, receipt.native_ref).read_text(encoding="utf-8"),
    ]
    assert all(FAKE_KEY not in item for item in surfaces)
    assert record[0]["headers"]["authorization"] == f"Bearer {FAKE_KEY}", "头里带是对的，别处不许出现"


def test_submit_refuses_a_task_assigned_to_another_agent(gateway):
    """身份那道拒绝要能被**单独**验到：岗位仍取 `reviewer`（注册表里 doubao 唯一的挂载），
    于是能拦住这张卡的只有 agent_id 比对。
    （上一版把岗写成 `developer` ⇒ 两道拒绝的 match 词都是「拒发」，把身份那道变异掉、
    岗位那道会替它红，用例分不清是谁出手 —— 本轮变异 M01 暴露。）
    """
    transport, record = build(gateway, [])
    with pytest.raises(DoubaoTransportError, match="本 transport 是") as caught:
        transport.submit(task(assignee={"agent_id": "qoder", "role_id": "reviewer"}))
    assert "qoder" in str(caught.value), "消息里要点名是谁的卡，否则排障只能靠猜"
    assert record == []


# —— 三、失败面：HTTP 状态、错误码与重试档 ——


def test_non_200_carries_status_vendor_code_and_request_id_but_not_the_message(gateway):
    """甲档实证：404 的自由文本里带账号 ID ⇒ 我们取**错误码**，不抄整句。"""
    body = {"error": {"code": "ModelNotOpen", "message": "Your account 2132870667 has not activated the model"}}
    record: list = []

    def _handler(request: httpx.Request) -> httpx.Response:
        record.append({"url": str(request.url), "headers": dict(request.headers), "body": {}})
        return httpx.Response(404, json=body, headers={"x-request-id": "req-err-1"})

    transport = Transport(
        api=spec(),
        http=httpx.Client(transport=httpx.MockTransport(_handler)),
        gateway=gateway,
        env={API_KEY_ENV: FAKE_KEY},
    )
    with pytest.raises(DoubaoTransportError) as caught:
        transport.submit(task())
    err = caught.value
    assert (err.http_status, err.vendor_code, err.retryable) == (404, "ModelNotOpen", False)
    assert err.request_id == "req-err-1", "请求号是厂商侧唯一可追的线索，失败路径也得带上"
    assert "ModelNotOpen" in str(err) and "HTTP 404" in str(err)
    assert "req-err-1" in str(err)
    assert "2132870667" not in str(err) and "has not activated" not in str(err)
    assert len(record) == 1, "失败不许自动重试（429 也只发一次：重试策略归编排层，不在 transport 里定）"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, False), (403, False), (404, False),
        (408, True), (409, True), (425, True), (429, True),
        (500, True), (503, True), (418, None),
    ],
)
def test_retryable_is_three_valued_not_defaulted(gateway, status, expected):
    """认证类与"资源不存在"是确定性失败 ⇒ 不可重试；认不出来就明写 `None`，不默认 True。"""
    transport, _ = build(gateway, [(status, {"error": {"code": "whatever", "message": "x"}})])
    with pytest.raises(DoubaoTransportError) as caught:
        transport.submit(task())
    assert caught.value.retryable is expected


def test_retryable_status_sets_are_pinned_by_literals():
    """两张表在本文件里用字面量重写一遍：实现里换掉任何一个状态码，这条就红。

    上一版这里写的是 `(status in NON_RETRYABLE_STATUS) is (expected is False) or ...`，
    读的是实现自己的常量 ⇒ 把 408/409/425 从可重试档里删掉它照样绿（本轮变异 M14 暴露），
    而且 `or` 那半支几乎恒真。分档本身归 `tasks.md` 的口径，这里只钉"表长什么样"。
    """
    assert sorted(NON_RETRYABLE_STATUS) == [401, 403, 404]
    assert {408, 409, 425, 429} <= RETRYABLE_STATUS
    assert all(status in RETRYABLE_STATUS for status in range(500, 600))
    assert not NON_RETRYABLE_STATUS & RETRYABLE_STATUS, "重叠会让同一个状态先命中哪档看遍历顺序"


def test_error_body_without_a_code_is_still_reported_as_such(gateway):
    transport, _ = build(gateway, [(500, "not json at all")])
    with pytest.raises(DoubaoTransportError) as caught:
        transport.submit(task())
    assert caught.value.vendor_code is None
    assert "响应体里没有 code" in str(caught.value)
    assert caught.value.retryable is True


def test_native_ref_falls_back_to_the_request_id_header(gateway):
    """厂商没报 `id` 时用请求号兜住（两处形态都是 `NEEDS VERIFICATION`，兜一层比造一个假数好）。"""

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=completion_body(id=None), headers={"x-request-id": "req-fallback-1"})

    transport = Transport(
        api=spec(),
        http=httpx.Client(transport=httpx.MockTransport(_handler)),
        gateway=gateway,
        env={API_KEY_ENV: FAKE_KEY},
    )
    assert transport.submit(task()).native_ref == "req-fallback-1"


def test_native_ref_refuses_when_neither_source_exists(gateway):

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [], "usage": {}})

    transport = Transport(
        api=spec(),
        http=httpx.Client(transport=httpx.MockTransport(_handler)),
        gateway=gateway,
        env={API_KEY_ENV: FAKE_KEY},
    )
    with pytest.raises(DoubaoTransportError, match="native_ref"):
        transport.submit(task())


# —— 四、回收：留档重查、产物实测与用量 ——


def test_collect_rereads_the_archive_from_disk_not_from_memory(gateway):
    """与 T041 同一条纪律：`collect()` 只拿得到 `Handle` ⇒ 读的是盘上那份。"""
    transport, _ = build(gateway, [(200, completion_body())])
    receipt = transport.submit(task())
    persisted(gateway, receipt.native_ref).write_bytes(
        json.dumps(completion_body(choices=[{"index": 0, "message": {"role": "assistant", "content": "盘上改成这句"}, "finish_reason": "stop"}], usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3})).encode("utf-8")
    )
    assert transport.collect(receipt).self_assessment == "盘上改成这句"


def test_collect_measures_the_artifact_from_disk(gateway, bus_root):
    """FR-019：三个数读自盘上那份字节。内容带多字节中文 ⇒ 取 `len(str)` 就红。"""
    content = "第一条：通过\n第二条：通过\n你好世界\n"
    transport, _ = build(gateway, [(200, completion_body(choices=[{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}], usage={"prompt_tokens": 5, "completion_tokens": 6, "total_tokens": 11}))])
    receipt = transport.submit(task())
    result = transport.collect(receipt)
    assert isinstance(result, TaskResult)
    (ref,) = result.artifacts
    on_disk = (bus_root / WS / REVIEWS_SUBDIR / f"T01-{receipt.native_ref}.md").read_bytes()
    assert ref.bytes == len(on_disk) == len(content.encode("utf-8"))
    assert ref.bytes != len(content), "取到字符串长度 = 没读盘"
    assert ref.lines == len(content.splitlines()) == 3
    assert ref.format == "md" and ref.rel_path == f"{WS}/{REVIEWS_SUBDIR}/T01-{receipt.native_ref}.md"
    assert ref.rel_path.startswith("doubao-workspace/reviews/"), (
        "目录名写死一份字面量：它同时是 `.gitignore` 之外的工作区约定，换名会让历史留档读不到"
    )


def test_the_response_archive_is_written_through_the_gateway_with_the_handle_identity(gateway):
    transport, _ = build(gateway, [(200, completion_body())])
    receipt = transport.submit(task())
    assert persisted(gateway, receipt.native_ref).is_file(), "响应没留档 ⇒ 重启后无从重查"
    stored = json.loads(persisted(gateway, receipt.native_ref).read_text(encoding="utf-8"))
    assert stored["id"] == receipt.native_ref
    assert (gateway.root / "doubao-workspace" / ".acp-responses" / "chatcmpl-fixture-0001.json").is_file(), (
        "留档目录名写死一份字面量：它与产物目录 `reviews/` 是两条不同的约定，"
        "只靠导入的常量比对会让「改目录名」这种漂移看不见"
    )


@pytest.mark.parametrize(
    ("finish", "expected"),
    [("stop", "success"), ("content_filter", "failed")],
)
def test_verdict_uses_only_what_the_vendor_reported(gateway, finish, expected):
    transport, _ = build(gateway, [(200, completion_body())])
    receipt = transport.submit(task())
    persisted(gateway, receipt.native_ref).write_text(
        json.dumps(
            {
                "id": receipt.native_ref,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "x"}, "finish_reason": finish}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        ),
        encoding="utf-8",
    )
    assert transport.collect(receipt).self_reported_status == expected


def test_truncation_stays_success_but_leaves_an_issue(gateway):
    """`length` 是"到上限收尾"，不是厂商说失败 ⇒ 达不达标归 `ValidateArtifact`（FR-019 / 契约 §2.2）。"""
    transport, _ = build(gateway, [(200, completion_body())])
    receipt = transport.submit(task())
    persisted(gateway, receipt.native_ref).write_text(
        json.dumps(
            {
                "id": receipt.native_ref,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "半句"}, "finish_reason": "length"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        ),
        encoding="utf-8",
    )
    result = transport.collect(receipt)
    assert result.self_reported_status == "success"
    assert any(note.startswith("truncated") for note in result.issues)

    # 反向：`stop` 一个字都不该多带。没有这半条，"无条件追加截断说明"的变异就没人红
    # （本轮变异 M24 暴露 —— 只验"该出现时出现"的判据，等于没验触发条件）。
    persisted(gateway, receipt.native_ref).write_text(
        json.dumps(
            {
                "id": receipt.native_ref,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "整句"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        ),
        encoding="utf-8",
    )
    stopped = transport.collect(receipt)
    assert stopped.self_reported_status == "success"
    assert stopped.issues == [], "正常收尾不该带截断说明，也不该带推理 token 那条（这份 usage 没报 details）"


def test_unrecognised_finish_reason_is_refused_not_defaulted(gateway):
    transport, _ = build(gateway, [(200, completion_body())])
    receipt = transport.submit(task())
    persisted(gateway, receipt.native_ref).write_text(
        json.dumps(
            {
                "id": receipt.native_ref,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "x"}, "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(DoubaoTransportError, match="不在本笔认的取值里") as caught:
        transport.collect(receipt)
    assert "tool_calls" in str(caught.value) and "content_filter" in str(caught.value)


def test_known_finish_reason_sets_are_disjoint_and_covered():
    """三档不许重叠：重叠会让同一个取值既进 success 又进 failed（先命中哪个看遍历顺序 = 假判据）。"""
    groups = [
        doubao_module.SUCCESS_FINISH_REASONS,
        doubao_module.VENDOR_REFUSAL_FINISH_REASONS,
        doubao_module.TRUNCATED_FINISH_REASONS,
    ]
    assert set().union(*groups) == KNOWN_FINISH_REASONS
    assert sum(len(group) for group in groups) == len(KNOWN_FINISH_REASONS)


@pytest.mark.parametrize(
    "body",
    [
        {"id": "x", "choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
        {"id": "x", "choices": {"0": {}}, "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
    ],
    ids=["empty-choices", "choices-not-a-list"],
)
def test_absent_or_malformed_choices_are_refused(gateway, body):
    transport, _ = build(gateway, [(200, body)])
    receipt = transport.submit(task())
    with pytest.raises(DoubaoTransportError, match="choices"):
        transport.collect(receipt)


def test_multiple_choices_are_refused_instead_of_picking_one(gateway):
    body = {
        "id": "chatcmpl-two",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "A"}, "finish_reason": "stop"},
            {"index": 1, "message": {"role": "assistant", "content": "B"}, "finish_reason": "stop"},
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    transport, _ = build(gateway, [(200, body)])
    receipt = transport.submit(task())
    with pytest.raises(DoubaoTransportError, match="没请求过"):
        transport.collect(receipt)


def test_non_mapping_response_body_is_refused_on_both_faces(gateway):
    """顶层是数组（厂商回了个列表）⇒ 下发和回收都得明着拒，不许落到 `.get` 的 AttributeError 上。

    本条是本轮变异 M19 补的：删掉 `isinstance(payload, dict)` 那道检查后，
    后面的 `payload.get(...)` 抛的是 AttributeError，`pytest.raises(DoubaoTransportError)` 收不到信号
    ⇒ 那条检查当时没有任何用例守着。
    """
    transport, _ = build(gateway, [(200, "[1, 2]")])
    with pytest.raises(DoubaoTransportError, match="顶层必须是映射"):
        transport.submit(task())

    transport2, _ = build(gateway, [(200, completion_body())])
    receipt = transport2.submit(task())
    persisted(gateway, receipt.native_ref).write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(DoubaoTransportError, match="顶层必须是映射"):
        transport2.collect(receipt)


def test_missing_usage_is_refused_instead_of_zero_filled(gateway):
    """FR-059：没读数就是没法记账，落 0 是造一笔假账。"""
    body = {
        "id": "chatcmpl-nousage",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "x"}, "finish_reason": "stop"}],
    }
    transport, _ = build(gateway, [(200, body)])
    receipt = transport.submit(task())
    with pytest.raises(DoubaoTransportError, match="无法记账") as caught:
        transport.collect(receipt)
    assert "usage" in str(caught.value)


def test_usage_missing_one_dimension_is_named(gateway):
    body = {
        "id": "chatcmpl-partial",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "x"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 12},
    }
    transport, _ = build(gateway, [(200, body)])
    receipt = transport.submit(task())
    with pytest.raises(DoubaoTransportError, match="completion_tokens"):
        transport.collect(receipt)


def test_non_numeric_usage_becomes_a_transport_error_not_a_pydantic_one(gateway):
    body = {
        "id": "chatcmpl-dirty",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "x"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": "banana", "completion_tokens": 3},
    }
    transport, _ = build(gateway, [(200, body)])
    receipt = transport.submit(task())
    with pytest.raises(DoubaoTransportError, match="不是数字") as caught:
        transport.collect(receipt)
    assert "prompt_tokens" in str(caught.value) and "str" in str(caught.value)


def test_usage_lands_as_two_token_notices_without_amounts(gateway):
    """口径 = `TOKEN`（§6：豆包按 Token；`SEAT_CREDIT` 是坐席、`BYOK` 是自带 Key）。"""
    transport, _ = build(gateway, [(200, completion_body())])
    result = transport.collect(transport.submit(task()))
    assert {(item.usage_kind, str(item.quantity)) for item in result.usage} == {("INPUT", "52"), ("OUTPUT", "30")}
    assert all(item.unit == TOKEN_UNIT and item.billing_basis == "TOKEN" for item in result.usage)
    for notice in result.usage:
        assert not hasattr(notice, "cost_amount") and not hasattr(notice, "currency"), "金额由账本按 §6 换算"


def test_reasoning_tokens_are_kept_as_evidence_not_a_fake_dimension(gateway):
    """D-1 要"单列"，而 `UsageKind` 没有那一档 ⇒ 落进 `issues` 留证，不自加枚举值（那是改契约）。"""
    transport, _ = build(gateway, [(200, completion_body())])
    result = transport.collect(transport.submit(task()))
    (note,) = [item for item in result.issues if item.startswith("reasoning_tokens=")]
    assert "29" in note and "UsageKind" in note and "待裁" in note

    body = completion_body(usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3})
    transport2, _ = build(gateway, [(200, body)])
    assert not any(
        item.startswith("reasoning_tokens=") for item in transport2.collect(transport2.submit(task())).issues
    )


def test_collect_refuses_a_handle_from_another_agent(gateway):
    transport, _ = build(gateway, [])
    foreign = Handle(
        task_id="T01",
        agent_id="qoder",
        native_ref="x",
        submitted_at=datetime.now(timezone.utc),
    )
    with pytest.raises(DoubaoTransportError, match="不是本 transport"):
        transport.collect(foreign)


def test_collect_refuses_a_missing_archive(gateway):
    transport, _ = build(gateway, [(200, completion_body())])
    transport.submit(task())
    with pytest.raises(DoubaoTransportError, match="回收找不到响应留档"):
        transport.collect(handle_of(body={"id": "chatcmpl-never"}))


def test_collect_refuses_a_corrupted_archive(gateway):
    transport, _ = build(gateway, [(200, completion_body())])
    receipt = transport.submit(task())
    persisted(gateway, receipt.native_ref).write_text("{ not json", encoding="utf-8")
    with pytest.raises(DoubaoTransportError, match="不是合法 JSON") as caught:
        transport.collect(receipt)
    assert caught.value.retryable is None


def test_leak_form_in_the_model_output_is_washed_on_the_way_in(gateway, bus_root):
    """响应正文里出现凭据形态 ⇒ 经网关落盘时被洗；产物与节选读到的都是洗过的那份。

    方向点名：这里"洗"发生在**我们写产物的那一步**（网关的写入口），不是事后补救；
    留档那一份同样经过它，所以两个面都不含形态凭据。
    """
    dirty = f"结论：通过。用的键是 {LEAK_FORM}"
    body = completion_body(
        choices=[{"index": 0, "message": {"role": "assistant", "content": dirty}, "finish_reason": "stop"}]
    )
    transport, _ = build(gateway, [(200, body)])
    receipt = transport.submit(task())
    result = transport.collect(receipt)
    assert LEAK_FORM not in result.log_excerpt and "[REDACTED]" in result.log_excerpt
    artifact = (bus_root / WS / REVIEWS_SUBDIR / f"T01-{receipt.native_ref}.md").read_text(encoding="utf-8")
    assert LEAK_FORM not in artifact
    assert LEAK_FORM not in persisted(gateway, receipt.native_ref).read_text(encoding="utf-8")
    assert result.artifacts[0].bytes == len(artifact.encode("utf-8"))


def test_log_excerpt_is_the_tail_of_the_content(gateway):
    long = "开头" + "x" * 500
    body = completion_body(
        choices=[{"index": 0, "message": {"role": "assistant", "content": long}, "finish_reason": "stop"}]
    )
    transport, _ = build(gateway, [(200, body)])
    result = transport.collect(transport.submit(task()))
    assert len(result.log_excerpt) == 400 and result.log_excerpt.startswith("x")


# —— 五、cancel ——


def test_cancel_is_an_explicit_noop_and_issues_no_request(gateway):
    """这一形态没有"可撤的远端对象"：补全请求返回时那一趟已经结束（与 T041 的未闭同源）。"""
    transport, record = build(gateway, [(200, completion_body())])
    receipt = transport.submit(task())
    assert transport.cancel(receipt) is None
    assert len(record) == 1, "撤销不该再发一次请求"


# —— 六、零耦合与文档对齐 ——


@pytest.mark.parametrize(
    ("vendor", "sdk_literal"),
    [("doubao", "openai"), ("doubao", "qoder_agent_sdk"), ("workbuddy", "openai"), ("qoder", "openai")],
)
def test_no_adapter_imports_a_vendor_sdk(vendor, sdk_literal):
    """适配器不 import 任何厂商 SDK。

    `openai>=1.30` 曾出现在运行时面但零 import，2026-10-29 体检已从 `pyproject.toml` 移除；
    这条仍然守住“豆包 / WorkBuddy / Qoder 三家都不许把 SDK 拖进传输层”的结构。
    """
    path = ADAPTER_DIR / vendor / "transport.py"
    hits = _protocol_fixture.vendor_import_hits(path.read_text(encoding="utf-8"))
    assert [hit for hit in hits if sdk_literal in hit] == [], (vendor, hits)


def test_vendor_import_scanner_still_catches_a_real_violation():
    assert _protocol_fixture.vendor_import_hits("from openai import OpenAI\n") == ["1: 从厂商 SDK 导入 openai"]
    assert _protocol_fixture.vendor_import_hits("from acp.adapters.workbuddy import transport" + chr(10))


def test_base_url_and_credential_name_match_params_ref_section_4():
    """§4 是人读的清单，这条把被跑的常量和它对齐（改一边另一边就红 = 不会漂）。"""
    row = next(
        line for line in PARAMS_REF.read_text(encoding="utf-8").splitlines() if line.startswith("| 豆包 |")
    )
    assert ARK_BASE_URL in row and API_KEY_ENV in row
    assert CHAT_COMPLETIONS_PATH.lstrip("/") in row or "chat/completions" not in row  # §4 只给 base_url
