"""豆包（火山方舟 · OpenAI 兼容 HTTP API 形态）的 transport（T042｜FR-012，R-01）。

`AgentAdapter` 四支 + 三条本笔的落点口径：

① **Mock 与 T040 同源**（owner 2026-10-13 裁决）：注入 `httpx.Client`，测试挂 `httpx.MockTransport`
⇒ 零真实出网、零 API 费（成本纪律 5）。请求构造、鉴权头、响应解析、落盘与量取全是真代码。
② **不用 `openai` SDK**：`pyproject` 的运行时面里有 `openai>=1.30`，本笔**一行都不 import** ——
OpenAI 兼容的 `POST /chat/completions` 一次请求就够，套上 SDK 会把"厂商专有客户端"变成
平台的硬依赖（`contracts/agent-protocol.md §4` 把厂商差异关在 transport 里的理由就是这个）。
⇒ 已登记为待裁：那支声明目前没有任何生产代码消费，减不减由 owner 定（减依赖是要单独点的动作）。
③ **`acceptance_criteria` 无条件随任务下发**（owner 2026-10-13 裁决 2，覆盖本笔 T042 那轮的
"按岗位筛"设计）：FR-029 要求审查逐条比对，比对主体手里必须有那份标准，而一支工具在某个任务上
挂哪个岗是**任务级**决定 ⇒ transport 里预先筛掉细则，等于让以审查者身份收到任务的一方没有标准可比。
曾经为此存在过注入位 `audience`（"谁能看到细则"当岗位事实表达）；三支口径统一后它不再改变任何
输出，一个不改变行为的注入位会在 API 面上说谎 ⇒ 已删，删除由 `test_all_adapters.py` 的符合性用例钉住。

**证据分两档（照 D-1 的口径，不外推）**：
- **甲 · 真机观测**（2026-10-11 22:00/22:01 两轮，owner 授权）：`POST {base_url}/chat/completions`
  带 `model` + `max_tokens=16` ⇒ HTTP **200**，`choices[0].finish_reason=stop`，`content` 长度 2 字符，
  `usage={prompt_tokens:52, completion_tokens:30/26, total_tokens:82/78}` 且
  `usage.completion_tokens_details.reasoning_tokens=29/25`、消息体含 `reasoning_content`；
  更早一轮同路径回 **404 `ModelNotOpen`**（**不是 401** ⇒ base_url 与 `ARK_API_KEY` 已被服务端确认有效）。
- **乙 · 包内源码所读**：`finish_reason` 的取值域与 `usage` 的字段名读自本机
  `openai==3.28.0` 的类型定义（`types/chat/chat_completion.py:39-41`、`types/completion_usage.py:56-66`）
  —— 方舟"OpenAI 兼容"就是照这个形状对齐的。**注意它仍不是"方舟真返这套字段"**：
  甲档已覆盖 `finish_reason` / `usage` / `content` 三处，未覆盖 `id` 的形态与错误体字段名
  ⇒ 那两处标 `NEEDS VERIFICATION`，真凭据冒烟时一并核。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

import httpx
from pydantic import ValidationError

from acp.bus.acl import ADAPTER_OWNER_PREFIX, NO_ROLE_DECLARED, Actor, Capability
from acp.bus.gateway import BusGateway
from acp.config.loader import env_value
from acp.growth import store as growth_store
from acp.protocol import CapabilityManifest, Handle, Task, TaskResult
from acp.protocol.messages import (
    ArtifactRef,
    CUSTOMER_INPUT_CLOSE,
    CUSTOMER_INPUT_OPEN,
    UNTRUSTED_INPUT_NOTE,
)
from acp.protocol.refs import BillingBasis, UsageKind, UsageNotice
from acp.registry.loader import load_registrations

AGENT_ID = "doubao"

#: 凭据变量名 = `config/params-ref.md §4` 与 §7 的"正确替代"那一列（官方名）。
#: §7 同时把另一种写法列为**已否决命名** ⇒ 本文件不出现那个字面量，连说明都不用（门禁扫 `.py`）。
API_KEY_ENV = "ARK_API_KEY"

#: 真装配面（B 计划 第一步，owner 2026-10-19）的约定：厂商模块自己报出"要哪几个环境变量"，
#: 工厂按这个名字取，不在它那里另抄一份名单 —— 抄一份就有两份会漂（`params-ref §4` 是人读的那份）。
#: 语义 = **元组内任一有值即可**，与 `acp.adapters.qoder` 的同名属性一致；本支只有一个元素，
#: 所以"任一"在这里就等价于"必须有"。跨实现一致性由 `tests/guards/test_real_transport_wiring.py` 钉。
CREDENTIAL_ENV_NAMES: tuple[str, ...] = (API_KEY_ENV,)

#: 一次请求的上限秒数（B 计划 第三步 1，owner 原话"网络请求必须带 `timeout=120`"）。
#: 这一行是**唯一载体**：`spec_factory` 造客户端时读它，超时消息里报的也是它，不留第二个 120。
#: 与 `QoderCliSpec.timeout_seconds` 同一个数但**不是同一个东西** —— 那一支是子进程上限（`params-ref §4` 末注）。
#: 120 的出处是 owner 的明示，不是实测分位；真跑一轮后若要用实测数替换它，改这一行并同笔改 §4。
HTTP_TIMEOUT_S: float = 120.0

#: `base_url` = `params-ref §4` 那一行的值（唯一载体在参数表，这里只是把它接进代码）。
ARK_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
CHAT_COMPLETIONS_PATH = "/chat/completions"

#: 模型 ID **刻意没有默认值**，与 T040 的 `service` 同一条纪律：`§4` 明写"具体型号以方舟控制台
#: 实际 Model ID 为准"，而 D-1 记的实测是"目录里有不等于账号已开通"（那个示例 ID 曾回 404）。
#: ⇒ 把某个 ID 写成默认值就是把一个部署事实伪装成代码事实。

#: `finish_reason` 的取值域读自本机 `openai==3.28.0`（乙档）。我们**只认其中两种**：
#: `stop` = 厂商正常收尾，`content_filter` = 厂商自己拒了内容（那是它的失败声明，不是我们的判断）。
#: `length` 走"如实 success + 留一条 issue"：截断不是厂商说的失败，达不达标由 `ValidateArtifact` 判
#: （FR-019 / 契约 §2.2 ⇒ 适配器没有"达标"的判定权）。剩下两种（工具/函数调用）我们没请求过，
#: 真出现说明我们对这套接口的理解已经错了 ⇒ 抛，不按任何默认走。
SUCCESS_FINISH_REASONS = frozenset({"stop"})
VENDOR_REFUSAL_FINISH_REASONS = frozenset({"content_filter"})
TRUNCATED_FINISH_REASONS = frozenset({"length"})
KNOWN_FINISH_REASONS = SUCCESS_FINISH_REASONS | VENDOR_REFUSAL_FINISH_REASONS | TRUNCATED_FINISH_REASONS

#: 用量单位与计费口径（§6：豆包按 Token；`params-ref §6` 那句"三类口径不可合并"是它的出处）。
TOKEN_UNIT = "token"
BILLING_BASIS: BillingBasis = "TOKEN"

#: 响应体落盘的位置。与 T041 同一理由：`collect()` 只拿得到 `Handle`（契约 §4.1），
#: 进程重启后内存里什么都没有 ⇒ 可重查的必须是盘上的东西。**且必须经网关写**（脱敏在写入口那一道）。
RESPONSES_SUBDIR = ".acp-responses"
#: 审查结论作为**产物**的位置（见 `_register_artifact` 的说明：产物不是厂商报的路径）。
REVIEWS_SUBDIR = "reviews"

#: 不可重试的 HTTP 档：认证/权限/资源不存在都是**确定性**失败，重试只会更慢（T041 同一条理由）。
NON_RETRYABLE_STATUS = frozenset({401, 403, 404})
RETRYABLE_STATUS = frozenset({408, 409, 425, 429}) | frozenset(range(500, 600))

Retryable = Literal[True, False, None]


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class DoubaoTransportError(RuntimeError):
    """transport 层失败。消息里只有 HTTP 状态、厂商错误**码**、请求号与字段名，
    MUST NOT 有凭据值，也 MUST NOT 有厂商错误体的 `message` 原文（见 `_http_failure`）。"""

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        vendor_code: str | None = None,
        request_id: str | None = None,
        retryable: Retryable = None,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.vendor_code = vendor_code
        self.request_id = request_id
        self.retryable = retryable


@dataclass(frozen=True)
class DoubaoApiSpec:
    """线协议字面量的集中处。带 `甲` / `乙` 的是已读到的，带 `未验` 的要等真凭据冒烟。"""

    #: **必填**（见上面那条注释）。装配点从配置里给，用例给 `fixture-model-id`。
    model: str
    base_url: str = ARK_BASE_URL
    path: str = CHAT_COMPLETIONS_PATH
    #: 甲档真机请求里带过 `max_tokens=16` ⇒ 这个键**存在且被接受**；但 16 是那次的最小探针值，
    #: 不是平台的参数。默认 `None` = **不发这个键**（让服务端按自己的默认上限走），
    #: 要设它只由构造点给 = `acp/adapters/spec_factory.py`（MVP-2 笔 H，owner 2026-10-19 裁决 2），
    #: 值来自 `config/runtime.yaml`（`params-ref §12`）⇒ 本文件不读那支文件，也不留第二个来源。
    max_tokens: int | None = None
    #: 未验：请求号在响应头里的字段名。`X-Request-Id` 是常见形态但**甲档没读到过它**，
    #: 所以只做"有就用、没有就不报"，不当成判据。
    request_id_header: str = "x-request-id"

    def __post_init__(self) -> None:
        if not self.model or not self.model.strip():
            raise ValueError(
                "DoubaoApiSpec.model MUST 是非空模型 ID（`config/params-ref.md §4` 明写实际值以"
                "方舟控制台为准 ⇒ 代码里不留默认值，留一个就是把部署事实写成代码事实）"
            )
        if not self.base_url.startswith("https://"):
            raise ValueError(
                f"base_url MUST 是 https（凭据要过的就是这一跳；收到 {self.base_url!r}）"
            )


class DoubaoTransport:
    """API 形态的适配器（`form = API`，取值域在 `protocol/refs.AgentForm`）。"""

    #: 契约 §4 的围栏写着 `agent_id: str`，而形状比对读 `cls.__annotations__` ⇒ 这行注解必须在类层。
    agent_id: str

    def __init__(
        self,
        *,
        api: DoubaoApiSpec,
        http: httpx.Client,
        gateway: BusGateway,
        env: Mapping[str, str] | None = None,
        clock=_now_utc,
        growth_root: Path | None = None,
    ) -> None:
        self.agent_id = AGENT_ID
        self.api = api
        self.http = http
        self.gateway = gateway
        self._env = env
        self._clock = clock
        #: 进化档案的根，`None` = 走 `params-ref §11` 登记的默认根。公开属性，理由同 qoder 那一支。
        self.growth_root = growth_root

    # --- describe ---------------------------------------------------------------

    def describe(self) -> CapabilityManifest:
        """交回注册表里那份申报单（与另两支同一做法：本方法不生产任何新事实，也不发起请求）。"""
        for registration in load_registrations():
            if registration.agent_id == self.agent_id:
                return registration.manifest
        raise DoubaoTransportError(
            f"注册表里没有 `{self.agent_id}` 这一条 ⇒ `describe()` 拿不到申报单"
            "（唯一来源 = `src/acp/registry/agents.yaml`，档位取值域 = `params-ref §9`）"
        )

    # --- submit -----------------------------------------------------------------

    def submit(self, task: Task) -> Handle:
        """一次 `POST /chat/completions`，响应原样经网关落盘，返回厂商报的请求号作回执。

        发出去的东西只有这些（`_body()` 是唯一组装点）：
        - `model`：来自 spec（部署事实，不是本文件的决定）；
        - `messages`：**一条 user 报文**，内容是任务卡可见的那三面 + 交付下限；
          审查方另加逐条验收标准（FR-029），执行方**不加**（那是 T040/T041 钉过的方向）；
        - `max_tokens`：只在显式配置时才出现。
        **不送 frontmatter、不送别的岗位信息、不送内部任务图** —— 分层纪律（宪法 II）在消息面的同一落点。
        """
        if task.assignee.agent_id != self.agent_id:
            raise DoubaoTransportError(
                f"任务卡指派给 {task.assignee.agent_id!r}，本 transport 是 {self.agent_id!r}"
                " ⇒ 拒发（拿别人的身份下发会让 workspace 归属比对失去意义）"
            )
        key = env_value(API_KEY_ENV, self._env)  # 缺键 ⇒ CredentialMissing，且发生在任何请求之前
        try:
            response = self.http.post(
                f"{self.api.base_url.rstrip('/')}{self.api.path}",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=self._body(task),
            )
        except httpx.TimeoutException as exc:
            # B 计划 第三步 1（owner 2026-10-19）：超时 MUST 变成一条 transport 失败，不许把裸异常抛给编排层。
            # 消息里只有类名与那一秒数；`str(exc)` 会带 URL，不带过来（URL 里没有密钥，但也没有必要多一处可漂的字符串）。
            raise DoubaoTransportError(
                f"请求在 {HTTP_TIMEOUT_S} 秒内没有完成（{type(exc).__name__}）⇒ 不猜结果、不在这里重试"
                "（重试策略归编排层，本形态没有幂等保证）",
                retryable=True,
            ) from exc
        except httpx.TransportError as exc:
            raise DoubaoTransportError(
                f"传输层失败（{type(exc).__name__}）⇒ 没有响应体可读，不猜内容"
                "（`retryable` 留空 = 这一发我们没有判定权，不是「可以重试」）",
                retryable=None,
            ) from exc
        if response.status_code != 200:
            raise self._http_failure(response)
        payload = self._decoded(response)
        native_ref = self._native_ref(payload, response)
        self._persist(native_ref, task.task_id, response.text)
        return Handle(
            task_id=task.task_id,
            agent_id=self.agent_id,
            native_ref=native_ref,
            submitted_at=self._clock(),
        )

    def collect(self, handle: Handle) -> TaskResult:
        """回收 = 从盘上重查那份响应，再按实况登记产物（FR-019：以盘上字节为准，厂商自报不算数）。

        本形态**没有"再查一次状态"的接口**（豆包是一次性补全，不是任务型 API），
        所以回收读的是 `submit()` 落盘的那份原样响应 —— 与 T040（再去查状态）和
        T041（读 CLI 吐的流水）都不同；三种形态的差别写在 `tasks.md` 的申报里。
        """
        if handle.agent_id != self.agent_id:
            raise DoubaoTransportError(
                f"回执的 agent_id 是 {handle.agent_id!r}，不是本 transport 的 {self.agent_id!r}"
            )
        payload = self._decoded_text(self._read_response(handle.native_ref))
        message, finish = self._choice(payload)
        content = str(message.get("content") or "")
        issues = self._truncation_note(finish) + self._reasoning_note(payload)
        return TaskResult(
            task_id=handle.task_id,
            self_reported_status=self._verdict(finish),
            artifacts=[self._register_artifact(handle, content)],
            log_excerpt=content[-400:],
            self_assessment=content,
            issues=issues,
            usage=self._usage_notices(payload),
        )

    def cancel(self, handle: Handle) -> None:
        """撤单在本形态**没有对应接口**：补全请求是同步跑完的，返回时那一趟已经结束。

        这不是"cancel 没实现"，是"这一形态没有可撤的远端对象"。与 T041 同源的那条仍未闭：
        要让它真能撤，得改成流式/非阻塞下发，而那一步与编排层的并发（Phase 6）是同一件事。
        这里保持**显式空操作**而不是抛 `NotImplementedError`：协议要求四支齐，
        抛错会让编排层在"撤销已完成的调用"这条正常路径上崩，而那才是错的。
        """
        return None

    # --- 下发面 --------------------------------------------------------------------

    # 路线 C（2026-10-31）：`_refuse_unmounted_role` 已删除 —— 自由路由下 API 模型可在任何
    # 角色下被调用；"谁不能审谁"由 `guard/triple.py` 的席位实例互斥与 `assign_guards` 负责，
    # 不在适配器里按静态挂载再拦一次（那会让用户在设置页点了却发不出去）。

    def _body(self, task: Task) -> dict:
        # 档案全文排在任务面之前，与另两支同一形状（owner 2026-10-19 MVP-2 第三步 ①）。
        # 这一支的报文面同样只有 user 一个槽：甲/乙档实测发出去的就是 `messages:[{role:user}]`，
        # 在这里新加一个 `role:"system"` 是**未验假定**（没有对照方），所以"进 System Prompt"这句
        # 落在"每次下发都无条件带着这段前缀"这个可证形态上，形状差登记为待裁。
        content = growth_store.render_block(root=self.growth_root) + self._prompt(task)
        body: dict = {"model": self.api.model, "messages": [{"role": "user", "content": content}]}
        if self.api.max_tokens is not None:
            body["max_tokens"] = self.api.max_tokens
        return body

    def _prompt(self, task: Task) -> str:
        """任务卡报文面：三面 + 交付下限 + 逐条验收标准（无条件发，owner 2026-10-13 裁决 2）。

        三面直接取 `Task` 的字段，不引 `gateway.CARD_LABELS`：那支常量是**文件面**（任务卡的行）的
        载体，消息面引它是把文件模板拽进 transport（分层反例）。T041 用它是为了让 prompt 与卡面
        字面一致，本支不需要那个一致性 —— 豆包只读内容，不读排版。
        标准要**带序号**：不编号就分不清哪条对哪条（FR-029 的比对单位是"条"）。
        """
        floor = task.artifact_floor
        lines = [
            UNTRUSTED_INPUT_NOTE,
            CUSTOMER_INPUT_OPEN,
            f"objective: {task.objective}",
            f"inputs: {'; '.join(task.inputs)}",
            f"expected_output: {task.expected_output}",
            "acceptance_criteria:",
            *(f"{index}. {item}" for index, item in enumerate(task.acceptance_criteria, 1)),
        ]
        if task.constraints:
            # 岗位约束（2026-10-28）。这一段此前没有任何人渲染：`constraints` 在协议里存在，
            # 但三个适配器都只取三面 + 下限 + 判据 ⇒ 填了也到不了模型。
            # 多行约束按行缩进两格，不压成一行：压成一行会让"守则 1/2/3"变成一句长文，
            # 模型对编号列表的遵守度明显高于对长句的。
            lines.append("constraints:")
            for item in task.constraints:
                head, *rest = str(item).replace("\r\n", "\n").strip().split("\n")
                lines.append(f"- {head}")
                lines.extend(f"  {line}" for line in rest)
        lines.append(CUSTOMER_INPUT_CLOSE)
        # 下限是平台口径，不是客户说的话，留在围栏外。
        lines.append(f"artifact_floor: format={floor.format} min_bytes={floor.min_bytes} min_lines={floor.min_lines}")
        return "\n".join(lines)

    def _http_failure(self, response: httpx.Response) -> DoubaoTransportError:
        """非 200 ⇒ 带状态码、厂商错误**码**与请求号，但**不带错误体的 message 原文**。

        甲档那条实测就是理由：404 的 message 原文是 `Your account 2132870667 has not activated…`
        —— 账号 ID 在厂商的自由文本里。错误码 `ModelNotOpen` 已经足够定位，
        把整句抄进日志等于把厂商回显的标识符搬进我们的产物面（宪法「二」的卫生线在错误路径上同样生效）。
        """
        status = response.status_code
        code: str | None = None
        try:
            decoded = response.json()
        except ValueError:
            decoded = None
        if isinstance(decoded, dict):
            error = decoded.get("error")
            if isinstance(error, dict):
                code = error.get("code") if isinstance(error.get("code"), str) else None
            else:
                code = decoded.get("code") if isinstance(decoded.get("code"), str) else None
        request_id = response.headers.get(self.api.request_id_header)
        retryable: Retryable = (
            True if status in RETRYABLE_STATUS else False if status in NON_RETRYABLE_STATUS else None
        )
        return DoubaoTransportError(
            f"{self.agent_id} 请求失败：HTTP {status} vendor_code={code or '（响应体里没有 code）'}"
            f"｜request_id={request_id or '（无）'}"
            f"｜retryable={retryable}"
            f"｜不可重试档={sorted(NON_RETRYABLE_STATUS)}"
            "｜厂商错误体的 message 原文不入消息（甲档实证：那句里有账号 ID）",
            http_status=status,
            vendor_code=code,
            request_id=request_id,
            retryable=retryable,
        )

    # --- 回收面 --------------------------------------------------------------------

    def _persist(self, native_ref: str, task_id: str, text: str) -> None:
        self.gateway.write_artifact(
            self._actor(),
            self._response_path(native_ref),
            text,
            Capability(agent_id=self.agent_id, task_id=task_id),
        )

    def _read_response(self, native_ref: str) -> str:
        rel = self._response_path(native_ref)
        target = self.gateway.root / rel
        if not target.is_file():
            raise DoubaoTransportError(
                f"回收找不到响应留档 {rel!r} ⇒ 无法重查。可能是 workspace 被清过，"
                "也可能这个 `native_ref` 不属于本机"
            )
        return target.read_text(encoding="utf-8")

    def _response_path(self, native_ref: str) -> str:
        return f"{self._workspace_dir()}/{RESPONSES_SUBDIR}/{native_ref}.json"

    def _decoded(self, response: httpx.Response) -> dict:
        return self._decoded_text(response.text)

    def _decoded_text(self, text: str) -> dict:
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise DoubaoTransportError(
                f"响应体不是合法 JSON（{exc.__class__.__name__}）⇒ 不猜内容。"
                f"留档仍在 workspace 里可人工复核（长度 {len(text)} 字符）"
            ) from exc
        if not isinstance(payload, dict):
            raise DoubaoTransportError(f"响应体顶层必须是映射，收到 {type(payload).__name__}")
        return payload

    def _native_ref(self, payload: dict, response: httpx.Response) -> str:
        """可重查标识 = 厂商报的补全 ID（乙档：`ChatCompletion.id` 必填、`str`）。

        **形态未验**（甲档没记过它的样子）⇒ 只要求非空字符串，不校验前缀；
        真冒烟读到别的形态时改这里，不改协议层。
        """
        candidate = payload.get("id")
        if isinstance(candidate, str) and candidate:
            return candidate
        header = response.headers.get(self.api.request_id_header)
        if header:
            return header
        raise DoubaoTransportError(
            f"响应里既没有非空 `id`（收到 {candidate!r}）也没有请求号头"
            f"（现有键 = {sorted(payload)}）"
            " ⇒ `Handle.native_ref` 造不出来，回收会无从重查（契约 §4.1）"
        )

    def _choice(self, payload: dict) -> tuple[dict, str]:
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise DoubaoTransportError(
                f"响应里的 `choices` 不是非空列表（收到 {type(choices).__name__}）"
                " ⇒ 无内容可回收。顶层键 = " + str(sorted(payload))
            )
        if len(choices) > 1:
            raise DoubaoTransportError(
                f"`choices` 有 {len(choices)} 项，而我们没请求过 `n>1`"
                " ⇒ 不自选一项当结论（那是把选择权从平台手里拿走）"
            )
        choice = choices[0]
        if not isinstance(choice, dict):
            raise DoubaoTransportError(f"`choices[0]` 不是映射，收到 {type(choice).__name__}")
        message = choice.get("message")
        if not isinstance(message, dict):
            raise DoubaoTransportError(
                f"`choices[0].message` 不是映射，收到 {type(message).__name__}"
                "（流式增量用的是 `delta`，本形态不接流式 ⇒ 这里不该为空）"
            )
        finish = choice.get("finish_reason")
        if not isinstance(finish, str):
            raise DoubaoTransportError(
                f"`finish_reason` 不是字符串（收到 {type(finish).__name__}）"
                f" ⇒ 无法判定自报状态。已认的取值 = {sorted(KNOWN_FINISH_REASONS)}"
            )
        return message, finish

    def _verdict(self, finish: str) -> Literal["success", "failed"]:
        """自报状态只翻**厂商自己报的那两个信号**，其余一律抛。

        `stop` ⇒ success；`content_filter` ⇒ failed（厂商拒绝产出，是它的声明）；
        `length` ⇒ success + 一条 issue（截断由产物下限去判，不是我们判）；
        别的取值 ⇒ 抛 —— 乙档那个五值域里剩下的两种是工具/函数调用，我们从未请求，
        出现就说明我们对这套接口的理解已经不成立，"按默认失败回收"会把这件事藏起来。
        """
        if finish in SUCCESS_FINISH_REASONS or finish in TRUNCATED_FINISH_REASONS:
            return "success"
        if finish in VENDOR_REFUSAL_FINISH_REASONS:
            return "failed"
        raise DoubaoTransportError(
            f"`finish_reason={finish!r}` 不在本笔认的取值里（已认 = {sorted(KNOWN_FINISH_REASONS)}）"
            " ⇒ 不猜，也不按『默认失败』回收"
        )

    def _truncation_note(self, finish: str) -> list[str]:
        if finish not in TRUNCATED_FINISH_REASONS:
            return []
        return [
            "truncated: 厂商在 `max_tokens` 上限处收尾（`finish_reason=length`）⇒ 自报仍按 success 交回，"
            "达不达标由 `ValidateArtifact` 与任务卡的 `artifact_floor` 判（FR-019 / 契约 §2.2）"
        ]

    def _reasoning_note(self, payload: dict) -> list[str]:
        """推理型模型的思考 token **留证**。

        D-1 的纪律是"`reasoning_tokens` MUST 单列，否则系统性低估 2.1-pro 的单价"。
        但 `protocol/refs.UsageKind` 只有 `INPUT/OUTPUT/CALL/SEAT_DAY` 四档 —— **没有能放它的地方**，
        而我 MUST NOT 自加枚举值（那是改契约）。所以本笔的处理是：
        `OUTPUT` 那笔账仍按 `completion_tokens` 全额交（思考 token 本就计在输出价里，不丢），
        同时把这个数作为一条 `issue` 留在结果里，等 owner 定它落哪儿（EN-13 加维度 / 账本读 details）。
        """
        usage = self._usage_of(payload)
        if not isinstance(usage, dict):
            return []
        details = usage.get("completion_tokens_details")
        if not isinstance(details, dict):
            return []
        reasoning = details.get("reasoning_tokens")
        if not isinstance(reasoning, int):
            return []
        return [
            f"reasoning_tokens={reasoning}：思考部分占输出的比例见 `research.md` D-1（方舟那两轮 "
            "completion 里绝大部分是 reasoning）。`UsageKind` 无对应档 ⇒ 未单独落 EN-13，"
            "已登记为待裁（不自加枚举值，那是改契约）"
        ]

    def _usage_notices(self, payload: dict) -> list[UsageNotice]:
        """`usage` 缺任何一个维度 ⇒ 抛，不按 0 落账。

        FR-059 要的是"每次调用都记账"；把缺读数记成 0 就是在造一笔假账（Qoder 那轮的
        `usage` 全 0 是同一条警戒的另一面：那边的结论是**不可信**，所以那边不落账、这边不落错账）。
        """
        usage = self._usage_of(payload)
        if not isinstance(usage, dict):
            raise DoubaoTransportError(
                f"响应里没有 `usage` 映射（顶层键 = {sorted(payload)}）"
                " ⇒ 这一笔调用无法记账，不落 0（FR-059）"
            )
        now = self._clock()
        notices: list[UsageNotice] = []
        dimensions: tuple[tuple[UsageKind, str], ...] = (
            ("INPUT", "prompt_tokens"),
            ("OUTPUT", "completion_tokens"),
        )
        for kind, field in dimensions:
            if field not in usage:
                raise DoubaoTransportError(
                    f"`usage` 缺 {field!r}（现有键 = {sorted(usage)}）⇒ 该维度不落账，但也不静默"
                )
            # 先把"是不是数字"这一步单独做掉：`Decimal("banana")` 抛的是
            # `decimal.InvalidOperation`，不是 pydantic 的 `ValidationError` —— 混在下面那个
            # `except` 里会让它直接冒出去，下游读到一条看不出是哪个维度的 `ConversionSyntax`。
            try:
                quantity = Decimal(str(usage[field]))
            except InvalidOperation as exc:
                raise DoubaoTransportError(
                    f"`usage.{field}` 读到的值不是数字（类型 = {type(usage[field]).__name__}）"
                    "⇒ 不落 EN-13，也不按 0 处理"
                ) from exc
            try:
                notices.append(
                    UsageNotice(
                        usage_kind=kind,
                        quantity=quantity,
                        unit=TOKEN_UNIT,
                        billing_basis=BILLING_BASIS,
                        occurred_at=now,
                    )
                )
            except ValidationError as exc:
                raise DoubaoTransportError(
                    f"`usage.{field}` 读到的值不是数字（类型 = {type(usage[field]).__name__}）"
                    f"⇒ 不落 EN-13，也不按 0 处理（{exc.errors()[0]['type']}）"
                ) from exc
        return notices

    def _usage_of(self, payload: dict) -> object:
        return payload.get("usage")

    def _register_artifact(self, handle: Handle, content: str) -> ArtifactRef:
        """产物 = **我们把响应正文经网关落盘后实测的那份文件**。

        与另两支的实质差别，点名不藏：T040 是"厂商报路径 + 内容我们写"、T041 是"外部进程自己写我们验"，
        而豆包是纯补全接口 —— 响应里根本没有"文件"这个概念。所以这里的产物**不是厂商报的产物清单**，
        是我们把它的结论落成 `reviews/` 下的一份 md，再让网关从盘上量三个数（FR-019 / 契约 §2.1
        "实测值由网关读工作区得出"）。⇒ 归属天然在本 agent 自己的 workspace 内，
        格式由我们定的文件名决定（`md` 在 `ArtifactFormat` 域内）。
        """
        rel_path = f"{self._workspace_dir()}/{REVIEWS_SUBDIR}/{handle.task_id}-{handle.native_ref}.md"
        return self.gateway.write_and_measure(self._actor(), rel_path, content, self._capability(handle))

    # --- 公共 --------------------------------------------------------------------

    def _workspace_dir(self) -> str:
        return f"{self.agent_id}-workspace"

    def _actor(self) -> Actor:
        return Actor(actor_id=f"{ADAPTER_OWNER_PREFIX}{self.agent_id}", role_id=NO_ROLE_DECLARED)

    def _capability(self, handle: Handle) -> Capability:
        return Capability(agent_id=self.agent_id, task_id=handle.task_id)


#: 注册表入口名（`registry/agents.yaml` 写的是 `acp.adapters.doubao:Transport`）。
#: 别名而不是改名：栈跟踪里 `DoubaoTransport` 更好读，而入口形状由 `loader.ENTRY_RE` 钉住。
Transport = DoubaoTransport
