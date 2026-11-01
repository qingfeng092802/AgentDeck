"""文件网关：跨 Agent 落盘的**唯一写入口**（T016 / T017 / T018，FR-013 / FR-014 / FR-033｜R-24）。

三件事在这里做，且只在这里做：① 按 `acl.py` 的静态表放行或拒绝；② 任务卡的格式校验
（三行封顶 + 字段齐全 + frontmatter 字段类封闭）；③ 渲染给 Agent 时**剥去 frontmatter**
（宪法 v1.0.3 原则 II 分层条件②的强制点，契约 §3 已成文）。

一处边界，明写在最显眼的位置：契约 §5 的**凭据脱敏过滤器**已挂在 `write_artifact` 落盘之前
（T027，`acp/config/loader.py`）。挂在网关而不是各调用点，是因为 `orchestrator_writer` 的
五个公共区动作**全部**经由本入口写盘 ⇒ 产物面只需一道，且新加写入方不会漏挂。
日志面由 `loader.install_logging_filter()` 负责（logging 的 `secrets` 过滤器），两个面各一处。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import get_args

from acp.bus import acl, layout
from acp.bus.acl import Actor, Capability, Decision
from acp.config.loader import redact_bus_text
from acp.protocol.messages import ArtifactRef
from acp.protocol.refs import ArtifactFormat

_LOG = logging.getLogger("acp.bus")

#: 产物格式的取值域：**从 `protocol/refs.ArtifactFormat` 推导**，不在网关里抄第二份字面量。
#: `ArtifactFloor.format`（任务卡声明的下限）与实测的 `ArtifactRef.format` 必须同一个域，
#: 否则"格式不符"这一支会在两侧各有一套合法值（FR-017 的第三项下限就此不可比）。
ARTIFACT_FORMATS: tuple[str, ...] = get_args(ArtifactFormat)

# 契约 §3 的正文三行（顺序即模板顺序）。分隔符固定成 ASCII 冒号：落盘面由本模块生成，
# MUST NOT 靠"中文冒号也认"的宽松解析碰运气——那会让"四行拒写"这种判据因输入形态不同而漂。
CARD_LABELS = ("目标", "输入", "期望产物")
CARD_LINE_RE = re.compile(r"^(目标|输入|期望产物): (.+)$")

FRONTMATTER_DELIMITER = "---"

# 宪法 v1.0.3 原则 II 列出的**五类**受控 frontmatter（plan.md 的 Constitution Check 表把
# "未增第六类"列为硬条件，故这里按类封闭，而不是按单个键名黑名单）。
FRONTMATTER_CLASSES: dict[str, tuple[str, ...]] = {
    "artifact_floor": ("artifact_floor",),
    "acceptance_criteria": ("acceptance_criteria",),
    "token_budget": ("token_budget",),
    "三岗 agent_id": ("executor_agent_id", "reviewer_agent_id", "acceptor_agent_id"),
    "轮次": ("review_round", "handoff_count"),
}
ALLOWED_FRONTMATTER_KEYS = frozenset(key for keys in FRONTMATTER_CLASSES.values() for key in keys)

FM_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*): ?(.*)$")


class BusError(Exception):
    """总线拒绝。`code` / `http_status` / `guard_kind` 直接来自契约 §2 与 §7。"""

    def __init__(self, code: str, http_status: int, guard_kind: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.http_status = http_status
        self.guard_kind = guard_kind
        self.detail = detail


@dataclass(frozen=True)
class GuardEvent:
    """审计事件。EN-14 `event_log.kind` 的 `GUARD_VIOLATION` / `READONLY_VIOLATION` 两支由它填。

    本阶段只投给注入的 sink（测试用列表），**不落任何文件**——落了就是自造第二份事实态（宪法 I）。
    PG 侧落库在 Phase 1.3 接线（T025 的 EN-14）。
    """

    kind: str
    code: str
    actor_id: str
    role_id: str
    path: str
    detail: str


@dataclass(frozen=True)
class TaskCardBody:
    """三行正文的语义位，与 `protocol.Task` 的 objective / inputs / expected_output 一一对应。"""

    objective: str
    inputs: str
    expected_output: str


def render_card(body: TaskCardBody, frontmatter: Mapping[str, object] = {}) -> str:
    """编排引擎落盘面：受控 frontmatter + 三行正文。

    与 `strip_frontmatter` 成对使用——落盘的多，看见的少，差值就是分层本身。
    """
    unknown = set(frontmatter) - ALLOWED_FRONTMATTER_KEYS
    if unknown:
        raise BusError(
            acl.WRITE_FORBIDDEN,
            403,
            "GUARD_VIOLATION",
            f"frontmatter 出现第六类字段 {sorted(unknown)}；允许的五类 = "
            f"{sorted(FRONTMATTER_CLASSES)} 下的 {sorted(ALLOWED_FRONTMATTER_KEYS)}",
        )
    lines = [
        f"{FRONTMATTER_DELIMITER}",
        *(f"{key}: {_scalar(value)}" for key, value in sorted(frontmatter.items())),
        f"{FRONTMATTER_DELIMITER}",
        f"目标: {body.objective}",
        f"输入: {body.inputs}",
        f"期望产物: {body.expected_output}",
    ]
    return "\n".join(lines) + "\n"


def strip_frontmatter(text: str) -> str:
    """T017 的渲染面：Agent 可见面恰为三行。"""
    return split_card(text)[1]


def split_card(text: str) -> tuple[dict[str, str], str]:
    """返回 (frontmatter, body)。无 frontmatter 时 frontmatter 为空——**这是解析器的义务**，
    因为契约 §3 条件③要求"无 frontmatter 时三行仍可独立解析"（PG 不可用时的回退路径靠它）。
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != FRONTMATTER_DELIMITER:
        return {}, text
    for index in range(1, len(lines)):
        if lines[index].strip() == FRONTMATTER_DELIMITER:
            meta: dict[str, str] = {}
            for raw in lines[1:index]:
                if not raw.strip():
                    continue
                hit = FM_LINE_RE.match(raw.strip())
                if hit is None:
                    raise BusError(
                        acl.WRITE_FORBIDDEN,
                        403,
                        "GUARD_VIOLATION",
                        f"frontmatter 行无法解析：{raw!r}",
                    )
                meta[hit.group(1)] = hit.group(2)
            return meta, "\n".join(lines[index + 1 :])
    raise BusError(
        acl.WRITE_FORBIDDEN, 403, "GUARD_VIOLATION", "frontmatter 有起无止（缺闭合分隔行）"
    )


def validate_card(text: str) -> TaskCardBody:
    """T018 的校验：三行封顶 + 字段齐全 + frontmatter 类封闭。

    "四行及以上拒写"与"缺行拒写"是同一条判据的两面——只判超行数会让缺 `期望产物` 的卡片拿到绿灯，
    而 D-4 写的是"三行封顶 + **字段齐全**"。
    """
    meta, body = split_card(text)
    offending = sorted(set(meta) - ALLOWED_FRONTMATTER_KEYS)
    if offending:
        raise BusError(
            acl.WRITE_FORBIDDEN,
            403,
            "GUARD_VIOLATION",
            f"frontmatter 超五类的字段 {offending} ⇒ 拒写（须走宪法修订）",
        )
    lines = [line for line in body.split("\n") if line.strip()]
    if len(lines) > len(CARD_LABELS):
        raise BusError(
            acl.WRITE_FORBIDDEN,
            403,
            "GUARD_VIOLATION",
            f"正文 {len(lines)} 行，超过三行封顶（契约 §3：第四行起视为协议违规）",
        )
    parsed: dict[str, str] = {}
    for line in lines:
        hit = CARD_LINE_RE.match(line.strip())
        if hit is None:
            raise BusError(
                acl.WRITE_FORBIDDEN,
                403,
                "GUARD_VIOLATION",
                f"正文行不符合 `<目标|输入|期望产物>: <内容>`：{line!r}",
            )
        if hit.group(1) in parsed:
            raise BusError(
                acl.WRITE_FORBIDDEN,
                403,
                "GUARD_VIOLATION",
                f"标签重复：{hit.group(1)!r}（三行各一次，不接受续行）",
            )
        parsed[hit.group(1)] = hit.group(2)
    missing = [label for label in CARD_LABELS if label not in parsed]
    if missing:
        raise BusError(
            acl.WRITE_FORBIDDEN,
            403,
            "GUARD_VIOLATION",
            f"缺行：{missing}（D-4 的判据是三行封顶 + 字段齐全）",
        )
    return TaskCardBody(parsed["目标"], parsed["输入"], parsed["期望产物"])


class BusGateway:
    def __init__(
        self,
        project_root: Path,
        audit_sink: Callable[[GuardEvent], None] | None = None,
    ) -> None:
        self.root = Path(project_root)
        self.audit_sink = audit_sink

    # --- 唯一写入口（T016）-------------------------------------------------------

    def write_artifact(
        self,
        actor: Actor,
        path: str,
        payload: str,
        capability: Capability | None = None,
    ) -> Path:
        decision = acl.can_write(actor, path, capability)
        if not decision.allowed:
            self._emit(decision, actor, path)
            raise BusError(decision.code, decision.http_status, decision.guard_kind, decision.reason)

        target = self._resolve(path)
        if self._escapes_root(target):
            decision = Decision(
                False, "path_escape", 403, "GUARD_VIOLATION", f"解析后越出总线根：{path!r}"
            )
            self._emit(decision, actor, path)
            raise BusError(decision.code, decision.http_status, decision.guard_kind, decision.reason)

        # 契约 §5 / 宪法「二」：凭据形态一律不落产物。洗在**校验之前**，
        # 因为 `validate_card` 与三元组重建读的都必须是"真正落到盘上的那份字节"，
        # 校验一份、落盘另一份 = 门禁与产物分叉。
        # 只报路径与长度，MUST NOT 把内容或命中片段带进日志（那正是本过滤器要防的事）。
        cleaned = redact_bus_text(payload)
        if cleaned != payload:
            _LOG.warning("bus_write_redacted path=%s chars=%d", path, len(payload))
            payload = cleaned

        if str(layout.normalize_rel_path(path)).startswith(f"{layout.TASKS_DIR}/"):
            validate_card(payload)  # 任务卡的三行与 frontmatter 判据（T018）

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload.encode("utf-8"))  # write_bytes：Windows 上 write_text 会把 LF 变 CRLF
        return target

    def write_and_measure(
        self,
        actor: Actor,
        path: str,
        payload: str,
        capability: Capability | None = None,
    ) -> ArtifactRef:
        """落盘 + **从盘上**量出 `bytes` / `lines` / `format`（契约 §2.1 的"实测值由网关读工作区得出"）。

        三个数一律来自 `target` 那份字节，不来自调用方给的任何自报值 ⇒ 回收路径上工具自报的
        "我写了 5000 字节"在结构上就进不了判定（FR-019 / FR-017 的下限比对读的就是这三个数）。
        量之前不洗、量之后不重读：写的就是 `write_artifact` 落的那份（脱敏在落盘前已发生，
        所以实测值描述的是**盘上的真东西**，而不是厂商声称的那份）。

        **格式在落盘前先验**：厂商回一个 `.ps1` 之类的域外后缀时，不能"先写进工作区再说"——
        那等于让被拒的产物留在盘上，下一个读工作区的人（或下一次 `ValidateArtifact`）就看见它了。
        """
        format_of(path)  # 先拒（同一支函数也用在量的那一侧，规则只有一份）
        target = self.write_artifact(actor, path, payload, capability)
        return measure_artifact(target, rel_path=path)
    def read(self, actor: Actor, path: str) -> str:
        """读不设跨 workspace 墙（FR-013：跨 workspace 读**允许**），只要求路径存在。

        `actor` 目前在本方法里不参与判定——这不是疏忽：契约与宪法都没有"按角色限读"的条款，
        真要加（例如 frontmatter 对 Agent 不可见）也在渲染层，不在读层。
        """
        target = self._resolve(path)
        if self._escapes_root(target):
            raise BusError("path_escape", 403, "READONLY_VIOLATION", f"解析后越出总线根：{path!r}")
        return target.read_text(encoding="utf-8")

    def render_for_agent(self, path: str) -> str:
        """Agent 可见面 = 三行正文。强制点 = 本方法（契约 §3 条件②）。"""
        return strip_frontmatter(self.read(Actor("agent", "unknown"), path))

    # --- 内部 --------------------------------------------------------------------

    def _resolve(self, path: str) -> Path:
        normalized: PurePosixPath = layout.normalize_rel_path(path)
        return self.root.joinpath(*normalized.parts)

    def _escapes_root(self, target: Path) -> bool:
        try:
            resolved = target.resolve()
            root = self.root.resolve()
        except OSError:
            return True
        return root not in resolved.parents and resolved != root

    def _emit(self, decision: Decision, actor: Actor, path: str) -> None:
        if self.audit_sink is None:
            return
        self.audit_sink(
            GuardEvent(
                kind=decision.guard_kind,
                code=decision.code,
                actor_id=actor.actor_id,
                role_id=actor.role_id,
                path=path,
                detail=decision.reason,
            )
        )


def _scalar(value: object) -> str:
    if isinstance(value, (list, tuple, set, frozenset)):
        return "[" + ", ".join(str(item) for item in value) + "]"
    if isinstance(value, Mapping):
        return "{" + ", ".join(f"{key}={item}" for key, item in sorted(value.items())) + "}"
    return str(value)


def format_of(rel_path: str) -> str:
    """取产物后缀并验它落在 `ARTIFACT_FORMATS` 里；不在 ⇒ `BusError`（规则只在这一份）。

    域外格式必须拒而不是"填个空串先落账"：`ArtifactFloor.format`（任务卡声明的下限）与实测的
    `ArtifactRef.format` 是同一个域，放过域外值会让"格式不符"这一支永远不可证伪
    —— `protocol/refs.py` 顶部把这条写成 T-5 的关闭条件。
    码名 `unsupported_format` 与既有的 `bad_path` / `path_escape` 同属**实现侧码**，
    `contracts/file-bus.md` 三个都没列 ⇒ 作为契约同步项登记在 `tasks.md` 的 T040 申报里。
    """
    suffix = PurePosixPath(rel_path).suffix.lstrip(".").lower()
    if suffix not in ARTIFACT_FORMATS:
        raise BusError(
            "unsupported_format",
            400,
            "GUARD_VIOLATION",
            f"产物 {rel_path!r} 的后缀 {suffix!r} 不在取值域 {ARTIFACT_FORMATS} 内",
        )
    return suffix


def measure_artifact(target: Path, *, rel_path: str) -> ArtifactRef:
    """从磁盘算一个 `ArtifactRef`：三个数都读自那份**真正落盘**的字节（FR-019）。

    `rel_path` 用调用方给的那条（总线相对路径），`format` 从磁盘上的文件名取 ⇒ 两者不一致时
    （网关被绕过、或有人把文件改名）读出来的是盘上实况，不是请求里的说法。
    """
    data = target.read_bytes()
    return ArtifactRef(
        rel_path=rel_path,
        bytes=len(data),
        # 行数口径 = `splitlines()` 的行数（末尾换行不多算一行）。定死这一条是因为 `min_lines`
        # 直接和它比：按 `\n` 数与按行拆在无末尾换行时差 1，而那 1 足够让刚达标的产物被判不达标。
        lines=len(data.decode("utf-8").splitlines()),
        format=format_of(target.name),
    )
