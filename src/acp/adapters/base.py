"""`AgentAdapter` 协议（T038｜FR-009 / FR-011，R-01；形状的唯一说明在 `contracts/agent-protocol.md §4`）。

四个成员与 §4 的 ```python 围栏**逐项同形**，由 `tests/contract/test_adapter_protocol.py` 机读比对钉住
⇒ 实现与契约不可能各自漂开（一事实一载体：契约说形状，本文件是实现，测试是核对）。

三样被刻意排除的东西写在这里，免得下一轮"顺手补一个"把它们带回来：

- **没有 `plan()` / `select()` / `route()`**：调度与选人不属于适配器（宪法 II），它们活在
  `orchestrate/nodes/`，参数是任务卡与注册表读数，不是适配器。
- **没有 `stream()`**：`plan.md` 设计项 6 与 `tasks.md` 的 T038 行列了它，但契约 §4 没有，
  `spec.md` 的 FR 全集里也没有任何一条要求流式回传（2026-10-12 现跑 `grep -rni stream spec.md` = 0 命中）。
  ⇒ 本文件按 owner 21:1x 的口径落**契约四支**；那两处文档与契约的差异已在 T038 行下登记待裁，
  不在实现里替谁作数。
- **签名不收裸 `dict`**（D-7）：旁路的典型形态就是"临时传个 dict"，在签名层禁掉比在文档里声明有效。

`runtime_checkable` 只为让 `isinstance(实例, AgentAdapter)` 可做**结构**检查（T045 的零耦合证明要用）。
两条已知边界，用之前要知道：它带一个非方法成员 `agent_id: str`，所以 `issubclass()` 会抛 `TypeError`
（MUST 走 `isinstance`）；且 `isinstance` 只查"有没有"，多出来的第五支方法它不管 ⇒
"不许多"这一半靠上面那个围栏比对，不靠结构检查。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from acp.protocol import CapabilityManifest, Handle, Task, TaskResult


@runtime_checkable
class AgentAdapter(Protocol):
    """编排层唯一可见的适配器形状（契约 §4 的四支 + 一个标识成员）。"""

    agent_id: str

    def describe(self) -> CapabilityManifest: ...

    def submit(self, task: Task) -> Handle: ...

    def collect(self, handle: Handle) -> TaskResult: ...

    def cancel(self, handle: Handle) -> None: ...
