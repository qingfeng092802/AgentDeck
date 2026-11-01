"""DeepSeek 审查客户端包（Phase 17 P2）。

本包只服务**审查**：`acp.adapters.deepseek.review` 送 `chat/completions`、按 POC 契约解析三值结论；
它不接执行派发、不实现 `AgentAdapter`。执行席仍由 qoder / doubao / workbuddy 承担。
凭据只从 `DEEPSEEK_API_KEY` 读，端点与请求模型名只在本包的 `review` 模块里出现一次。
"""

from __future__ import annotations

from acp.review import policy as review_policy


class ReviewEntry:
    """注册表入口的**标记类**：这一条身份只接审查，不是执行 transport。

    `agents.yaml` 的 `adapter_entry` 指向它，`Registration.resolve()` 能取回一个真实的对象；
    但本类没有 `submit/collect/cancel/describe`，所以任何把它当执行适配器用的路径都会在
    协议成员检查那里响亮失败，而不是悄悄拿一个审查客户端去派发任务。
    """

    agent_id: str = review_policy.DEEPSEEK_REVIEWER
