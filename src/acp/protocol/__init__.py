"""协议层对外面：编排层与适配器**只**从这里取消息类型（宪法 IV / FR-009）。

导出面刻意只放跨 Agent 的三个结构与它们的引用类型，不放 transport 相关的任何东西——
从 `acp.protocol` 导不到厂商专有字段，是"无私有旁路"最便宜的一道结构性保证。
"""

from __future__ import annotations

from acp.protocol.messages import (
    ArtifactFloor,
    ArtifactRef,
    CapabilityManifest,
    Task,
    TaskResult,
)
from acp.protocol.refs import (
    AgentForm,
    AgentRoleRef,
    ArtifactFormat,
    BillingBasis,
    CostBand,
    Handle,
    SelfReportedStatus,
    UsageKind,
    UsageNotice,
)

__all__ = [
    "AgentForm",
    "AgentRoleRef",
    "ArtifactFloor",
    "ArtifactFormat",
    "ArtifactRef",
    "BillingBasis",
    "CapabilityManifest",
    "CostBand",
    "Handle",
    "SelfReportedStatus",
    "Task",
    "TaskResult",
    "UsageKind",
    "UsageNotice",
]
