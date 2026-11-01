"""豆包的**纯 API 执行**适配器（路线 B，2026-10-31）：把一次补全落成一个代码文件。

与同包的 `transport.py` 分工：那一支是审查席的补全（产物 `reviews/*.md`），本支是执行席的补全
（产物是**被点名的那个文件**）。两者共用 `settings_run._chat` 的传输层（思考字段、退避重试、
预算翻倍、错误消息），但不共用产物形状。

为什么和 doubao 同包：注册表条数被 `params-ref §3` 的"并发上限 = 首批接入 Agent 数"关系式钉着，
新增一条就等于改 owner 定的平台并发值；而 `tests/contract/test_all_adapters.py` 又禁止适配器之间
互相 import。所以"同一支 API agent 的第二种形态"住在它自己的包里，由 `spec_factory` 在
**有 `model_routing` 时**切换过来（包级约定 `spec_from_route` / `RoutedTransport`）。

写盘前必须过 `acp.content_gate.artifact_completeness()`（P0-1）：截断 ⇒ 不写盘、交回零产物 ⇒
门禁按 `ARTIFACT_INVALID` 判。**密钥不在路由里**：路由会随读面回给浏览器。
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from acp import settings_run
from acp.bus.acl import ADAPTER_OWNER_PREFIX, NO_ROLE_DECLARED, Actor, Capability
from acp.bus.gateway import BusGateway
from acp.config.loader import env_value
from acp.content_gate import artifact_completeness
from acp.routing import RoutingSpecError, executor_target, thinking_of
from acp.protocol import CapabilityManifest, Handle, Task, TaskResult
from acp.protocol.refs import UsageKind, UsageNotice
from acp.registry.loader import load_registrations

AGENT_ID = "doubao"

#: 三家的官方凭据变量名（`params-ref §4`）。工厂只问"有没有一把非空"，本支在发请求时按厂商取那一把。
CREDENTIAL_ENV_NAMES = ("DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY", "ARK_API_KEY")

#: 厂商 id → 凭据变量名。**不抄端点**：端点从 `settings_run.BASE_URLS` 取，那份表已有判据钉住。
VENDOR_KEY_ENV: Mapping[str, str] = {
    "deepseek": "DEEPSEEK_API_KEY",
    "qwen": "DASHSCOPE_API_KEY",
    "doubao": "ARK_API_KEY",
}

#: 一次写代码请求的墙钟上限（与四席直跑的 execute 同一档）。
HTTP_TIMEOUT_S = 600.0

#: 预算缺省：卡的 `token_budget` 没给时用这一档（与四席直跑的 execute 同值）。
DEFAULT_MAX_TOKENS = 8192

RUNS_SUBDIR = ".acp-runs"

_NAME_RE = re.compile(r"[A-Za-z0-9_-]+\.(?:html|css|js|md|json|py)", re.IGNORECASE)

SYSTEM = (
    "你是执行工程师。只输出目标文件的完整正文，不要 Markdown 代码围栏，不要解释。"
    "内容必须完整、可运行、可直接保存；不要省略任何代码。"
)


class ApiExecError(RuntimeError):
    """这一支的失败。消息里只有任务号、文件名与变量**名**，MUST NOT 出现任何凭据值。"""


@dataclass(frozen=True)
class ApiExecSpec:
    """路由化后的执行目标：哪家、哪个模型、打哪个端点、凭据从哪来。

    `api_key` **只可能来自请求级内存仓**（路线 C）：不从磁盘读、不落库、repr 里不出现。
    `key_env` 是内置厂商的回落路径（环境变量名）。
    """

    vendor: str
    vendor_id: str
    model: str
    base_url: str
    key_env: str = ""
    api_key: str = field(default="", repr=False)
    thinking: str = "medium"


def spec_from_route(
    routing: Mapping[str, Any] | None, *, env: Mapping[str, str] | None = None
) -> ApiExecSpec:
    """把 `model_routing.executor` 翻成 spec。缺厂商/模型 / 厂商不认识 / 缺那把 key ⇒ 拒（不猜默认值）。"""
    target = executor_target(routing)
    if target is None:
        raise RoutingSpecError(
            "纯 API 执行器（本包的第二形态）必须由 model_routing.executor 点名 vendor 与 model"
            "（缺一即拒：不替调用方猜一个厂商，那是把部署事实写成默认值）"
        )
    vendor, model = target
    seat = routing.get("executor") if isinstance(routing, Mapping) else None
    base_url = str(seat.get("base_url") or "").strip() if isinstance(seat, Mapping) else ""
    api_key = str(seat.get("api_key") or "") if isinstance(seat, Mapping) else ""
    vendor_id = settings_run.VENDOR_IDS.get(vendor)
    if not base_url:
        if vendor_id is None or vendor_id not in settings_run.BASE_URLS:
            raise RoutingSpecError(
                f"model_routing.executor.vendor={vendor!r} 不是内置厂商且没给 base_url"
                " ⇒ 拒绝装配，不猜端点"
            )
        base_url = settings_run.BASE_URLS[vendor_id]
    key_env = VENDOR_KEY_ENV.get(vendor_id or "", "")
    if not api_key:
        if not key_env:
            raise RoutingSpecError(
                "自定义执行厂商没有随请求携带 api_key，也没有可回落的环境变量名 ⇒ 拒绝装配"
            )
        env_value(key_env, env)  # 缺了就在装配期拒，而不是等第一份报文才失败
    return ApiExecSpec(
        vendor=vendor,
        vendor_id=vendor_id or "custom",
        model=model,
        base_url=base_url,
        key_env=key_env,
        api_key=api_key,
        thinking=thinking_of(routing),
    )


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def strip_fence(text: str) -> str:
    """剥掉一整段 Markdown 围栏（```lang … ```）。与四席直跑同一形状，不认半截围栏。"""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def target_name(task: Task) -> str:
    """目标文件名：先在任务卡的可见面里找与下限格式相同的文件名，找不到才退 `<卡号>.<格式>`。

    为什么不是固定 `<卡号>.<格式>`：PRD 投影出来的卡，判据写的常常是"产物 index.html 存在且非空" ——
    文件名对不上，审查与验收会判它没交付。反过来，随便认一个名字也不安全：只认**后缀等于卡的格式**
    的那些，避免把判据里提到的 `style.css` 当成目标。
    """
    fmt = str(task.artifact_floor.format).lower()
    haystack = " ".join([task.objective, *task.inputs, task.expected_output, *task.acceptance_criteria])
    for candidate in _NAME_RE.findall(haystack):
        if candidate.rsplit(".", 1)[1].lower() == fmt:
            return candidate
    return f"{task.task_id}.{fmt}"


class ApiExecTransport:
    """纯 API 执行器：`submit()` 只发一次补全并把原文留档，`collect()` 剥围栏 + 判完整 + 落盘。"""

    agent_id: str

    def __init__(
        self,
        *,
        api: ApiExecSpec,
        http: object | None = None,
        gateway: BusGateway,
        env: Mapping[str, str] | None = None,
        clock=_now_utc,
        agent_id: str = AGENT_ID,
    ) -> None:
        # 路线 B 复用 `doubao` 这一条注册（见 `orchestrate/routing.py` 的说明）：
        # 身份跟着调用方给的 agent_id，厂商/模型只从 spec 来。
        self.agent_id = agent_id
        self.api = api
        #: 本支不走原生 httpx（传输层复用 `settings_run._chat`），这一格只为与工厂的构造约定同形。
        self.http = http
        self.gateway = gateway
        self._env = env
        self._clock = clock

    # --- describe -------------------------------------------------------------

    def describe(self) -> CapabilityManifest:
        for registration in load_registrations():
            if registration.agent_id == self.agent_id:
                return registration.manifest
        raise ApiExecError(
            f"注册表里没有 `{self.agent_id}` 这一条 ⇒ `describe()` 拿不到申报单"
            "（唯一来源 = `src/acp/registry/agents.yaml`）"
        )

    # --- submit ---------------------------------------------------------------

    def submit(self, task: Task) -> Handle:
        if task.assignee.agent_id != self.agent_id:
            raise ApiExecError(
                f"任务卡指派给 {task.assignee.agent_id!r}，本 transport 是 {self.agent_id!r} ⇒ 拒发"
            )
        key = self.api.api_key or env_value(self.api.key_env, self._env)  # 缺失 ⇒ CredentialMissing
        seat = settings_run.SeatModel(
            vendor=self.api.vendor,
            model=self.api.model,
            api_key=key,
            base_url=self.api.base_url,
            thinking=self.api.thinking,
        )
        name = target_name(task)
        text, usage = settings_run._chat(
            seat,
            system=SYSTEM,
            user=self._prompt(task, name),
            max_tokens=int(task.token_budget) if task.token_budget else DEFAULT_MAX_TOKENS,
            timeout=HTTP_TIMEOUT_S,
        )
        record = {
            "task_id": task.task_id,
            "name": name,
            "format": str(task.artifact_floor.format).lower(),
            "text": text,
            "finish_reason": str(usage.get("finish_reason") or ""),
            "usage": {key_: str(value) for key_, value in usage.items() if isinstance(value, (int, float, str))},
        }
        native_ref = f"{task.task_id}-{self._clock().strftime('%Y%m%d%H%M%S%f')}"
        self.gateway.write_artifact(
            self._actor(),
            self._record_path(native_ref),
            json.dumps(record, ensure_ascii=False),
            Capability(agent_id=self.agent_id, task_id=task.task_id),
        )
        return Handle(
            task_id=task.task_id,
            agent_id=self.agent_id,
            native_ref=native_ref,
            submitted_at=self._clock(),
        )

    # --- collect --------------------------------------------------------------

    def collect(self, handle: Handle) -> TaskResult:
        if handle.agent_id != self.agent_id:
            raise ApiExecError(
                f"回执的 agent_id 是 {handle.agent_id!r}，不是本 transport 的 {self.agent_id!r}"
            )
        record = self._read_record(handle.native_ref)
        name = str(record.get("name") or f"{handle.task_id}.{record.get('format', 'html')}")
        fmt = str(record.get("format") or Path(name).suffix.lstrip(".")).lower()
        body = strip_fence(str(record.get("text") or ""))
        finish = str(record.get("finish_reason") or "")
        # 截断的两条判据：结构性不完整 **或** 厂商自己说被 `max_tokens` 截了。
        # 第二条是兜底：括号配平的半份文件（比如在一个完整块之后停住）只看结构是看不出来的。
        incomplete = artifact_completeness(fmt, body)
        truncation = incomplete or ("finish_reason=length" if finish == "length" else "") or ""
        usage = self._usage_notices(record)
        if truncation:
            reason = incomplete or "厂商在 max_tokens 上限处收尾（finish_reason=length）"
            return TaskResult(
                task_id=handle.task_id,
                self_reported_status="failed",
                artifacts=[],
                log_excerpt=body[-400:],
                self_assessment=f"{name} 未落盘：{reason}",
                issues=[f"truncated: {name} {reason} ⇒ 不写盘，交门禁判 ARTIFACT_INVALID"],
                usage=usage,
            )
        if not body.strip():
            return TaskResult(
                task_id=handle.task_id,
                self_reported_status="failed",
                artifacts=[],
                log_excerpt="",
                self_assessment=f"{name} 正文为空",
                issues=["empty: 模型返回空正文 ⇒ 不写盘"],
                usage=usage,
            )
        ref = self.gateway.write_and_measure(
            self._actor(),
            f"{self._workspace_dir()}/{name}",
            body,
            Capability(agent_id=self.agent_id, task_id=handle.task_id),
        )
        return TaskResult(
            task_id=handle.task_id,
            self_reported_status="success",
            artifacts=[ref],
            log_excerpt=body[-400:],
            self_assessment=f"{name} 由 {self.api.model} 生成（{len(body.encode('utf-8'))}B）",
            issues=[],
            usage=usage,
        )

    def cancel(self, handle: Handle) -> None:
        """一次性补全没有可撤销的远端作业；回执本身留档，回收时照常读。"""
        if handle.agent_id != self.agent_id:
            raise ApiExecError(f"回执的 agent_id 是 {handle.agent_id!r}，不是 {self.agent_id!r}")

    # --- helpers --------------------------------------------------------------

    def _prompt(self, task: Task, name: str) -> str:
        lines = [
            f"客户需求（任务目标）：{task.objective}",
            f"目标文件：{name}",
            f"格式/下限：{task.artifact_floor.format}，至少 {task.artifact_floor.min_bytes} 字节、"
            f"{task.artifact_floor.min_lines} 行",
        ]
        if task.inputs:
            lines.append("参考输入：" + "；".join(task.inputs))
        if task.expected_output:
            lines.append(f"期望产出：{task.expected_output}")
        if task.acceptance_criteria:
            lines.append("验收标准（逐条都必须满足）：")
            lines.extend(f"{index}. {item}" for index, item in enumerate(task.acceptance_criteria, start=1))
        lines.append(f"现在只输出 `{name}` 的完整正文，不要围栏、不要解释。")
        return "\n".join(lines)

    def _workspace_dir(self) -> str:
        return f"{self.agent_id}-workspace"

    def _record_path(self, native_ref: str) -> str:
        return f"{self._workspace_dir()}/{RUNS_SUBDIR}/{native_ref}.json"

    def _read_record(self, native_ref: str) -> dict[str, Any]:
        rel = self._record_path(native_ref)
        target = self.gateway.root / rel
        if not target.is_file():
            raise ApiExecError(
                f"回收找不到执行留档 {rel!r} ⇒ 无法重查。可能是 workspace 被清过，或这个回执不属于本机"
            )
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ApiExecError(f"执行留档 {rel!r} 不是合法 JSON（{exc.msg}）⇒ 不猜内容") from exc
        if not isinstance(payload, dict):
            raise ApiExecError(f"执行留档 {rel!r} 的顶层不是对象 ⇒ 不猜内容")
        return payload

    def _usage_notices(self, record: Mapping[str, Any]) -> list[UsageNotice]:
        """厂商给了 prompt/completion 就落两条 EN-13 读数；缺任何一个维度就**不落**（不按 0 记假账）。"""
        usage = record.get("usage")
        if not isinstance(usage, Mapping):
            return []
        notices: list[UsageNotice] = []
        dimensions: tuple[tuple[UsageKind, str], ...] = (
            ("INPUT", "prompt_tokens"),
            ("OUTPUT", "completion_tokens"),
        )
        for kind, field in dimensions:
            raw = usage.get(field)
            if raw is None:
                return []
            try:
                quantity = Decimal(str(raw))
            except Exception:  # noqa: BLE001 - 读数不是数字就整笔不落，不猜
                return []
            notices.append(
                UsageNotice(
                    usage_kind=kind,
                    quantity=quantity,
                    unit="token",
                    # **BYOK 而不是 TOKEN**：这一支花的是用户自己的厂商 key（`params-ref §6` 的 BYOK 行：
                    # "自带厂商 Key，费用由模型厂商结算，不占平台账"），而 §6 的 token 价目只有豆包四行
                    # —— 对 DeepSeek / 千问标 TOKEN 会在定价器那一步 `model_row_missing`，整张卡在
                    # 回收段失败（2026-10-31 真机实测）。BYOK 的单价是 0、`source` 指到 §6 那一行，
                    # 账上记 0 是**口径**不是缺价。
                    billing_basis="BYOK",
                    model_ref=self.api.model,
                    occurred_at=self._clock(),
                )
            )
        return notices

    def _actor(self) -> Actor:
        return Actor(actor_id=f"{ADAPTER_OWNER_PREFIX}{self.agent_id}", role_id=NO_ROLE_DECLARED)

    def _capability(self, task_id: str) -> Capability:
        return Capability(agent_id=self.agent_id, task_id=task_id)
