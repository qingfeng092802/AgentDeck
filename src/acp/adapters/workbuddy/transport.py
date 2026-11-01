"""WorkBuddy（平台内形态）的 transport（T040｜FR-012 / FR-071，R-01）。

`AgentAdapter` 四支在这里落地，厂商差异**只在本文件内消化**（契约 §4：`adapters/<vendor>/transport.py`
之外出现厂商专有字段即算越界）。三条口径先说清楚，免得这份文件被读成"接入已经验完"：

1. **本笔不真调 WorkBuddy API**（owner 2026-10-13 成本纪律）。闭环靠注入的 `httpx.Client`
   （测试走 `httpx.MockTransport`）：下发 → 执行 → 回收三段都用真的请求构造与响应解析代码，
   只有网络端是假的 ⇒ 验的是**我们的构造与解析**，不是腾讯服务端认不认。
2. **厂商侧的 Action 名、响应字段名、状态取值域是假定值**，逐条集中在 `WorkBuddyApiSpec` 里并标
   `NEEDS VERIFICATION`（D-1）。它们不在 `config/params-ref.md §4` 里 —— §4 只给了端点、
   `Version=2026-07-09` 与 CAM 鉴权形态（TC3-HMAC-SHA256 + SecretId / SecretKey）。
   ⇒ **`service` 刻意没有默认值**：§4 没这一项，给一个猜的默认值就是把猜测变成事实源；
   真凭据轮次核对后要么写进 §4、要么由装配点显式传，两者都比这里的默认值诚实。
3. **凭据只从环境来**（宪法「二」）：`WORKBUDDY_SECRET_ID` / `WORKBUDDY_SECRET_KEY` 经
   `config/loader.env_value()` 读，缺失即 `CredentialMissing`（消息里只有变量名，不可能带出值）。
   签名头里只出现 `SecretId`；错误摘要整体过一遍 `redact_bus_text`（失败路径也是最容易漏的一面）。

`describe()` 的数据来自注册表（`registry/agents.yaml` → `registry/loader`），**这里一份都不重述**：
FR-011 的四项申报一旦有第二份抄本，改一处就留下另一处，而选人次级读的正是这一支。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal, cast, get_args

import httpx
from pydantic import ValidationError

from acp.bus.acl import ADAPTER_OWNER_PREFIX, NO_ROLE_DECLARED, Actor, Capability
from acp.bus.gateway import BusGateway
from acp.config.loader import env_value, redact_bus_text
from acp.growth import store as growth_store
from acp.protocol import CapabilityManifest, Handle, Task, TaskResult
from acp.protocol.messages import ArtifactRef
from acp.protocol.refs import UsageKind, UsageNotice
from acp.registry.loader import load_registrations

AGENT_ID = "workbuddy"

#: 用量类型的允许取值：**从 `protocol/refs.UsageKind` 推导**，不在这里抄第二份字面量
#: （与 `refs.py` 顶部那段"PG 侧 MUST 引用本文件常量"的纪律同向）。
USAGE_KINDS = frozenset(get_args(UsageKind))

#: 端点与版本 = `config/params-ref.md §4` 那两行的运行期抄本（§4 是人读的清单，这里是被跑的那份；
#: 改动 MUST 两边同批动 —— 与 `bus/layout.py` ↔ `contracts/file-bus.md §1` 同一条纪律）。
ENDPOINT = "https://workbuddyenterprise.tencentcloudapi.com"
API_VERSION = "2026-07-09"
CONTENT_TYPE = "application/json; charset=utf-8"

#: 单次请求上限（秒）。与豆包适配器同口径；注入的 `httpx.Client` 可以有自己的默认值，
#: 但传输层不能把“有没有超时”交给调用方 —— 一个忘了配 client 的装配点会把执行槽挂死。
HTTP_TIMEOUT_S: float = 120.0

#: P1-13：只对"连接没建立起来"重试 2 次（合计 3 发），指数退避。
#: 不重试 4xx/5xx：那两类已经拿到服务端响应，重发可能重复下单；连接错误则明确没有成功下发。
RETRY_BACKOFF_S: tuple[float, ...] = (0.2, 0.4)


def _post_with_retry(
    http: httpx.Client,
    url: str,
    *,
    content: bytes,
    headers: Mapping[str, str],
    timeout: float,
    backoff: tuple[float, ...] = RETRY_BACKOFF_S,
) -> httpx.Response:
    """POST 一次；`httpx.ConnectError` 按 `backoff` 重试，最后一次仍失败就原样抛。

    `backoff` 是注入位：判据用 `(0, 0)` 跑，不真的睡 0.6 秒。
    """
    attempts = len(backoff) + 1
    for attempt in range(attempts):
        try:
            return http.post(url, content=content, headers=dict(headers), timeout=timeout)
        except httpx.ConnectError:
            if attempt == attempts - 1:
                raise
            time.sleep(backoff[attempt])
    raise AssertionError("unreachable")  # pragma: no cover - 循环必然 return/raise

SECRET_ID_ENV = "WORKBUDDY_SECRET_ID"
SECRET_KEY_ENV = "WORKBUDDY_SECRET_KEY"

#: 云 API 3.0 的响应外层信封与错误位（NEEDS VERIFICATION 的部分只有字段名，信封结构是框架级约定）。
ENVELOPE = "Response"
ERROR_KEY = "Error"
CODE_KEY = "Code"
MESSAGE_KEY = "Message"
REQUEST_ID_KEY = "RequestId"

#: `Handle`（契约 §4.1）只有四个字段，**不带 role_id** ⇒ 回收时无法知道这次执行挂的是哪个岗。
#: 值取自 `acl.NO_ROLE_DECLARED`（三家适配器共用的**单点载体**，owner 2026-10-13 待裁② 的处置）：
#: workspace 写权限判定用的是任务级能力凭证里的 `agent_id` 与目录声明（`acl.can_write`），
#: `role_id` 只进审计事件。真要岗位进审计，接线点是 Phase 2 写 EN-14 `event_log` 的那一步
#: （那里有完整任务上下文），不在 transport 里编。

StateVerdict = Literal["success", "failed"]


def _now_epoch() -> int:
    """默认时间戳来源。定义在类**之前**：它当的是 `__init__` 的默认值，类体定义时就要能取到。

    单独一支函数而不是 `time.time`：签名作用域里的日期必须由它推出来（`fromtimestamp`），
    传浮点秒和传整数秒会差在 `%Y-%m-%d` 之外的边界上，固定成整数更可控。
    """
    return int(datetime.now(timezone.utc).timestamp())


class WorkBuddyTransportError(RuntimeError):
    """transport 层失败。消息里只出现变量名、字段名、状态取值与 `RequestId`，MUST NOT 出现凭据值。"""


@dataclass(frozen=True)
class WorkBuddyApiSpec:
    """厂商线协议字面量的集中处（逐条 `NEEDS VERIFICATION`，见模块 docstring 第 2 条）。

    集中成一个可注入对象不是为了"将来好换厂商"，而是**用例必须能注入一份合成 spec**：
    只有换掉 spec 后行为跟着变，才能证明 transport 真在按 spec 解析，而不是把字段名焊在逻辑里。
    """

    service: str
    create_action: str = "CreateAgentTask"
    status_action: str = "DescribeAgentTask"
    cancel_action: str = "CancelAgentTask"
    ref_field: str = "TaskId"
    state_field: str = "TaskState"
    succeeded_states: frozenset[str] = frozenset({"SUCCESS"})
    failed_states: frozenset[str] = frozenset({"FAILED"})
    artifacts_field: str = "Artifacts"
    artifact_path_field: str = "Path"
    artifact_content_field: str = "Content"
    log_field: str = "LogExcerpt"
    assessment_field: str = "SelfAssessment"
    usage_field: str = "Usage"
    usage_kind_field: str = "Kind"
    usage_quantity_field: str = "Quantity"
    usage_unit_field: str = "Unit"

    def __post_init__(self) -> None:
        if not self.service:
            raise ValueError(
                "`service` 无默认值可套（`params-ref §4` 未列该项）⇒ 装配点 MUST 显式给一个"
            )


class WorkBuddyTransport:
    """平台内形态的适配器（`form = IN_PLATFORM`，取值域在 `protocol/refs.AgentForm`）。"""

    #: 数据成员在类层声明（不只是在 `__init__` 里赋值）：契约 §4 的围栏写的就是 `agent_id: str`，
    #: 而 `tests/contract/test_adapter_protocol.py::protocol_shape()` 取的是 `cls.__annotations__`
    #: ⇒ 少了这行注解，"实现与契约同形"这条判据会报"数据成员只在一侧出现"（真形而非假红）。
    agent_id: str

    def __init__(
        self,
        *,
        api: WorkBuddyApiSpec,
        http: httpx.Client,
        gateway: BusGateway,
        env: Mapping[str, str] | None = None,
        clock: Callable[[], int] = _now_epoch,
        growth_root: Path | None = None,
    ) -> None:
        self.agent_id = AGENT_ID
        self.api = api
        self.http = http
        self.gateway = gateway
        self._env = env
        #: 签名时间戳的来源。可注入是为了**用例能固定时间**：TC3 的作用域里带日期，
        #: 不固定就只能断言"形状对"而不能断言"同一个输入得同一个签名"，变异对照也就没法比签名。
        self._clock = clock
        #: 进化档案的根，`None` = 走 `params-ref §11` 登记的默认根。公开属性，理由同另两支。
        self.growth_root = growth_root

    # --- describe ---------------------------------------------------------------

    def describe(self) -> CapabilityManifest:
        """交回注册表里那份申报单（本方法不生产任何新事实，也不合并默认值）。"""
        for registration in load_registrations():
            if registration.agent_id == self.agent_id:
                return registration.manifest
        raise WorkBuddyTransportError(
            f"注册表里没有 `{self.agent_id}` 这一条 ⇒ `describe()` 拿不到申报单"
            "（唯一来源 = `src/acp/registry/agents.yaml`，档位取值域 = `params-ref §9`）"
        )

    # --- submit -----------------------------------------------------------------

    def submit(self, task: Task) -> Handle:
        """下发：三行语义（objective / inputs / expected_output）+ 交付下限的口径 + **逐条验收标准**。

        `acceptance_criteria` 随任务下发是 owner 2026-10-13 裁决 2 定的统一口径，它**覆盖**了本笔首版
        "细则不发给被考核方"的纪律（理由原文：FR-029 的"审查 MUST 逐条比对"要求审查方手里有标准，
        而一支工具挂哪个岗是任务级的决定，不在 transport 里预先筛掉）。
        键名 `AcceptanceCriteria` 是**厂商侧未验假定**（`NEEDS VERIFICATION`）：
        本文件的 `WorkBuddyApiSpec` 里读得到的字段是甲/乙档，这个**发出去的**键没有任何对照方
        ⇒ 验收点是"真凭据冒烟第一次不被拒"，归 Phase 12（owner 裁决 6）。
        """
        if task.assignee.agent_id != self.agent_id:
            raise WorkBuddyTransportError(
                f"任务卡指派给 {task.assignee.agent_id!r}，本 transport 是 {self.agent_id!r}"
                " ⇒ 拒发（拿别人的身份下发会让 workspace 归属比对失去意义）"
            )
        payload = {
            "TaskId": task.task_id,
            "ProjectKey": task.project_key,
            # 这一支的报文面是**结构化字段**，没有整段正文那一格，所以档案全文落在 `Objective` 这个
            # 自由文本字段上（与另两支"任务面之前先带档案"同一顺序）。
            # 不新造 `GrowthContext` 这类键名：本文件里连 `AcceptanceCriteria` 都还标着"厂商侧未验假定"，
            # 再叠一个没人对照过的键就是把猜测当协议。
            "Objective": growth_store.render_block(root=self.growth_root) + task.objective,
            "Inputs": list(task.inputs),
            "ExpectedOutput": task.expected_output,
            "AcceptanceCriteria": list(task.acceptance_criteria),
            "Action": self.api.create_action,
            "Version": API_VERSION,
        }
        body = self._call(self.api.create_action, payload)
        ref = _required_field(body, self.api.ref_field, self.api.create_action)
        return Handle(
            task_id=task.task_id,
            agent_id=self.agent_id,
            native_ref=str(ref),
            submitted_at=datetime.now(timezone.utc),
        )

    # --- collect ----------------------------------------------------------------

    def collect(self, handle: Handle) -> TaskResult:
        """回收：读厂商状态与产物，**产物先经网关落盘、再从盘上量**（FR-019 / 契约 §2.1）。

        厂商自报的字节数与行数**一个都不读**：`ArtifactRef.bytes` / `.lines` 是
        `gateway.write_and_measure()` 从磁盘算出来的，而 `ValidateArtifact` 拿这三个数比
        `artifact_floor` ⇒ 自报值在结构上就拿不到权重。这一条由用例钉（响应里带假的自报数，
        实测值仍以磁盘为准）。
        """
        if handle.agent_id != self.agent_id:
            raise WorkBuddyTransportError(
                f"回执的 agent_id 是 {handle.agent_id!r}，不是本 transport 的 {self.agent_id!r}"
            )
        body = self._call(self.api.status_action, self._ref_payload(handle, self.api.status_action))
        state = str(_required_field(body, self.api.state_field, self.api.status_action))
        if state in self.api.succeeded_states:
            verdict: StateVerdict = "success"
        elif state in self.api.failed_states:
            verdict = "failed"
        else:
            raise WorkBuddyTransportError(
                f"未知状态 {state!r}：允许取值 = 成功侧 {sorted(self.api.succeeded_states)} / "
                f"失败侧 {sorted(self.api.failed_states)} ⇒ 不猜，也不按『默认失败』回收"
            )
        return TaskResult(
            task_id=handle.task_id,
            self_reported_status=verdict,
            artifacts=self._collect_artifacts(handle, body),
            log_excerpt=str(body.get(self.api.log_field, "")),
            self_assessment=str(body.get(self.api.assessment_field, "")),
            usage=self._collect_usage(body),
        )

    def cancel(self, handle: Handle) -> None:
        """撤单。返回值只用来确认调用发生；撤销后还写文件就是越界（用例钉住"零次写"）。"""
        self._call(self.api.cancel_action, self._ref_payload(handle, self.api.cancel_action))

    # --- 内部 --------------------------------------------------------------------

    def _ref_payload(self, handle: Handle, action: str) -> dict:
        """`collect()` / `cancel()` 共用的请求体：厂商回执 + 两个公共参数。"""
        return {
            self.api.ref_field: handle.native_ref,
            "Action": action,
            "Version": API_VERSION,
        }

    def _collect_artifacts(self, handle: Handle, body: dict) -> list[ArtifactRef]:
        raw = body.get(self.api.artifacts_field) or []
        if not isinstance(raw, list):
            raise WorkBuddyTransportError(
                f"字段 {self.api.artifacts_field!r} 不是列表，收到 {type(raw).__name__}"
            )
        actor = Actor(
            actor_id=f"{ADAPTER_OWNER_PREFIX}{self.agent_id}",
            role_id=NO_ROLE_DECLARED,
        )
        capability = Capability(agent_id=self.agent_id, task_id=handle.task_id)
        measured: list[ArtifactRef] = []
        for item in raw:
            if not isinstance(item, dict):
                raise WorkBuddyTransportError(f"产物条目必须是映射，收到 {type(item).__name__}")
            rel_path = str(item[self.api.artifact_path_field])
            content = str(item[self.api.artifact_content_field])
            measured.append(self.gateway.write_and_measure(actor, rel_path, content, capability))
        return measured

    def _collect_usage(self, body: dict) -> list[UsageNotice]:
        raw = body.get(self.api.usage_field) or []
        if not isinstance(raw, list):
            raise WorkBuddyTransportError(
                f"字段 {self.api.usage_field!r} 不是列表，收到 {type(raw).__name__}"
            )
        notices: list[UsageNotice] = []
        now = datetime.now(timezone.utc)
        for item in raw:
            if not isinstance(item, dict):
                raise WorkBuddyTransportError(f"用量条目必须是映射，收到 {type(item).__name__}")
            kind = str(item[self.api.usage_kind_field])
            # 下面那句 `in USAGE_KINDS` 才是真判据；cast 只把这件已经成立的事告诉类型检查器。
            if kind not in USAGE_KINDS:
                raise WorkBuddyTransportError(
                    f"用量类型 {kind!r} 不在取值域内（允许 = {sorted(USAGE_KINDS)}）"
                    " ⇒ 不落账也不静默丢：丢了就是用量少计，FR-059 会失真"
                )
            # 同 doubao / qoder：`Decimal(...)` 的失败类型是 `InvalidOperation`，不是 pydantic 的。
            try:
                quantity = Decimal(str(item[self.api.usage_quantity_field]))
            except InvalidOperation as exc:
                raise WorkBuddyTransportError(
                    f"用量条目不合法：`{self.api.usage_quantity_field}` 读到的值不是数字"
                    f"（类型 = {type(item[self.api.usage_quantity_field]).__name__}）"
                ) from exc
            try:
                notices.append(
                    UsageNotice(
                        usage_kind=cast(UsageKind, kind),
                        quantity=quantity,
                        unit=str(item[self.api.usage_unit_field]),
                        # §6：WorkBuddy 按坐席订阅 + Credits 计量 ⇒ 口径固定 `SEAT_CREDIT`，
                        # 与 TOKEN / BYOK 分列（FR-061）。金额不在这一层：由 cost/ledger.py
                        # 依 §6 单价换算，让工具自报金额等于把口径交回报价方。
                        billing_basis="SEAT_CREDIT",
                        occurred_at=now,
                    )
                )
            except ValidationError as exc:
                raise WorkBuddyTransportError(f"用量条目不合法：{exc}") from exc
        return notices

    def _call(self, action: str, payload: dict) -> dict:
        """发一次带 CAM 签名的 POST，返回 `Response` 里那份映射。"""
        secret_id = env_value(SECRET_ID_ENV, self._env)
        secret_key = env_value(SECRET_KEY_ENV, self._env)
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        headers = self._authorized_headers(
            action=action,
            payload_text=text,
            timestamp=self._clock(),
            secret_id=secret_id,
            secret_key=secret_key,
        )
        response = _post_with_retry(
            self.http,
            ENDPOINT,
            content=text.encode("utf-8"),
            headers=headers,
            timeout=HTTP_TIMEOUT_S,
        )
        if response.status_code >= 400:
            raise WorkBuddyTransportError(f"{action} 回 HTTP {response.status_code}：{_error_brief(response)}")
        try:
            decoded = response.json()
        except ValueError as exc:
            raise WorkBuddyTransportError(f"{action} 的响应体不是 JSON：{type(exc).__name__}") from exc
        if not isinstance(decoded, dict) or ENVELOPE not in decoded:
            raise WorkBuddyTransportError(f"{action} 的响应缺 {ENVELOPE!r} 信封")
        body = decoded[ENVELOPE]
        if not isinstance(body, dict):
            raise WorkBuddyTransportError(f"{action} 的 {ENVELOPE!r} 不是映射")
        if ERROR_KEY in body:
            raise WorkBuddyTransportError(f"{action} 业务失败：{_error_brief(response)}")
        return body

    def _authorized_headers(
        self, *, action: str, payload_text: str, timestamp: int, secret_id: str, secret_key: str
    ) -> dict:
        """TC3-HMAC-SHA256 签名头。作用域 = 日期 / 服务 / `tc3_request`，被签的头是 `content-type` 与 `host`。

        算法本身是云 API 3.0 的公开规范（哈希链 `TC3`+SecretKey → 日期 → 服务 → `tc3_request`，
        再对 StringToSign 出签名）。**签名与腾讯服务端逐字一致这一点本笔未验**（不真调 ⇒ 没有
        第四方可对照）⇒ 记 `NEEDS VERIFICATION`，真凭据轮次的第一次 403 就是它的验收点。
        """
        date = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")
        canonical_headers = f"content-type:{CONTENT_TYPE}\nhost:{_host_of(ENDPOINT)}\n"
        signed_headers = "content-type;host"
        payload_hash = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
        canonical_request = "\n".join(
            ["POST", "/", "", canonical_headers, signed_headers, payload_hash]
        )
        credential_scope = f"{date}/{self.api.service}/tc3_request"
        string_to_sign = "\n".join(
            [
                "TC3-HMAC-SHA256",
                str(timestamp),
                credential_scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            ]
        )
        signature = _tc3_sign(secret_key, date, self.api.service, string_to_sign)
        return {
            "Content-Type": CONTENT_TYPE,
            "Authorization": (
                f"TC3-HMAC-SHA256 Credential={secret_id}/{credential_scope}, "
                f"SignedHeaders={signed_headers}, Signature={signature}"
            ),
            "X-TC-Action": action,
            "X-TC-Version": API_VERSION,
            "X-TC-Timestamp": str(timestamp),
        }


def _tc3_sign(secret_key: str, date: str, service: str, string_to_sign: str) -> str:
    """密钥推导链用 **SHA1**（三步），最后对 StringToSign 出签名用 **SHA256**（64 位十六进制）。

    这两个哈希不是可自选的：官方规范里推导链是 SHA1 而签名是 SHA256，
    写成两个 SHA1 会得到 40 位的签名 —— 本文件这轮就错过一次，被用例的长度断言抓下来。
    """
    secret_date = hmac.new(f"TC3{secret_key}".encode(), date.encode(), hashlib.sha1).digest()
    secret_service = hmac.new(secret_date, service.encode(), hashlib.sha1).digest()
    secret_terminal = hmac.new(secret_service, b"tc3_request", hashlib.sha1).digest()
    return hmac.new(secret_terminal, string_to_sign.encode(), hashlib.sha256).hexdigest()


def _required_field(body: dict, key: str, action: str) -> object:
    if key not in body:
        raise WorkBuddyTransportError(f"{action} 的响应缺字段 {key!r}（现有键 = {sorted(body)}）")
    return body[key]


def _host_of(url: str) -> str:
    return url.split("://", 1)[1].split("/", 1)[0]


#: 注册表入口名（`registry/agents.yaml` 写的是 `acp.adapters.workbuddy:Transport`）。
#: 别名而不是改名：类名 `WorkBuddyTransport` 在栈跟踪里更好读，而入口形状由 `loader.ENTRY_RE` 钉住。
Transport = WorkBuddyTransport


def _error_brief(response: httpx.Response) -> str:
    """错误摘要：只取 `Error.Code` / `Error.Message` / `RequestId`，且整体过一遍脱敏。

    厂商的 `Message` 有可能回显请求内容 ⇒ 成功路径由网关洗、失败路径没人洗就是漏的那一面
    （宪法「二」的"日志与产物两个面"里，异常最终会进日志）。只报截断后的摘要，不带整包。
    """
    try:
        decoded = response.json()
    except ValueError:
        return redact_bus_text(response.text[:200])
    body = decoded.get(ENVELOPE, decoded) if isinstance(decoded, dict) else {}
    if not isinstance(body, dict):
        return redact_bus_text(str(body)[:200])
    error = body.get(ERROR_KEY)
    if isinstance(error, dict):
        return redact_bus_text(
            f"{CODE_KEY}={error.get(CODE_KEY)!r} {MESSAGE_KEY}={error.get(MESSAGE_KEY)!r} "
            f"{REQUEST_ID_KEY}={body.get(REQUEST_ID_KEY)!r}"
        )
    return redact_bus_text(json.dumps(body, ensure_ascii=False)[:200])
