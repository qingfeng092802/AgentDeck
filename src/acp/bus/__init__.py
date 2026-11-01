"""文件总线（R-24）：目录布局、静态归属表、唯一写入口、公共区单点代笔。

导入顺序 **MUST 保持** layout → acl → gateway → orchestrator_writer：
下层不 import 上层（acl 只用 layout，gateway 用 layout + acl），所以按这个序不会成环；
反过来说，任何一层里出现 `from acp.bus.orchestrator_writer import ...` 就是在把权限判定
往回拖，MUST NOT 这么写。
"""

from __future__ import annotations

from acp.bus import acl, layout
from acp.bus.acl import ALLOW, Actor, Capability, Decision, Rule, RULES, can_write, declared_owner
from acp.bus.gateway import (
    CARD_LABELS,
    ALLOWED_FRONTMATTER_KEYS,
    FRONTMATTER_CLASSES,
    BusError,
    BusGateway,
    GuardEvent,
    TaskCardBody,
    render_card,
    split_card,
    strip_frontmatter,
    validate_card,
)
from acp.bus.orchestrator_writer import (
    ORCHESTRATOR,
    OrchestratorWriter,
    StatusRow,
    status_table,
)

__all__ = [
    "ALLOW",
    "ALLOWED_FRONTMATTER_KEYS",
    "CARD_LABELS",
    "FRONTMATTER_CLASSES",
    "ORCHESTRATOR",
    "RULES",
    "Actor",
    "BusError",
    "BusGateway",
    "Capability",
    "Decision",
    "GuardEvent",
    "OrchestratorWriter",
    "Rule",
    "StatusRow",
    "TaskCardBody",
    "acl",
    "can_write",
    "declared_owner",
    "gateway",
    "layout",
    "orchestrator_writer",
    "render_card",
    "split_card",
    "status_table",
    "strip_frontmatter",
    "validate_card",
]
