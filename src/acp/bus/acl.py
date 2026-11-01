"""写入归属**静态表**（T015，FR-013｜R-24；论证见 research.md D-4）。

表在这里**硬编码**、运行时不推断——D-4 的原话：一文件一写入者若靠约定，违规只能事后发现；
查表放行把违规变成**写入即失败**。所以本模块不允许出现"根据目录内容/ctime/actor 自称的角色
猜归属"这类逻辑，也不允许未声明路径默认放行：**匹配不到 = 拒绝**（fail closed）。

形状与 `contracts/file-bus.md` §2 的表逐行对应；改这张表 = 改契约，MUST 同批动并跑
`tests/guards/test_bus_isolation.py`（它遍历本表断言"每个路径模式恰好一个 owner"）。
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass

from acp.bus import layout

PUBLIC_OWNER = "orchestrator_writer"
ADAPTER_OWNER_PREFIX = "adapter:"

#: 适配器写盘时 `Actor.role_id` 的占位值（owner 2026-10-13 待裁② 的处置：三份适配器实现共用的**一个**载体）。
#: 为什么要有它：`Handle`（`contracts/agent-protocol.md §4.1`）只有四个字段、**不带 `role_id`**，
#: 而 `can_write()` 要判"只读岗在任何路径都无写权限"⇒ 适配器必须交出一个 `Actor`。
#: 此时**不许编一个岗位**（那等于让适配器替编排层宣称身份），也不许留空串（空串会被读成"没填"，
#: 与"这是一个真实岗位"在日志里长得一样）。这个常量的含义就是那句实话：岗位不由 `Handle` 决定。
#: 三家适配器（`adapters/workbuddy` / `adapters/qoder` / `adapters/doubao`）都从这里取，
#: 不在各自目录里再抄一份字面量 ⇒ T045 的"删目录后编排图仍可构建"不会被一份共享字面量破坏
#: （依赖方向是 适配器 → 总线，总线的义务本来就有归属判定这一条）。
NO_ROLE_DECLARED = "unassigned-by-handle"


@dataclass(frozen=True)
class Rule:
    pattern: str
    owner: str
    note: str

    @property
    def is_public(self) -> bool:
        return self.owner == PUBLIC_OWNER


# 顺序 = 契约 §2 的行序。`*-workspace/**` 放最后：它靠目录名前缀解出 owner，
# 前面的公共区模式都不该被它吞掉（`matches()` 取第一个命中，故公共区在前）。
RULES: tuple[Rule, ...] = (
    Rule("tasks/**", PUBLIC_OWNER, "任务卡；全体只读"),
    Rule("results/**", PUBLIC_OWNER, "产物索引；全体只读"),
    Rule("status.md", PUBLIC_OWNER, "状态总览"),
    Rule("daily-log.md", PUBLIC_OWNER, "追加式过程日志（FR-052）"),
    Rule("AGENTS.md", PUBLIC_OWNER, "协议说明，且仅协议变更时写"),
    Rule("docs/meetings/**", PUBLIC_OWNER, "代架构师岗位执笔"),
    Rule("*-workspace/**", f"{ADAPTER_OWNER_PREFIX}<agent_id>", "目录名前缀 = 唯一写入者；其余只读"),
)


@dataclass(frozen=True)
class Actor:
    """请求主体。`read_only` 来自 `role_spec.read_only`（EN-04），不是请求方自述。"""

    actor_id: str
    role_id: str
    read_only: bool = False

    @property
    def is_orchestrator(self) -> bool:
        return self.actor_id == PUBLIC_OWNER


@dataclass(frozen=True)
class Capability:
    """任务级能力凭证（D-2）：本次下发授予哪个 agent 的写位。

    刻意**不带路径**——凭证说"你能写你自己的 workspace"，具体哪条路径由 ACL 判，
    否则等于把归属权交回请求方（D-2 把强制点放在适配器边界，正是为了不出现这种事）。
    """

    agent_id: str
    task_id: str


@dataclass(frozen=True)
class Decision:
    allowed: bool
    code: str
    http_status: int
    guard_kind: str
    reason: str


ALLOW = Decision(True, "allowed", 200, "", "")
UNMAPPED_PATH = "unmapped_path"
WRITE_FORBIDDEN = "write_forbidden"
READ_ONLY_VIOLATION = "read_only_violation"


def matches(rel_path: str) -> Rule | None:
    """取**第一个**命中的模式；匹配不到就是 None（调用方 MUST 拒，不得默认放行）。"""
    normalized = str(layout.normalize_rel_path(rel_path))
    for rule in RULES:
        if _pattern_hit(rule.pattern, normalized):
            return rule
    return None


def _pattern_hit(pattern: str, rel_path: str) -> bool:
    """模式语义只有两种：`X/**`（目录内的一切）与精确文件名。

    `X` 允许是 glob（`*-workspace`），因为 workspace 的 owner 由目录名前缀决定；
    首版把 `*-workspace/**` 当成字面量前缀去 `startswith`，结果是**任何** workspace 都匹配不到，
    全部落进"未声明 ⇒ 拒"——写侧表现为"合法写入被拒"，读侧则整张表形同空表。
    第二版又栽在一次比较上：`docs/meetings/**` 的 head 是**两段**，拿 `parts[0]` 去比永远不等
    ⇒ 纪要目录形同没有归属声明。故这里按 head 的**段数**切片比较，而不是只比首段。
    """
    if pattern.endswith("/**"):
        head, parts = pattern[: -len("/**")], rel_path.split("/")
        head_parts = head.split("/")
        if len(parts) <= len(head_parts):
            return False
        if "*" in head or "?" in head:
            return len(head_parts) == 1 and fnmatch.fnmatchcase(parts[0], head)
        return parts[: len(head_parts)] == head_parts
    return rel_path == pattern


def declared_owner(rel_path: str) -> str:
    """路径的唯一写入者名字；workspace 会把 `<agent_id>` 展开成目录前缀。"""
    rule = matches(rel_path)
    if rule is None:
        raise KeyError(f"未声明归属的路径：{rel_path!r}（fail closed，不默认放行）")
    if ADAPTER_OWNER_PREFIX in rule.owner:
        normalized = str(layout.normalize_rel_path(rel_path))
        agent_id = layout.workspace_agent_id(normalized.split("/", 1)[0])
        if agent_id is None:
            raise KeyError(f"workspace 目录名前缀解不出 agent_id：{rel_path!r}")
        return f"{ADAPTER_OWNER_PREFIX}{agent_id}"
    return rule.owner


def can_write(actor: Actor, rel_path: str, capability: Capability | None) -> Decision:
    """ACL 判定。顺序即优先级，四条各有明确理由：

    1. 越界/未声明路径先拒——归因清楚，且不给后续判定喂脏输入。
    2. `read_only` 身份**只对"它自己那一份 workspace"放行**，其余路径一律拒（FR-033 / D-2，
       owner 2026-10-13 裁决 1：只读按**任务**算、不按工具算 —— 同一支工具可以是别的任务的执行者）。
       这一条仍排在归属比对**之前**，但判据取 `declared_owner()`：路径的唯一写入者是谁只有一个载体
       （契约 §2），在这里再拼一遍 `-workspace/` 前缀就是第二个载体，且会漏掉带层级的写法。
       放行不等于免除后续检查：写自己 workspace 仍要过第 4 条的任务级能力凭证。
    3. 公共区只接受编排引擎身份（适配器一律 403，契约 §2 规则二）。
    4. workspace 比对能力凭证与目录前缀（凭证缺失 = 拒，不猜）。
    """
    try:
        rule = matches(rel_path)
    except ValueError as exc:
        return _deny("bad_path", 400, "GUARD_VIOLATION", str(exc))
    if rule is None:
        return _deny(UNMAPPED_PATH, 403, "GUARD_VIOLATION", f"归属表里没有 {rel_path!r}")
    if actor.read_only:
        path_owner = declared_owner(rel_path)
        if path_owner != actor.actor_id:
            return _deny(
                READ_ONLY_VIOLATION,
                403,
                "READONLY_VIOLATION",
                f"只读岗（role_id={actor.role_id!r}）试图写唯一写入者为 {path_owner!r} 的路径 ⇒ 拒"
                "（owner 2026-10-13 裁决 1：只读按任务算，仅其自身 workspace 可写）",
            )
    if rule.is_public:
        if not actor.is_orchestrator:
            return _deny(
                WRITE_FORBIDDEN,
                403,
                "GUARD_VIOLATION",
                f"公共区 {rule.pattern} 的唯一写入者是 {PUBLIC_OWNER}，收到 {actor.actor_id!r} 的写请求",
            )
        return ALLOW
    if capability is None:
        return _deny(WRITE_FORBIDDEN, 403, "GUARD_VIOLATION", "workspace 写请求缺任务级能力凭证")
    owner = f"{ADAPTER_OWNER_PREFIX}{capability.agent_id}"
    if owner != declared_owner(rel_path):
        return _deny(
            WRITE_FORBIDDEN,
            403,
            "GUARD_VIOLATION",
            f"能力凭证是 {owner!r} 而目录声明的唯一写入者是 {declared_owner(rel_path)!r}",
        )
    return ALLOW


def _deny(code: str, status: int, guard_kind: str, reason: str) -> Decision:
    return Decision(False, code, status, guard_kind, reason)
