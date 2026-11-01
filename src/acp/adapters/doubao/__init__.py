"""豆包适配器包（T042）。

`__init__` 重导出 `Transport`：注册表入口写的是 `acp.adapters.doubao:Transport`
（`registry/loader.py` 的 `ENTRY_RE` 按这个形状校验），而 `Registration.resolve()` 是
`getattr(模块, 类名)`。

**这里刻意不 import `openai`**：本包的实现走注入的 `httpx.Client` 直连 OpenAI 兼容端点，
一行都不用那支 SDK。理由与 T041 不装 `qodercli` 是同一条 ——
把厂商专有客户端变成平台的硬依赖，FR-012 要的"新增 Agent 不改编排图、删掉本目录仍可构建"
就会反过来取决于装没装上那个包。`pyproject` 运行时面里的 `openai>=1.30` 目前**没有生产代码消费**，
已作为待裁登记在 `tasks.md` 的 T042 申报里（减依赖要 owner 单独点，我自不动）。
"""

from acp.adapters.doubao.exec import ApiExecTransport, spec_from_route
from acp.adapters.doubao.transport import CREDENTIAL_ENV_NAMES, HTTP_TIMEOUT_S, Transport

#: 路线 B（2026-10-31）：同一支 agent 身份在**带 `model_routing` 时**换成纯 API 执行适配器。
#: 为什么挂在 doubao 包上而不是新开一条注册：注册表条数被 `params-ref §3` 的并发关系式钉着，
#: 加一条就等于改 owner 定的平台并发值。工厂按包上的这两个名字切换（见 `spec_factory.ROUTED_*`）。
#: 不带路由时本包行为一字不改（`Transport` 仍是审查补全那一支）。
RoutedTransport = ApiExecTransport


#: 五个名字一起进公开面，是因为 `spec_factory.build_real_transport()` 按**包**取约定属性
#: （`adapter_entry` 写的就是 `acp.adapters.doubao:Transport`）：凭据变量名与超时秒数的唯一载体在本包，
#: 工厂里不留副本。少了其中任何一个，真装配那一发就拒 —— 不猜。
__all__ = ["CREDENTIAL_ENV_NAMES", "HTTP_TIMEOUT_S", "RoutedTransport", "Transport", "spec_from_route"]
