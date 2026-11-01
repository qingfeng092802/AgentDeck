"""`serve` 装配点用的**本机旁路补全器** —— 它补全的东西一件都没有，这是有意为之。

为什么需要它（owner 2026-10-22 Phase 15 指令："修复之前报 503 的问题，确保 completer 注入"）：
`plan.py` 的模块头禁止把规则版补全器塞进**节点**当默认值，那条禁令管的是"形状判据的实现体里长出
第二个事实源"；它没有、也不该管住**装配点**。装配点必须自己决定注什么，否则 `POST /requests`
在生产面上永远 503，看板连"提一句话需求"这一格都做不出来。

为什么它的产出是**空的**而不是几句硬编码话术：FR-002 那句"总管自行补全模糊点"的主语是适配器
（`adapters/<vendor>/transport.py`）。关键词命中式的"规则补全"给出的每一条自行补全项都是编的，
一旦 `serve` 默认带上它，`summary_digest` 就再也读不出"这是模型补的"还是"这是字符串匹配凑的"。
空列表在 `plan.py` 里是合法值（含义 = 没有需要补的模糊点），所以旁路如实交空最诚实：
它不冒充 FR-002，也不逼调用方编条目过关。

**本模块证明不到什么**（点名，别被"端点通了"读成"总管会补全了"）：
它只证明 `POST /requests` 的**形状与落库**成立。FR-002 的实证仍是适配器那一支，归 Phase 3 之后。
响应体因此带 `completer_id`，看板与 CLI 都能当场区分"本地旁路"与真总管 —— 这个标识的唯一产地
就是下面的 `COMPLETER_ID`，别处不许再抄一份字面量。
"""

from __future__ import annotations

from acp.orchestrate.nodes.plan import CompleterOutcome

#: `POST /requests` 响应里 `completer_id` 的取值唯一载体。
COMPLETER_ID = "local-passthrough"


class LocalPassthroughCompleter:
    """原样收下需求，不补全、不追问。`completer_id` 让这一件事在读数上可见。"""

    completer_id = COMPLETER_ID

    def complete(self, raw_input: str) -> CompleterOutcome:
        # 签名按 `RequirementCompleter` 的 Protocol 走；原文不参与任何推断，所以这里不读它。
        return CompleterOutcome()
