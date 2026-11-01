"""T041：Qoder transport 的契约测试（CLI 子进程形态，全程 `FakeRunner`，不起真进程）。

**不装 qodercli、不花 API 费**（owner 2026-10-13 裁决 2 + 成本纪律 5）：注入点 = `CliRunner` 这支
Protocol。argv 构造、stdin 报文渲染、退出码判定、流水重查、归属/格式/脱敏/实测、usage 两档全是真代码，
只有"子进程"这一环是脚本化的应答。⇒ 本文件证明的是"我们对 CLI 的调用与解析自洽且不可旁路"，
**不是**"真 CLI 接受这套旗标"（后者是丙档，冒烟轮次的事，`tasks.md` 的 T041 申报按档挂账）。

`SubprocessRunner` 那一节（九）例外地要注一个 `cli_path`：那一条不注入就会去查**这台机器**上有没有
CLI（本文件因此仍然是"不起真进程"的，但它不再与真机的安装状态无关）。查找的六档顺序（CN 优先；
2026-10-24 由三档扩成六档）、真起一发的活性档、与厂商 SDK 的符合性都在
`tests/guards/test_qoder_cli_lookup.py`。

"双向"同样落在两处、方向不同：
- **契约 → 实现**：`contracts/agent-protocol.md §4` 的 ```python 围栏与 `Transport` 的运行期形状做差集，
  用的是 `test_adapter_protocol.py` 里**同一套**解析与比对函数（复制一份比较器 = 承认两份会漂开）；
- **实现 → 契约**：`registry/agents.yaml` 那条 `acp.adapters.qoder:Transport` 经 `Registration.resolve()`
  必须取回**这个类本身**。

与 T040 的一实质差别（本文件三分之一的用例在为它服务）：Qoder 是**外部进程自己写盘**，
产物不经我们的写入口 ⇒ 归属、存在性、格式、脱敏四道都必须在"登记"之前重跑一遍，不能信厂商报的清单。
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import logging
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

import acp.adapters.qoder.transport as qoder_transport
from acp.adapters.base import AgentAdapter
from acp.adapters.qoder import Transport
from acp.adapters.qoder.transport import (
    CLI_BINARY,
    CN_CLI_BINARY,
    CN_CLI_VERSION_EXPECTED,
    CN_CLI_WRAPPER,
    CN_CONFIG_DIR_NAME,
    CN_FLAVOR,
    CN_PAT_ENV,
    CREDENTIAL_ENV_NAMES,
    IMPORT_NAME,
    INTL_CLI_BINARY,
    INTL_CLI_VERSION_EXPECTED,
    INTL_FLAVOR,
    INTL_PAT_ENV,
    LOG_FILE_NAME,
    NO_ROLE_DECLARED,
    PACKAGE_NAME,
    PROJECTS_DIR_NAME,
    RUNS_DIR_NAME,
    RUNS_SUBDIR,
    SDK_IDENTITY_ENV_NAMES,
    SERVICE_ACCOUNT_ENV,
    CliRunner,
    QoderCliSpec,
    QoderTransportError,
    ResolvedCli,
    RunOutcome,
    SubprocessRunner,
    confine_cwd,
    parse_events,
    tools_in_flight,
)
from acp.bus.gateway import BusError, BusGateway
from acp.protocol import Handle, Task, TaskResult
from acp.registry.loader import load_registrations

REPO_ROOT = Path(__file__).resolve().parents[2]
QUICKSTART = REPO_ROOT / "specs" / "001-multi-agent-collab-platform" / "quickstart.md"
PARAMS_REF = REPO_ROOT / "config" / "params-ref.md"
ADAPTER_DIR = REPO_ROOT / "src" / "acp" / "adapters"

WS = "qoder-workspace"

#: 假凭据。刻意不构成任何真实形态；`sk-` 那一支在下面单独造，用来验脱敏而不是用来当凭据。
FAKE_SA = "fixture-service-account-key-value"
FAKE_PAT = "fixture-pat-value"
FAKE_CN_PAT = "fixture-cn-pat-value"
LEAK_FORM = "sk-" + "qomocknotreal0123456789"
#: 假可执行文件路径。`SubprocessRunner` 一节每条都要注入它：不注入 = 那一发会去查**这台机器**上的
#: CLI（本机有 ⇒ 绿；换一台没有的 ⇒ 整节 error）。查这件事本身由
#: `tests/guards/test_qoder_cli_lookup.py` 负责，那里才有真机的顺序与那一发真起进程。
FIXTURE_CLI = "fixture-cli-abs-path"
#: 两个品牌各自的假路径：文件名决定 flavor（取证根与凭据映射都按它选），文件本身不必存在。
FIXTURE_CN_CLI = f"fixture-cn-cli/{CN_CLI_BINARY}.exe"
FIXTURE_INTL_CLI = f"fixture-intl-cli/{INTL_CLI_BINARY}.exe"


def _load_sibling(name: str):
    path = Path(__file__).with_name(f"{name}.py")
    spec = importlib.util.spec_from_file_location(f"acp_contract_fixture.{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_protocol_fixture = _load_sibling("test_adapter_protocol")
_messages = _load_sibling("test_protocol")


def make_env(**override) -> dict[str, str]:
    env = {SERVICE_ACCOUNT_ENV: FAKE_SA, INTL_PAT_ENV: FAKE_PAT, CN_PAT_ENV: FAKE_CN_PAT}
    env.update(override)
    for key, value in list(env.items()):
        if value is None:
            del env[key]
    return env


@dataclass
class Call:
    """一次 `runner.run()` 的入参实况。"""

    argv: tuple[str, ...]
    stdin_text: str
    run_key: str
    timeout_seconds: float
    cwd: str


class FakeRunner:
    """脚本化应答（owner 裁决 2 的 `FakeRunner`）。队列空了还被叫 ⇒ 直接抛，不给偷偷重试的机会。"""

    def __init__(self, outcomes: list[RunOutcome] | None = None) -> None:
        self.outcomes: list[RunOutcome] = list(outcomes or [])
        self.calls: list[Call] = []
        self.aborted: list[str] = []

    def run(
        self,
        *,
        argv: tuple[str, ...],
        stdin_text: str,
        run_key: str,
        timeout_seconds: float,
        cwd: Path | str,
    ) -> RunOutcome:
        self.calls.append(Call(argv, stdin_text, run_key, timeout_seconds, str(cwd)))
        if not self.outcomes:
            raise AssertionError("transport 发起了比用例预期更多的运行（不许偷偷重试）")
        return self.outcomes.pop(0)

    def abort(self, run_key: str) -> bool:
        self.aborted.append(run_key)
        return False


class RecordingGateway(BusGateway):
    """把每次写入口的 (actor, path, capability) 记下来 ⇒ 归属判定用的身份可以被断言，而不是被声称。"""

    def __init__(self, project_root: Path) -> None:
        super().__init__(project_root)
        self.writes: list[dict] = []

    def write_artifact(self, actor, path, payload, capability=None):  # type: ignore[override]
        self.writes.append(
            {"actor": actor, "path": path, "payload": payload, "capability": capability}
        )
        return super().write_artifact(actor, path, payload, capability)


def stream_writes(gateway: RecordingGateway) -> list[dict]:
    return [item for item in gateway.writes if RUNS_SUBDIR in item["path"]]


def artifact_writes(gateway: RecordingGateway) -> list[dict]:
    return [item for item in gateway.writes if RUNS_SUBDIR not in item["path"]]


def event(type_value: str, **fields) -> dict:
    return {"type": type_value, **fields}


def assistant(text: str) -> dict:
    return event(
        "assistant",
        message={
            "role": "assistant",
            "model": "fixture-model",
            "content": [{"type": "text", "text": text}],
        },
    )


def tool_use(name: str, path: str) -> dict:
    return event(
        "assistant",
        message={
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "tu-1", "name": name, "input": {"file_path": path}}],
        },
    )


def result_event(**override) -> dict:
    base = {
        "subtype": "success",
        "is_error": False,
        "num_turns": 1,
        "duration_ms": 1200,
        "duration_api_ms": 1100,
        "session_id": "s-1",
        "result": "已产出 index.html",
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "total_cost_usd": 0.0,
    }
    return event("result", **{**base, **override})


def stdout_of(*events: dict, noise: tuple[str, ...] = ()) -> str:
    lines = [json.dumps(item, ensure_ascii=False) for item in events]
    return "\n".join([*noise, *lines]) + "\n"


def outcome(stdout: str = "", **override) -> RunOutcome:
    fields = {"exit_code": 0, "reason": "success", "stdout": stdout, "stderr": "", "log_path": None}
    fields.update(override)
    return RunOutcome(**fields)  # type: ignore[arg-type]


@pytest.fixture
def bus_root(tmp_path: Path) -> Path:
    root = tmp_path / "AI项目台" / "demo"
    root.mkdir(parents=True)
    return root


@pytest.fixture
def gateway(bus_root: Path) -> RecordingGateway:
    return RecordingGateway(bus_root)


def build(gateway: BusGateway, outcomes: list[RunOutcome] | None = None, **kwargs):
    """造一个走 FakeRunner 的 transport；返回 (transport, runner)。

    `cwd` 不默认成"进程的当前目录"：这里取 `gateway.root`（那份临时总线根），形状与生产同一个载体
    （生产交的是 `archive.require_archive_root(project_key)`，同一个目录也是网关的根）。
    用例可以显式传一个别的值，用来证明 transport 交出去的就是它手里那一个，不改写、不留默认。
    """
    runner = FakeRunner(outcomes)
    transport = Transport(
        runner=runner,
        gateway=gateway,
        cwd=kwargs.pop("cwd", gateway.root),
        spec=kwargs.pop("spec", None),
        env=kwargs.pop("env", make_env()),
        **kwargs,
    )
    return transport, runner


def task(**override) -> Task:
    """借用 `test_protocol.py` 的那份 payload（13 项必填只有一份抄本）。

    只改一件事：岗位取 `architect` —— 原 fixture 写 `frontend`（注册表从没申报过它），T041 那一轮改成
    `developer`（当时 `role_mounts` 是 `[developer]`）；owner 2026-10-13 裁决 2 把 `developer` 这个岗取消、
    申报换成三个前置岗 ⇒ 这里跟着换。选 `architect` 而不是 `reviewer` / `acceptor`：后两者按 FR-033 是只读岗，
    而本文件几乎所有用例都要走"写产物"那一条路。照抄一个没申报过的岗位，每条用例都会先撞上别的理由。
    """
    payload = _messages.task_payload(
        **{"assignee": {"agent_id": "qoder", "role_id": "architect"}, **override}
    )
    return Task(**payload)


def handle(run_key: str = "T01-fixed0000") -> Handle:
    return Handle(
        task_id="T01",
        agent_id="qoder",
        native_ref=run_key,
        submitted_at=datetime(2026, 10, 13, 4, 0, tzinfo=timezone.utc),
    )


def write_artifact(bus_root: Path, rel: str, content: str) -> Path:
    """模拟"CLI 自己写了这个文件"：直接落盘，不经我们的写入口（这正是本适配器的处境）。"""
    target = bus_root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content.encode("utf-8"))
    return target


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


def test_transport_is_structurally_an_agent_adapter(gateway):
    transport, _ = build(gateway, [])
    assert isinstance(transport, AgentAdapter)


def test_transport_signatures_carry_no_bare_dict():
    """D-7 的签名面：实现交出去的四个签名里不许出现裸 `dict`。"""
    assert _protocol_fixture.bare_dict_hits(_protocol_fixture.protocol_shape(Transport)) == []


def test_registry_entry_resolves_to_this_transport_class():
    """双向的右半边：注册表那条入口字符串必须取回**这个类本身**（顺带钉住 `__init__.py` 的重导出）。"""
    registrations = {item.agent_id: item for item in load_registrations()}
    assert registrations["qoder"].adapter_entry == "acp.adapters.qoder:Transport"
    assert registrations["qoder"].resolve() is Transport


def test_both_runner_implementations_satisfy_the_cli_runner_protocol(gateway):
    """注入点的两面：测试用 `FakeRunner`、生产用 `SubprocessRunner`，形状都必须是 `CliRunner`。"""
    transport, runner = build(gateway, [])
    assert isinstance(runner, CliRunner)
    assert isinstance(SubprocessRunner(), CliRunner)
    assert transport.runner is runner, "构造函数 MUST 用注入的那支，而不是自己 new 一支真的"


# —— 二、describe：数据只从注册表来 ——


def test_describe_returns_the_registry_manifest_itself(gateway):
    transport, runner = build(gateway, [])
    manifest = transport.describe()
    registered = {item.agent_id: item.manifest for item in load_registrations()}["qoder"]
    assert manifest == registered
    assert manifest.agent_id == transport.agent_id == "qoder"
    assert runner.calls == [], "describe 不该起子进程：它是申报单的回读，不是一次调用"


def test_transport_source_does_not_restate_the_declaration():
    """`transport.py` 的源码文本里不许出现注册表里那几项申报值 ⇒ 证明它真去读注册表而不是抄了一份。"""
    text = Path(inspect.getfile(Transport)).read_text(encoding="utf-8")
    manifest = {item.agent_id: item.manifest for item in load_registrations()}["qoder"]
    tokens = list(manifest.capabilities) + list(manifest.role_mounts) + [manifest.cost_band]
    offenders = [token for token in tokens if token in text]
    assert offenders == [], offenders


# —— 三、下发：argv 与 stdin 的构造 ——


def test_submit_builds_a_requeryable_handle(gateway):
    transport, runner = build(gateway, [outcome()])
    receipt = transport.submit(task())
    assert isinstance(receipt, Handle)
    assert receipt.agent_id == "qoder" and receipt.task_id == "T01"
    assert receipt.native_ref == runner.calls[0].run_key, "native_ref 必须是 runner 那一趟的键，否则 cancel/collect 找不到"
    assert receipt.submitted_at.tzinfo is not None, "契约 §4.1：`submitted_at` 带时区"


def test_submit_passes_argv_and_timeout_verbatim(gateway):
    """transport 不改写 argv：旗标的唯一来源是 `QoderCliSpec`（冒烟那轮只改一处）。"""
    spec = QoderCliSpec(argv=("fixture-cli", "--print"), timeout_seconds=7.5)
    transport, runner = build(gateway, [outcome()], spec=spec)
    transport.submit(task())
    assert runner.calls[0].argv == ("fixture-cli", "--print")
    assert runner.calls[0].timeout_seconds == 7.5


def test_the_default_cli_mode_accepts_edits_inside_cwd_and_nothing_wider():
    """owner 2026-10-21 真实点火后的裁决：只放行 cwd 内的编辑，不拿全系统旁路换"能落盘"。

    原值 `dontAsk` 在无头模式下一律拒掉需要审批的动作 ⇒ 模型把 CSS 写在回复正文里、`Write` 被 CLI
    拦在落盘前（`qodercli.log` 原话："This action needs approval, and the \"Don't ask\" permission mode
    does not prompt."）。现取值 `accept_edits` 的官方语义 = 自动放行**工作目录内**的安全文件编辑；
    工作目录由 `realrun._confine()` 交 `<存档根>/<agent>-workspace`、并由 `confine_cwd()` 再验一次
    ⇒ 放行面就是 `qoder-workspace` 这一格。三支更宽的口子（`bypass_permissions` / `yolo` /
    `--dangerously-skip-permissions`）全系统跳审批，`--add-dir` 会把信任面扩到 cwd 之外 ⇒ 都不许回来。
    """
    argv = QoderCliSpec().argv
    assert argv[0] == CLI_BINARY
    assert argv[-2:] == ("--permission-mode", "accept_edits"), "规范拼写是下划线形态（CLI --help 的取值表）"
    banned = frozenset(
        {
            "dontAsk",
            "dont_ask",
            "bypass_permissions",
            "bypassPermissions",
            "yolo",
            "--dangerously-skip-permissions",
            "--add-dir",
            "auto",
        }
    )
    assert banned.isdisjoint(argv), f"越权口子不许回到默认 argv：{banned.intersection(argv)}"


def test_prompt_carries_the_visible_face_and_the_criteria(gateway):
    """裁决 2（2026-10-13）在消息面的落点：三面 + 交付下限 + **逐条标准**都随任务下发。

    前身 `test_prompt_carries_the_visible_face_and_nothing_else` 钉的是"细则不发"；owner 裁定
    FR-029 的比对主体手里必须有标准 ⇒ 断言方向整体反转。反转不等于放开整张卡：
    frontmatter 仍不许进消息面，那半条纪律留在这里。
    """
    transport, runner = build(gateway, [outcome()])
    transport.submit(task())
    payload = json.loads(runner.calls[0].stdin_text)
    content = payload["message"]["content"]
    card = task()
    assert payload["type"] == "user" and payload["message"]["role"] == "user"
    assert isinstance(content, str), "SDK 的 user 报文用字符串 content（`client.py:296-301`）"
    assert card.objective in content and card.expected_output in content
    assert card.artifact_floor.format in content and "200" in content
    for index, criterion in enumerate(card.acceptance_criteria, 1):
        assert f"{index}. {criterion}" in content, "逐条编号送出：不编号就分不清哪条对哪条"
    assert "---" not in content, "frontmatter 不进消息面"


def test_stdin_session_id_is_the_run_key(gateway):
    """一次下发 = 一个会话：厂商侧会话标识与我们侧的可重查标识对齐（契约 §4.1）。"""
    transport, runner = build(gateway, [outcome()])
    receipt = transport.submit(task())
    payload = json.loads(runner.calls[0].stdin_text)
    assert payload["session_id"] == runner.calls[0].run_key == receipt.native_ref
    assert payload["parent_tool_use_id"] is None


def test_submit_persists_the_stream_before_returning(gateway):
    transport, _ = build(gateway, [outcome(stdout=stdout_of(result_event()))])
    receipt = transport.submit(task())
    (write,) = stream_writes(gateway)
    assert write["path"] == f"{WS}/{RUNS_SUBDIR}/{receipt.native_ref}.jsonl"
    assert "已产出 index.html" in write["payload"]


def test_submit_refuses_a_task_assigned_to_another_agent(gateway):
    transport, runner = build(gateway, [])
    with pytest.raises(QoderTransportError, match="拒发"):
        transport.submit(task(assignee={"agent_id": "workbuddy", "role_id": "architect"}))
    assert runner.calls == []


def test_missing_credential_stops_before_any_run(gateway):
    """两个名字都没值 ⇒ 抛，且一次都不起进程（"先试一下再看错误"会真花钱）。

    单点参数化在这里是**假的**：`_require_credential()` 在两个都没值时走同一支分支，
    所以这里只钉一支，"只有一个有值"那一支由下面那条负对照钉。
    """
    transport, runner = build(gateway, [], env={})
    with pytest.raises(QoderTransportError, match="凭据未注入") as caught:
        transport.submit(task())
    for name in CREDENTIAL_ENV_NAMES:
        assert name in str(caught.value), "错误消息只许报变量名，两个候选都要列出来"
    assert runner.calls == []


@pytest.mark.parametrize("kept", list(CREDENTIAL_ENV_NAMES))
def test_either_credential_name_alone_is_enough(gateway, kept):
    """§4 那行写的是"Service Account（推荐）/ PAT（备选）"⇒ 两选一，不是两个都要。"""
    env = make_env(**{name: None for name in CREDENTIAL_ENV_NAMES if name != kept})
    transport, runner = build(gateway, [outcome()], env={k: v for k, v in env.items() if v})
    transport.submit(task())
    assert len(runner.calls) == 1


def test_credential_values_never_enter_the_child_invocation(gateway):
    """凭据只从环境来（宪法「二」）：argv、stdin、落盘流水、网关写载荷里都不许出现值本身。"""
    transport, runner = build(gateway, [outcome(stdout=stdout_of(result_event()))])
    transport.submit(task())
    seen = gateway.root.joinpath(*Path(WS, RUNS_SUBDIR).parts)
    surface = "|".join(
        [
            " ".join(runner.calls[0].argv),
            runner.calls[0].stdin_text,
            *(item["payload"] for item in gateway.writes),
            *(path.read_text(encoding="utf-8") for path in seen.rglob("*.jsonl")),
        ]
    )
    for value in (FAKE_SA, FAKE_PAT, FAKE_CN_PAT):
        assert value not in surface


# —— 四、退出取证：约束 ①②③ ——


def test_nonzero_exit_fails_loudly_with_code_reason_and_log_path(gateway):
    """2026-10-11 那次的实况形态（`exit_code=41 reason="auth_error"`、stderr 为空）MUST 立即失败。"""
    transport, runner = build(
        gateway, [outcome(exit_code=41, reason="auth_error", log_path="/root/.qoder/logs/runs/x/qodercli.log")]
    )
    with pytest.raises(QoderTransportError) as caught:
        transport.submit(task())
    err = caught.value
    assert (err.exit_code, err.reason, err.retryable) == (41, "auth_error", False)
    assert "exit_code=41" in str(err) and 'reason="auth_error"' in str(err)
    assert err.log_path.endswith("qodercli.log") and "qodercli.log" in str(err)
    assert runner.calls and len(runner.calls) == 1


@pytest.mark.parametrize(
    ("reason", "expected"),
    [("auth_error", False), ("access_token_invalid", False), ("invalid_request_error", False)],
)
def test_deterministic_failures_are_marked_non_retryable(gateway, reason, expected):
    transport, _ = build(gateway, [outcome(exit_code=41, reason=reason)])
    with pytest.raises(QoderTransportError) as caught:
        transport.submit(task())
    assert caught.value.retryable is expected


@pytest.mark.parametrize(
    ("reason", "timed_out", "expected"),
    [
        ("timeout", False, True),
        ("control_request_timeout", False, True),
        (None, True, True),
        ("weird_reason", False, None),
        (None, False, None),
    ],
    ids=["timeout-reason", "control-timeout", "killed-by-us", "unheard-reason", "no-reason-at-all"],
)
def test_timeout_is_retryable_while_an_unknown_reason_is_explicitly_none(gateway, reason, timed_out, expected):
    """超时=可重试；**认不出来=明写 `None`**，不许默认成 True（那等于给确定性失败发重试券）。"""
    transport, _ = build(gateway, [outcome(exit_code=124, reason=reason, timed_out=timed_out)])
    with pytest.raises(QoderTransportError) as caught:
        transport.submit(task())
    assert caught.value.retryable is expected


def test_zero_exit_with_success_reason_is_not_a_failure(gateway):
    """负对照：约束 ① 不许把成功也判成失败，否则每一次下发都在抛。"""
    transport, _ = build(gateway, [outcome(exit_code=0, reason="success")])
    assert transport.submit(task()).task_id == "T01"
    transport2, _ = build(gateway, [outcome(exit_code=0, reason=None)])
    assert transport2.submit(task()).task_id == "T01"


def test_failure_writes_nothing(gateway):
    """进程已失败 ⇒ 连流水都不落盘：留一份"失败运行"的产物在工作区里，下一次读工作区的人会当成结果。"""
    transport, _ = build(gateway, [outcome(exit_code=41, reason="auth_error", stdout="{}")])
    with pytest.raises(QoderTransportError):
        transport.submit(task())
    assert gateway.writes == []


def test_failure_message_carries_no_log_body_and_no_credential(gateway, bus_root):
    """约束 ②：日志面**只进路径**。真那份日志里有令牌形态的串和正文，一个字都不许被带进异常消息。"""
    log = bus_root / "vendor.log"
    log.write_text(f'process.exiting exit_code=41 reason="auth_error"\n{LEAK_FORM}\n正文\n', encoding="utf-8")
    transport, _ = build(
        gateway, [outcome(exit_code=41, reason="auth_error", log_path=str(log))]
    )
    with pytest.raises(QoderTransportError) as caught:
        transport.submit(task())
    message = str(caught.value)
    assert LEAK_FORM not in message and "正文" not in message
    assert message.count("vendor.log") == 1


# —— 五、回收：流水重查 + 产物五步 ——


def test_collect_rereads_the_stream_from_disk_not_from_memory(gateway):
    """FR-019 的结构性一面：`collect()` 只拿得到 `Handle`，读数必须来自盘上那份。

    下发后把盘上的流水改成另一句 `result` ⇒ 回收读到的必须是**改过的那份**；
    若实现缓存了内存里的 events，这条就红。
    """
    transport, _ = build(gateway, [outcome(stdout=stdout_of(result_event(result="原来那句")))])
    receipt = transport.submit(task())
    stream = gateway.root.joinpath(*Path(WS, RUNS_SUBDIR, f"{receipt.native_ref}.jsonl").parts)
    stream.write_bytes(stdout_of(result_event(result="盘上现在这句")).encode("utf-8"))
    assert transport.collect(receipt).self_assessment == "盘上现在这句"


def test_collect_refuses_an_unknown_handle(gateway):
    transport, _ = build(gateway, [outcome(stdout=stdout_of(result_event()))])
    transport.submit(task())
    with pytest.raises(QoderTransportError, match="回收找不到运行流水"):
        transport.collect(handle("T01-doesnotexist"))


def test_collect_refuses_a_handle_from_another_agent(gateway):
    transport, _ = build(gateway, [])
    foreign = Handle(
        task_id="T01",
        agent_id="doubao",
        native_ref="x",
        submitted_at=datetime.now(timezone.utc),
    )
    with pytest.raises(QoderTransportError, match="不是本 transport"):
        transport.collect(foreign)


def test_collect_refuses_a_stream_without_a_result_event(gateway, bus_root):
    write_artifact(bus_root, f"{WS}/{RUNS_SUBDIR}/T01-nosuchresult.jsonl", stdout_of(assistant("只说话没结果")))
    transport, _ = build(gateway, [])
    with pytest.raises(QoderTransportError, match="没有") as caught:
        transport.collect(handle("T01-nosuchresult"))
    assert "assistant" in str(caught.value), "报错要列出真读到的类型，否则排查只能靠猜"


def test_control_request_in_the_stream_is_refused_and_not_retryable(gateway, bus_root):
    """单向跑法不应答控制面 ⇒ 读到了就明写，并且**归入不可重试**（重试只是再僵一次）。"""
    line = stdout_of(event("control_request", subtype="can_use_tool", request_id="r-1"), result_event())
    write_artifact(bus_root, f"{WS}/{RUNS_SUBDIR}/T01-ctrl0000.jsonl", line)
    transport, _ = build(gateway, [])
    with pytest.raises(QoderTransportError) as caught:
        transport.collect(handle("T01-ctrl0000"))
    assert caught.value.retryable is False
    assert "can_use_tool" in str(caught.value)


@pytest.mark.parametrize(
    ("subtype", "is_error", "expected"),
    [
        ("success", False, "success"),
        ("success", True, "failed"),
        ("error_during_execution", False, "failed"),
    ],
)
def test_collect_maps_only_the_recognised_verdicts(gateway, bus_root, subtype, is_error, expected):
    write_artifact(bus_root, f"{WS}/index.html", "<html>\n<p>你好世界</p>\n</html>\n")
    line = stdout_of(
        tool_use("Write", f"{WS}/index.html"),
        result_event(subtype=subtype, is_error=is_error),
    )
    transport, _ = build(gateway, [outcome(stdout=line)])
    receipt = transport.submit(task())
    assert transport.collect(receipt).self_reported_status == expected


def test_collect_refuses_an_unrecognised_subtype_instead_of_guessing(gateway, bus_root):
    line = stdout_of(
        tool_use("Write", f"{WS}/index.html"), result_event(subtype="error_max_turns", is_error=False)
    )
    transport, _ = build(gateway, [outcome(stdout=line)])
    receipt = transport.submit(task())
    with pytest.raises(QoderTransportError, match="不在已认的取值里") as caught:
        transport.collect(receipt)
    assert "error_max_turns" in str(caught.value)


def test_collect_measures_artifacts_from_disk_not_from_the_stream(gateway, bus_root):
    """FR-019 的落点：三个数读自盘上那份字节。

    内容里放了多字节中文 ⇒ `len(str)` 与 `len(bytes)` 必然不等；若实现取的是字符串长度或厂商自报值，
    这条就红。**本用例根本不提供任何自报字段** —— 流水里只有一个路径。
    """
    content = "<html>\n你好世界\n</html>\n"
    write_artifact(bus_root, f"{WS}/index.html", content)
    line = stdout_of(tool_use("Edit", f"{WS}/index.html"), result_event())
    transport, _ = build(gateway, [outcome(stdout=line)])
    result = transport.collect(transport.submit(task()))
    (ref,) = result.artifacts
    on_disk = (bus_root / WS / "index.html").read_bytes()
    assert ref.bytes == len(on_disk) == len(content.encode("utf-8"))
    assert ref.bytes != len(content), "取到字符串长度 = 没读盘"
    assert ref.lines == len(on_disk.decode("utf-8").splitlines()) == 3
    assert ref.format == "html" and ref.rel_path == f"{WS}/index.html"


def test_collect_normalizes_an_absolute_path_under_the_bus_root(gateway, bus_root):
    """厂商报绝对路径是常态：落在总线根内就换算成相对形态，落点与实测都不受影响。"""
    write_artifact(bus_root, f"{WS}/index.html", "<html>\n<p>x</p>\n</html>\n")
    line = stdout_of(tool_use("Write", f"{gateway.root.as_posix()}/{WS}/index.html"), result_event())
    transport, _ = build(gateway, [outcome(stdout=line)])
    result = transport.collect(transport.submit(task()))
    assert [item.rel_path for item in result.artifacts] == [f"{WS}/index.html"]


def test_collect_refuses_an_absolute_path_elsewhere(gateway, bus_root):
    """换算只承认"总线根之下"这一种 ⇒ 别处的绝对路径不能被"洗成相对"后放进来。"""
    line = stdout_of(tool_use("Write", "/etc/passwd"), result_event())
    transport, _ = build(gateway, [outcome(stdout=line)])
    receipt = transport.submit(task())
    with pytest.raises(QoderTransportError, match="不在本 agent 的 workspace"):
        transport.collect(receipt)


def test_collect_refuses_a_foreign_workspace(gateway, bus_root):
    """FR-013 / 契约 §4：适配器不许持有自身 workspace 外的写句柄 —— 报了也不登记。"""
    write_artifact(bus_root, "workbuddy-workspace/notes.md", "# 别人的产物\n")
    line = stdout_of(tool_use("Write", "workbuddy-workspace/notes.md"), result_event())
    transport, _ = build(gateway, [outcome(stdout=line)])
    receipt = transport.submit(task())
    before = (bus_root / "workbuddy-workspace" / "notes.md").read_bytes()
    with pytest.raises(QoderTransportError, match="FR-013"):
        transport.collect(receipt)
    assert (bus_root / "workbuddy-workspace" / "notes.md").read_bytes() == before
    assert artifact_writes(gateway) == []


def test_collect_refuses_a_parent_traversal_path(gateway, bus_root):
    """路径以 `qoder-workspace/` 开头但含 `..` ⇒ 必须拒。只比前缀会漏这一支（`startswith` 不是防线）。"""
    write_artifact(bus_root, "leaked.md", "# 越界\n")
    line = stdout_of(tool_use("Write", f"{WS}/../leaked.md"), result_event())
    transport, _ = build(gateway, [outcome(stdout=line)])
    receipt = transport.submit(task())
    with pytest.raises(QoderTransportError, match="不是合法的总线相对路径"):
        transport.collect(receipt)


def test_collect_refuses_an_off_domain_format(gateway, bus_root):
    """`ArtifactFormat` 之外的后缀不许被登记成产物（域外值会让"格式不符"这一支永远不可证伪）。

    方向要点名：这个文件是**外部进程自己写的**，我们已经拦不住它落盘；能拦的是"把它当交付物"。
    内容里放一个凭据形态是为了钉**次序**：`format_of` 必须在"读—洗—回写"之前。写晚一步的话，
    这个域外文件会被我们的网关洗一遍再写回去 —— 那等于我们亲手承认它是产物，而 `BusError` 照样抛、
    表面上"拒收了"。变异对照 M5 就是把这一行前移删掉，那条只有这里的 `artifact_writes` 断言能抓。
    """
    write_artifact(bus_root, f"{WS}/setup.ps1", f"Write-Host {LEAK_FORM}\n")
    line = stdout_of(tool_use("Write", f"{WS}/setup.ps1"), result_event())
    transport, _ = build(gateway, [outcome(stdout=line)])
    receipt = transport.submit(task())
    with pytest.raises(BusError, match="unsupported_format") as caught:
        transport.collect(receipt)
    assert "ps1" in caught.value.detail
    assert artifact_writes(gateway) == [], "拒收不许先把内容洗一遍再写回去（那等于承认它是产物）"
    assert (bus_root / WS / "setup.ps1").read_bytes() == f"Write-Host {LEAK_FORM}\n".encode("utf-8"), (
        "我们没动它：域外文件留在盘上是外部进程做的事，不是我们把秘密抄进工作区的理由"
    )


def test_uppercase_suffix_is_accepted_as_the_same_format(gateway, bus_root):
    write_artifact(bus_root, f"{WS}/INDEX.HTML", "<html>\n<p>x</p>\n</html>\n")
    line = stdout_of(tool_use("Write", f"{WS}/INDEX.HTML"), result_event())
    transport, _ = build(gateway, [outcome(stdout=line)])
    result = transport.collect(transport.submit(task()))
    assert result.artifacts[0].format == "html"


def test_declared_but_absent_file_is_refused(gateway, bus_root):
    """厂商报了路径、盘上没有 ⇒ 不登记（"以实况为准"，厂商自报不算数）。"""
    line = stdout_of(tool_use("Write", f"{WS}/never-written.md"), result_event())
    transport, _ = build(gateway, [outcome(stdout=line)])
    receipt = transport.submit(task())
    with pytest.raises(QoderTransportError, match="盘上没有"):
        transport.collect(receipt)
    assert artifact_writes(gateway) == []


def test_external_write_is_washed_and_rewritten_through_the_gateway(gateway, bus_root):
    """外部进程绕过唯一写入口 ⇒ 由"读出—洗—有差异就经网关写回"这一步补上，而不是假装它不存在。"""
    dirty = f"<html>\n<p>key {LEAK_FORM}</p>\n</html>\n"
    write_artifact(bus_root, f"{WS}/leaky.md", dirty)
    line = stdout_of(tool_use("Write", f"{WS}/leaky.md"), result_event())
    transport, _ = build(gateway, [outcome(stdout=line)])
    result = transport.collect(transport.submit(task()))
    on_disk = (bus_root / WS / "leaky.md").read_bytes()
    assert LEAK_FORM not in on_disk.decode("utf-8") and b"[REDACTED]" in on_disk
    (re_write,) = artifact_writes(gateway)
    assert re_write["path"] == f"{WS}/leaky.md"
    assert result.artifacts[0].bytes == len(on_disk), "实测值描述的是盘上的真东西，不是厂商那份脏内容"


def test_a_clean_external_file_is_not_rewritten(gateway, bus_root):
    """负对照：没脏就不回写。否则"脱敏"会变成每次回收都重排一遍工作区，抹掉厂商文件的原始字节。"""
    write_artifact(bus_root, f"{WS}/index.html", "<html>\n<p>fine</p>\n</html>\n")
    line = stdout_of(tool_use("Write", f"{WS}/index.html"), result_event())
    transport, _ = build(gateway, [outcome(stdout=line)])
    transport.collect(transport.submit(task()))
    assert artifact_writes(gateway) == []


def test_write_actor_and_capability_come_from_the_handle(gateway, bus_root):
    """写盘身份只有一个合法来源：`adapter:<agent_id>` + 任务级凭证，岗位不编（`Handle` 里没有它）。"""
    write_artifact(bus_root, f"{WS}/index.html", "<html>\n<p>x</p>\n</html>\n")
    line = stdout_of(tool_use("Write", f"{WS}/index.html"), result_event())
    transport, _ = build(gateway, [outcome(stdout=line)])
    receipt = transport.submit(task())
    transport.collect(receipt)
    assert gateway.writes, "一次都没写 = 这条判据没开火"
    for item in gateway.writes:
        assert item["actor"].actor_id == "adapter:qoder"
        assert item["actor"].role_id == NO_ROLE_DECLARED
        assert item["actor"].read_only is False
        assert item["capability"].agent_id == "qoder"
        assert item["capability"].task_id == receipt.task_id


def test_only_whitelisted_tools_register_and_a_miss_is_reported(gateway, bus_root):
    """白名单漏掉的工具 ⇒ 少报，但**明写进 `issues`**：判定达标是门禁的权，适配器不越权抛错。"""
    write_artifact(bus_root, f"{WS}/read-only.md", "# 只是读过的文件\n")
    line = stdout_of(tool_use("Read", f"{WS}/read-only.md"), result_event())
    transport, _ = build(gateway, [outcome(stdout=line)])
    result = transport.collect(transport.submit(task()))
    assert result.artifacts == []
    assert any(note.startswith("artifact_discovery_suspect") and "Read" in note for note in result.issues)
    ok_line = stdout_of(tool_use("Write", f"{WS}/read-only.md"), result_event())
    transport2, _ = build(gateway, [outcome(stdout=ok_line)])
    clean = transport2.collect(transport2.submit(task()))
    assert len(clean.artifacts) == 1 and not any(
        note.startswith("artifact_discovery_suspect") for note in clean.issues
    )


def test_a_read_followed_by_a_write_on_the_same_path_still_registers(gateway, bus_root):
    """放行判定看**名字集合**而不是首个名字：`Read` 在前、`Edit` 在后时不许漏报（先出现的不是作者）。"""
    write_artifact(bus_root, f"{WS}/index.html", "<html>\n<p>x</p>\n</html>\n")
    line = stdout_of(
        tool_use("Read", f"{WS}/index.html"),
        tool_use("Edit", f"{WS}/index.html"),
        result_event(),
    )
    transport, _ = build(gateway, [outcome(stdout=line)])
    result = transport.collect(transport.submit(task()))
    assert [item.rel_path for item in result.artifacts] == [f"{WS}/index.html"]
    assert not any(note.startswith("artifact_discovery_suspect") for note in result.issues)


def test_duplicate_tool_calls_collapse_to_one_artifact(gateway, bus_root):
    """同一文件被写两次（Edit 后补一刀）⇒ 产物一条，实测按盘上最后一次。"""
    write_artifact(bus_root, f"{WS}/index.html", "<html>\n<p>second</p>\n</html>\n")
    line = stdout_of(
        tool_use("Write", f"{WS}/index.html"), tool_use("Edit", f"{WS}/index.html"), result_event()
    )
    transport, _ = build(gateway, [outcome(stdout=line)])
    assert len(transport.collect(transport.submit(task())).artifacts) == 1


# —— 六、报文流解析 ——


def test_parse_events_skips_lines_it_cannot_read():
    """CLI 会把人类日志混进同一个流（那次 `stderr` 为空、诊断在日志文件里）⇒ 一行读不懂不该让整次运行失败。"""
    stream = "\n".join(
        [
            "[info] qodercli starting",
            json.dumps(result_event()),
            "",
            "[warn] not json at all",
            json.dumps(["an", "array"]),
            json.dumps(assistant("ok")),
        ]
    )
    events = parse_events(stream)
    assert [item.get("type") for item in events] == ["result", "assistant"], "顺序也要保住"


def test_log_excerpt_comes_from_assistant_text_blocks(gateway, bus_root):
    line = stdout_of(
        assistant("第一段"),
        tool_use("Write", f"{WS}/index.html"),
        assistant("第二段"),
        result_event(result="结果句"),
    )
    write_artifact(bus_root, f"{WS}/index.html", "<html>\n<p>x</p>\n</html>\n")
    transport, _ = build(gateway, [outcome(stdout=line)])
    result = transport.collect(transport.submit(task()))
    assert result.log_excerpt == "第一段\n第二段"


def test_log_excerpt_falls_back_to_the_result_text(gateway, bus_root):
    line = stdout_of(result_event(result="只有结果句"))
    transport, _ = build(gateway, [outcome(stdout=line)])
    assert transport.collect(transport.submit(task())).log_excerpt == "只有结果句"


def test_log_excerpt_is_tailored_not_truncated_from_the_head(gateway, bus_root):
    """末尾 400 字：结论在尾部，头部往往是过程话。"""
    long = "开头" + "x" * 500
    transport, _ = build(gateway, [outcome(stdout=stdout_of(assistant(long), result_event()))])
    result = transport.collect(transport.submit(task()))
    assert len(result.log_excerpt) == 400 and result.log_excerpt.startswith("x")


def test_excerpt_and_stream_are_washed_before_anyone_can_read_them(gateway, bus_root):
    """本厂商与 T040 的一实质差别：流水**过网关落盘** ⇒ 从盘上重读出来的节选已不含凭据形态。

    契约 §2.3 那句"消息面未闭"承接的是 workbuddy 那种"直接拿响应体填 `log_excerpt`"；
    这里同一支 `redact_bus_text` 在写入口已经跑过，所以形态凭据进不了节选。
    """
    line = stdout_of(assistant(f"用了 {LEAK_FORM} 这个键"), result_event())
    transport, _ = build(gateway, [outcome(stdout=line)])
    receipt = transport.submit(task())
    result = transport.collect(receipt)
    assert LEAK_FORM not in result.log_excerpt and "[REDACTED]" in result.log_excerpt
    # `RecordingGateway` 记的是**进网关前**那份（洗发生在网关内部），所以这一条读盘上的字节。
    persisted = (
        gateway.root / WS / RUNS_SUBDIR / f"{receipt.native_ref}.jsonl"
    ).read_text(encoding="utf-8")
    assert LEAK_FORM not in persisted and "[REDACTED]" in persisted


# —— 七、用量：不可信计量的两档 ——


def test_untrusted_usage_is_kept_as_evidence_not_as_a_ledger_entry(gateway, bus_root):
    """D-1 的挂账：tokens 全 0 / `total_cost_usd=0` ⇒ 不落 EN-13。但静默丢掉会少一笔用量（FR-059 失真），
    所以原样写进 `issues` 留证。"""
    line = stdout_of(result_event(usage={"input_tokens": 0, "output_tokens": 0}, total_cost_usd=0.0))
    transport, _ = build(gateway, [outcome(stdout=line)])
    result = transport.collect(transport.submit(task()))
    assert result.usage == []
    (note,) = [item for item in result.issues if item.startswith("usage_untrusted")]
    assert "input_tokens" in note and "research.md" in note and "trust_usage" in note


def test_trusted_usage_lands_as_two_byok_notices_without_amounts(gateway, bus_root):
    """翻了 `trust_usage` 才落账，口径 = BYOK（§4：费用由所配模型厂商结算，不占 Qoder Credits）。"""
    spec = QoderCliSpec(trust_usage=True)
    line = stdout_of(result_event(usage={"input_tokens": 120, "output_tokens": 45}))
    transport, _ = build(gateway, [outcome(stdout=line)], spec=spec)
    result = transport.collect(transport.submit(task()))
    kinds = {item.usage_kind for item in result.usage}
    assert kinds == {"INPUT", "OUTPUT"} and len(result.usage) == 2
    assert {item.quantity for item in result.usage} == {120, 45}
    assert all(item.billing_basis == "BYOK" and item.unit == "token" for item in result.usage)
    for notice in result.usage:
        assert not hasattr(notice, "cost_amount") and not hasattr(notice, "currency"), "金额由账本换算，工具不报"


def test_trusted_usage_without_a_numeric_value_is_a_transport_error(gateway, bus_root):
    """脏值转成本层的错并带上字段名与类型；让它以 pydantic 的形态冒出去，下游只能猜是哪一维脏了。"""
    line = stdout_of(result_event(usage={"input_tokens": "n/a", "output_tokens": 3}))
    transport, _ = build(gateway, [outcome(stdout=line)], spec=QoderCliSpec(trust_usage=True))
    receipt = transport.submit(task())
    with pytest.raises(QoderTransportError, match="不是数字") as caught:
        transport.collect(receipt)
    assert "input_tokens" in str(caught.value) and "str" in str(caught.value)


def test_trusted_usage_missing_a_dimension_is_refused_not_zero_filled(gateway, bus_root):
    line = stdout_of(result_event(usage={"input_tokens": 10}))
    transport, _ = build(gateway, [outcome(stdout=line)], spec=QoderCliSpec(trust_usage=True))
    receipt = transport.submit(task())
    with pytest.raises(QoderTransportError, match="output_tokens"):
        transport.collect(receipt)


def test_trusted_usage_with_a_non_mapping_usage_is_refused(gateway, bus_root):
    """配置说"信"，实况却没读数可落 ⇒ 这是矛盾，不是 0。"""
    line = stdout_of(result_event(usage=None))
    transport, _ = build(gateway, [outcome(stdout=line)], spec=QoderCliSpec(trust_usage=True))
    receipt = transport.submit(task())
    with pytest.raises(QoderTransportError, match="不是映射"):
        transport.collect(receipt)


# —— 八、cancel ——


def test_cancel_aborts_by_native_ref_and_writes_nothing(gateway):
    transport, runner = build(gateway, [outcome()])
    receipt = transport.submit(task())
    before = len(gateway.writes)
    transport.cancel(receipt)
    assert runner.aborted == [receipt.native_ref]
    assert len(gateway.writes) == before, "撤销不该顺手产生一次写"


def test_cancel_after_a_synchronous_submit_is_a_harmless_noop(gateway):
    """`abort()` 返回 False = 没有活着的子进程可撤。本笔 `submit()` 同步跑完 ⇒ 这是常态，不是缺陷。"""
    transport, runner = build(gateway, [outcome()])
    transport.cancel(transport.submit(task()))
    assert runner.aborted, "至少要把 native_ref 交给 runner，否则'撤过'这件事没被记录"


# —— 九、SubprocessRunner：生产那一面（注入假 popen，仍不起真进程）——


class FakeProcess:
    def __init__(self, *, stdout="", stderr="", returncode=0, pid=4242, timeout_once=False):
        self.stdout, self.stderr, self.returncode, self.pid = stdout, stderr, returncode, pid
        self.timeout_once = timeout_once
        self.stdin_seen = None
        self.killed = False
        self.terminated = False
        self._attempts = 0

    def communicate(self, input=None, timeout=None):
        self.stdin_seen = input
        self._attempts += 1
        if self.timeout_once and self._attempts == 1:
            raise subprocess.TimeoutExpired(cmd="qodercli", timeout=timeout)
        return self.stdout, self.stderr

    def kill(self):
        self.killed = True

    def terminate(self):
        self.terminated = True


class FakePopen:
    """替 `subprocess.Popen`：记下它收到什么，好让"argv 是列表、没有 shell=True"这类断言可证。"""

    def __init__(self, process: FakeProcess) -> None:
        self.process = process
        self.args: list = []
        self.kwargs: dict = {}

    def __call__(self, *args, **kwargs):
        self.args, self.kwargs = list(args), kwargs
        return self.process


def test_subprocess_runner_passes_an_argv_list_and_no_shell(tmp_path):
    process = FakeProcess()
    popen = FakePopen(process)
    SubprocessRunner(
        log_root=tmp_path, projects_root=tmp_path, popen=popen, cli_path=FIXTURE_CLI
    ).run(
        argv=("qodercli", "--print"),
        stdin_text="{}\n",
        run_key="k1",
        timeout_seconds=3.0,
        cwd=tmp_path,
    )
    assert popen.args == [[FIXTURE_CLI, "--print"]], f"位置参数只许一个 argv 列表，收到 {popen.args}"
    assert "shell" not in popen.kwargs, "shell=True 会把 argv 拼成一条命令串（命令注入面）"
    assert popen.kwargs["text"] is True
    assert popen.kwargs["stdin"] is subprocess.PIPE and popen.kwargs["stdout"] is subprocess.PIPE
    assert process.stdin_seen == "{}\n"
    # `cwd` 这一格是 owner 2026-10-20 裁决 1 的落点：它 MUST 被交给 `Popen`，缺省就是"继承调用方的目录"。
    assert popen.kwargs["cwd"] == str(tmp_path.resolve()), popen.kwargs


def test_the_runner_replaces_only_the_executable_and_pins_the_decode(tmp_path):
    """注入 `cli_path` 时：argv[0] 换成它、**其余一项都不动**，且两条解码口径钉死。

    另一半在 `tests/guards/test_qoder_cli_lookup.py`：那里量"不注入时它到底查不查、什么时候查"。
    `encoding` / `errors` 这两格是笔 W 顺手补的：本机区域编码 cp936 而 CLI 写 UTF-8，
    不钉就是"CLI 一起中文就当场炸在读流线程里"（同一形态在 `git` 那侧 2026-10-19 实测过，
    两份执行器的口径符合性由 `test_the_two_subprocess_executors_decode_the_same_way` 守）。
    """
    popen = FakePopen(FakeProcess(stdout="{}\n"))
    SubprocessRunner(log_root=tmp_path, projects_root=tmp_path, popen=popen, cli_path=FIXTURE_CLI).run(
        argv=("qodercli", "--print", "--output-format", "stream-json"),
        stdin_text="",
        run_key="k",
        timeout_seconds=1.0,
        cwd=tmp_path,
    )
    assert popen.args[0] == [FIXTURE_CLI, "--print", "--output-format", "stream-json"], popen.args
    assert popen.kwargs["encoding"] == "utf-8" and popen.kwargs["errors"] == "replace"


def test_a_popen_refusal_becomes_a_named_transport_error_not_an_oserror(tmp_path):
    """`Popen` 说"起不了" ⇒ 交回的是 `QoderTransportError`，而且归不可重试。

    为什么单独钉这一发：`FileNotFoundError` 是 `OSError` 的后裔，而 `realrun` 的 `SWALLOWABLE` 只收
    `RuntimeError` 与 `ValueError` ⇒ 原样抛出去就是 owner 2026-10-20 那一屏 traceback（他这次撞到的
    正是 `WinError 2`，下面那个桩就是按它造的）。这里断的是"抛出来的是什么类"，不是"消息好不好看"。
    """
    def boom(*args, **kwargs):
        raise FileNotFoundError(2, "系统找不到指定的文件")

    runner = SubprocessRunner(
        log_root=tmp_path, projects_root=tmp_path, popen=boom, cli_path=FIXTURE_CLI
    )
    with pytest.raises(QoderTransportError) as caught:
        runner.run(argv=("qodercli",), stdin_text="", run_key="k", timeout_seconds=1.0, cwd=tmp_path)
    assert isinstance(caught.value, RuntimeError), "换错基类就还留在 traceback 那一侧"
    assert caught.value.reason == "cli_launch_failed" and caught.value.retryable is False
    assert FIXTURE_CLI in str(caught.value) and "QODERCLI_PATH" in str(caught.value), str(caught.value)
    assert runner._live == {}, "起不来却把句柄记在 `_live` 里 ⇒ 后面每次 `cancel()` 都在撤一个不存在的过程"


def test_subprocess_runner_reports_the_outcome_and_the_exit_code(tmp_path):
    """`log_root` / `projects_root` 一律注入临时目录：真机上 `~/.qoder/projects/` **是存在的**，
    用默认值就是让用例去读用户主目录 —— 换一台机器、换一个用户就换一批读数（本轮自己差点踩到）。"""
    popen = FakePopen(FakeProcess(stdout="{}\n", stderr="", returncode=0))
    result = SubprocessRunner(
        log_root=tmp_path, projects_root=tmp_path, popen=popen, cli_path=FIXTURE_CLI
    ).run(argv=("qodercli",), stdin_text="", run_key="k", timeout_seconds=1.0, cwd=tmp_path)
    assert (result.exit_code, result.stdout, result.timed_out) == (0, "{}\n", False)
    assert result.log_path is None, "找不到日志就是 None，不猜路径"


def test_subprocess_runner_reads_reason_from_the_log_when_stderr_is_empty(tmp_path):
    """约束 ①② 的交接点：真那次 `stderr` 为空而诊断在日志里 ⇒ 取 reason 的顺序是 日志 → stdout → stderr。

    诱饵目录的时间戳**排在前面**（`2020-…-p9999`）：要是实现"取 glob 的第一个"就会拿到它，
    读到的 reason 变成 `success` ⇒ 这条红。变异 M17 正是这么改的，红了，所以这个诱饵不是摆设。
    """
    log_dir = tmp_path / "runs" / "2026-10-11T23-12-p4242"
    log_dir.mkdir(parents=True)
    (log_dir / "qodercli.log").write_text(
        'process.exiting exit_code=41 reason="auth_error"\n', encoding="utf-8"
    )
    (tmp_path / "runs" / "2020-01-01T00-00-p9999").mkdir()
    (tmp_path / "runs" / "2020-01-01T00-00-p9999" / "qodercli.log").write_text(
        'process.exiting exit_code=0 reason="success"\n', encoding="utf-8"
    )
    popen = FakePopen(FakeProcess(stdout='{"x":1}\n', returncode=41, pid=4242))
    runner = SubprocessRunner(
        log_root=tmp_path, projects_root=tmp_path / "none", popen=popen, cli_path=FIXTURE_CLI
    )
    result = runner.run(argv=("qodercli",), stdin_text="", run_key="k", timeout_seconds=1.0, cwd=tmp_path)
    assert result.reason == "auth_error", "stdout 里那行没有 process.exiting；日志里那行才是权威"
    # 取证路径按**结构**比（name + 父目录名），不按"正斜杠字面串"比 —— 后者把判据绑在 OS 分隔符上，
    # 在 Windows 树上恒红（owner 2026-10-19 裁决 2：形状缺陷 P2，非环境残差）。
    assert result.log_path, "两面取证的日志侧塌了"
    log_file = Path(result.log_path)
    assert log_file.name == "qodercli.log", result.log_path
    assert log_file.parent.name == "2026-10-11T23-12-p4242", result.log_path
    assert runner.log_path_for(None) is None
def test_subprocess_runner_falls_back_to_stdout_then_stderr(tmp_path):
    line = 'process.exiting exit_code=1 reason="stream_reason"\n'
    popen = FakePopen(FakeProcess(stdout=line, stderr=line, returncode=1))
    result = SubprocessRunner(
        log_root=tmp_path, projects_root=tmp_path / "none", popen=popen, cli_path=FIXTURE_CLI
    ).run(argv=("qodercli",), stdin_text="", run_key="k", timeout_seconds=1.0, cwd=tmp_path)
    assert result.reason == "stream_reason"


def test_subprocess_runner_timeout_kills_the_child_and_marks_it(tmp_path):
    popen = FakePopen(FakeProcess(returncode=-9, timeout_once=True))
    result = SubprocessRunner(
        log_root=tmp_path, projects_root=tmp_path, popen=popen, cli_path=FIXTURE_CLI, home=tmp_path
    ).run(argv=("qodercli",), stdin_text="", run_key="k", timeout_seconds=0.5, cwd=tmp_path)
    assert result.timed_out is True and popen.process.killed is True


def test_abort_terminates_only_a_live_child_and_says_so(tmp_path):
    runner = SubprocessRunner(log_root=tmp_path, popen=FakePopen(FakeProcess()))
    live = FakeProcess()
    runner._live["k1"] = live  # 直接摆一个"还活着"的句柄：run() 是阻塞的，正常路径下这里已经空了
    assert runner.abort("k1") is True and live.terminated is True
    assert runner.abort("k-nothing") is False, "没有这个键就明写 False，别抛（`cancel()` 把它当空操作）"


def transcript_root(tmp_path: Path) -> Path:
    """约束 ② 的第二面：`~/.qoder/projects/<project_id>/<session_id>.jsonl`。"""
    root = tmp_path / "projects"
    (root / "proj-a").mkdir(parents=True)
    (root / "proj-a" / "T01-fixed0000.jsonl").write_text('{"body":"完整对话"}\n', encoding="utf-8")
    (root / "proj-b").mkdir(parents=True)
    (root / "proj-b" / "T99-else00000.jsonl").write_text('{"body":"别人的会话"}\n', encoding="utf-8")
    return root


def test_transcript_is_located_by_session_id_not_by_most_recent(tmp_path):
    root = transcript_root(tmp_path)
    runner = SubprocessRunner(log_root=tmp_path / "logs", projects_root=root, popen=FakePopen(FakeProcess()))
    assert runner.transcript_path_for("T01-fixed0000") == str(root / "proj-a" / "T01-fixed0000.jsonl")
    assert runner.transcript_path_for("T99-else00000") == str(root / "proj-b" / "T99-else00000.jsonl")
    assert runner.transcript_path_for("nope") is None, "找不到就 None，不猜 project_id、不取最新那份"
    missing = SubprocessRunner(
        log_root=tmp_path / "logs", projects_root=tmp_path / "none", popen=FakePopen(FakeProcess())
    )
    assert missing.transcript_path_for("T01-fixed0000") is None


def test_run_reports_the_transcript_of_its_own_session(tmp_path):
    """`session_id` 由我们生成 ⇒ 真机跑完后能按它反查 transcript；反查的必须是**这一次**的。"""
    root = transcript_root(tmp_path)
    popen = FakePopen(FakeProcess(stdout='{"x":1}\n', returncode=0))
    result = SubprocessRunner(
        log_root=tmp_path / "logs", projects_root=root, popen=popen, cli_path=FIXTURE_CLI
    ).run(argv=("qodercli",), stdin_text="", run_key="T01-fixed0000", timeout_seconds=1.0, cwd=tmp_path)
    assert result.transcript_path and result.transcript_path.endswith("T01-fixed0000.jsonl")


# —— 九之二：2026-10-24 CN 切换（子环境注入 / 取证根 / 降级警告）——


def test_cn_child_env_receives_the_cn_pat_under_both_names(tmp_path):
    """CN 运行时：`QODERCN_PERSONAL_ACCESS_TOKEN` 的值要同时出现在两个 PAT 名下。

    为什么两个名字都给：CN CLI 二进制内嵌文档说的是 `QODER_PERSONAL_ACCESS_TOKEN`，而 SDK 的 CN
    分支（以及 `.env` 的契约名）是 `QODERCN_PERSONAL_ACCESS_TOKEN`；我们直起 CLI、不经 SDK。
    两个名字同值 ⇒ 无论它读哪个都拿到同一把 CN 凭据，也不会因父环境里还有一把旧值而歧义。
    """
    popen = FakePopen(FakeProcess())
    env = {CN_PAT_ENV: FAKE_CN_PAT, "KEEP_ME": "1"}
    runner = SubprocessRunner(popen=popen, cli_path=FIXTURE_CN_CLI, env=env, home=tmp_path)
    runner.run(argv=(CLI_BINARY,), stdin_text="", run_key="k", timeout_seconds=1.0, cwd=tmp_path)
    child = popen.kwargs["env"]
    assert child[CN_PAT_ENV] == FAKE_CN_PAT and child[INTL_PAT_ENV] == FAKE_CN_PAT, sorted(child)
    assert "KEEP_ME" not in child, "P0-1：白名单之外的父环境变量 MUST NOT 进子进程"
    assert child["HOME"] == child["USERPROFILE"], "子进程必须被关进一次性临时 HOME"
    assert str(child["HOME"]) != str(Path.home()), "临时 HOME MUST NOT 是宿主真实家目录"
    assert Path(child["HOME"]).is_dir()


def test_cn_child_env_falls_back_to_the_intl_pat_name_with_a_warning(tmp_path, caplog):
    """父环境只有旧名 ⇒ CN 子环境仍拿到值，且 warning 点名"用的是旧变量名"（不静默换）。"""
    popen = FakePopen(FakeProcess())
    env = {INTL_PAT_ENV: FAKE_PAT}
    runner = SubprocessRunner(popen=popen, cli_path=FIXTURE_CN_CLI, env=env, home=tmp_path)
    with caplog.at_level(logging.WARNING, logger="acp.adapters.qoder.transport"):
        runner.run(argv=(CLI_BINARY,), stdin_text="", run_key="k", timeout_seconds=1.0, cwd=tmp_path)
    child = popen.kwargs["env"]
    assert child[CN_PAT_ENV] == FAKE_PAT and child[INTL_PAT_ENV] == FAKE_PAT
    assert CN_PAT_ENV in caplog.text and INTL_PAT_ENV in caplog.text, caplog.text


def test_child_env_drops_the_sdk_identity_markers(tmp_path):
    """父环境里带着 SDK 身份标记时，子环境 MUST 摘掉它们（2026-10-24 冒烟实锤的那条分叉）。

    IDE/厂商 SDK 会给子进程设 `QODER_AGENT_SDK_ENTRYPOINT=sdk-py` 与一个 payload 文件路径；
    CLI 见到这组标记就判定 `mode="sdk"`，硬要 payload 文件、**完全不看我们注入的 PAT**
    （冒烟 v1/v2：`exit 41`，`auth_payload_invalid` / `auth_payload_missing`）。摘掉后同一套旗标、
    同一行 stdin ⇒ `result subtype=success`、`result="ok"`。漏摘一个 = 那条路静默退回到
    "IDE 会话的 temp 文件坏了就永远点不着"。
    """
    env = {name: "fixture-sdk-marker" for name in SDK_IDENTITY_ENV_NAMES}
    env[CN_PAT_ENV] = FAKE_CN_PAT
    env["KEEP_ME"] = "1"
    env["ACP_DB_DSN"] = "postgresql://postgres:not-a-real-password@127.0.0.1:5432/acp"
    env["ARK_API_KEY"] = "not-a-real-key"
    popen = FakePopen(FakeProcess())
    runner = SubprocessRunner(popen=popen, cli_path=FIXTURE_CN_CLI, env=env, home=tmp_path)
    runner.run(argv=(CLI_BINARY,), stdin_text="", run_key="k", timeout_seconds=1.0, cwd=tmp_path)
    child = popen.kwargs["env"]
    leaked = sorted(name for name in SDK_IDENTITY_ENV_NAMES if name in child)
    assert leaked == [], f"这些 SDK 身份标记漏进子环境 ⇒ CLI 会改走 payload 认证：{leaked}"
    assert "KEEP_ME" not in child, "P0-1：白名单之外的父环境变量 MUST NOT 进子进程"
    assert "ACP_DB_DSN" not in child, "P0-1：数据库连接串 MUST NOT 交给外部 CLI"
    assert "ARK_API_KEY" not in child, "P0-1：厂商 key MUST NOT 交给外部 CLI"
    assert child[CN_PAT_ENV] == FAKE_CN_PAT and child[INTL_PAT_ENV] == FAKE_CN_PAT


def test_cn_missing_falls_back_to_intl_with_a_warning(monkeypatch, tmp_path, caplog):
    """CN 找不到 ⇒ 回退 INTL 时必须有一条明确 warning（owner 2026-10-24 裁决 3：只走 logging）。"""
    monkeypatch.setattr(
        qoder_transport,
        "resolve_cli",
        lambda **kw: ResolvedCli("D:/x/qodercli.exe", INTL_FLAVOR, CN_FLAVOR),
    )
    popen = FakePopen(FakeProcess())
    runner = SubprocessRunner(popen=popen, env={CN_PAT_ENV: FAKE_CN_PAT}, home=tmp_path)
    with caplog.at_level(logging.WARNING, logger="acp.adapters.qoder.transport"):
        result = runner.run(argv=(CLI_BINARY,), stdin_text="", run_key="k", timeout_seconds=1.0, cwd=tmp_path)
    assert "CN" in caplog.text and "回退" in caplog.text and "D:/x/qodercli.exe" in caplog.text, caplog.text
    assert result.cli_flavor == INTL_FLAVOR and result.cli_fallback_from == CN_FLAVOR


def test_cn_wrapper_emits_a_warning(tmp_path, caplog):
    """CN 命中的是 `.cmd`（PowerShell dispatcher）⇒ 必须有一条 warning（owner 2026-10-24 裁决 2 的后半句）。

    裁决原文是"接受 `qodercn.cmd` 作为 CN 的最后手段，排原生 exe 后面，**并打 warning**"。
    排序那一半由 guards 档钉（`test_the_cn_wrapper_is_only_the_last_cn_candidate`），这一条只钉"说出来"：
    包装器多一层 PowerShell、也不是首选，静默用它等于把一条可诊断性丢掉。
    走 `cli_path` 注入是因为 runner 的解析只收 `env`/`home`，造不出"PATH 命中的 .cmd"；
    而 `_warn_on_resolution` 判的是**这次起的文件后缀**，注入位与真解析位同一条分支。
    """
    popen = FakePopen(FakeProcess())
    wrapper = tmp_path / "bin" / f"{CN_CLI_WRAPPER}.cmd"
    runner = SubprocessRunner(popen=popen, cli_path=str(wrapper), env={}, home=tmp_path)
    with caplog.at_level(logging.WARNING, logger="acp.adapters.qoder.transport"):
        result = runner.run(argv=(CLI_BINARY,), stdin_text="", run_key="k", timeout_seconds=1.0, cwd=tmp_path)
    assert "qodercn" in caplog.text and "包装器" in caplog.text, caplog.text
    assert result.cli_flavor == CN_FLAVOR


def test_explicit_intl_path_does_not_warn(tmp_path, caplog):
    """显式 `cli_path` 指向 INTL ⇒ 人工选择，不是降级 ⇒ 不许打回退 warning。"""
    popen = FakePopen(FakeProcess())
    runner = SubprocessRunner(popen=popen, cli_path=FIXTURE_INTL_CLI, env={}, home=tmp_path)
    with caplog.at_level(logging.WARNING, logger="acp.adapters.qoder.transport"):
        result = runner.run(argv=(CLI_BINARY,), stdin_text="", run_key="k", timeout_seconds=1.0, cwd=tmp_path)
    assert "回退" not in caplog.text, caplog.text
    assert result.cli_flavor == INTL_FLAVOR and result.cli_fallback_from is None


def test_cn_launch_failure_does_not_retry_intl(tmp_path):
    """CN 选好了但起不了 ⇒ `cli_launch_failed` 一次就停：不许拿 INTL 重试（那会拿另一边额度重跑）。"""
    calls: list[list[str]] = []

    def boom(args, **kwargs):
        calls.append(list(args))
        raise FileNotFoundError(2, "系统找不到指定的文件")

    runner = SubprocessRunner(
        popen=boom, cli_path=FIXTURE_CN_CLI, env={CN_PAT_ENV: FAKE_CN_PAT}, home=tmp_path
    )
    with pytest.raises(QoderTransportError) as caught:
        runner.run(argv=(CLI_BINARY,), stdin_text="", run_key="k", timeout_seconds=1.0, cwd=tmp_path)
    assert len(calls) == 1, f"起不了一次就停；实际试了 {len(calls)} 次"
    assert caught.value.reason == "cli_launch_failed" and caught.value.retryable is False
    assert runner._live == {}


def test_the_pat_value_never_appears_in_logs_or_errors(tmp_path, caplog):
    """两条面都要洗：warning 文本与启动失败的异常消息里都不许出现 token 值。"""
    secret = "fixture-cn-pat-ABCDEF0123456789"
    popen = FakePopen(FakeProcess(returncode=1))
    runner = SubprocessRunner(popen=popen, cli_path=FIXTURE_CN_CLI, env={INTL_PAT_ENV: secret}, home=tmp_path)
    with caplog.at_level(logging.WARNING, logger="acp.adapters.qoder.transport"):
        runner.run(argv=(CLI_BINARY,), stdin_text="", run_key="k", timeout_seconds=1.0, cwd=tmp_path)
    assert secret not in caplog.text

    def boom(args, **kwargs):
        raise FileNotFoundError(2, "系统找不到指定的文件")

    runner2 = SubprocessRunner(popen=boom, cli_path=FIXTURE_CN_CLI, env={INTL_PAT_ENV: secret}, home=tmp_path)
    with pytest.raises(QoderTransportError) as caught:
        runner2.run(argv=(CLI_BINARY,), stdin_text="", run_key="k", timeout_seconds=1.0, cwd=tmp_path)
    assert secret not in str(caught.value)


def test_cn_runs_read_logs_and_transcripts_from_dot_qoder_cn(tmp_path):
    """CN 选中 ⇒ 默认取证根落到 `~/.qoder-cn/{logs,projects}`，两条面都找得到。"""
    home = tmp_path / "home"
    run_dir = home / CN_CONFIG_DIR_NAME / "logs" / RUNS_DIR_NAME / "2026-10-24T00-00-p4242"
    run_dir.mkdir(parents=True)
    log_file = run_dir / LOG_FILE_NAME
    log_file.write_text('process.exiting exit_code=0 reason="success"\n', encoding="utf-8")
    project = home / CN_CONFIG_DIR_NAME / PROJECTS_DIR_NAME / "proj-a"
    project.mkdir(parents=True)
    (project / "T01-fixed0000.jsonl").write_text('{"body":"完整对话"}\n', encoding="utf-8")

    popen = FakePopen(FakeProcess(pid=4242))
    runner = SubprocessRunner(popen=popen, cli_path=FIXTURE_CN_CLI, env={}, home=home)
    result = runner.run(argv=(CLI_BINARY,), stdin_text="", run_key="T01-fixed0000", timeout_seconds=1.0, cwd=tmp_path)
    assert result.cli_flavor == CN_FLAVOR
    assert result.log_path == str(log_file), result.log_path
    assert result.transcript_path == str(project / "T01-fixed0000.jsonl"), result.transcript_path


def test_intl_runs_still_read_logs_from_dot_qoder(tmp_path):
    """INTL 选中 ⇒ 默认取证根仍是 `~/.qoder/...`（回归：本笔不许把国际版那条一起改了）。"""
    home = tmp_path / "home"
    run_dir = home / ".qoder" / "logs" / RUNS_DIR_NAME / "2026-10-24T00-00-p4242"
    run_dir.mkdir(parents=True)
    log_file = run_dir / LOG_FILE_NAME
    log_file.write_text("x\n", encoding="utf-8")
    popen = FakePopen(FakeProcess(pid=4242))
    runner = SubprocessRunner(popen=popen, cli_path=FIXTURE_INTL_CLI, env={}, home=home)
    result = runner.run(argv=(CLI_BINARY,), stdin_text="", run_key="k", timeout_seconds=1.0, cwd=tmp_path)
    assert result.cli_flavor == INTL_FLAVOR
    assert result.log_path == str(log_file), result.log_path


def test_failure_message_names_the_transcript_path_but_not_its_body(gateway):
    outcome_ = outcome(
        exit_code=41,
        reason="auth_error",
        transcript_path="/root/.qoder/projects/proj-a/T01-fixed0000.jsonl",
    )
    transport, _ = build(gateway, [outcome_])
    with pytest.raises(QoderTransportError) as caught:
        transport.submit(task())
    message = str(caught.value)
    assert "T01-fixed0000.jsonl" in message and "取证transcript=" in message
    assert "完整对话" not in message and LEAK_FORM not in message


# —— 十、注入式 spec：证明字段名没焊在逻辑里 ——

SYNTHETIC = QoderCliSpec(
    argv=("fixture-cli", "--once"),
    event_type_field="kind",
    result_type="final",
    assistant_type="reply",
    user_type="ask",
    control_request_type="ask_back",
    message_field="payload",
    content_field="parts",
    session_id_field="sid",
    parent_tool_use_field="ptid",
    text_block_type="txt",
    text_field="body",
    result_subtype_field="code",
    result_text_field="summary",
    error_flag_field="bad",
    success_subtype="ok",
    failure_subtypes=("boom",),
    tool_use_block_type="use",
    tool_name_field="tool",
    tool_input_field="args",
    writing_tools=("WriteFile",),
    artifact_path_field="target",
    usage_field="cost",
    usage_input_field="tin",
    usage_output_field="tout",
    trust_usage=True,
)


def synthetic_stream() -> str:
    return "\n".join(
        [
            json.dumps(
                {
                    "kind": "reply",
                    "payload": {"parts": [{"kind": "txt", "body": "按另一套字段说话"}]},
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "kind": "reply",
                    "payload": {"parts": [{"kind": "use", "tool": "WriteFile", "args": {"target": "qoder-workspace/index.html"}}]},
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {"kind": "final", "code": "ok", "bad": False, "summary": "另一份结果句", "cost": {"tin": 5, "tout": 6}},
                ensure_ascii=False,
            ),
        ]
    ) + "\n"


def test_synthetic_spec_changes_every_wire_field_the_transport_reads(gateway, bus_root):
    """换一份 spec、换一路报文 ⇒ 下发、回收、产物、节选、用量全跟着变。

    这是"字段名没焊死"的正照：实现里任何一处把字面量写死，这一条就会在那一处炸。
    """
    write_artifact(bus_root, f"{WS}/index.html", "<html>\n<p>x</p>\n</html>\n")
    transport, runner = build(gateway, [outcome(stdout=synthetic_stream())], spec=SYNTHETIC)
    receipt = transport.submit(task())
    assert json.loads(runner.calls[0].stdin_text)["sid"] == receipt.native_ref
    result = transport.collect(receipt)
    assert result.self_reported_status == "success"
    assert result.self_assessment == "另一份结果句"
    assert result.log_excerpt == "按另一套字段说话"
    assert [item.rel_path for item in result.artifacts] == [f"{WS}/index.html"]
    assert {item.usage_kind for item in result.usage} == {"INPUT", "OUTPUT"}
    assert {item.quantity for item in result.usage} == {5, 6}


def test_default_spec_refuses_the_synthetic_stream(gateway, bus_root):
    """反照（MUST 保持红以外的方向）：默认 spec 读同一份报文要**明确拒绝**，而不是"碰巧也能读"。

    没有这一条，上一条就证明不了任何事 —— 那可能只是解析器宽松。
    """
    transport, _ = build(gateway, [outcome(stdout=synthetic_stream())])
    receipt = transport.submit(task())
    with pytest.raises(QoderTransportError, match="没有"):
        transport.collect(receipt)


# —— 十一、与已签批文档的对齐 + 零耦合 ——


def test_package_and_import_names_match_params_ref_section_4():
    """§4 是人读的清单，这条把它和被跑的常量对齐（改一边另一边就红 = 不会漂）。

    `| Qoder |` 那一行仍是国际版 SDK 的三字面量；国内版那一行以 `| Qoder（国内版` 开头，
    单独核 CN 二进制名与 CN PAT 名 —— 两条都从同一份文件里读，不写第二份名单。
    """
    text = PARAMS_REF.read_text(encoding="utf-8")
    row = next(line for line in text.splitlines() if line.startswith("| Qoder |"))
    for literal in (PACKAGE_NAME, IMPORT_NAME, CLI_BINARY):
        assert literal in row, f"{literal} 不在 params-ref §4 的 Qoder 行里"
    assert "qodercn" not in row, "包装器名字不许进国际版那一行（它属于国内版那一行）"
    cn_row = next((line for line in text.splitlines() if line.startswith("| Qoder（国内版")), "")
    assert cn_row, "params-ref §4 少一行国内版执行器（2026-10-24 裁决 1）"
    for literal in (CN_CLI_BINARY, CN_PAT_ENV):
        assert literal in cn_row, f"{literal} 不在 params-ref §4 的国内版行里"


def test_both_expected_cli_versions_are_recorded_in_the_docs():
    """两个版本的载体是 `quickstart.md` 的查找顺序那一节，不是本文件的自述（改文档才红）。"""
    quickstart = QUICKSTART.read_text(encoding="utf-8")
    for version in (INTL_CLI_VERSION_EXPECTED, CN_CLI_VERSION_EXPECTED):
        assert version in quickstart, f"{version} 没被记到 quickstart ⇒ 换了版本没人知道"
    for binary in (INTL_CLI_BINARY, CN_CLI_BINARY):
        assert f"{binary} --version" in quickstart, f"{binary} --version 那一行没了"


def test_adapter_package_imports_no_vendor_sdk_and_no_sibling_adapter():
    """FR-012 的可跑形态：删掉 `src/acp/adapters/qoder/` 之后，别的适配器与编排层都不该跟着倒。

    判据函数复用 T038 的 `vendor_import_hits`。**扫描面只收 `transport.py`**：`__init__.py` 合法地
    从自己包里 import `Transport`（注册表入口就写 `acp.adapters.qoder:Transport`），把它算成"从具体
    适配器导入"是扫描面选错，不是违例 —— 选错的话这条判据会恒红，然后被人加豁免、就再也不看它了。
    SDK 那一条对 `__init__.py` 单独查（它才是最容易被顺手 import 的地方）。

    尤其"厂商 SDK 不得被 import"这一条：SDK 的导入链在 Windows 侧被应用控制拦过
    （`quickstart.md §1`），变成硬依赖就是让注册表里的一条取决于本机装不装得上。
    """
    implementations = [ADAPTER_DIR / vendor / "transport.py" for vendor in ("qoder", "workbuddy")]
    offenders = {}
    for path in implementations:
        hits = _protocol_fixture.vendor_import_hits(path.read_text(encoding="utf-8"))
        if hits:
            offenders[path.relative_to(REPO_ROOT).as_posix()] = hits
    assert offenders == {}, offenders
    for vendor in ("qoder", "workbuddy"):
        hits = _protocol_fixture.vendor_import_hits(
            (ADAPTER_DIR / vendor / "__init__.py").read_text(encoding="utf-8")
        )
        # 这里 MUST 用 AST 版扫描器而不是"文本里有没有 `qoder_agent_sdk` 这个词"：
        # `qoder/__init__.py` 的 docstring 正是在**解释为什么不能 import 它**，按子串查会把说明当成违例。
        assert [hit for hit in hits if "SDK" in hit] == [], (vendor, hits)
    assert _protocol_fixture.vendor_import_hits("import qoder_agent_sdk\n"), "正对照：扫描器要能抓到真违例"
    sibling = "from acp.adapters.workbuddy import transport" + chr(10)
    assert len(_protocol_fixture.vendor_import_hits(sibling)) == 1


def test_no_role_declared_has_one_carrier_after_the_ruling():
    """`NO_ROLE_DECLARED` 的字面量只许出现在 `bus/acl.py` 一处（owner 2026-10-13 待裁② 的处置）。

    这条用例本来写的是"恰好 2 份、各写一份是刻意的"，并在 docstring 里预告：**T042 落地那一轮它会红，
    届时按登记的做法挪到公共位置**而不是加第三份抄本。本轮就是那一轮 ⇒ 断言按裁定翻转，
    历史留在申报与这里的注释里，不当成"我一直就这么想"。
    依赖方向也一并钉死：适配器 → 总线（`acl`）是合法方向；**跨厂商**（`adapters/x` import `adapters/y`）
    仍不许，否则 T045 的"删目录后编排图仍可构建"会变成两台厂商代码互相依赖。
    """
    from acp.bus.acl import NO_ROLE_DECLARED as ACL_VALUE

    assert ACL_VALUE == "unassigned-by-handle"
    carriers = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in [
            REPO_ROOT / "src/acp/bus/acl.py",
            *ADAPTER_DIR.glob("*/transport.py"),
        ]
        if '"unassigned-by-handle"' in path.read_text(encoding="utf-8")
    ]
    assert carriers == ["src/acp/bus/acl.py"], carriers
    for vendor in ("workbuddy", "qoder", "doubao"):
        text = (ADAPTER_DIR / vendor / "transport.py").read_text(encoding="utf-8")
        assert "from acp.bus.acl import" in text and "NO_ROLE_DECLARED" in text, vendor
        assert f"NO_ROLE_DECLARED = \"" not in text, vendor


def test_registry_declaration_is_the_only_source_of_the_run_shape(gateway):
    """装配点上没有任何地方写死 qoder 的档位/并发 ⇒ `describe()` 换注册表就跟着换。

    做法：把注册表里的 `max_parallel` 读回来比对，而不是比一个写死的数 —— 写死的数改注册表就红，
    那才是我们要的方向（本笔不碰 `agents.yaml`，所以这里只读不写）。
    """
    transport, _ = build(gateway, [])
    declared = {item.agent_id: item.manifest for item in load_registrations()}["qoder"]
    manifest = transport.describe()
    assert manifest.max_parallel == declared.max_parallel
    assert manifest.tier == declared.tier and manifest.cost_band == declared.cost_band


# —— 十二、子进程的家与超时（owner 2026-10-20 裁决 1/2/3 的落点）——


def test_submit_hands_the_runner_exactly_the_cwd_it_holds(gateway, tmp_path):
    """transport 手里那个 `cwd` 必须**原样**进 runner：不改写、不补默认、不拿进程自己的目录顶上去。

    诱饵 = 用例进程的 cwd（今天它就是平台仓库根）。实现只要写过一次"没给就用 `Path.cwd()`"，
    这条就拿到诱饵而红 —— 那正是第四发点火 manifest 里 `cwd` 记成平台仓库根的形状。
    """
    jail = tmp_path / "AI项目台" / "confined"
    jail.mkdir()
    transport, runner = build(gateway, [outcome()], cwd=jail)
    transport.submit(task())
    assert runner.calls[0].cwd == str(jail), runner.calls[0]
    assert runner.calls[0].cwd != str(Path.cwd()), "拿调用方的目录顶替这一格 = 把平台源码交给一次外部执行"


def test_the_transport_cannot_be_built_without_naming_a_cwd(gateway):
    """没有默认值是设计而不是不便利：少交 `cwd` 必须在**构造期**就 `TypeError`。

    为什么不做成可选参数再运行时判：可选参数的默认值只能是"某一个目录"，而这个场景里任何默认都是错的
    （继承调用方 = 平台源码根）。构造期拒 ⇒ 装配面不可能悄悄漏掉这一格，也不会在跑起来之后才发现。
    """
    with pytest.raises(TypeError) as caught:
        Transport(runner=FakeRunner([]), gateway=gateway, env=make_env())
    assert "cwd" in str(caught.value), caught.value


def test_confine_refuses_the_platform_root_and_its_ancestors_but_not_a_nested_dir():
    """那道闸判的是**解析后的路径**：等于平台根或高在它上面 ⇒ 拒；嵌在它里面 ⇒ 放。

    "嵌在里面"那一发是反向对照而不是宽容：默认的存档根就是 `results/<project>`，它本来就在平台仓库里，
    把它一起拦掉等于让真跑入口无路可走（那时候人最容易做的不是修根，是给判据加豁免）。
    真正把它关在外面的是另一头：`require_archive_root()` 要求那个目录自己是仓库顶层。
    """
    for bad in (REPO_ROOT, REPO_ROOT.parent):
        with pytest.raises(QoderTransportError) as caught:
            confine_cwd(bad)
        assert caught.value.reason == "cwd_is_platform_repo", (str(bad), caught.value.reason)
        assert isinstance(caught.value, RuntimeError), "换错基类就回到 traceback 那一侧（不在 SWALLOWABLE 里）"
        assert caught.value.retryable is False
    nested = REPO_ROOT / "config"
    assert confine_cwd(nested) == nested.resolve(), "嵌在平台仓库里的合法存档根不许被这道闸一起拦掉"


def test_confine_refuses_a_directory_that_is_not_there_yet(tmp_path):
    """目录不存在 ⇒ 单独一个原因词，而且不许冒充"CLI 找不到"。

    坏 cwd 交给 `Popen` 也是 `OSError`，会落进下面那个 `except OSError` ⇒ 报成 `cli_launch_failed`。
    人照着那句去查 CLI，永远查不到真正错的那一层是"装配没把家建出来"。
    """
    with pytest.raises(QoderTransportError) as caught:
        confine_cwd(tmp_path / "not-yet")
    assert caught.value.reason == "cwd_not_a_directory", caught.value.reason
    assert "require_archive_root" in str(caught.value), "拒了却不告诉人正确的来源在哪 ⇒ 还得翻代码才知道"


def test_a_refused_cwd_never_reaches_popen(tmp_path):
    """拒必须发生在起进程之前：`FakePopen` 一次都没被叫，`_live` 里也不许留句柄。"""
    popen = FakePopen(FakeProcess())
    runner = SubprocessRunner(
        log_root=tmp_path, projects_root=tmp_path, popen=popen, cli_path=FIXTURE_CLI
    )
    with pytest.raises(QoderTransportError, match="平台源码根"):
        runner.run(argv=("qodercli",), stdin_text="", run_key="k", timeout_seconds=1.0, cwd=REPO_ROOT)
    assert popen.args == [] and popen.kwargs == {}, "已经起了进程才拒 ⇒ 这道闸是装饰"
    assert runner._live == {}


def test_the_cwd_is_judged_before_the_executable_is_resolved():
    """顺序判据：`confine_cwd` 排在 `_launch_argv` 与 `self._popen(` 之前。

    用**源码偏移量**比而不是"三个都在文件里"：把 `cwd` 那一段挪到 `Popen` 之后照样"三个都在"，
    而那时平台源码已经交给厂商跑过了。这一段文本在本文件里各自唯一，挪走任何一处这条会红得可读。
    """
    text = Path(inspect.getfile(Transport)).read_text(encoding="utf-8")
    judged, resolving, spawning = (
        text.index("confine_cwd(cwd)"),
        text.index("self._launch_argv(resolved, argv)"),
        text.index("self._popen("),
    )
    assert judged < resolving < spawning, (judged, resolving, spawning)


def test_the_run_timeout_has_one_number_on_both_sides():
    """900 只有两个载体：`QoderCliSpec.timeout_seconds` 与 `params-ref §1` 那一句。这条把它们对读。

    取数方式 = "定位那一句 → 抠出 `默认值 = N s` 里那个数"，不是全文搜 900：§1 里到处是别的载体的数
    （"2 小时"、被改口之前的 120），全文搜会把判据写成运气测试。写死的那个 900 是**锚**：
    两侧一起漂（都改成 600）也照样红，这才叫鉴别力而不是自我一致。
    那一句话刻意不写成表格 —— 理由与踩过的现场记在 `params-ref §1` 那一段自己末条里。
    """
    text = PARAMS_REF.read_text(encoding="utf-8")
    line = next((item for item in text.splitlines() if item.startswith("**非卡点执行超时的默认值 = ")), None)
    assert line is not None, "§1 里那一句话没了 ⇒ 这个数又退回'代码里有、配置里没有'"
    hit = re.search(r"默认值 = (\d+) s", line)
    assert hit is not None, f"那一句话里读不出秒数：{line}"
    anchor = 900
    assert int(hit.group(1)) == anchor, f"params-ref §1 写的是 {hit.group(1)} s，裁决定的是 {anchor} s"
    assert QoderCliSpec().timeout_seconds == float(anchor), QoderCliSpec().timeout_seconds


def timeout_log(tmp_path: Path, extra: str = "") -> Path:
    """按真机那份日志的形状造一份取证面：两行工具块 + 一行退出。`extra` 用来放诱饵。"""
    directory = tmp_path / "runs" / "2026-10-20T12-38-p23820"
    directory.mkdir(parents=True)
    target = directory / "qodercli.log"
    target.write_text(
        '[AgentLoop] Streaming: enqueued tool_use block "Read" (id=call_aaa)\n'
        '[AgentLoop] Streaming: enqueued tool_use block "Bash" (id=call_bbb)\n' + extra,
        encoding="utf-8",
    )
    return target


def test_the_timeout_message_names_the_last_tool_not_the_log_body(gateway, tmp_path):
    """裁决 3 的形状：撞超时那条消息要说"最后一个在跑的工具 + 累计几个块"，而不说"它卡住了吗"。

    `累计工具块>0` 就是"在干活时被杀"的证据，与"启动就僵住"是两种诊断 —— 第四发那条消息读不出这个差别，
    人只能重新点火一次才知道。诱饵那一行（令牌形态 + 路径 + 中文正文）MUST NOT 进消息。
    """
    log = timeout_log(tmp_path, extra='正文里的诱饵 令牌 ' + LEAK_FORM + " 路径 /home/someone/secret.txt\n")
    transport, _ = build(gateway, [outcome(exit_code=-9, reason=None, timed_out=True, log_path=str(log))])
    with pytest.raises(QoderTransportError) as caught:
        transport.submit(task())
    message = str(caught.value)
    assert "最后发起的工具=Bash" in message, message
    assert "累计工具块=2" in message, message
    assert f"撞 {QoderCliSpec().timeout_seconds} s 上限后被 kill" in message, message
    assert LEAK_FORM not in message and "诱饵" not in message and "/home/someone" not in message, message
    assert caught.value.retryable is True, "超时那一档按约束 ③ 是可重试，诊断不该改它"


def test_the_timeout_message_says_so_when_the_log_has_no_tool_line(gateway, tmp_path):
    """读不到就说读不到（`None` / 0），不猜"大概在读文件"。缺省值在这里必须是可证的空。"""
    directory = tmp_path / "runs" / "2026-10-20T12-38-p9"
    directory.mkdir(parents=True)
    (directory / "qodercli.log").write_text("nothing structural here\n", encoding="utf-8")
    log = directory / "qodercli.log"
    assert tools_in_flight(log) == (None, 0)
    assert tools_in_flight(None) == (None, 0), "没有日志路径也要给出同一个空读数，不抛"
    transport, _ = build(gateway, [outcome(exit_code=-9, reason=None, timed_out=True, log_path=str(log))])
    with pytest.raises(QoderTransportError) as caught:
        transport.submit(task())
    assert "最后发起的工具=（日志里没读到 tool_use 结构行）" in str(caught.value), str(caught.value)


def test_a_quoted_tool_name_holding_content_is_not_read_as_a_tool(gateway, tmp_path):
    """窄字符类是真闸不是样子货：引号里混进路径/正文/令牌 ⇒ 一个字都不该进消息。

    这是"最后一轮在跑什么工具"那条诊断的反向对照。把 `TOOL_LINE` 放宽成 `"([^"]*)"` 的话，
    这一发红在两处：读数不再是 `(None, 0)`，而且诱饵会跟着进那条会被日志文件接住的消息。
    """
    directory = tmp_path / "runs" / "2026-10-20T12-38-p8"
    directory.mkdir(parents=True)
    log = directory / "qodercli.log"
    log.write_text(
        '[AgentLoop] Streaming: enqueued tool_use block "Read /home/someone/secret.txt '
        + LEAK_FORM
        + '" (id=call_ccc)\n',
        encoding="utf-8",
    )
    assert tools_in_flight(log) == (None, 0), "匹配器吃进了带空格与引号内容的串 ⇒ 它读的是正文不是结构"
    transport, _ = build(gateway, [outcome(exit_code=-9, reason=None, timed_out=True, log_path=str(log))])
    with pytest.raises(QoderTransportError) as caught:
        transport.submit(task())
    assert LEAK_FORM not in str(caught.value) and "/home/someone" not in str(caught.value)


# —— 十三、总线根的拼法与产物归属（2026-10-20 第五发点火的 FR-013）——


def test_a_relative_bus_root_still_converts_an_absolute_vendor_path(tmp_path, monkeypatch):
    """总线根是相对串时，绝对厂商路径**照样要换算** —— 第五发烧的就是这一格。

    `--bus-root` 的默认值是 `results`（相对本进程 cwd），`_gateway()` 原样交进网关 ⇒ `gateway.root` 是一串
    相对写法；而厂商报回来的是绝对写法（它的家由 `confine_cwd()` 交出，那一支 resolve 过）。两侧字面串
    不同、指的是同一个目录 ⇒ 只比"原样那一份"会把合法产物读成越界，一次成功的执行被自己的安全防线
    整批拒收（`RUN FAIL … 产物 'D:/…/styles.css' 不在本 agent 的 workspace` 那条消息里的**绝对形态本身就是指纹**：
    它说明换算从没发生过）。

    **这条用例此前造不出来**：节里每一个 fixture 的总线根都是 `tmp_path` 下面的绝对串 ⇒ 判据的默认值
    正好是缺陷的反面，怎么跑都绿。这一发用 `monkeypatch.chdir` + 相对根把生产的拼法搬进测试。
    """
    absolute = tmp_path / "bus" / "demo"
    (absolute / WS).mkdir(parents=True)
    (absolute / WS / "index.html").write_text("<p>相对根也要能登记</p>", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    gateway = RecordingGateway(Path("bus") / "demo")
    line = stdout_of(tool_use("Write", (absolute / WS / "index.html").as_posix()), result_event())
    transport, _ = build(gateway, [outcome(stdout=line)], cwd=absolute / WS)
    result = transport.collect(transport.submit(task()))
    assert [item.rel_path for item in result.artifacts] == [f"{WS}/index.html"], (
        "相对根下的绝对产物没能换算 ⇒ 会被 FR-013 误拒（第五发的形状）"
    )


@pytest.mark.skipif(
    sys.platform != "win32", reason="盘符大小写只属于 Windows 的路径语义（POSIX 上 `normcase` 是恒等）"
)
def test_the_vendor_may_spell_the_drive_letter_in_another_case(bus_root):
    """换算比的是"同一个目录"，不是同一个字符串：盘符换大小写照样换算，而尾段的大小写一个都不改。

    两半各挡一种坏法：只把比较放宽、却从归一化后的串上切 ⇒ 尾段被洗成小写，`Index.HTML` 变成
    `index.html`（盘上那个文件就不见了，实测会撞 `target.is_file()` 那条拒）；两边都放宽成
    "忽略前缀取尾段" ⇒ 别的目录被当成本根下面（下一条用例钉那一格）。
    """
    (bus_root / WS).mkdir(parents=True, exist_ok=True)
    (bus_root / WS / "Index.HTML").write_text("<p>大小写是文件名的一部分</p>", encoding="utf-8")
    gateway = RecordingGateway(bus_root)
    absolute = (bus_root / WS / "Index.HTML").as_posix()
    transport, _ = build(gateway, [outcome(stdout=stdout_of(tool_use("Write", absolute[:1].lower() + absolute[1:]), result_event()))])
    result = transport.collect(transport.submit(task()))
    assert [item.rel_path for item in result.artifacts] == [f"{WS}/Index.HTML"], (
        "尾段被换算改写或盘符拼法没被认下：两侧都要按原串切"
    )


def test_a_relative_root_still_refuses_a_path_under_a_different_directory(tmp_path, monkeypatch):
    """放宽的只是**同一个目录的第二种拼法**，不是"任何绝对前缀"。

    诱饵与总线根同级、且是一个盘上真存在的文件 —— 若哪天换算改成"取最后 N 段"或"跳过前缀比对"，
    这一发红而不是被"文件确实在盘上"糊过去。拒的形态保持原样：整串交给 `_register` 在那里被 FR-013 拦下。
    """
    side = tmp_path / "side" / WS
    side.mkdir(parents=True)
    (side / "notes.md").write_text("# 别的目录\n", encoding="utf-8")
    (tmp_path / "bus" / "demo" / WS).mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    gateway = RecordingGateway(Path("bus") / "demo")
    line = stdout_of(tool_use("Write", (tmp_path / "side" / WS / "notes.md").as_posix()), result_event())
    transport, _ = build(gateway, [outcome(stdout=line)], cwd=tmp_path / "bus" / "demo" / WS)
    receipt = transport.submit(task())
    with pytest.raises(QoderTransportError, match="FR-013"):
        transport.collect(receipt)
    assert artifact_writes(gateway) == [], "越界产物被拒的同时不许经网关登记任何产物（流水那发不算，口径同节 八）"


def test_a_real_child_writes_inside_the_directory_the_driver_named(tmp_path):
    """行为档（真起本机解释器、零出网）：子进程自己写的文件必须落在被点名的那一格**里面**。

    前面几条钉的是"我们交了什么参数"与"字符串怎么换算"；这一发钉"那个参数真的生效了"——
    断言不读 stdout 里的路径（那是"我们让它打印什么它打印什么"），而是**到期望的那一层去收文件**：
    `marker.txt` 由子进程在它自己的 cwd 里写，父进程不代笔，所以它在 workspace 那一层就等于家落对了。
    最后一格钉的是第五发的形状：同一个文件出现在 workspace 的上一层，就是那次整批拒收的现场。
    """
    home = tmp_path / WS
    home.mkdir()
    runner = SubprocessRunner(
        log_root=tmp_path / "logs", projects_root=tmp_path / "projects", cli_path=sys.executable
    )
    script = (
        "import os, pathlib;"
        "pathlib.Path('marker.txt').write_text(os.getcwd(), encoding='utf-8')"
    )
    result = runner.run(
        argv=("qodercli", "-c", script), stdin_text="", run_key="pen-z", timeout_seconds=60.0, cwd=home
    )
    assert result.exit_code == 0, f"子进程没起来：reason={result.reason!r}"
    marker = home / "marker.txt"
    assert marker.is_file(), "workspace 那一层没有子进程写的文件 ⇒ 它的家不是我们交的那一格"
    assert Path(marker.read_text(encoding="utf-8")).resolve() == home.resolve()
    assert not (tmp_path / "marker.txt").exists(), "文件落在 workspace 的上一层 ⇒ 正是第五发撞到的形状"
