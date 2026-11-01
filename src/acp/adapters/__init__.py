"""适配器层的对外面：只放 `AgentAdapter` 协议本体（T038）。

刻意**不从这里导出任何厂商实现**：`acp.adapters.workbuddy` 之类只能由注册表按入口字符串定位
（T039），编排层 `import acp.adapters` 拿到的是协议而不是任何一个具体 transport。
这是"新增 Agent = 加一份配置 + 一个适配器实现，编排图不动"（FR-012）在导入面上的兑现——
导得到具体实现，就总有一天会有人在 `orchestrate/` 里直接 import 它。
"""

from __future__ import annotations

from acp.adapters.base import AgentAdapter

__all__ = ["AgentAdapter"]
