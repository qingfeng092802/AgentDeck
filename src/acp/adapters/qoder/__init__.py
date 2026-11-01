"""Qoder 适配器包（T041）。

`__init__` 重导出 `Transport` 与 workbuddy 那一支同理：注册表入口写的是
`acp.adapters.qoder:Transport`（`registry/loader.py` 的 `ENTRY_RE` 按这个形状校验），
而 `Registration.resolve()` 是 `getattr(模块, 类名)`。

**这里刻意不 import `qoder_agent_sdk`**：本包的实现走 CLI 子进程（`CliRunner` 注入），
不碰 SDK 那层。SDK 的导入链在 Windows 侧曾被应用控制策略拦过（`rpds.pyd`，见 `quickstart.md §1`），
把它变成硬依赖会让"注册表里有 qoder"这件事取决于一个本机装不上的包 ——
而 FR-012 要的是删掉本目录后编排图仍可构建。
"""

from acp.adapters.qoder.transport import CREDENTIAL_ENV_NAMES, PRODUCTION_RUNNER, Transport

#: 同 `acp.adapters.doubao` 那一支的写法：真装配（`spec_factory.build_real_transport()`）按包取
#: 凭据名单与"生产用哪个 runner"，工厂里不留这两个副本。用例注入的是 `FakeRunner`，不是 `PRODUCTION_RUNNER`。
__all__ = ["CREDENTIAL_ENV_NAMES", "PRODUCTION_RUNNER", "Transport"]
